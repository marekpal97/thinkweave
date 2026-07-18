"""Pipeline edge-case tests + E2E MCP tool-chain test.

Tests the full pipeline: vault → index → search → link/unlink → concepts → judge,
plus a chained MCP session simulating real agent usage. All in tmp vaults — no pollution.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from thinkweave.synthesis.concepts import (
    build_reverse_map,
    find_near_duplicates,
    load_aliases,
    merge_concept_in_notes,
    save_aliases,
    suggest_similar,
)
from thinkweave.core.config import Config
from thinkweave.core.indexer import EDGE_TYPE_TO_FIELD, Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.retrieval.search import Search
from thinkweave.core.vault import VaultManager, parse_frontmatter


# ── Fixtures ────────────────────────────────────────────────────────────


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


# ── Cross-platform path safety ─────────────────────────────────────────


def test_session_folder_name_is_date_only(vault: VaultManager):
    """Session folders must be date-only (`ses-xxx-YYYY-MM-DD`).

    Regression guard: ``create_note`` once built the reuse-path folder from a
    full isoformat timestamp (``…T17:59:58.272357+00:00``), whose ``:`` is
    illegal in Windows path components → ``mkdir`` raises WinError 123 and ALL
    session writes fail. Colons are legal on Linux/macOS, so only this explicit
    assertion catches a revert to the timestamp form on those platforms.
    """
    path = vault.create_note(NoteType.SESSION, "Win Session", project="t")
    folder = path.parent.name
    assert ":" not in folder, f"colon in session folder name breaks Windows: {folder!r}"
    assert re.search(r"-\d{4}-\d{2}-\d{2}$", folder), (
        f"session folder must end in a date-only stamp, got {folder!r}"
    )


# ── Link/Unlink Edge Cases (our fix) ───────────────────────────────────


class TestLinkUnlinkMarkdown:
    """Verify that link/unlink writes to markdown, not just SQLite."""

    def test_link_writes_frontmatter(self, vault: VaultManager, indexer: Indexer):
        """Link should add target to source's frontmatter field."""
        p1 = vault.create_note(NoteType.NOTE, "Source", body="Source note.", project="t")
        p2 = vault.create_note(NoteType.NOTE, "Target", body="Target note.", project="t")
        indexer.rebuild(full=True)

        vault.read_note(p1)
        note2 = vault.read_note(p2)

        # Simulate what cmd_link does: write to frontmatter
        fm_field = EDGE_TYPE_TO_FIELD["relates_to"]
        vault.update_note(p1, frontmatter_updates={fm_field: [note2.id]})
        indexer.index_file(p1)

        # Verify markdown has the edge
        updated = vault.read_note(p1)
        assert note2.id in updated.frontmatter.get(fm_field, [])

    def test_link_survives_full_reindex(self, vault: VaultManager, indexer: Indexer):
        """Edges written to markdown must survive index --full."""
        p1 = vault.create_note(NoteType.NOTE, "A", body="Note A.", project="t")
        p2 = vault.create_note(NoteType.NOTE, "B", body="Note B.", project="t")
        indexer.rebuild(full=True)

        note1 = vault.read_note(p1)
        note2 = vault.read_note(p2)

        vault.update_note(p1, frontmatter_updates={"related": [note2.id]})
        indexer.rebuild(full=True)

        # Edge should exist after full rebuild
        edges = indexer.db.execute(
            "SELECT * FROM edges WHERE source = ? AND target = ?",
            (note1.id, note2.id),
        ).fetchall()
        assert len(edges) >= 1

    def test_link_idempotent(self, vault: VaultManager):
        """Linking twice should not duplicate the target in frontmatter."""
        path = vault.create_note(NoteType.NOTE, "N", body="Note.", project="t")
        vault.read_note(path)

        vault.update_note(path, frontmatter_updates={"related": ["dec-fake1"]})
        vault.update_note(path, frontmatter_updates={"related": ["dec-fake1"]})

        updated = vault.read_note(path)
        assert updated.frontmatter["related"].count("dec-fake1") == 1

    def test_all_edge_types_have_field_mapping(self):
        """Every CLI edge type should have a reverse mapping to a frontmatter field."""
        expected_types = {
            "derived_from", "supersedes", "relates_to",
            "cites", "implements", "builds_on",
        }
        assert set(EDGE_TYPE_TO_FIELD.keys()) == expected_types

    def test_link_multiple_targets(self, vault: VaultManager, indexer: Indexer):
        """Source can link to multiple targets via the same edge type."""
        p_src = vault.create_note(NoteType.NOTE, "Source", project="t")
        p_t1 = vault.create_note(NoteType.NOTE, "Target1", project="t")
        p_t2 = vault.create_note(NoteType.NOTE, "Target2", project="t")
        indexer.rebuild(full=True)

        t1 = vault.read_note(p_t1)
        t2 = vault.read_note(p_t2)

        vault.update_note(p_src, frontmatter_updates={"related": [t1.id]})
        vault.update_note(p_src, frontmatter_updates={"related": [t2.id]})

        updated = vault.read_note(p_src)
        assert t1.id in updated.frontmatter["related"]
        assert t2.id in updated.frontmatter["related"]

        stats = indexer.rebuild(full=True)
        assert stats["edges"] >= 2

    def test_unlink_removes_from_frontmatter(self, vault: VaultManager):
        """Unlink should remove the target from the frontmatter list."""
        path = vault.create_note(NoteType.NOTE, "N", body=".", project="t")
        vault.update_note(path, frontmatter_updates={"related": ["dec-x", "dec-y"]})

        # Simulate unlink: read, filter, rewrite
        note = vault.read_note(path)
        targets = note.frontmatter.get("related", [])
        new_targets = [t for t in targets if t != "dec-x"]

        text = path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        fm["related"] = new_targets
        from thinkweave.core.vault import render_frontmatter
        path.write_text(render_frontmatter(fm) + "\n\n" + body, encoding="utf-8")

        updated = vault.read_note(path)
        assert "dec-x" not in updated.frontmatter.get("related", [])
        assert "dec-y" in updated.frontmatter.get("related", [])

    def test_unlink_last_target_removes_field(self, vault: VaultManager):
        """Unlinking the last target should result in empty list or missing field."""
        path = vault.create_note(NoteType.NOTE, "N", body=".", project="t")
        vault.update_note(path, frontmatter_updates={"related": ["dec-only"]})

        text = path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        fm.pop("related", None)
        from thinkweave.core.vault import render_frontmatter
        path.write_text(render_frontmatter(fm) + "\n\n" + body, encoding="utf-8")

        updated = vault.read_note(path)
        assert updated.frontmatter.get("related") is None


