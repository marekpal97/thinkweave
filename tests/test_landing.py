"""Tests for landing document generation — DECISIONS.md, BACKLOG.md, STATE.md."""

from __future__ import annotations

from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.synthesis.landing import (
    DEFAULT_LANDING_FILENAMES,
    LANDING_FILENAMES,
    backlog_summary,
    decisions_ledger,
    generate_all,
    landing_filename_set,
    landing_filenames,
    state_of_play,
    state_of_play_context,
    write_landing_docs,
)
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager


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
        result = decisions_ledger(config, "test_proj")
        assert "No decisions recorded yet." in result
        assert "# Decisions — test_proj" in result

    def test_active_decisions_table(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.DECISION,
            "Use markdown as SoT",
            body="Store everything in .md files.",
            project="test_proj",
            tags=["architecture"],
            extra_frontmatter={"status": "accepted", "summary": "Markdown files are the source of truth"},
        )
        vault.create_note(
            NoteType.DECISION,
            "FTS5 for search",
            body="Use SQLite FTS5 virtual table.",
            project="test_proj",
            tags=["search"],
            extra_frontmatter={"status": "accepted", "summary": "SQLite FTS5 for full-text search"},
        )
        _index_all(vault, indexer)

        result = decisions_ledger(config, "test_proj")
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
            project="test_proj",
            extra_frontmatter={"status": "superseded", "summary": "No longer used"},
        )
        _index_all(vault, indexer)

        result = decisions_ledger(config, "test_proj")
        assert "## Superseded / Deprecated" in result
        assert "Old approach" in result

    def test_summary_fallback_to_body(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.DECISION,
            "No summary field",
            body="This is the first sentence. More details here.",
            project="test_proj",
            extra_frontmatter={"status": "accepted"},
        )
        _index_all(vault, indexer)

        result = decisions_ledger(config, "test_proj")
        assert "This is the first sentence." in result

    def test_mermaid_dag_with_edges(self, vault: VaultManager, indexer: Indexer, config: Config):
        p1 = vault.create_note(
            NoteType.DECISION,
            "Old decision",
            project="test_proj",
            extra_frontmatter={"status": "superseded"},
        )
        old_note = vault.read_note(p1)
        old_id = old_note.id

        vault.create_note(
            NoteType.DECISION,
            "New decision",
            project="test_proj",
            extra_frontmatter={
                "status": "accepted",
                "supersedes": [old_id],
            },
        )
        _index_all(vault, indexer)

        result = decisions_ledger(config, "test_proj")
        assert "```mermaid" in result
        assert "superseded" in result

    def test_filters_by_project(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.DECISION,
            "Project A decision",
            project="proj_a",
            extra_frontmatter={"status": "accepted"},
        )
        vault.create_note(
            NoteType.DECISION,
            "Project B decision",
            project="proj_b",
            extra_frontmatter={"status": "accepted"},
        )
        _index_all(vault, indexer)

        result_a = decisions_ledger(config, "proj_a")
        result_b = decisions_ledger(config, "proj_b")
        assert "Project A decision" in result_a
        assert "Project B decision" not in result_a
        assert "Project B decision" in result_b


# --- BACKLOG.md ---


class TestBacklogSummary:
    def test_empty_backlog(self, vault: VaultManager, indexer: Indexer, config: Config):
        _index_all(vault, indexer)
        result = backlog_summary(config, "test_proj")
        assert "No open items" in result

    def test_todo_items(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.NOTE,
            "Add vector search",
            body="Implement semantic search with embeddings.",
            project="test_proj",
            tags=["todo"],
        )
        vault.create_note(
            NoteType.NOTE,
            "Fix bug in parser",
            body="Edge case with nested lists.",
            project="test_proj",
            tags=["todo", "bugfix"],
        )
        _index_all(vault, indexer)

        result = backlog_summary(config, "test_proj")
        assert "## Open" in result
        assert "Add vector search" in result
        assert "Fix bug in parser" in result

    def test_stalled_proposals(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.DECISION,
            "Plugin system",
            body="Extensibility via plugins.",
            project="test_proj",
            extra_frontmatter={
                "status": "proposed",
                "summary": "Add plugin extensibility",
            },
            # Date defaults to today, so it won't be stalled yet...
            # We need it older than 7 days. Let's test the query logic works.
        )
        _index_all(vault, indexer)

        # By default, today's proposals aren't stalled (< 7 days old)
        result = backlog_summary(config, "test_proj")
        # The proposed decision should NOT appear as stalled since it's from today
        assert "Stalled Proposals" not in result or "Plugin system" not in result

    def test_parked_items(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.NOTE,
            "Real-time sync",
            body="Too complex for now. Out of scope.",
            project="test_proj",
            tags=["parked"],
        )
        _index_all(vault, indexer)

        result = backlog_summary(config, "test_proj")
        assert "## Parked" in result
        assert "Real-time sync" in result


