"""Tests for ``mem rlvr export`` — slice 6 of the RLVR substrate.

The CLI layer is thin (one ``json.dumps`` per row + stdout) — these tests
exercise: schema parses, filters apply, stdout is pure JSONL (no decoration),
status line goes to stderr when ``--verbose``, missing action prints help.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager


def _patch_fm(decision_id: str, fm_updates: dict):
    """Patch ``VaultManager.read_note`` to inject fm fields for a decision.

    Needed for tests that exercise the list-of-dict ``prediction_history``
    shape — the homegrown YAML renderer/parser doesn't round-trip nested
    structures, so we inject the structured payload directly.
    """
    original = VaultManager.read_note

    def patched(self, path):
        note = original(self, path)
        if note.id == decision_id:
            note.frontmatter.update(fm_updates)
        return note

    return patch.object(VaultManager, "read_note", patched)


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
                explode_history: bool = False,
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
        "explode_history": explode_history,
    })()
    cmd_rlvr(args)
    out = capsys.readouterr()
    return out.out, out.err


def _seed_decision_with_history(
    vault: VaultManager, *, history: list[dict] | None,
    predicted_outcome: str = "it will land", project: str = "t",
) -> str:
    """Seed a single session + decision; attach the denormalized prediction
    fields to frontmatter.

    NOTE: ``history`` itself is NOT written to disk here — the homegrown YAML
    renderer doesn't round-trip list-of-dicts. The caller is expected to wrap
    the test body in :func:`_patch_fm` to inject the structured list at read
    time. Pass ``history=None`` for a decision lacking ``predicted_outcome``
    entirely.
    """
    sess_path = vault.create_note(
        NoteType.SESSION, "S0", body="## Summary\n", project=project,
    )
    sess_id = vault.read_note(sess_path).id
    fm = {
        "status": "accepted",
        "committed": True,
        "source_session": sess_id,
        "derived_from": [sess_id],
        "concepts": ["a", "b"],
        "date": "2026-05-10",
    }
    if history is not None:
        fm["predicted_outcome"] = predicted_outcome
        if history:
            fm["prediction_match"] = history[-1]["match"]
            fm["judged_at"] = history[-1]["judged_at"]
    dec_path = vault.create_note(
        NoteType.DECISION, "D0",
        body="## Context\n\n## Decision\n",
        project=project,
        extra_frontmatter=fm,
        output_dir=sess_path.parent,
    )
    return vault.read_note(dec_path).id


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

    def test_default_mode_unchanged(
        self, config: Config, vault: VaultManager, monkeypatch, capsys
    ):
        # Default mode (no --explode-history flag): one row per decision,
        # and prediction.history is present on each row.
        history = [
            {"match": "pending", "judged_at": "2026-05-10T00:00:00Z",
             "reason": "r1"},
            {"match": "confirmed", "judged_at": "2026-05-12T00:00:00Z",
             "reason": "r2"},
        ]
        dec_id = _seed_decision_with_history(vault, history=history)
        _index(config)
        with _patch_fm(dec_id, {"prediction_history": history}):
            stdout, _ = _run_export(
                vault=config.vault_root,
                monkeypatch=monkeypatch, capsys=capsys,
            )
        rows = [json.loads(l) for l in stdout.splitlines() if l]
        assert len(rows) == 1
        assert rows[0]["prediction"]["history"] == history
        assert rows[0]["prediction"]["match"] == "confirmed"

    def test_explode_history_flag(
        self, config: Config, vault: VaultManager, monkeypatch, capsys
    ):
        # 3 history entries → 3 lines, each carrying per-entry fields and
        # a running entry_index.
        history = [
            {"match": "pending", "judged_at": "2026-05-10T00:00:00Z",
             "reason": "first judge"},
            {"match": "unevaluable", "judged_at": "2026-05-12T00:00:00Z",
             "reason": "still unclear"},
            {"match": "confirmed", "judged_at": "2026-05-14T00:00:00Z",
             "reason": "tests passed"},
        ]
        dec_id = _seed_decision_with_history(vault, history=history)
        _index(config)

        with _patch_fm(dec_id, {"prediction_history": history}):
            stdout, _ = _run_export(
                vault=config.vault_root, explode_history=True,
                monkeypatch=monkeypatch, capsys=capsys,
            )
        rows = [json.loads(l) for l in stdout.splitlines() if l]
        assert len(rows) == 3
        # All rows share the same decision_id (denormalized).
        assert all(r["decision_id"] == dec_id for r in rows)
        # Per-entry match / judged_at / reason / entry_index.
        for i, (row, entry) in enumerate(zip(rows, history)):
            assert row["prediction"]["match"] == entry["match"]
            assert row["prediction"]["judged_at"] == entry["judged_at"]
            assert row["prediction"]["reason"] == entry["reason"]
            assert row["prediction"]["entry_index"] == i
            # history is OMITTED in exploded mode (redundant).
            assert "history" not in row["prediction"]

    def test_explode_history_no_prediction(
        self, config: Config, vault: VaultManager, monkeypatch, capsys
    ):
        # Decision lacking predicted_outcome entirely. Invariant: exactly one
        # row emitted (one decision = at least one row), with empty fields.
        _seed_decision_with_history(vault, history=None)
        _index(config)

        stdout, _ = _run_export(
            vault=config.vault_root, explode_history=True,
            monkeypatch=monkeypatch, capsys=capsys,
        )
        rows = [json.loads(l) for l in stdout.splitlines() if l]
        assert len(rows) == 1
        pred = rows[0]["prediction"]
        assert pred["text"] == ""
        assert pred["match"] == ""
        assert pred["judged_at"] == ""
        assert pred["reason"] == ""
        assert pred["entry_index"] == 0


class TestDispatchTable:
    def test_rlvr_in_dispatch(self):
        # Pin the wiring: if someone removes the dispatch entry, the test
        # catches it before any user does.
        from personal_mem.surfaces.cli import _DISPATCH

        assert "rlvr" in _DISPATCH

    def test_subcommand_count_bumped(self):
        # P1-4 dropped ``mem connect`` (deprecation alias): 34 → 33.
        # `mem dream` (vault-hygiene cycle) and `mem news-stats`
        # (per-outlet drain stats) added later: 33 → 35.
        # Phase-3 prediction-judge rework adds `mem judge`: 35 → 36.
        # C24 CLI parity (Slice 4) adds unlink, timeline,
        # project-snapshot, prompts: 36 → 40.
        # `mem pause` / `mem resume` (hook pause toggle) + `mem themes`
        # (themes registry rebuild) added later: 40 → 43.
        # `mem schedule` (cross-platform scheduler — crontab / Task
        # Scheduler) added: 43 → 44.
        # Cost-tracking (`mem spend`) shipped 2026-06-01 and was removed
        # 2026-06-10 — net zero on the count.
        # `mem news-stats` removed in the 2026-06-13 pre-ship dead-code
        # sweep (zero callers in skills/docs/crontab): 44 → 43.
        # CLAUDE.md §7 reflects the same count (see CLAUDE.md §7); if
        # either slips, the other catches doc drift.
        from personal_mem.surfaces.cli import _DISPATCH

        assert len(_DISPATCH) == 43