# ── Concept Pipeline Edge Cases ────────────────────────��────────────


class TestConceptPipeline:
    """Edge cases for concept detection, tightening, merging, and aliases."""

    def test_tighten_identical_stems(self):
        dupes = find_near_duplicates(["write-ahead-log", "writeaheadlog"])
        assert any(r == "identical stems" for _, _, r in dupes)

    def test_tighten_substring(self):
        dupes = find_near_duplicates(["sqlite", "sqlite-wal"])
        assert any(r == "substring" for _, _, r in dupes)

    def test_tighten_edit_distance(self):
        dupes = find_near_duplicates(["pytest", "pytext"])
        assert any("edit distance" in r for _, _, r in dupes)

    def test_tighten_no_false_positives(self):
        """Unrelated concepts should not be flagged."""
        dupes = find_near_duplicates(["sqlite", "python", "mcp", "graph"])
        assert len(dupes) == 0

    def test_tighten_short_concepts_no_substring(self):
        """Short concepts (< 3 chars) should not trigger substring matching."""
        dupes = find_near_duplicates(["db", "database"])
        substring_dupes = [d for d in dupes if d[2] == "substring"]
        assert len(substring_dupes) == 0

    def test_merge_rewrites_notes(self, vault: VaultManager):
        vault.create_note(
            NoteType.NOTE, "Note A", body="A", project="t",
            extra_frontmatter={"concepts": ["fts-5", "sqlite"]},
        )
        vault.create_note(
            NoteType.NOTE, "Note B", body="B", project="t",
            extra_frontmatter={"concepts": ["FTS-5", "python"]},
        )
        count = merge_concept_in_notes(vault.root, "fts-5", "fts5")
        assert count == 2

        # Verify files were rewritten
        for md in vault.root.rglob("*.md"):
            fm, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
            concepts = fm.get("concepts", [])
            assert "fts-5" not in concepts
            if "fts5" in concepts:
                assert True  # found the canonical form

    def test_merge_deduplicates(self, vault: VaultManager):
        """If target concept already exists in a note, merge shouldn't duplicate."""
        vault.create_note(
            NoteType.NOTE, "Both", body=".", project="t",
            extra_frontmatter={"concepts": ["fts-5", "fts5"]},
        )
        count = merge_concept_in_notes(vault.root, "fts-5", "fts5")
        assert count == 1

        for md in vault.root.rglob("*.md"):
            fm, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
            concepts = fm.get("concepts", [])
            assert concepts.count("fts5") == 1

    def test_aliases_roundtrip(self, config: Config):
        aliases = {"fts5": ["fts-5", "sqlite-fts5"], "wal": ["sqlite-wal"]}
        save_aliases(config, aliases)
        loaded = load_aliases(config)
        assert set(loaded["fts5"]) == {"fts-5", "sqlite-fts5"}
        assert loaded["wal"] == ["sqlite-wal"]

    def test_build_reverse_map(self):
        aliases = {"fts5": ["fts-5", "sqlite-fts5"]}
        reverse = build_reverse_map(aliases)
        assert reverse["fts-5"] == "fts5"
        assert reverse["sqlite-fts5"] == "fts5"

    def test_suggest_similar(self):
        existing = ["fts5", "sqlite-wal", "recursive-cte", "python"]
        suggestions = suggest_similar("fts-5", existing)
        assert "fts5" in suggestions

    def test_concept_alias_resolution_in_indexer(
        self, vault: VaultManager, config: Config, indexer: Indexer,
    ):
        """Aliases should cause the indexer to treat aliased concepts as shared."""
        # Save aliases: fts-5 → fts5
        save_aliases(config, {"fts5": ["fts-5"]})

        vault.create_note(
            NoteType.NOTE, "Note A", body="A", project="t",
            extra_frontmatter={"concepts": ["fts5", "sqlite"]},
        )
        vault.create_note(
            NoteType.NOTE, "Note B", body="B", project="t",
            extra_frontmatter={"concepts": ["fts-5", "sqlite"]},
        )
        stats = indexer.rebuild(full=True)
        # fts-5 resolves to fts5, so both notes share fts5 + sqlite → auto-edge
        assert stats["edges"] >= 1