# --- STATE.md ---


class TestStateOfPlay:
    def test_empty_project(self, vault: VaultManager, indexer: Indexer, config: Config):
        _index_all(vault, indexer)
        result = state_of_play(config, "test_proj")
        assert "# State of Play — test_proj" in result

    def test_decisions_worth_understanding(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.DECISION,
            "Untested decision",
            body="Needs review.",
            project="test_proj",
            extra_frontmatter={"status": "accepted", "summary": "Needs human review"},
        )
        _index_all(vault, indexer)

        result = state_of_play(config, "test_proj")
        assert "Decisions Worth Understanding" in result
        assert "Untested decision" in result

    def test_probe_notes_in_explorations(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.NOTE,
            "How does the recursive CTE work?",
            body="It traverses edges bidirectionally using UNION.",
            project="test_proj",
            tags=["probe"],
        )
        _index_all(vault, indexer)

        result = state_of_play(config, "test_proj")
        # Phase 4 E renamed the heading from "What You've Been Exploring"
        # to "Open Probes" when it merged manual `probe`-tagged notes
        # with classified prompt events.
        assert "Open Probes" in result
        assert "How does the recursive CTE work?" in result

    def test_concept_landscape(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.NOTE,
            "SQLite notes",
            body="About SQLite.",
            project="test_proj",
            extra_frontmatter={"concepts": ["sqlite", "fts5"]},
        )
        vault.create_note(
            NoteType.NOTE,
            "More SQLite",
            body="More about it.",
            project="test_proj",
            extra_frontmatter={"concepts": ["sqlite", "wal"]},
        )
        _index_all(vault, indexer)

        result = state_of_play(config, "test_proj")
        assert "Concept Landscape" in result
        assert "`sqlite` (2)" in result

    def test_key_files_from_decisions(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.DECISION,
            "Refactor vault.py",
            body="Major refactor of the vault module.",
            project="test_proj",
            extra_frontmatter={
                "status": "accepted",
                "file_paths": ["src/vault.py", "src/config.py"],
            },
        )
        _index_all(vault, indexer)

        result = state_of_play(config, "test_proj")
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
            project="test_proj",
            extra_frontmatter={"status": "accepted"},
        )
        _index_all(vault, indexer)

        result = state_of_play_context(config, "test_proj")
        assert "Context for STATE.md" in result
        assert "Test decision" in result


# --- generate_all / write_landing_docs ---


