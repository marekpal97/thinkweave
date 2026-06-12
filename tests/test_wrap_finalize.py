"""Tests for the deterministic wrap-finalize tail (operations/wrap.py + CLI).

``mem wrap-finalize`` is phase 2 of ``/mem-wrap`` — after ``mem_extract`` has
written a session's insights/decisions, this bundles prune → index → judge →
landing → drift-advisory into one process. These tests build a tmp vault that
looks like a just-extracted session and assert the chain runs cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager
from personal_mem.operations.wrap import WrapFinalizeResult, finalize_wrap


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


def _index(config: Config) -> None:
    idx = Indexer(config=config)
    idx.rebuild(full=True)
    idx.close()


def _seed_session_with_decision(vm: VaultManager) -> str:
    """Create a session note + a decision derived from it (mimics mem_extract output).

    Returns the session note ID.
    """
    sess_path = vm.create_note(
        NoteType.SESSION,
        "Did some work",
        body="## Summary\nDid some work.\n",
        project="t",
        extra_frontmatter={"processed": True, "processed_at": "2026-05-13"},
    )
    session_id = vm.read_note(sess_path).id
    vm.create_note(
        NoteType.DECISION,
        "Use SQLite for the index",
        body="## Context\n\nNeeded a derived index.\n\n## Decision\n\nUse SQLite.",
        project="t",
        extra_frontmatter={
            "status": "accepted",
            "committed": True,
            "source_session": session_id,
            "derived_from": [session_id],
            "concepts": ["sqlite", "memory-system"],
        },
        output_dir=sess_path.parent,
    )
    return session_id


class TestFinalizeWrap:
    def test_runs_end_to_end(self, config: Config, vault: VaultManager):
        session_id = _seed_session_with_decision(vault)
        _index(config)

        result = finalize_wrap(config, session_id=session_id, project="t")

        assert isinstance(result, WrapFinalizeResult)
        assert result.errors == []
        assert result.decisions_judged == 1
        assert sum(result.verdicts.values()) == 1
        assert len(result.landing_written) >= 1
        assert any("DECISION" in name.upper() for name in result.landing_written)
        assert any("BACKLOG" in name.upper() for name in result.landing_written)
        # P1-9 — every step contributes a timing entry (even if the step is a
        # no-op or errors out; the `finally` blocks stamp wall time regardless).
        assert set(result.timings) == {
            "prune", "index", "judge", "landing", "drift",
        }
        assert all(v >= 0.0 for v in result.timings.values())

    def test_judge_writes_verdict_to_decision_frontmatter(
        self, config: Config, vault: VaultManager
    ):
        session_id = _seed_session_with_decision(vault)
        _index(config)

        finalize_wrap(config, session_id=session_id, project="t")

        from personal_mem.synthesis.judge import find_decisions

        idx = Indexer(config=config)
        try:
            decs = find_decisions(idx.db, vault, session_id=session_id)
        finally:
            idx.close()
        assert len(decs) == 1
        assert "verdict" in decs[0].frontmatter
        assert "judged_at" in decs[0].frontmatter

    def test_no_decisions_is_fine(self, config: Config, vault: VaultManager):
        sess_path = vault.create_note(
            NoteType.SESSION,
            "Empty session",
            body="## Summary\nNothing happened.\n",
            project="t",
            extra_frontmatter={"processed": True},
        )
        session_id = vault.read_note(sess_path).id
        _index(config)

        result = finalize_wrap(config, session_id=session_id, project="t")
        assert result.errors == []
        assert result.decisions_judged == 0

    def test_missing_project_is_recorded_as_error(
        self, config: Config, vault: VaultManager
    ):
        sess_path = vault.create_note(
            NoteType.SESSION, "S", body="## Summary\n", project="t"
        )
        session_id = vault.read_note(sess_path).id
        _index(config)

        result = finalize_wrap(config, session_id=session_id, project="")
        assert any("landing" in e for e in result.errors)

    def test_prune_removes_orphan_folder(self, config: Config, vault: VaultManager):
        orphan = config.vault_root / "projects" / "t" / "sessions" / "orphan-old"
        orphan.mkdir(parents=True, exist_ok=True)
        (orphan / "session.md").write_text(
            "---\ntype: session\nid: ses-orphan1\ndate: '2020-01-01'\nproject: t\n"
            "files_touched: []\ncommits: []\n---\n\n# orphan stub\n",
            encoding="utf-8",
        )
        session_id = _seed_session_with_decision(vault)
        _index(config)

        result = finalize_wrap(config, session_id=session_id, project="t", prune=True)
        assert result.orphans_pruned == 1
        assert not orphan.exists()

    def test_no_prune_keeps_orphan_folder(self, config: Config, vault: VaultManager):
        orphan = config.vault_root / "projects" / "t" / "sessions" / "orphan-old"
        orphan.mkdir(parents=True, exist_ok=True)
        (orphan / "session.md").write_text(
            "---\ntype: session\nid: ses-orphan2\ndate: '2020-01-01'\nproject: t\n"
            "files_touched: []\ncommits: []\n---\n\n# orphan stub\n",
            encoding="utf-8",
        )
        session_id = _seed_session_with_decision(vault)
        _index(config)

        result = finalize_wrap(config, session_id=session_id, project="t", prune=False)
        assert result.orphans_pruned == 0
        assert orphan.exists()


class TestWrapFinalizeCLI:
    def test_json_output_parses(
        self, config: Config, vault: VaultManager, monkeypatch, capsys
    ):
        session_id = _seed_session_with_decision(vault)
        _index(config)

        # Point load_config at our tmp vault.
        monkeypatch.setenv("PERSONAL_MEM_VAULT", str(config.vault_root))
        monkeypatch.setenv("PERSONAL_MEM_PROJECT", "t")

        from personal_mem.surfaces.cli.wrap import cmd_wrap_finalize

        args = type(
            "Args",
            (),
            {"session_id": session_id, "project": "t", "json": True, "no_prune": True},
        )()
        with pytest.raises(SystemExit) as exc:
            cmd_wrap_finalize(args)
        assert exc.value.code == 0  # no errors

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["session_id"] == session_id
        assert payload["decisions_judged"] == 1
        assert payload["errors"] == []