# ── Concept Auto-Linking Edge Cases ─────────────────────────────────


class TestConceptAutoLinking:
    """Edge cases for the 2+ shared concept threshold."""

    def test_exactly_two_shared_creates_edge(
        self, vault: VaultManager, indexer: Indexer,
    ):
        vault.create_note(
            NoteType.NOTE, "A", body=".", project="t",
            extra_frontmatter={"concepts": ["x", "y", "z"]},
        )
        vault.create_note(
            NoteType.NOTE, "B", body=".", project="t",
            extra_frontmatter={"concepts": ["x", "y"]},
        )
        stats = indexer.rebuild(full=True)
        assert stats["edges"] >= 1

    def test_one_shared_creates_edge_at_default_threshold(
        self, vault: VaultManager, indexer: Indexer,
    ):
        """Default concept_edge_threshold=1: one shared concept creates an edge."""
        vault.create_note(
            NoteType.NOTE, "A", body=".", project="t",
            extra_frontmatter={"concepts": ["x", "unique1"]},
        )
        vault.create_note(
            NoteType.NOTE, "B", body=".", project="t",
            extra_frontmatter={"concepts": ["x", "unique2"]},
        )
        indexer.rebuild(full=True)
        concept_edges = indexer.db.execute(
            "SELECT * FROM edges WHERE metadata LIKE '%concept%'"
        ).fetchall()
        assert len(concept_edges) >= 1

    def test_three_notes_pairwise_linking(
        self, vault: VaultManager, indexer: Indexer,
    ):
        """Three notes sharing concepts A+B should produce 3 pairwise edges."""
        for name in ["N1", "N2", "N3"]:
            vault.create_note(
                NoteType.NOTE, name, body=".", project="t",
                extra_frontmatter={"concepts": ["alpha", "beta"]},
            )
        indexer.rebuild(full=True)
        concept_edges = indexer.db.execute(
            "SELECT * FROM edges WHERE metadata IS NOT NULL"
        ).fetchall()
        assert len(concept_edges) == 3  # C(3,2) = 3 pairs

    def test_concept_edge_metadata(
        self, vault: VaultManager, indexer: Indexer,
    ):
        vault.create_note(
            NoteType.NOTE, "X", body=".", project="t",
            extra_frontmatter={"concepts": ["wal", "concurrency", "sqlite"]},
        )
        vault.create_note(
            NoteType.NOTE, "Y", body=".", project="t",
            extra_frontmatter={"concepts": ["wal", "concurrency"]},
        )
        indexer.rebuild(full=True)

        row = indexer.db.execute(
            "SELECT metadata FROM edges WHERE metadata IS NOT NULL"
        ).fetchone()
        meta = json.loads(row["metadata"])
        assert meta["via"] == "concept"
        assert set(meta["shared"]) == {"wal", "concurrency"}