class TestGenerateAll:
    def test_generates_three_docs(self, vault: VaultManager, indexer: Indexer, config: Config):
        vault.create_note(
            NoteType.DECISION,
            "A decision",
            project="test_proj",
            extra_frontmatter={"status": "accepted"},
        )
        _index_all(vault, indexer)

        result = generate_all(config, "test_proj")
        assert "DECISIONS.md" in result
        assert "BACKLOG.md" in result
        assert "STATE.md" in result
        assert "A decision" in result["DECISIONS.md"]

    def test_write_landing_docs(self, vault: VaultManager, indexer: Indexer, config: Config):
        _index_all(vault, indexer)

        # docs="all" now writes 3 project docs + the global THEMES.md.
        written = write_landing_docs(config, "test_proj", docs="all")
        assert len(written) == 4
        for filename, path in written.items():
            assert path.exists()
            assert filename in LANDING_FILENAMES
        # THEMES.md is global (vault root), the others are project-scoped.
        assert written["THEMES.md"] == config.vault_root / "THEMES.md"

    def test_write_single_doc(self, vault: VaultManager, indexer: Indexer, config: Config):
        _index_all(vault, indexer)

        written = write_landing_docs(config, "test_proj", docs="decisions")
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
            project="test_proj",
        )
        _index_all(vault, indexer)

        # Write landing docs
        write_landing_docs(config, "test_proj", docs="all")

        # Re-index
        stats = indexer.rebuild(full=True)

        # Landing docs should NOT be in the index
        from thinkweave.retrieval.search import Search
        s = Search(config=config)
        for fname in LANDING_FILENAMES:
            results = s.search(query=fname.replace(".md", ""), limit=10)
            for r in results:
                assert not r.path.endswith(fname), f"{fname} should be excluded from index"
        s.close()

    def test_index_file_skips_landing_doc(self, vault: VaultManager, indexer: Indexer, config: Config):
        _index_all(vault, indexer)

        # Create a landing doc file
        project_dir = config.vault_root / "projects" / "test_proj"
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


# --- Config-driven landing filenames (Phase 4 H4) ---


class TestLandingFilenamesConfig:
    """Verify landing-doc filenames flow through sources.yaml overrides."""

    def test_defaults_when_no_user_yaml(self, tmp_path: Path):
        names = landing_filenames(tmp_path / "vault")
        assert names == DEFAULT_LANDING_FILENAMES

    def test_user_override_replaces_filename(self, tmp_path: Path):
        vault_root = tmp_path / "vault"
        (vault_root / "config").mkdir(parents=True, exist_ok=True)
        (vault_root / "config" / "sources.yaml").write_text(
            "landing_files:\n  state: STATUS.md\n  backlog: TODO.md\n",
            encoding="utf-8",
        )
        names = landing_filenames(vault_root)
        assert names["state"] == "STATUS.md"
        assert names["backlog"] == "TODO.md"
        # Untouched defaults remain
        assert names["decisions"] == "DECISIONS.md"

    def test_filename_set_picks_up_overrides(self, tmp_path: Path):
        vault_root = tmp_path / "vault"
        (vault_root / "config").mkdir(parents=True, exist_ok=True)
        (vault_root / "config" / "sources.yaml").write_text(
            "landing_files:\n  state: STATUS.md\n", encoding="utf-8",
        )
        s = landing_filename_set(vault_root)
        assert "STATUS.md" in s
        assert "STATE.md" not in s

    def test_write_landing_docs_respects_override(
        self, tmp_path: Path
    ):
        vault_root = tmp_path / "vault"
        (vault_root / "config").mkdir(parents=True, exist_ok=True)
        (vault_root / "config" / "sources.yaml").write_text(
            "landing_files:\n  state: STATUS.md\n  decisions: ADR.md\n",
            encoding="utf-8",
        )
        cfg = Config(vault_root=vault_root)
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        idx = Indexer(config=cfg)
        try:
            idx.rebuild(full=True)
        finally:
            idx.close()

        written = write_landing_docs(cfg, "any-proj", docs="all")
        # The renamed names appear in the result; old defaults don't.
        assert "STATUS.md" in written
        assert "ADR.md" in written
        assert "STATE.md" not in written
        assert "DECISIONS.md" not in written

    def test_legacy_constant_still_importable(self):
        # Backwards-compat: the old set is still importable for callers
        # that only need the in-code defaults.
        assert "STATE.md" in LANDING_FILENAMES
        assert "DECISIONS.md" in LANDING_FILENAMES

    def test_write_landing_docs_explicit_override(
        self, tmp_path: Path
    ):
        """Caller-passed ``landing_filenames_override`` wins over the
        sources.yaml mapping AND the defaults."""
        vault_root = tmp_path / "vault"
        (vault_root / "config").mkdir(parents=True, exist_ok=True)
        # User config sets one rename — the override should still beat it.
        (vault_root / "config" / "sources.yaml").write_text(
            "landing_files:\n  state: STATUS.md\n", encoding="utf-8",
        )
        cfg = Config(vault_root=vault_root)
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        idx = Indexer(config=cfg)
        try:
            idx.rebuild(full=True)
        finally:
            idx.close()

        written = write_landing_docs(
            cfg,
            "any-proj",
            docs="all",
            landing_filenames_override={
                "state": "OVERVIEW.md",
                "decisions": "ADR.md",
            },
        )
        # Explicit override beats both defaults and sources.yaml entries
        assert "OVERVIEW.md" in written
        assert "ADR.md" in written
        assert "STATUS.md" not in written  # sources.yaml value got beaten
        assert "STATE.md" not in written
        # Untouched keys still resolve via the resolved (sources.yaml ∪
        # defaults) chain
        assert "BACKLOG.md" in written
        assert "THEMES.md" in written
        # All overridden files exist on disk under the project dir
        assert (vault_root / "projects" / "any-proj" / "OVERVIEW.md").exists()
        assert (vault_root / "projects" / "any-proj" / "ADR.md").exists()


