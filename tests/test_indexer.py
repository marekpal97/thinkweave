"""Tests for the SQLite indexer and search engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.config import Config
from personal_mem.indexer import Indexer
from personal_mem.schemas import NoteType
from personal_mem.search import Search
from personal_mem.vault import VaultManager


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

    def test_single_shared_concept_no_edge(
        self, vault: VaultManager, indexer: Indexer
    ):
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
        # Only 1 shared concept — should NOT create an edge
        row = indexer.db.execute(
            "SELECT metadata FROM edges WHERE metadata IS NOT NULL"
        ).fetchone()
        assert row is None

    def test_no_concepts_no_edges(
        self, vault: VaultManager, indexer: Indexer
    ):
        vault.create_note(NoteType.NOTE, "Plain note", body="No concepts.", project="test")
        stats = indexer.rebuild(full=True)
        row = indexer.db.execute(
            "SELECT COUNT(*) as cnt FROM edges WHERE metadata IS NOT NULL"
        ).fetchone()
        assert row["cnt"] == 0