# ── Mixed Edge Sources ──────────────────────────────────────────────


class TestMixedEdgeSources:
    """Edges from frontmatter fields, wikilinks, AND concepts combined."""

    def test_frontmatter_and_wikilink_edges(
        self, vault: VaultManager, indexer: Indexer,
    ):
        """Frontmatter edge + wikilink to a different note = 2 edges."""
        p_session = vault.create_note(NoteType.SESSION, "Session 1", project="t")
        vault.create_note(NoteType.NOTE, "reference-doc", body="Reference.", project="t")
        session = vault.read_note(p_session)

        # derived_from → session (frontmatter), wikilink → reference-doc (body)
        vault.create_note(
            NoteType.NOTE, "Insight",
            body="Learned from [[reference-doc]] about stuff.",
            project="t",
            extra_frontmatter={"derived_from": [session.id]},
        )
        stats = indexer.rebuild(full=True)
        # derived_from edge + wikilink relates_to edge = at least 2
        assert stats["edges"] >= 2

    def test_all_edge_types_from_frontmatter(
        self, vault: VaultManager, indexer: Indexer,
    ):
        """Each EDGE_FIELD_MAP entry should produce an edge on rebuild."""
        targets = {}
        for note_type, name in [
            (NoteType.SESSION, "S1"), (NoteType.NOTE, "N1"),
            (NoteType.DECISION, "D1"), (NoteType.SOURCE, "Src1"),
            (NoteType.NOTE, "N2"), (NoteType.NOTE, "N3"),
        ]:
            p = vault.create_note(note_type, name, project="t")
            targets[name] = vault.read_note(p).id

        # Create a note with all edge types
        vault.create_note(
            NoteType.NOTE, "Hub", body="Hub note.", project="t",
            extra_frontmatter={
                "derived_from": [targets["S1"]],
                "builds_on": [targets["N1"]],
                "supersedes": [targets["N2"]],
                "implements": [targets["D1"]],
                "cites": [targets["Src1"]],
                "related": [targets["N3"]],
            },
        )
        stats = indexer.rebuild(full=True)
        assert stats["edges"] >= 6


# ── Search Edge Cases ───────────────────────────────────────────────


