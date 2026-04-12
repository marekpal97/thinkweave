"""Tests for claude-mem importer — type mapping, body generation, project normalization, idempotency."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from personal_mem.config import Config
from personal_mem.importers.claude_mem import (
    META_CONCEPT_TO_TAG,
    PROJECT_MAP,
    _build_session_map,
    _content_hash,
    _deduplicate_observations,
    _load_manifest,
    _load_observations,
    _load_session_summaries,
    _observation_tags,
    _parse_json_list,
    _save_manifest,
    build_decision_body,
    build_observation_body,
    build_session_body,
    import_claude_mem,
    normalize_project,
)
from personal_mem.schemas import NoteType
from personal_mem.vault import VaultManager, parse_frontmatter


# ── Fixtures ──────────────────────────────────────────────────────


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


def _create_claude_mem_db(path: Path) -> sqlite3.Connection:
    """Create a minimal claude-mem database with test data."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE observations (
            id INTEGER PRIMARY KEY,
            memory_session_id TEXT,
            project TEXT,
            text TEXT,
            type TEXT,
            title TEXT,
            subtitle TEXT,
            facts TEXT,
            narrative TEXT,
            concepts TEXT,
            files_read TEXT,
            files_modified TEXT,
            prompt_number INTEGER,
            created_at TEXT,
            created_at_epoch INTEGER,
            discovery_tokens INTEGER
        );

        CREATE TABLE session_summaries (
            id INTEGER PRIMARY KEY,
            memory_session_id TEXT,
            project TEXT,
            request TEXT,
            investigated TEXT,
            learned TEXT,
            completed TEXT,
            next_steps TEXT,
            files_read TEXT,
            files_edited TEXT,
            notes TEXT,
            prompt_number INTEGER,
            created_at TEXT,
            created_at_epoch INTEGER,
            discovery_tokens INTEGER
        );
    """)
    return conn


@pytest.fixture
def claude_mem_db(tmp_path: Path) -> Path:
    """Create a test claude-mem database with sample data."""
    db_path = tmp_path / "claude-mem.db"
    conn = _create_claude_mem_db(db_path)

    # Session with summary + 2 observations (1 discovery, 1 decision)
    conn.execute("""
        INSERT INTO session_summaries
        (id, memory_session_id, project, request, investigated, learned, completed,
         next_steps, notes, prompt_number, created_at, created_at_epoch, discovery_tokens)
        VALUES (1, 'ses-aaa-111', 'options_engine',
                'Set up trading system', 'Explored IBKR integration',
                'Three-layer architecture', 'Updated CLAUDE.md',
                'Add tests', 'Good codebase', 1,
                '2026-01-25T15:20:00Z', 1769354400000, 500)
    """)

    conn.execute("""
        INSERT INTO observations
        (id, memory_session_id, project, type, title, subtitle, facts, narrative,
         concepts, files_read, files_modified, prompt_number, created_at,
         created_at_epoch, discovery_tokens)
        VALUES (1, 'ses-aaa-111', 'options_engine', 'discovery',
                'Options Engine Architecture', 'Python trading system',
                '["Uses IBKR API", "Python 3.11+"]',
                'The system has three layers.',
                '["how-it-works", "gotcha"]',
                '["pyproject.toml"]', '[]',
                1, '2026-01-25T15:17:00Z', 1769354220000, 300)
    """)

    conn.execute("""
        INSERT INTO observations
        (id, memory_session_id, project, type, title, subtitle, facts, narrative,
         concepts, files_read, files_modified, prompt_number, created_at,
         created_at_epoch, discovery_tokens)
        VALUES (2, 'ses-aaa-111', 'options_engine', 'decision',
                'Use three-layer architecture', 'Separate IBKR, strategy, execution',
                '["Clean separation of concerns"]',
                'After evaluating options, decided on three layers.',
                '["trade-off", "pattern"]',
                '["main.py"]', '["src/arch.py"]',
                2, '2026-01-25T15:18:00Z', 1769354280000, 200)
    """)

    # Session with observations only (no summary)
    conn.execute("""
        INSERT INTO observations
        (id, memory_session_id, project, type, title, subtitle, facts, narrative,
         concepts, files_read, files_modified, prompt_number, created_at,
         created_at_epoch, discovery_tokens)
        VALUES (3, 'ses-bbb-222', 'hive_swarm', 'feature',
                'Added worker pool', 'Dynamic worker scaling',
                '["Pool auto-scales"]',
                'Implemented dynamic worker pool.',
                '["how-it-works"]',
                '["worker.py"]', '["pool.py"]',
                1, '2026-02-01T10:00:00Z', 1769940000000, 400)
    """)

    # Empty-project observation
    conn.execute("""
        INSERT INTO observations
        (id, memory_session_id, project, type, title, subtitle, facts, narrative,
         concepts, files_read, files_modified, prompt_number, created_at,
         created_at_epoch, discovery_tokens)
        VALUES (4, 'ses-ccc-333', '', 'bugfix',
                'Fixed crash on startup', 'Null pointer in config',
                '["Config was missing default"]',
                'The app crashed because config.default was None.',
                '[]',
                '[]', '["config.py"]',
                1, '2026-02-05T08:00:00Z', 1770278400000, 150)
    """)

    # Summary-only session (no observations)
    conn.execute("""
        INSERT INTO session_summaries
        (id, memory_session_id, project, request, investigated, learned, completed,
         next_steps, notes, prompt_number, created_at, created_at_epoch, discovery_tokens)
        VALUES (2, 'ses-ddd-444', 'code_graph',
                'Explore code graph', 'Looked at AST parsing',
                'AST is fast enough', 'Set up initial parser',
                'Add tests', NULL, 1,
                '2026-02-10T12:00:00Z', 1770710400000, 300)
    """)

    conn.commit()
    conn.close()
    return db_path


# ── Unit tests ────────────────────────────────────────────────────


class TestParseJsonList:
    def test_valid_list(self):
        assert _parse_json_list('["a", "b"]') == ["a", "b"]

    def test_empty_string(self):
        assert _parse_json_list("") == []

    def test_none(self):
        assert _parse_json_list(None) == []

    def test_invalid_json(self):
        assert _parse_json_list("not json") == []

    def test_non_list(self):
        assert _parse_json_list('"string"') == []


class TestNormalizeProject:
    def test_real_project(self):
        assert normalize_project("thinkmesh_neural") == "thinkmesh_neural"

    def test_thinkmesh_stays_separate(self):
        assert normalize_project("thinkmesh") == "thinkmesh"

    def test_empty(self):
        assert normalize_project("") == "_unscoped"

    def test_none(self):
        assert normalize_project(None) == "_unscoped"

    def test_automated(self):
        assert normalize_project("MAR-21") == "_automated"
        assert normalize_project("manual-001") == "_automated"

    def test_unknown_passthrough(self):
        assert normalize_project("some_new_project") == "some_new_project"

    def test_dot_claude(self):
        assert normalize_project(".claude") == "_claude_config"


class TestObservationTags:
    def test_discovery_with_gotcha(self):
        tags = _observation_tags("discovery", '["how-it-works", "gotcha"]')
        assert "discovery" in tags
        assert "gotcha" in tags
        assert "how-it-works" not in tags

    def test_decision_type_excluded(self):
        tags = _observation_tags("decision", '["trade-off"]')
        assert "decision" not in tags  # decision maps to NoteType, not tag
        assert "trade-off" in tags

    def test_empty_concepts(self):
        tags = _observation_tags("feature", "[]")
        assert tags == ["feature"]


class TestContentHash:
    def test_same_content(self):
        h1 = _content_hash("narrative", '["fact"]')
        h2 = _content_hash("narrative", '["fact"]')
        assert h1 == h2

    def test_different_content(self):
        h1 = _content_hash("a", '["b"]')
        h2 = _content_hash("c", '["d"]')
        assert h1 != h2


class TestBuildObservationBody:
    def test_full_body(self):
        body = build_observation_body(
            subtitle="A subtitle",
            narrative="Some narrative.",
            facts_json='["Fact one", "Fact two"]',
            files_read_json='["a.py"]',
            files_modified_json='["b.py"]',
        )
        assert "A subtitle" in body
        assert "## Narrative" in body
        assert "Some narrative." in body
        assert "- Fact one" in body
        assert "- Fact two" in body
        assert "**Read**: a.py" in body
        assert "**Modified**: b.py" in body

    def test_empty_sections_omitted(self):
        body = build_observation_body(
            subtitle="",
            narrative="",
            facts_json='["Only fact"]',
            files_read_json="[]",
            files_modified_json="[]",
        )
        assert "## Narrative" not in body
        assert "## Files" not in body
        assert "- Only fact" in body

    def test_no_facts(self):
        body = build_observation_body(
            subtitle="Sub",
            narrative="Narr",
            facts_json="[]",
            files_read_json="[]",
            files_modified_json="[]",
        )
        assert "## Key Facts" not in body


class TestBuildDecisionBody:
    def test_full_decision(self):
        body = build_decision_body(
            subtitle="Use three layers",
            narrative="After evaluating options...",
            facts_json='["Clean separation"]',
        )
        assert "## Context" in body
        assert "After evaluating options..." in body
        assert "## Decision" in body
        assert "Use three layers" in body
        assert "- Clean separation" in body


class TestBuildSessionBody:
    def test_full_session(self):
        summary = {
            "request": "Set up trading",
            "investigated": "Explored IBKR",
            "learned": "Three layers",
            "completed": "Updated docs",
            "next_steps": "Add tests",
            "notes": "Good code",
        }
        body = build_session_body(summary)
        assert "## Request" in body
        assert "Set up trading" in body
        assert "## Next Steps" in body

    def test_empty_sections_omitted(self):
        summary = {
            "request": "Do something",
            "investigated": "",
            "learned": None,
            "completed": "Done",
            "next_steps": "",
            "notes": "",
        }
        body = build_session_body(summary)
        assert "## Investigated" not in body
        assert "## Learned" not in body
        assert "## Request" in body
        assert "## Completed" in body


class TestDeduplication:
    def test_removes_duplicates(self):
        obs = [
            {"id": 1, "narrative": "same", "facts": '["fact"]'},
            {"id": 2, "narrative": "same", "facts": '["fact"]'},
            {"id": 3, "narrative": "different", "facts": '["other"]'},
        ]
        result = _deduplicate_observations(obs)
        assert len(result) == 2
        assert result[0]["id"] == 1
        assert result[1]["id"] == 3


# ── Data loading tests ────────────────────────────────────────────


class TestDataLoading:
    def test_load_observations(self, claude_mem_db: Path):
        conn = sqlite3.connect(str(claude_mem_db))
        conn.row_factory = sqlite3.Row
        obs = _load_observations(conn)
        conn.close()
        assert len(obs) == 4
        assert obs[0]["title"] == "Options Engine Architecture"

    def test_load_session_summaries(self, claude_mem_db: Path):
        conn = sqlite3.connect(str(claude_mem_db))
        conn.row_factory = sqlite3.Row
        summaries = _load_session_summaries(conn)
        conn.close()
        assert len(summaries) == 2
        assert "ses-aaa-111" in summaries
        assert "ses-ddd-444" in summaries

    def test_build_session_map(self, claude_mem_db: Path):
        conn = sqlite3.connect(str(claude_mem_db))
        conn.row_factory = sqlite3.Row
        obs = _load_observations(conn)
        summaries = _load_session_summaries(conn)
        conn.close()

        session_map = _build_session_map(obs, summaries)
        # 4 sessions total: aaa, bbb, ccc, ddd
        assert len(session_map) == 4

        # aaa has summary + 2 observations
        aaa = session_map["ses-aaa-111"]
        assert aaa["summary"] is not None
        assert len(aaa["observations"]) == 2
        assert aaa["project"] == "options_engine"

        # bbb has observations only
        bbb = session_map["ses-bbb-222"]
        assert bbb["summary"] is None
        assert len(bbb["observations"]) == 1
        assert bbb["project"] == "hive_swarm"

        # ccc has empty project → _unscoped
        ccc = session_map["ses-ccc-333"]
        assert ccc["project"] == "_unscoped"

        # ddd has summary only
        ddd = session_map["ses-ddd-444"]
        assert ddd["summary"] is not None
        assert len(ddd["observations"]) == 0
        assert ddd["project"] == "code_graph"


# ── Manifest tests ────────────────────────────────────────────────


class TestManifest:
    def test_round_trip(self, tmp_path: Path):
        manifest = {"version": 1, "imported_ids": {"obs-1": "n-abc123"}}
        _save_manifest(tmp_path, manifest)
        loaded = _load_manifest(tmp_path)
        assert loaded["imported_ids"]["obs-1"] == "n-abc123"

    def test_missing_manifest(self, tmp_path: Path):
        loaded = _load_manifest(tmp_path)
        assert loaded["version"] == 1
        assert loaded["imported_ids"] == {}


# ── Integration test ──────────────────────────────────────────────


class TestImportClaudeMem:
    def test_full_import(self, config: Config, claude_mem_db: Path):
        """End-to-end: import test DB into a fresh vault."""
        vm = VaultManager(config=config)
        vm.ensure_dirs()

        stats = import_claude_mem(config, db_path=claude_mem_db)

        # 4 sessions: aaa (obs+summary), bbb (obs only), ccc (obs only), ddd (summary only)
        assert stats["sessions"] == 4
        # 2 notes: obs 1 (discovery) + obs 3 (feature) + obs 4 (bugfix)
        assert stats["notes"] == 3
        # 1 decision: obs 2
        assert stats["decisions"] == 1
        assert stats["errors"] == 0

        # Verify vault structure: check that session folders exist
        projects_dir = config.vault_root / "projects"
        assert (projects_dir / "options_engine" / "sessions").exists()
        assert (projects_dir / "hive_swarm" / "sessions").exists()
        assert (projects_dir / "_unscoped" / "sessions").exists()
        assert (projects_dir / "code_graph" / "sessions").exists()

        # Check that the manifest was saved
        manifest = _load_manifest(config.mem_dir)
        assert len(manifest["imported_ids"]) == 8  # 4 sessions + 4 observations

    def test_idempotency(self, config: Config, claude_mem_db: Path):
        """Running import twice should not create duplicates."""
        vm = VaultManager(config=config)
        vm.ensure_dirs()

        stats1 = import_claude_mem(config, db_path=claude_mem_db)
        stats2 = import_claude_mem(config, db_path=claude_mem_db)

        assert stats1["sessions"] == 4
        assert stats2["sessions"] == 0
        assert stats2["skipped"] == 8  # 4 sessions + 4 observations

    def test_project_filter(self, config: Config, claude_mem_db: Path):
        """Project filter should only import matching sessions."""
        vm = VaultManager(config=config)
        vm.ensure_dirs()

        stats = import_claude_mem(
            config, db_path=claude_mem_db, project_filter="hive_swarm"
        )

        assert stats["sessions"] == 1
        assert stats["notes"] == 1
        assert stats["decisions"] == 0

    def test_dry_run(self, config: Config, claude_mem_db: Path):
        """Dry run should not write any files."""
        vm = VaultManager(config=config)
        vm.ensure_dirs()

        stats = import_claude_mem(config, db_path=claude_mem_db, dry_run=True)

        assert stats["sessions"] == 4
        assert stats["notes"] == 3
        assert stats["decisions"] == 1

        # No files should be created (except the vault dirs)
        projects_dir = config.vault_root / "projects"
        project_folders = [p for p in projects_dir.iterdir() if p.is_dir()] if projects_dir.exists() else []
        # Only pre-existing dirs from ensure_dirs(), no project session folders
        session_mds = list(projects_dir.rglob("session.md"))
        assert len(session_mds) == 0

    def test_missing_db(self, config: Config, tmp_path: Path):
        """Should return error for missing database."""
        stats = import_claude_mem(config, db_path=tmp_path / "nonexistent.db")
        assert "error" in stats

    def test_decision_frontmatter(self, config: Config, claude_mem_db: Path):
        """Decisions should get status=accepted, summary, derived_from."""
        vm = VaultManager(config=config)
        vm.ensure_dirs()

        import_claude_mem(config, db_path=claude_mem_db)

        # Find the decision note
        decision_files = list(config.vault_root.rglob("use-three-layer-architecture*.md"))
        assert len(decision_files) == 1

        fm, body = parse_frontmatter(decision_files[0].read_text(encoding="utf-8"))
        assert fm["type"] == "decision"
        assert fm["status"] == "accepted"
        assert fm["imported_from"] == "claude-mem"
        assert "source_id" in fm
        assert "derived_from" in fm
        assert "## Context" in body
        assert "## Decision" in body

    def test_session_has_files_touched(self, config: Config, claude_mem_db: Path):
        """Session notes should aggregate files_touched from observations."""
        vm = VaultManager(config=config)
        vm.ensure_dirs()

        import_claude_mem(config, db_path=claude_mem_db)

        # Find a session.md for options_engine (has observations with files)
        session_files = list(
            (config.vault_root / "projects" / "options_engine").rglob("session.md")
        )
        assert len(session_files) >= 1

        fm, _ = parse_frontmatter(session_files[0].read_text(encoding="utf-8"))
        files = fm.get("files_touched", [])
        assert "pyproject.toml" in files
        assert "main.py" in files

    def test_observation_placed_in_session_folder(self, config: Config, claude_mem_db: Path):
        """Observation notes should be inside their session's folder."""
        vm = VaultManager(config=config)
        vm.ensure_dirs()

        import_claude_mem(config, db_path=claude_mem_db)

        # The discovery note should be in the same folder as its session
        discovery_files = list(config.vault_root.rglob("options-engine-architecture*.md"))
        assert len(discovery_files) == 1

        # Its parent should be a session folder (not misc/)
        parent = discovery_files[0].parent
        assert parent.name != "misc"
        # Should have a session.md sibling
        assert (parent / "session.md").exists()

    def test_original_date_preserved(self, config: Config, claude_mem_db: Path):
        """Imported notes should have the original claude-mem date, not today."""
        vm = VaultManager(config=config)
        vm.ensure_dirs()

        import_claude_mem(config, db_path=claude_mem_db)

        discovery_files = list(config.vault_root.rglob("options-engine-architecture*.md"))
        fm, _ = parse_frontmatter(discovery_files[0].read_text(encoding="utf-8"))
        assert fm["date"].startswith("2026-01-25")
