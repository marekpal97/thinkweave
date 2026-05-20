"""Tests for the SQLite indexer and search engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.retrieval.search import Search
from personal_mem.core.vault import VaultManager


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def config(vault_dir: Path) -> Config:
    return Config(vault_root=vault_dir)


@pytest.fixture
def vault(config: Config) -> VaultManager:
    vm = VaultManager(config=config)
    vm.ensure_dirs()
    return vm


@pytest.fixture
def indexer(config: Config) -> Indexer:
    idx = Indexer(config=config)
    yield idx
    idx.close()


@pytest.fixture
def search(config: Config, indexer: Indexer) -> Search:
    s = Search(config=config)
    yield s
    s.close()


class TestIndexer:
    def test_empty_vault_rebuild(self, vault: VaultManager, indexer: Indexer):
        stats = indexer.rebuild(full=True)
        assert stats["indexed"] == 0
        assert stats["skipped"] == 0

    def test_index_single_note(self, vault: VaultManager, indexer: Indexer):
        vault.create_note(
            NoteType.NOTE,
            "SQLite WAL Mode",
            body="WAL mode enables concurrent reads.",
            tags=["sqlite", "gotcha"],
            project="test-proj",
        )
        stats = indexer.rebuild(full=True)
        assert stats["indexed"] == 1

        db_stats = indexer.get_stats()
        assert db_stats["notes_total"] == 1
        assert db_stats["notes_note"] == 1

    def test_incremental_skip_unchanged(self, vault: VaultManager, indexer: Indexer):
        vault.create_note(NoteType.NOTE, "Note A", body="Body A", project="p")
        indexer.rebuild(full=True)

        # Second rebuild — should skip the unchanged file
        vault.create_note(NoteType.NOTE, "Note B", body="Body B", project="p")
        stats = indexer.rebuild(full=False)
        assert stats["indexed"] == 1  # Only Note B
        assert stats["skipped"] == 1  # Note A unchanged

    def test_remove_stale(self, vault: VaultManager, indexer: Indexer):
        path = vault.create_note(NoteType.NOTE, "Temp Note", project="p")
        indexer.rebuild(full=True)
        assert indexer.get_stats()["notes_total"] == 1

        # Delete the file
        path.unlink()
        stats = indexer.rebuild(full=False)
        assert stats["removed"] == 1
        assert indexer.get_stats()["notes_total"] == 0

    def test_edge_derivation_from_wikilinks(self, vault: VaultManager, indexer: Indexer):
        vault.create_note(NoteType.NOTE, "concept-a", body="A is fundamental.", project="p")
        vault.create_note(
            NoteType.NOTE,
            "concept-b",
            body="B builds on [[concept-a]] extensively.",
            project="p",
        )
        stats = indexer.rebuild(full=True)
        assert stats["edges"] >= 1

    def test_edge_derivation_from_frontmatter(self, vault: VaultManager, indexer: Indexer):
        p1 = vault.create_note(NoteType.SESSION, "Session 1", project="p")
        note1 = vault.read_note(p1)

        vault.create_note(
            NoteType.NOTE,
            "Insight from session",
            project="p",
            extra_frontmatter={"derived_from": [note1.id]},
        )
        stats = indexer.rebuild(full=True)
        assert stats["edges"] >= 1

    def test_index_file_single(self, vault: VaultManager, indexer: Indexer):
        # Full rebuild first
        indexer.rebuild(full=True)

        # Add a file and index it individually
        path = vault.create_note(NoteType.NOTE, "Single Add", project="p")
        indexer.index_file(path)
        assert indexer.get_stats()["notes_total"] == 1

    def test_multiple_note_types(self, vault: VaultManager, indexer: Indexer):
        vault.create_note(NoteType.NOTE, "A Note", project="p")
        vault.create_note(NoteType.SESSION, "A Session", project="p")
        vault.create_note(NoteType.DECISION, "A Decision", project="p")
        vault.create_note(NoteType.SOURCE, "A Source", extra_frontmatter={"source_type": "article"})

        stats = indexer.rebuild(full=True)
        assert stats["indexed"] == 4

        db_stats = indexer.get_stats()
        assert db_stats["notes_note"] == 1
        assert db_stats["notes_session"] == 1
        assert db_stats["notes_decision"] == 1
        assert db_stats["notes_source"] == 1


class TestMtimeGate:
    """Regression for P0-8: mtime-gated incremental rebuild.

    On a no-op rebuild we must NOT call ``read_text`` for files whose
    on-disk mtime matches the cached value. Slow-path readback dominated
    no-op rebuild on a 6.5k-file vault (~25s wall on WSL→9P).
    """

    def test_noop_rebuild_does_not_read_unchanged_files(
        self, vault: VaultManager, indexer: Indexer, tmp_path: Path,
        monkeypatch,
    ):
        # Populate vault with 5 notes, full-index once.
        paths = []
        for i in range(5):
            p = vault.create_note(NoteType.NOTE, f"Note {i}", body=f"Body {i}", project="p")
            paths.append(p)
        indexer.rebuild(full=True)

        # Patch Path.read_text to count invocations across all md files.
        from pathlib import Path as _P
        original_read_text = _P.read_text
        read_calls: list[str] = []

        def counting_read_text(self, *a, **kw):
            if self.suffix == ".md":
                read_calls.append(str(self))
            return original_read_text(self, *a, **kw)

        monkeypatch.setattr(_P, "read_text", counting_read_text)

        # No-op incremental — nothing should be read.
        stats = indexer.rebuild(full=False)
        assert stats["indexed"] == 0
        assert stats["removed"] == 0
        assert stats["skipped"] == 5
        assert len(read_calls) == 0, (
            f"expected zero reads on no-op rebuild, got {len(read_calls)}: {read_calls}"
        )

    def test_one_changed_file_reads_only_that_file(
        self, vault: VaultManager, indexer: Indexer, monkeypatch,
    ):
        # 5 notes, indexed once.
        paths = []
        for i in range(5):
            p = vault.create_note(NoteType.NOTE, f"Note {i}", body=f"Body {i}", project="p")
            paths.append(p)
        indexer.rebuild(full=True)

        # Touch one file forward in time + change content.
        target = paths[2]
        import os
        import time
        new_mtime = time.time() + 10
        target.write_text(target.read_text() + "\n\nupdated\n")
        os.utime(target, (new_mtime, new_mtime))

        # Count reads.
        from pathlib import Path as _P
        original_read_text = _P.read_text
        read_calls: list[str] = []

        def counting_read_text(self, *a, **kw):
            if self.suffix == ".md":
                read_calls.append(str(self))
            return original_read_text(self, *a, **kw)

        monkeypatch.setattr(_P, "read_text", counting_read_text)

        stats = indexer.rebuild(full=False)
        assert stats["indexed"] == 1
        assert stats["skipped"] == 4
        # Exactly one read for the touched file
        assert len(read_calls) == 1
        assert str(target) in read_calls[0]


class TestIndexPaths:
    """Regression for P0-8 layer 2: targeted path indexing without rglob."""

    def test_index_paths_only_processes_given_paths(
        self, vault: VaultManager, indexer: Indexer, monkeypatch,
    ):
        a = vault.create_note(NoteType.NOTE, "Note A", body="A", project="p")
        b = vault.create_note(NoteType.NOTE, "Note B", body="B", project="p")
        c = vault.create_note(NoteType.NOTE, "Note C", body="C", project="p")
        indexer.rebuild(full=True)

        # rglob must NOT be called when using index_paths.
        rglob_calls: list[str] = []
        original_rglob = type(vault.root).rglob

        def counting_rglob(self, pattern):
            rglob_calls.append(pattern)
            return original_rglob(self, pattern)

        monkeypatch.setattr(type(vault.root), "rglob", counting_rglob)

        # Modify only b — pass only b's path.
        b.write_text(b.read_text() + "\n\nchanged\n")
        import os
        import time
        new_mtime = time.time() + 10
        os.utime(b, (new_mtime, new_mtime))

        stats = indexer.index_paths([b])
        assert stats["indexed"] == 1
        assert stats["skipped"] == 0
        # The vault-wide rglob in get_all_md_files() should not have run.
        assert "*.md" not in rglob_calls

    def test_index_paths_removes_missing(
        self, vault: VaultManager, indexer: Indexer,
    ):
        a = vault.create_note(NoteType.NOTE, "Note A", body="A", project="p")
        indexer.rebuild(full=True)
        assert indexer.get_stats()["notes_total"] == 1

        a.unlink()
        stats = indexer.index_paths([a])
        assert stats["removed"] == 1
        assert indexer.get_stats()["notes_total"] == 0

    def test_index_paths_handles_unchanged(
        self, vault: VaultManager, indexer: Indexer,
    ):
        a = vault.create_note(NoteType.NOTE, "Note A", body="A", project="p")
        indexer.rebuild(full=True)

        # Re-passing unchanged path should skip.
        stats = indexer.index_paths([a])
        assert stats["indexed"] == 0
        assert stats["skipped"] == 1
        assert stats["removed"] == 0

    def test_index_paths_ignores_outside_vault(
        self, vault: VaultManager, indexer: Indexer, tmp_path: Path,
    ):
        outside = tmp_path / "elsewhere.md"
        outside.write_text("# Foreign\n")
        # Should not raise — just silently ignore.
        stats = indexer.index_paths([outside])
        assert stats["indexed"] == 0
        assert stats["skipped"] == 0


class TestSearch:
    def _populate_vault(self, vault: VaultManager, indexer: Indexer) -> None:
        vault.create_note(
            NoteType.NOTE,
            "SQLite WAL Mode",
            body="WAL mode enables concurrent reads and writes.",
            tags=["sqlite", "database"],
            project="infra",
        )
        vault.create_note(
            NoteType.NOTE,
            "Python Dataclasses",
            body="Dataclasses provide automatic __init__ and __repr__.",
            tags=["python", "patterns"],
            project="learning",
        )
        vault.create_note(
            NoteType.DECISION,
            "Use Markdown First",
            body="Decided to use markdown as source of truth for portability.",
            tags=["architecture"],
            project="personal-mem",
        )
        vault.create_note(
            NoteType.SESSION,
            "Refactored indexer",
            body="Rewrote the FTS rebuild logic for correctness.",
            tags=["refactor"],
            project="personal-mem",
        )
        indexer.rebuild(full=True)

    def test_fts_search(self, vault: VaultManager, indexer: Indexer, search: Search):
        self._populate_vault(vault, indexer)
        results = search.search("concurrent reads")
        assert len(results) >= 1
        assert any("WAL" in r.title for r in results)

    def test_search_by_type(self, vault: VaultManager, indexer: Indexer, search: Search):
        self._populate_vault(vault, indexer)
        results = search.search("", note_type="decision")
        assert len(results) == 1
        assert results[0].type == "decision"

    def test_search_by_project(self, vault: VaultManager, indexer: Indexer, search: Search):
        self._populate_vault(vault, indexer)
        results = search.search("", project="personal-mem")
        assert len(results) == 2  # decision + session

    def test_search_by_tags(self, vault: VaultManager, indexer: Indexer, search: Search):
        self._populate_vault(vault, indexer)
        results = search.search("", tags=["sqlite"])
        assert len(results) >= 1
        assert all("sqlite" in r.tags for r in results)

    def test_get_context(self, vault: VaultManager, indexer: Indexer, search: Search):
        self._populate_vault(vault, indexer)
        results = search.get_context(project="personal-mem", limit=5)
        assert len(results) >= 1

    def test_get_note_by_id(self, vault: VaultManager, indexer: Indexer, search: Search):
        path = vault.create_note(NoteType.NOTE, "Findable", project="p")
        note = vault.read_note(path)
        indexer.rebuild(full=True)

        found = search.get_note_by_id(note.id)
        assert found is not None
        assert found["title"] == "Findable"

    def test_render_graph_text(self, vault: VaultManager, indexer: Indexer, search: Search):
        vault.create_note(NoteType.NOTE, "center-node", body="The center.", project="p")
        vault.create_note(
            NoteType.NOTE, "linked-node", body="Links to [[center-node]].", project="p"
        )
        indexer.rebuild(full=True)

        # Get the center node's ID
        results = search.search("center")
        assert len(results) >= 1
        center_id = results[0].id

        text = search.render_graph_text(center_id)
        assert center_id in text

    def test_render_graph_mermaid(self, vault: VaultManager, indexer: Indexer, search: Search):
        vault.create_note(NoteType.NOTE, "node-a", body="Node A.", project="p")
        vault.create_note(NoteType.NOTE, "node-b", body="Links to [[node-a]].", project="p")
        indexer.rebuild(full=True)

        results = search.search("Node A")
        if results:
            mermaid = search.render_graph_mermaid(results[0].id)
            assert "graph LR" in mermaid

    def test_search_empty_vault(self, vault: VaultManager, indexer: Indexer, search: Search):
        indexer.rebuild(full=True)
        results = search.search("anything")
        assert results == []


class TestNoteConceptsTable:
    """Tests for the materialized note_concepts table."""

    def test_concepts_populated_on_index(
        self, vault: VaultManager, indexer: Indexer
    ):
        vault.create_note(
            NoteType.NOTE, "ML Note", body="About ML.",
            project="test",
            extra_frontmatter={"concepts": ["pytorch", "neural-networks"]},
        )
        indexer.rebuild(full=True)

        rows = indexer.db.execute(
            "SELECT concept FROM note_concepts ORDER BY concept"
        ).fetchall()
        concepts = [r["concept"] for r in rows]
        assert "pytorch" in concepts
        assert "neural-networks" in concepts

    def test_concepts_cleaned_on_remove(
        self, vault: VaultManager, indexer: Indexer
    ):
        path = vault.create_note(
            NoteType.NOTE, "Temp", body="Temp.",
            project="test",
            extra_frontmatter={"concepts": ["pytorch"]},
        )
        indexer.rebuild(full=True)
        assert indexer.db.execute("SELECT COUNT(*) as cnt FROM note_concepts").fetchone()["cnt"] == 1

        path.unlink()
        indexer.rebuild(full=False)
        assert indexer.db.execute("SELECT COUNT(*) as cnt FROM note_concepts").fetchone()["cnt"] == 0

    def test_concepts_cleaned_on_full_rebuild(
        self, vault: VaultManager, indexer: Indexer
    ):
        vault.create_note(
            NoteType.NOTE, "ML Note", body="About ML.",
            project="test",
            extra_frontmatter={"concepts": ["pytorch"]},
        )
        indexer.rebuild(full=True)
        assert indexer.db.execute("SELECT COUNT(*) as cnt FROM note_concepts").fetchone()["cnt"] == 1

        # Full rebuild should still have the concept
        indexer.rebuild(full=True)
        assert indexer.db.execute("SELECT COUNT(*) as cnt FROM note_concepts").fetchone()["cnt"] == 1

    def test_concepts_in_stats(
        self, vault: VaultManager, indexer: Indexer
    ):
        vault.create_note(
            NoteType.NOTE, "Note A", body="A.",
            project="test",
            extra_frontmatter={"concepts": ["pytorch", "cuda"]},
        )
        vault.create_note(
            NoteType.NOTE, "Note B", body="B.",
            project="test",
            extra_frontmatter={"concepts": ["pytorch"]},
        )
        indexer.rebuild(full=True)
        stats = indexer.get_stats()
        assert stats["concepts_total"] == 2  # pytorch and cuda

    def test_no_concepts_no_rows(
        self, vault: VaultManager, indexer: Indexer
    ):
        vault.create_note(NoteType.NOTE, "Plain", body="No concepts.", project="test")
        indexer.rebuild(full=True)
        assert indexer.db.execute("SELECT COUNT(*) as cnt FROM note_concepts").fetchone()["cnt"] == 0


class TestConceptSearch:
    """Tests for concept-based search via the Search class."""

    def test_search_by_concept(
        self, vault: VaultManager, indexer: Indexer, search: Search
    ):
        vault.create_note(
            NoteType.NOTE, "PyTorch Basics", body="Tensors.",
            project="ml",
            extra_frontmatter={"concepts": ["pytorch", "neural-networks"]},
        )
        vault.create_note(
            NoteType.NOTE, "SQL Queries", body="SELECT.",
            project="data",
            extra_frontmatter={"concepts": ["sql", "sqlite"]},
        )
        indexer.rebuild(full=True)

        results = search.search_by_concept("pytorch")
        assert len(results) == 1
        assert results[0].title == "PyTorch Basics"

    def test_search_by_concept_with_project(
        self, vault: VaultManager, indexer: Indexer, search: Search
    ):
        vault.create_note(
            NoteType.NOTE, "ML in Proj A", body="A.",
            project="proj-a",
            extra_frontmatter={"concepts": ["pytorch"]},
        )
        vault.create_note(
            NoteType.NOTE, "ML in Proj B", body="B.",
            project="proj-b",
            extra_frontmatter={"concepts": ["pytorch"]},
        )
        indexer.rebuild(full=True)

        results = search.search_by_concept("pytorch", project="proj-a")
        assert len(results) == 1
        assert results[0].project == "proj-a"

    def test_get_project_concepts(
        self, vault: VaultManager, indexer: Indexer, search: Search
    ):
        vault.create_note(
            NoteType.NOTE, "Note 1", body="1.",
            project="ml-proj",
            extra_frontmatter={"concepts": ["pytorch", "cuda"]},
        )
        vault.create_note(
            NoteType.NOTE, "Note 2", body="2.",
            project="ml-proj",
            extra_frontmatter={"concepts": ["pytorch"]},
        )
        indexer.rebuild(full=True)

        concepts = search.get_project_concepts("ml-proj")
        assert concepts["pytorch"] == 2
        assert concepts["cuda"] == 1

    def test_get_concept_cooccurrence(
        self, vault: VaultManager, indexer: Indexer, search: Search
    ):
        vault.create_note(
            NoteType.NOTE, "A", body="A.",
            project="p",
            extra_frontmatter={"concepts": ["pytorch", "cuda", "gpu"]},
        )
        vault.create_note(
            NoteType.NOTE, "B", body="B.",
            project="p",
            extra_frontmatter={"concepts": ["pytorch", "cuda"]},
        )
        indexer.rebuild(full=True)

        cooccur = search.get_concept_cooccurrence("pytorch")
        concept_names = [c for c, _ in cooccur]
        assert "cuda" in concept_names

    def test_context_with_concepts_param(
        self, vault: VaultManager, indexer: Indexer, search: Search
    ):
        vault.create_note(
            NoteType.NOTE, "Relevant", body="PyTorch stuff.",
            project="ml",
            extra_frontmatter={"concepts": ["pytorch"]},
        )
        vault.create_note(
            NoteType.NOTE, "Irrelevant", body="Cooking.",
            project="misc",
        )
        indexer.rebuild(full=True)

        results = search.get_context(concepts=["pytorch"], limit=5)
        assert len(results) >= 1
        assert any(r.title == "Relevant" for r in results)


class TestConceptEdges:
    """Tests for automatic concept-based edge creation."""

    def test_shared_concepts_create_edges(
        self, vault: VaultManager, indexer: Indexer
    ):
        vault.create_note(
            NoteType.NOTE, "Note A", body="About WAL.",
            project="test",
            extra_frontmatter={"concepts": ["sqlite-wal", "write-ahead-log", "concurrency"]},
        )
        vault.create_note(
            NoteType.NOTE, "Note B", body="Also about WAL.",
            project="test",
            extra_frontmatter={"concepts": ["sqlite-wal", "write-ahead-log"]},
        )
        stats = indexer.rebuild(full=True)
        assert stats["edges"] >= 1

        # Verify edge has concept metadata
        row = indexer.db.execute(
            "SELECT metadata FROM edges WHERE edge_type = 'relates_to' AND metadata IS NOT NULL"
        ).fetchone()
        assert row is not None
        import json
        meta = json.loads(row["metadata"])
        assert meta["via"] == "concept"
        assert "sqlite-wal" in meta["shared"]

    def test_single_shared_concept_creates_edge_at_threshold_1(
        self, vault: VaultManager, indexer: Indexer
    ):
        """With default concept_edge_threshold=1, one shared concept IS enough."""
        vault.create_note(
            NoteType.NOTE, "Note X", body="X.",
            project="test",
            extra_frontmatter={"concepts": ["python"]},
        )
        vault.create_note(
            NoteType.NOTE, "Note Y", body="Y.",
            project="test",
            extra_frontmatter={"concepts": ["python"]},
        )
        stats = indexer.rebuild(full=True)
        row = indexer.db.execute(
            "SELECT metadata FROM edges WHERE metadata IS NOT NULL"
        ).fetchone()
        assert row is not None
        import json
        meta = json.loads(row["metadata"])
        assert meta["via"] == "concept"
        assert "python" in meta["shared"]

    def test_concept_freq_cap_skips_broad_concepts(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        """Concepts appearing in >5% of notes are skipped for edge generation."""
        # Set a very low cap: 50% — with only 2 notes, cap = 1, so any concept
        # appearing in 2 notes exceeds cap.
        config.concept_edge_max_freq_pct = 0.50
        # Create 2 notes with the same concept: 2 > 50% of 2 = 1
        vault.create_note(
            NoteType.NOTE, "Note A", body="A.",
            project="test",
            extra_frontmatter={"concepts": ["ubiquitous"]},
        )
        vault.create_note(
            NoteType.NOTE, "Note B", body="B.",
            project="test",
            extra_frontmatter={"concepts": ["ubiquitous"]},
        )
        stats = indexer.rebuild(full=True)
        row = indexer.db.execute(
            "SELECT metadata FROM edges WHERE edge_type = 'relates_to' AND metadata LIKE '%concept%'"
        ).fetchone()
        assert row is None  # Frequency cap prevented the edge

    def test_no_concepts_no_edges(
        self, vault: VaultManager, indexer: Indexer
    ):
        vault.create_note(NoteType.NOTE, "Plain note", body="No concepts.", project="test")
        stats = indexer.rebuild(full=True)
        row = indexer.db.execute(
            "SELECT COUNT(*) as cnt FROM edges WHERE metadata IS NOT NULL"
        ).fetchone()
        assert row["cnt"] == 0


class TestSessionDirectoryEdges:
    """Tests for automatic session directory inference."""

    def test_session_siblings_get_derived_from_edges(
        self, vault: VaultManager, indexer: Indexer
    ):
        """Notes in a session directory get derived_from edges to the session."""
        # Create a session (this creates sessions/<id>-<date>/session.md)
        session_path = vault.create_note(
            NoteType.SESSION, "Work session", project="test"
        )
        session_note = vault.read_note(session_path)
        session_dir = session_path.parent

        # Create a sibling note in the same session directory
        insight_path = vault.create_note(
            NoteType.NOTE, "Insight from session", project="test",
            output_dir=session_dir,
        )

        stats = indexer.rebuild(full=True)

        # The insight should have a derived_from edge to the session
        import json
        rows = indexer.db.execute(
            "SELECT source, target, metadata FROM edges WHERE edge_type = 'derived_from'"
        ).fetchall()
        session_dir_edges = [
            r for r in rows
            if r["metadata"] and json.loads(r["metadata"]).get("via") == "session_dir"
        ]
        assert len(session_dir_edges) >= 1
        assert any(r["target"] == session_note.id for r in session_dir_edges)

    def test_session_itself_not_self_linked(
        self, vault: VaultManager, indexer: Indexer
    ):
        """A session note should not create a derived_from edge to itself."""
        vault.create_note(NoteType.SESSION, "Solo session", project="test")
        stats = indexer.rebuild(full=True)

        import json
        rows = indexer.db.execute(
            "SELECT source, target FROM edges WHERE edge_type = 'derived_from'"
        ).fetchall()
        for r in rows:
            assert r["source"] != r["target"]


class TestTagEdges:
    """Tests for automatic tag-based edge creation."""

    def test_shared_tags_create_edges(
        self, vault: VaultManager, indexer: Indexer
    ):
        """Notes sharing 2+ topical tags get relates_to edges."""
        vault.create_note(
            NoteType.NOTE, "Note A", body="A.",
            project="test", tags=["debugging", "performance"],
        )
        vault.create_note(
            NoteType.NOTE, "Note B", body="B.",
            project="test", tags=["debugging", "performance", "refactor"],
        )
        stats = indexer.rebuild(full=True)

        import json
        rows = indexer.db.execute(
            "SELECT metadata FROM edges WHERE metadata LIKE '%tag%'"
        ).fetchall()
        assert len(rows) >= 1
        meta = json.loads(rows[0]["metadata"])
        assert meta["via"] == "tag"
        assert "debugging" in meta["shared"]
        assert "performance" in meta["shared"]

    def test_structural_tags_excluded(
        self, vault: VaultManager, indexer: Indexer
    ):
        """Structural tags (todo, probe, parked, til) don't create edges."""
        vault.create_note(
            NoteType.NOTE, "Todo A", body="A.",
            project="test", tags=["todo", "probe"],
        )
        vault.create_note(
            NoteType.NOTE, "Todo B", body="B.",
            project="test", tags=["todo", "probe"],
        )
        stats = indexer.rebuild(full=True)

        rows = indexer.db.execute(
            "SELECT metadata FROM edges WHERE metadata LIKE '%tag%'"
        ).fetchall()
        assert len(rows) == 0

    def test_single_shared_tag_no_edge(
        self, vault: VaultManager, indexer: Indexer
    ):
        """One shared tag is below the default threshold of 2."""
        vault.create_note(
            NoteType.NOTE, "Note A", body="A.",
            project="test", tags=["architecture"],
        )
        vault.create_note(
            NoteType.NOTE, "Note B", body="B.",
            project="test", tags=["architecture"],
        )
        stats = indexer.rebuild(full=True)

        rows = indexer.db.execute(
            "SELECT metadata FROM edges WHERE metadata LIKE '%tag%'"
        ).fetchall()
        assert len(rows) == 0