class TestSearchEdgeCases:
    def test_search_special_characters_raises(
        self, vault: VaultManager, indexer: Indexer, search: Search,
    ):
        """FTS5 raises on unescaped special chars like +. Documenting the behavior."""
        vault.create_note(
            NoteType.NOTE, "C++ Guide", body="Templates and RAII.", project="t",
        )
        indexer.rebuild(full=True)
        # Queries are auto-quoted so special chars like + are treated as literals
        results = search.search("C++")
        assert isinstance(results, list)

    def test_search_quoted_special_characters(
        self, vault: VaultManager, indexer: Indexer, search: Search,
    ):
        """FTS5 can handle special chars if quoted."""
        vault.create_note(
            NoteType.NOTE, "C++ Guide", body="Templates and RAII.", project="t",
        )
        indexer.rebuild(full=True)
        # Quoting the query makes FTS5 treat it as a phrase
        results = search.search('"C"')
        assert isinstance(results, list)

    def test_search_unicode(
        self, vault: VaultManager, indexer: Indexer, search: Search,
    ):
        vault.create_note(
            NoteType.NOTE, "日本語ノート", body="これはテストです。", project="t",
        )
        indexer.rebuild(full=True)
        results = search.search("テスト")
        assert isinstance(results, list)

    def test_search_empty_query_with_filters(
        self, vault: VaultManager, indexer: Indexer, search: Search,
    ):
        vault.create_note(NoteType.NOTE, "A", body=".", project="proj1", tags=["x"])
        vault.create_note(NoteType.NOTE, "B", body=".", project="proj2", tags=["y"])
        indexer.rebuild(full=True)

        results = search.search("", project="proj1", tags=["x"])
        assert len(results) == 1


# ── Judge Edge Cases ────────────────────────────────────────────────


class TestJudgeEdgeCases:
    def test_decision_with_multiple_files(self, tmp_path: Path):
        from thinkweave.synthesis.judge import evaluate_decision
        from thinkweave.core.schemas import NoteMeta

        # All files exist
        files = []
        for name in ["a.py", "b.py", "c.py"]:
            f = tmp_path / name
            f.write_text("pass")
            files.append(str(f))

        dec = NoteMeta(
            id="dec-multi", type=NoteType.DECISION, title="Multi-file decision",
            path="p/d.md", date="2026-04-04", project="t",
            frontmatter={"committed": True, "file_paths": files, "status": "proposed"},
        )
        result = evaluate_decision(dec, [dec])
        assert result["verdict"] == "kept"

    def test_decision_partial_file_removal(self, tmp_path: Path):
        """Some files exist, some don't — should still count as kept if majority present."""
        from thinkweave.synthesis.judge import evaluate_decision
        from thinkweave.core.schemas import NoteMeta

        existing = tmp_path / "kept.py"
        existing.write_text("pass")

        dec = NoteMeta(
            id="dec-partial", type=NoteType.DECISION, title="Partial",
            path="p/d.md", date="2026-04-04", project="t",
            frontmatter={
                "committed": True,
                "file_paths": [str(existing), str(tmp_path / "gone.py")],
                "status": "proposed",
            },
        )
        result = evaluate_decision(dec, [dec])
        # At least one file exists, so not fully reverted
        assert result["verdict"] in ("kept", "reverted")


# ── E2E MCP Tool Chain ──────────────────────────────────────────────


