"""Tests for landing document generation — DECISIONS.md, BACKLOG.md, STATE.md."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.synthesis.landing import (
    LANDING_FILENAMES,
    backlog_summary,
    decisions_ledger,
    generate_all,
    state_of_play,
    state_of_play_context,
    write_landing_docs,
)
from personal_mem.core.schemas import NoteType
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


def _index_all(vault: VaultManager, indexer: Indexer):
    """Helper: full rebuild."""
    indexer.rebuild(full=True)


# --- DECISIONS.md ---


class TestDecisionsLedger:
    def test_empty_project(self, vault: VaultManager, indexer: Indexer, config: Config):
        _index_all(vault, indexer)
        result = decisions_ledger(config, "test-proj")
        assert "No decisions recorded yet." in result
        assert "# Decisions — test-proj" in result

    def test_active_decisions_table(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.DECISION,
            "Use markdown as SoT",
            body="Store everything in .md files.",
            project="test-proj",
            tags=["architecture"],
            extra_frontmatter={"status": "accepted", "summary": "Markdown files are the source of truth"},
        )
        vault.create_note(
            NoteType.DECISION,
            "FTS5 for search",
            body="Use SQLite FTS5 virtual table.",
            project="test-proj",
            tags=["search"],
            extra_frontmatter={"status": "accepted", "summary": "SQLite FTS5 for full-text search"},
        )
        _index_all(vault, indexer)

        result = decisions_ledger(config, "test-proj")
        assert "## Active" in result
        assert "Use markdown as SoT" in result
        assert "FTS5 for search" in result
        assert "Markdown files are the source of truth" in result
        assert "SQLite FTS5 for full-text search" in result

    def test_superseded_decisions_section(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.DECISION,
            "Old approach",
            body="Was replaced.",
            project="test-proj",
            extra_frontmatter={"status": "superseded", "summary": "No longer used"},
        )
        _index_all(vault, indexer)

        result = decisions_ledger(config, "test-proj")
        assert "## Superseded / Deprecated" in result
        assert "Old approach" in result

    def test_summary_fallback_to_body(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.DECISION,
            "No summary field",
            body="This is the first sentence. More details here.",
            project="test-proj",
            extra_frontmatter={"status": "accepted"},
        )
        _index_all(vault, indexer)

        result = decisions_ledger(config, "test-proj")
        assert "This is the first sentence." in result

    def test_mermaid_dag_with_edges(self, vault: VaultManager, indexer: Indexer, config: Config):
        p1 = vault.create_note(
            NoteType.DECISION,
            "Old decision",
            project="test-proj",
            extra_frontmatter={"status": "superseded"},
        )
        old_note = vault.read_note(p1)
        old_id = old_note.id

        vault.create_note(
            NoteType.DECISION,
            "New decision",
            project="test-proj",
            extra_frontmatter={
                "status": "accepted",
                "supersedes": [old_id],
            },
        )
        _index_all(vault, indexer)

        result = decisions_ledger(config, "test-proj")
        assert "```mermaid" in result
        assert "superseded" in result

    def test_filters_by_project(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.DECISION,
            "Project A decision",
            project="proj-a",
            extra_frontmatter={"status": "accepted"},
        )
        vault.create_note(
            NoteType.DECISION,
            "Project B decision",
            project="proj-b",
            extra_frontmatter={"status": "accepted"},
        )
        _index_all(vault, indexer)

        result_a = decisions_ledger(config, "proj-a")
        result_b = decisions_ledger(config, "proj-b")
        assert "Project A decision" in result_a
        assert "Project B decision" not in result_a
        assert "Project B decision" in result_b


# --- BACKLOG.md ---


class TestBacklogSummary:
    def test_empty_backlog(self, vault: VaultManager, indexer: Indexer, config: Config):
        _index_all(vault, indexer)
        result = backlog_summary(config, "test-proj")
        assert "No open items" in result

    def test_todo_items(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.NOTE,
            "Add vector search",
            body="Implement semantic search with embeddings.",
            project="test-proj",
            tags=["todo"],
        )
        vault.create_note(
            NoteType.NOTE,
            "Fix bug in parser",
            body="Edge case with nested lists.",
            project="test-proj",
            tags=["todo", "bugfix"],
        )
        _index_all(vault, indexer)

        result = backlog_summary(config, "test-proj")
        assert "## Open" in result
        assert "Add vector search" in result
        assert "Fix bug in parser" in result

    def test_stalled_proposals(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.DECISION,
            "Plugin system",
            body="Extensibility via plugins.",
            project="test-proj",
            extra_frontmatter={
                "status": "proposed",
                "summary": "Add plugin extensibility",
            },
            # Date defaults to today, so it won't be stalled yet...
            # We need it older than 7 days. Let's test the query logic works.
        )
        _index_all(vault, indexer)

        # By default, today's proposals aren't stalled (< 7 days old)
        result = backlog_summary(config, "test-proj")
        # The proposed decision should NOT appear as stalled since it's from today
        assert "Stalled Proposals" not in result or "Plugin system" not in result

    def test_parked_items(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.NOTE,
            "Real-time sync",
            body="Too complex for now. Out of scope.",
            project="test-proj",
            tags=["parked"],
        )
        _index_all(vault, indexer)

        result = backlog_summary(config, "test-proj")
        assert "## Parked" in result
        assert "Real-time sync" in result


# --- STATE.md ---


class TestStateOfPlay:
    def test_empty_project(self, vault: VaultManager, indexer: Indexer, config: Config):
        _index_all(vault, indexer)
        result = state_of_play(config, "test-proj")
        assert "# State of Play — test-proj" in result

    def test_decisions_worth_understanding(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.DECISION,
            "Untested decision",
            body="Needs review.",
            project="test-proj",
            extra_frontmatter={"status": "accepted", "summary": "Needs human review"},
        )
        _index_all(vault, indexer)

        result = state_of_play(config, "test-proj")
        assert "Decisions Worth Understanding" in result
        assert "Untested decision" in result

    def test_probe_notes_in_explorations(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.NOTE,
            "How does the recursive CTE work?",
            body="It traverses edges bidirectionally using UNION.",
            project="test-proj",
            tags=["probe"],
        )
        _index_all(vault, indexer)

        result = state_of_play(config, "test-proj")
        assert "What You've Been Exploring" in result
        assert "How does the recursive CTE work?" in result

    def test_concept_landscape(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.NOTE,
            "SQLite notes",
            body="About SQLite.",
            project="test-proj",
            extra_frontmatter={"concepts": ["sqlite", "fts5"]},
        )
        vault.create_note(
            NoteType.NOTE,
            "More SQLite",
            body="More about it.",
            project="test-proj",
            extra_frontmatter={"concepts": ["sqlite", "wal"]},
        )
        _index_all(vault, indexer)

        result = state_of_play(config, "test-proj")
        assert "Concept Landscape" in result
        assert "`sqlite` (2)" in result

    def test_key_files_from_decisions(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.DECISION,
            "Refactor vault.py",
            body="Major refactor of the vault module.",
            project="test-proj",
            extra_frontmatter={
                "status": "accepted",
                "file_paths": ["src/vault.py", "src/config.py"],
            },
        )
        _index_all(vault, indexer)

        result = state_of_play(config, "test-proj")
        assert "## Key Files" in result
        assert "src/vault.py" in result
        assert "Refactor vault.py" in result

    def test_state_context_returns_structured_data(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.DECISION,
            "Test decision",
            body="A test.",
            project="test-proj",
            extra_frontmatter={"status": "accepted"},
        )
        _index_all(vault, indexer)

        result = state_of_play_context(config, "test-proj")
        assert "Context for STATE.md" in result
        assert "Test decision" in result


# --- generate_all / write_landing_docs ---


class TestGenerateAll:
    def test_generates_three_docs(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.DECISION,
            "A decision",
            project="test-proj",
            extra_frontmatter={"status": "accepted"},
        )
        _index_all(vault, indexer)

        result = generate_all(config, "test-proj")
        assert "DECISIONS.md" in result
        assert "BACKLOG.md" in result
        assert "STATE.md" in result
        assert "A decision" in result["DECISIONS.md"]

    def test_write_landing_docs(self, vault: VaultManager, indexer: Indexer, config: Config):
        _index_all(vault, indexer)

        # docs="all" now writes 3 project docs + the global THEMES.md.
        written = write_landing_docs(config, "test-proj", docs="all")
        assert len(written) == 4
        for filename, path in written.items():
            assert path.exists()
            assert filename in LANDING_FILENAMES
        # THEMES.md is global (vault root), the others are project-scoped.
        assert written["THEMES.md"] == config.vault_root / "THEMES.md"

    def test_write_single_doc(self, vault: VaultManager, indexer: Indexer, config: Config):
        _index_all(vault, indexer)

        written = write_landing_docs(config, "test-proj", docs="decisions")
        assert len(written) == 1
        assert "DECISIONS.md" in written


# --- Indexer exclusion ---


class TestIndexerExclusion:
    def test_landing_docs_excluded_from_index(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.NOTE,
            "A regular note",
            body="Should be indexed.",
            project="test-proj",
        )
        _index_all(vault, indexer)

        # Write landing docs
        write_landing_docs(config, "test-proj", docs="all")

        # Re-index
        stats = indexer.rebuild(full=True)

        # Landing docs should NOT be in the index
        from personal_mem.retrieval.search import Search
        s = Search(config=config)
        for fname in LANDING_FILENAMES:
            results = s.search(query=fname.replace(".md", ""), limit=10)
            for r in results:
                assert not r.path.endswith(fname), f"{fname} should be excluded from index"
        s.close()

    def test_index_file_skips_landing_doc(self, vault: VaultManager, indexer: Indexer, config: Config):
        _index_all(vault, indexer)

        # Create a landing doc file
        project_dir = config.vault_root / "projects" / "test-proj"
        project_dir.mkdir(parents=True, exist_ok=True)
        decisions_path = project_dir / "DECISIONS.md"
        decisions_path.write_text("---\ntype: note\n---\n# Test", encoding="utf-8")

        # index_file should skip it silently
        indexer.index_file(decisions_path)

        # Verify it's not in the index
        row = indexer.db.execute(
            "SELECT COUNT(*) as cnt FROM notes WHERE path LIKE '%DECISIONS.md'"
        ).fetchone()
        assert row["cnt"] == 0
