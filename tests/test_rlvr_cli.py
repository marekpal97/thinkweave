"""Tests for ``mem rlvr export`` — slice 6 of the RLVR substrate.

The CLI layer is thin (one ``json.dumps`` per row + stdout) — these tests
exercise: schema parses, filters apply, stdout is pure JSONL (no decoration),
status line goes to stderr when ``--verbose``, missing action prints help.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
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


def _seed_n_decisions(
    vault: VaultManager, n: int, *,
    project: str = "t", committed: bool = True,
) -> None:
    for i in range(n):
        sess_path = vault.create_note(
            NoteType.SESSION, f"S{i}", body="## Summary\n", project=project,
        )
        sess_id = vault.read_note(sess_path).id
        vault.create_note(
            NoteType.DECISION, f"D{i}",
            body="## Context\n\n## Decision\n",
            project=project,
            extra_frontmatter={
                "status": "accepted",
                "committed": committed,
                "source_session": sess_id,
                "derived_from": [sess_id],
                "concepts": ["a", "b"],
                "date": f"2026-05-{10 + i:02d}",
            },
            output_dir=sess_path.parent,
        )


def _index(config: Config) -> None:
    idx = Indexer(config=config)
    idx.rebuild(full=True)
    idx.close()


def _run_export(*, vault: Path, project: str = "",
                committed_only: bool = False, since: str = "",
                until: str = "", verbose: bool = False,
                monkeypatch=None, capsys=None) -> tuple[str, str]:
    """Invoke ``mem rlvr export`` and return (stdout, stderr)."""
    from personal_mem.surfaces.cli.rlvr import cmd_rlvr

    monkeypatch.setenv("PERSONAL_MEM_VAULT", str(vault))
    args = type("Args", (), {
        "rlvr_action": "export",
        "project": project,
        "since": since,
        "until": until,
        "committed_only": committed_only,
        "verbose": verbose,
    })()
    cmd_rlvr(args)
    out = capsys.readouterr()
    return out.out, out.err


class TestRLVRExportCLI:
    def test_emits_pure_jsonl(
        self, config: Config, vault: VaultManager, monkeypatch, capsys
    ):
        _seed_n_decisions(vault, 2)
        _index(config)

        stdout, _ = _run_export(
            vault=config.vault_root, monkeypatch=monkeypatch, capsys=capsys,
        )
        lines = [l for l in stdout.splitlines() if l]
        assert len(lines) == 2
        # Each line must parse cleanly — no headers, no prose interleaved.
        rows = [json.loads(line) for line in lines]
        assert all("decision_id" in r for r in rows)
        assert all("prediction" in r and "outcome" in r and "context" in r for r in rows)

    def test_project_filter_applies(
        self, config: Config, vault: VaultManager, monkeypatch, capsys
    ):
        _seed_n_decisions(vault, 2, project="alpha")
        _seed_n_decisions(vault, 1, project="beta")
        _index(config)

        stdout, _ = _run_export(
            vault=config.vault_root, project="alpha",
            monkeypatch=monkeypatch, capsys=capsys,
        )
        rows = [json.loads(l) for l in stdout.splitlines() if l]
        assert len(rows) == 2
        assert all(r["project"] == "alpha" for r in rows)

    def test_committed_only_filter(
        self, config: Config, vault: VaultManager, monkeypatch, capsys
    ):
        _seed_n_decisions(vault, 1, committed=True)
        _seed_n_decisions(vault, 2, committed=False)
        _index(config)

        stdout, _ = _run_export(
            vault=config.vault_root, committed_only=True,
            monkeypatch=monkeypatch, capsys=capsys,
        )
        rows = [json.loads(l) for l in stdout.splitlines() if l]
        assert len(rows) == 1
        assert rows[0]["outcome"]["committed"] is True

    def test_verbose_status_to_stderr(
        self, config: Config, vault: VaultManager, monkeypatch, capsys
    ):
        _seed_n_decisions(vault, 3)
        _index(config)

        stdout, stderr = _run_export(
            vault=config.vault_root, verbose=True,
            monkeypatch=monkeypatch, capsys=capsys,
        )
        # stdout still pure JSONL — verbose doesn't pollute the pipe.
        rows = [json.loads(l) for l in stdout.splitlines() if l]
        assert len(rows) == 3
        # stderr carries the summary line.
        assert "3 row(s)" in stderr

    def test_empty_vault_emits_nothing(
        self, config: Config, vault: VaultManager, monkeypatch, capsys
    ):
        _index(config)  # empty index
        stdout, _ = _run_export(
            vault=config.vault_root, monkeypatch=monkeypatch, capsys=capsys,
        )
        assert stdout == ""

    def test_missing_action_prints_help_and_exits_2(
        self, monkeypatch, capsys
    ):
        from personal_mem.surfaces.cli.rlvr import cmd_rlvr

        args = type("Args", (), {"rlvr_action": None})()
        with pytest.raises(SystemExit) as exc:
            cmd_rlvr(args)
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "rlvr" in captured.err

    def test_date_window_filter(
        self, config: Config, vault: VaultManager, monkeypatch, capsys
    ):
        _seed_n_decisions(vault, 4)  # dates 2026-05-10..2026-05-13
        _index(config)
        stdout, _ = _run_export(
            vault=config.vault_root, since="2026-05-11", until="2026-05-12",
            monkeypatch=monkeypatch, capsys=capsys,
        )
        rows = [json.loads(l) for l in stdout.splitlines() if l]
        assert len(rows) == 2


class TestDispatchTable:
    def test_rlvr_in_dispatch(self):
        # Pin the wiring: if someone removes the dispatch entry, the test
        # catches it before any user does.
        from personal_mem.surfaces.cli import _DISPATCH

        assert "rlvr" in _DISPATCH

    def test_subcommand_count_bumped(self):
        # 33 → 34 after this slice lands; if it slips, CLAUDE.md says 33
        # and tests still see 34 — a deliberate test to catch doc drift.
        from personal_mem.surfaces.cli import _DISPATCH

        assert len(_DISPATCH) == 34