class TestWikilinkTypeInference:
    """Tests for wikilink edge type inference based on target note type."""

    def test_wikilink_to_source_creates_cites_edge(
        self, vault: VaultManager, indexer: Indexer
    ):
        source_path = vault.create_note(
            NoteType.SOURCE, "research-paper",
            body="An important paper.",
            extra_frontmatter={"source_type": "article"},
        )
        vault.create_note(
            NoteType.NOTE, "My notes",
            body="Based on [[research-paper]] findings.",
            project="test",
        )
        stats = indexer.rebuild(full=True)

        rows = indexer.db.execute(
            "SELECT edge_type FROM edges WHERE edge_type = 'cites'"
        ).fetchall()
        assert len(rows) >= 1

    def test_wikilink_to_session_creates_derived_from_edge(
        self, vault: VaultManager, indexer: Indexer
    ):
        session_path = vault.create_note(
            NoteType.SESSION, "work-session", project="test"
        )
        vault.create_note(
            NoteType.NOTE, "Follow up",
            body="Continuing from [[work-session]].",
            project="test",
        )
        stats = indexer.rebuild(full=True)

        rows = indexer.db.execute(
            "SELECT edge_type FROM edges WHERE edge_type = 'derived_from'"
        ).fetchall()
        assert len(rows) >= 1

    def test_wikilink_to_note_stays_relates_to(
        self, vault: VaultManager, indexer: Indexer
    ):
        vault.create_note(NoteType.NOTE, "concept-a", body="A.", project="test")
        vault.create_note(
            NoteType.NOTE, "concept-b",
            body="Related to [[concept-a]].",
            project="test",
        )
        stats = indexer.rebuild(full=True)

        rows = indexer.db.execute(
            "SELECT edge_type FROM edges"
        ).fetchall()
        assert any(r["edge_type"] == "relates_to" for r in rows)


class TestNoteTags:
    """Tests for the materialized note_tags table."""

    def test_tags_populated_on_index(
        self, vault: VaultManager, indexer: Indexer
    ):
        vault.create_note(
            NoteType.NOTE, "Tagged", body="Has tags.",
            project="test", tags=["debugging", "performance"],
        )
        indexer.rebuild(full=True)

        rows = indexer.db.execute(
            "SELECT tag FROM note_tags ORDER BY tag"
        ).fetchall()
        tags = [r["tag"] for r in rows]
        assert "debugging" in tags
        assert "performance" in tags

    def test_tags_cleaned_on_remove(
        self, vault: VaultManager, indexer: Indexer
    ):
        path = vault.create_note(
            NoteType.NOTE, "Temp", body="T.",
            project="test", tags=["cleanup"],
        )
        indexer.rebuild(full=True)
        assert indexer.db.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0] == 1

        path.unlink()
        indexer.rebuild(full=False)
        assert indexer.db.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0] == 0