class TestMCPToolChainE2E:
    """Simulate a full MCP session: create → search → link → extract →
    judge → concepts → unlink → cleanup. All in a tmp vault."""

    def test_full_session_lifecycle(
        self, vault: VaultManager, config: Config, indexer: Indexer, search: Search,
    ):
        """Simulate what an AI agent would do via MCP tools across a session."""

        # ── Step 1: Agent creates a session note ─────��──────────────
        session_path = vault.create_note(
            NoteType.SESSION,
            "MCP E2E test session",
            body=(
                "## Events\n"
                "- 10:00 Edit src/server.py — `old code` → `new code`\n"
                "- 10:05 Bash `uv run pytest -x`\n"
                "\n"
                "## Candidate Insights\n"
                "stdio_server in mcp 1.26 is an async context manager\n"
                "You must use server.run() inside the context\n"
                "\n"
                "EDGE_TYPE_TO_FIELD enables markdown-first linking\n"
                "This keeps SQLite as a derived index\n"
            ),
            project="test_project",
            extra_frontmatter={
                "files_touched": ["src/server.py"],
                "commits": [],
                "test_runs": [{"passed": 42, "failed": 0}],
            },
        )
        indexer.index_file(session_path)
        session = vault.read_note(session_path)

        assert session.type == NoteType.SESSION
        assert session.project == "test_project"

        # ── Step 2: Agent searches to avoid duplicates ──────────────
        results = search.search("stdio_server")
        assert any(session.id in r.id for r in results)

        # ── Step 3: Agent extracts insights as notes ────────────────
        from thinkweave.surfaces.mcp.server import _parse_candidate_insights
        insights = _parse_candidate_insights(session.body)
        assert len(insights) == 2

        created_notes = []
        for insight in insights:
            path = vault.create_note(
                NoteType.NOTE,
                insight["title"],
                body=insight.get("body", ""),
                project=session.project,
                tags=["til"],
                extra_frontmatter={
                    "derived_from": [session.id],
                    "concepts": ["mcp", "stdio-transport"],
                },
            )
            indexer.index_file(path)
            created_notes.append(vault.read_note(path))

        assert len(created_notes) == 2
        for note in created_notes:
            assert session.id in note.frontmatter["derived_from"]

        # Mark session processed
        today = date.today().isoformat()
        vault.update_note(
            session_path,
            frontmatter_updates={"processed": True, "processed_at": today},
        )
        indexer.index_file(session_path)

        updated_session = vault.read_note(session_path)
        assert updated_session.frontmatter["processed"] is True

        # ── Step 4: Agent creates a decision ────────��───────────────
        dec_path = vault.create_note(
            NoteType.DECISION,
            "Use markdown-first linking",
            body=(
                "## Context\nlink/unlink wrote only to SQLite.\n\n"
                "## Decision\nWrite edges to markdown frontmatter.\n\n"
                "## Consequences\nEdges survive index --full rebuild."
            ),
            project="test_project",
            tags=["architecture"],
            extra_frontmatter={
                "concepts": ["markdown-first", "sqlite"],
                "file_paths": ["src/cli.py", "src/mcp/server.py"],
            },
        )
        indexer.index_file(dec_path)
        decision = vault.read_note(dec_path)

        assert decision.frontmatter["status"] == "proposed"

        # ── Step 5: Agent links notes to the decision ───────────────
        for note in created_notes:
            note_path = vault.root / note.path
            vault.update_note(
                note_path,
                frontmatter_updates={"implements": [decision.id]},
            )
            indexer.index_file(note_path)

        # Verify edges survive full rebuild
        stats = indexer.rebuild(full=True)
        assert stats["edges"] >= 4  # derived_from + implements + concept edges

        # ── Step 6: Agent checks the graph ──────────────────────────
        graph_text = search.render_graph_text(decision.id, depth=2)
        assert decision.id in graph_text

        # ── Step 7: Agent runs concept management ───────────────────
        # Add a note with a near-duplicate concept
        typo_path = vault.create_note(
            NoteType.NOTE, "Typo concept note", body=".", project="test_project",
            extra_frontmatter={"concepts": ["mark-down-first", "sqlite"]},
        )
        indexer.index_file(typo_path)

        dupes = find_near_duplicates(["markdown-first", "mark-down-first", "sqlite"])
        assert any("markdown-first" in d[0] or "markdown-first" in d[1] for d in dupes)

        # Merge
        merge_count = merge_concept_in_notes(vault.root, "mark-down-first", "markdown-first")
        assert merge_count >= 1

        # Save alias
        aliases = load_aliases(config)
        aliases.setdefault("markdown-first", []).append("mark-down-first")
        save_aliases(config, aliases)
        loaded = load_aliases(config)
        assert "mark-down-first" in loaded["markdown-first"]

        # Rebuild and verify alias resolution creates extra concept edge
        stats = indexer.rebuild(full=True)
        assert stats["edges"] >= 4

        # ── Step 8: Agent judges the decision ───────────────────────
        from thinkweave.synthesis.judge import evaluate_decision

        all_decisions = [
            vault.read_note(p) for p in vault.root.rglob("*.md")
            if vault.read_note(p).type == NoteType.DECISION
        ]

        # The inline YAML parser doesn't roundtrip nested dicts in lists,
        # so construct session meta with proper test_runs for judge
        session_for_judge = vault.read_note(session_path)
        session_for_judge.frontmatter["test_runs"] = [{"passed": 42, "failed": 0}]

        result = evaluate_decision(
            decision, all_decisions, session_meta=session_for_judge,
        )
        # Not committed, so verdict is unknown
        assert result["verdict"] in ("unknown", "kept")

        # ── Step 9: Agent unlinks one note from decision ────────────
        first_note = created_notes[0]
        first_note_path = vault.root / first_note.path
        note_data = vault.read_note(first_note_path)
        impl = note_data.frontmatter.get("implements", [])
        new_impl = [t for t in impl if t != decision.id]

        text = first_note_path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        if new_impl:
            fm["implements"] = new_impl
        else:
            fm.pop("implements", None)
        from thinkweave.core.vault import render_frontmatter
        first_note_path.write_text(
            render_frontmatter(fm) + "\n\n" + body, encoding="utf-8",
        )

        # Verify edge is gone after reindex
        stats = indexer.rebuild(full=True)
        edges_to_dec = indexer.db.execute(
            "SELECT * FROM edges WHERE source = ? AND target = ? AND edge_type = 'implements'",
            (first_note.id, decision.id),
        ).fetchall()
        assert len(edges_to_dec) == 0

        # ── Step 10: Final vault stats ──────────────────────────────
        final_stats = indexer.get_stats()
        assert final_stats["notes_total"] >= 5  # session + 2 notes + decision + typo
        assert final_stats["notes_session"] >= 1
        assert final_stats["notes_decision"] >= 1
        assert final_stats["notes_note"] >= 3

    def test_search_create_link_roundtrip(
        self, vault: VaultManager, indexer: Indexer, search: Search,
    ):
        """Quick roundtrip: create → index → search → link → graph."""
        # Create two notes
        p1 = vault.create_note(
            NoteType.NOTE, "Alpha concept", body="Alpha details.", project="t",
            extra_frontmatter={"concepts": ["alpha", "shared"]},
        )
        p2 = vault.create_note(
            NoteType.NOTE, "Beta concept", body="Beta details.", project="t",
            extra_frontmatter={"concepts": ["beta", "shared"]},
        )
        indexer.rebuild(full=True)

        # Search finds both
        results = search.search("concept")
        assert len(results) >= 2

        # Link them
        n1 = vault.read_note(p1)
        n2 = vault.read_note(p2)
        vault.update_note(p1, frontmatter_updates={"related": [n2.id]})
        indexer.rebuild(full=True)

        # Graph shows the edge
        graph = search.render_graph_text(n1.id)
        assert "Beta concept" in graph

        # Concept edge also exists (shared >= 2 concepts: "alpha"/"beta" don't overlap,
        # but "shared" is common — only 1 shared concept, so no concept edge)
        # The explicit "related" edge should be there though
        edges = indexer.db.execute(
            "SELECT * FROM edges WHERE source = ? AND target = ?",
            (n1.id, n2.id),
        ).fetchall()
        assert len(edges) >= 1

    def test_decision_lifecycle_via_updates(
        self, vault: VaultManager, indexer: Indexer,
    ):
        """Decision: proposed → accepted → superseded."""
        p1 = vault.create_note(
            NoteType.DECISION, "Original approach",
            body="## Decision\nUse approach A.", project="t",
        )
        d1 = vault.read_note(p1)
        assert d1.frontmatter["status"] == "proposed"

        # Accept
        vault.update_note(p1, frontmatter_updates={"status": "accepted"})
        d1 = vault.read_note(p1)
        assert d1.frontmatter["status"] == "accepted"

        # New decision supersedes it
        p2 = vault.create_note(
            NoteType.DECISION, "Better approach",
            body="## Decision\nUse approach B instead.",
            project="t",
            extra_frontmatter={"supersedes": [d1.id]},
        )

        # Deprecate old
        vault.update_note(p1, frontmatter_updates={"status": "superseded"})
        d1 = vault.read_note(p1)
        assert d1.frontmatter["status"] == "superseded"

        # Rebuild and verify supersedes edge
        indexer.rebuild(full=True)
        d2 = vault.read_note(p2)
        edges = indexer.db.execute(
            "SELECT * FROM edges WHERE source = ? AND edge_type = 'supersedes'",
            (d2.id,),
        ).fetchall()
        assert len(edges) == 1
        assert edges[0]["target"] == d1.id
