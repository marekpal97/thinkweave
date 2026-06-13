"""Tests for ``weave judge`` — Phase-3 prediction-judge CLI surface.

Exercises the three flags (``--drain``, ``--rejudge``, ``--list-pending``)
and the worklist JSON shape that ``/judge-prediction`` consumes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager
from thinkweave.operations import rejudge_queue


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def cfg(vault_dir: Path) -> Config:
    return Config(vault_root=vault_dir)


@pytest.fixture
def vault(cfg: Config) -> VaultManager:
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    return vm


def _seed_decision(
    vault: VaultManager, *, title: str,
    prediction_match: str | None = None,
    judged_at: str | None = None,
    predicted_outcome: str = "",
) -> str:
    sess_path = vault.create_note(
        NoteType.SESSION, "S", body="## Summary\n", project="t",
    )
    sess_id = vault.read_note(sess_path).id
    fm: dict = {
        "status": "accepted",
        "committed": True,
        "source_session": sess_id,
        "derived_from": [sess_id],
    }
    if predicted_outcome:
        fm["predicted_outcome"] = predicted_outcome
    if prediction_match:
        fm["prediction_match"] = prediction_match
    if judged_at:
        fm["judged_at"] = judged_at
    dec_path = vault.create_note(
        NoteType.DECISION, title,
        body="## Context\n\n## Decision\n",
        project="t",
        extra_frontmatter=fm,
        output_dir=sess_path.parent,
    )
    return vault.read_note(dec_path).id


def _index(cfg: Config) -> None:
    idx = Indexer(config=cfg)
    idx.rebuild(full=True)
    idx.close()


def _args(**kwargs):
    """Tiny argparse.Namespace stand-in."""
    base = {
        "drain": False,
        "rejudge": "",
        "list_pending": False,
        "max": 20,
        "json": False,
    }
    base.update(kwargs)
    return type("Args", (), base)()


def _run_judge(cfg: Config, monkeypatch, capsys, **kwargs):
    monkeypatch.setenv("THINKWEAVE_VAULT", str(cfg.vault_root))
    from thinkweave.surfaces.cli.judge import cmd_judge
    cmd_judge(_args(**kwargs))
    return capsys.readouterr()


# ---------------------------------------------------------------------------
# --drain
# ---------------------------------------------------------------------------


def test_drain_empty_queue_emits_empty_array(
    cfg: Config, vault: VaultManager, monkeypatch, capsys
) -> None:
    """Empty queue + no pending decisions → ``[]`` on stdout."""
    _index(cfg)
    out = _run_judge(cfg, monkeypatch, capsys, drain=True, json=True)
    rows = json.loads(out.out)
    assert rows == []


def test_drain_includes_pending_due_even_with_empty_queue(
    cfg: Config, vault: VaultManager, monkeypatch, capsys
) -> None:
    """Stale pending decisions merge in even when supersession queue is empty."""
    stale = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    dec_id = _seed_decision(
        vault, title="D_stale",
        prediction_match="pending", judged_at=stale,
        predicted_outcome="will land",
    )
    _index(cfg)

    out = _run_judge(cfg, monkeypatch, capsys, drain=True, json=True)
    rows = json.loads(out.out)
    assert len(rows) == 1
    assert rows[0]["decision_id"] == dec_id
    assert rows[0]["trigger"] == "cron"
    assert rows[0]["predicted_outcome"] == "will land"


def test_drain_caps_worklist_at_max(
    cfg: Config, vault: VaultManager, monkeypatch, capsys
) -> None:
    """``--max 2`` caps the worklist to two items."""
    stale = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    for i in range(5):
        _seed_decision(
            vault, title=f"D{i}",
            prediction_match="pending", judged_at=stale,
            predicted_outcome=f"will land {i}",
        )
    _index(cfg)

    out = _run_judge(cfg, monkeypatch, capsys, drain=True, json=True, max=2)
    rows = json.loads(out.out)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# --rejudge
# ---------------------------------------------------------------------------


def test_rejudge_enqueues_and_invokes_subprocess(
    cfg: Config, vault: VaultManager, monkeypatch
) -> None:
    """``--rejudge <id>`` enqueues + shells to ``/judge-prediction``."""
    dec_id = _seed_decision(
        vault, title="D",
        predicted_outcome="will land",
    )
    _index(cfg)
    monkeypatch.setenv("THINKWEAVE_VAULT", str(cfg.vault_root))

    fake_result = type("R", (), {"returncode": 0})()
    with patch("subprocess.run", return_value=fake_result) as mock_run:
        with pytest.raises(SystemExit) as exc:
            from thinkweave.surfaces.cli.judge import cmd_judge
            cmd_judge(_args(rejudge=dec_id))
        assert exc.value.code == 0

    # Subprocess invoked exactly once with the expected /judge-prediction call.
    mock_run.assert_called_once()
    call_args = mock_run.call_args[0][0]
    assert call_args[0] == "claude"
    assert call_args[1] == "-p"
    assert dec_id in call_args[2]
    assert "/judge-prediction" in call_args[2]

    # Queue carries the manual-rejudge entry.
    items = rejudge_queue.peek(cfg)
    assert len(items) == 1
    assert items[0]["decision_id"] == dec_id
    assert items[0]["source"] == "manual"


# ---------------------------------------------------------------------------
# --list-pending
# ---------------------------------------------------------------------------


def test_list_pending_prints_one_id_per_line(
    cfg: Config, vault: VaultManager, monkeypatch, capsys
) -> None:
    """``--list-pending`` enumerates decisions with prediction_match=pending."""
    stale = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    id_a = _seed_decision(
        vault, title="A", prediction_match="pending", judged_at=stale,
        predicted_outcome="x",
    )
    id_b = _seed_decision(
        vault, title="B", prediction_match="pending", judged_at=stale,
        predicted_outcome="y",
    )
    _seed_decision(
        vault, title="C", prediction_match="confirmed", judged_at=stale,
        predicted_outcome="z",
    )
    _index(cfg)

    out = _run_judge(cfg, monkeypatch, capsys, list_pending=True)
    lines = [l for l in out.out.splitlines() if l.strip()]
    assert sorted(lines) == sorted([id_a, id_b])