class TestProbeCapsFromConfig:
    """Bucket-3 audit: landing probe caps read config ``landing.*``."""

    def test_open_probes_display_cap_override(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        """STATE's "Open Probes" section displays at most
        ``landing.probes_display_cap`` entries."""
        for i in range(4):
            vault.create_note(
                NoteType.NOTE,
                f"Probe question number {i}?",
                body="exploratory",
                project="test_proj",
                tags=["probe"],
            )
        _index_all(vault, indexer)

        config.landing_probes_display_cap = 2
        result = state_of_play(config, "test_proj")
        shown = [
            ln for ln in result.splitlines()
            if "Probe question number" in ln
        ]
        assert len(shown) == 2

    def test_open_probes_gather_cap_override(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        """``_gather_prompt_probes``'s default limit reads
        ``landing.open_probes_cap``."""
        import json as _json
        from datetime import datetime, timedelta, timezone

        from thinkweave.synthesis.landing import _gather_prompt_probes

        sess_dir = (
            config.vault_root / "projects" / "test_proj" / "sessions" / "s1"
        )
        sess_dir.mkdir(parents=True)
        base = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        rows = [
            {
                "type": "prompt",
                "text": f"What about thing {i}?",
                "session_id": "abc",
                "ts": (base + timedelta(minutes=i)).isoformat(),
            }
            for i in range(5)
        ]
        (sess_dir / "events.jsonl").write_text(
            "\n".join(_json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )

        # Default config cap (20) returns all five probes.
        assert len(_gather_prompt_probes(config, "test_proj")) == 5

        config.landing_open_probes_cap = 3
        probes = _gather_prompt_probes(config, "test_proj")
        assert len(probes) == 3
        # Explicit limit still overrides config.
        assert len(_gather_prompt_probes(config, "test_proj", limit=1)) == 1


class TestReflink:
    """Contract for the landing ``_reflink`` helper. Pinned before it was
    collapsed onto ``synthesis.hub.reflink`` (QW / issue #15) so the
    consolidation stays behaviour-preserving on the paths landing exercises.
    """

    def _reflink(self, *args, **kwargs):
        from thinkweave.synthesis.landing import _reflink

        return _reflink(*args, **kwargs)

    def test_in_map_without_display_uses_id_alias(self):
        idmap = {"dec-1": "projects/p/decisions/dec-1"}
        assert self._reflink(idmap, "dec-1") == "[[projects/p/decisions/dec-1|dec-1]]"

    def test_in_map_with_display_uses_display_alias(self):
        idmap = {"thm-1": "themes/thm-1-slug"}
        assert self._reflink(idmap, "thm-1", "My Theme") == "[[themes/thm-1-slug|My Theme]]"

    def test_missing_id_without_display_is_bare_link(self):
        assert self._reflink({}, "dec-9") == "[[dec-9]]"

    def test_missing_id_with_display_keeps_label(self):
        # Dangling ref (e.g. a parent theme not yet indexed) still shows its
        # label — landing never dropped the alias here.
        assert self._reflink({}, "thm-9", "Parent") == "[[thm-9|Parent]]"
