"""Tests for the per-vault rejudge queue primitive.

The queue is the spine of Phase-3 prediction-judge work — supersession
triggers and the cron ``pending_due`` sweep both feed it; ``mem judge
--drain`` consumes it. These tests pin the dedupe semantics, the atomic
drain, and the SQL-driven ``pending_due`` filter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager
from personal_mem.operations import rejudge_queue


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


# ---------------------------------------------------------------------------
# enqueue + peek
# ---------------------------------------------------------------------------


def test_enqueue_writes_one_item(cfg: Config) -> None:
    rejudge_queue.enqueue(
        cfg, decision_id="dec-aaa111", reason="manual rejudge", source="manual"
    )
    items = rejudge_queue.peek(cfg)
    assert len(items) == 1
    item = items[0]
    assert item["decision_id"] == "dec-aaa111"
    assert item["reason"] == "manual rejudge"
    assert item["source"] == "manual"
    assert item["enqueued_at"]


def test_enqueue_idempotent_on_decision_id(cfg: Config) -> None:
    """Second enqueue for the same decision_id is a no-op."""
    rejudge_queue.enqueue(
        cfg, decision_id="dec-aaa111", reason="superseded by dec-xyz", source="supersession"
    )
    rejudge_queue.enqueue(
        cfg, decision_id="dec-aaa111", reason="manual rejudge", source="manual"
    )
    items = rejudge_queue.peek(cfg)
    assert len(items) == 1
    # First-wins: original reason/source preserved.
    assert items[0]["reason"] == "superseded by dec-xyz"
    assert items[0]["source"] == "supersession"


def test_enqueue_distinct_ids_add_separate_items(cfg: Config) -> None:
    rejudge_queue.enqueue(
        cfg, decision_id="dec-aaa111", reason="r1", source="supersession"
    )
    rejudge_queue.enqueue(
        cfg, decision_id="dec-bbb222", reason="r2", source="supersession"
    )
    items = rejudge_queue.peek(cfg)
    assert len(items) == 2
    ids = {it["decision_id"] for it in items}
    assert ids == {"dec-aaa111", "dec-bbb222"}


# ---------------------------------------------------------------------------
# drain
# ---------------------------------------------------------------------------


def test_drain_all_returns_items_and_truncates(cfg: Config) -> None:
    rejudge_queue.enqueue(
        cfg, decision_id="dec-aaa111", reason="r1", source="supersession"
    )
    rejudge_queue.enqueue(
        cfg, decision_id="dec-bbb222", reason="r2", source="manual"
    )
    drained = rejudge_queue.drain_all(cfg)
    assert len(drained) == 2
    # File is left present but empty (truncate-to-zero, not delete).
    queue_path = Path(cfg.vault_root) / ".mem" / "rejudge_queue.jsonl"
    assert queue_path.exists()
    assert queue_path.read_text(encoding="utf-8") == ""
    # Second drain yields nothing.
    assert rejudge_queue.drain_all(cfg) == []


def test_remove_consumes_only_named_ids(cfg: Config) -> None:
    """`remove` drops the judged entries; unjudged ones survive intact."""
    rejudge_queue.enqueue(
        cfg, decision_id="dec-aaa111", reason="r1", source="supersession"
    )
    rejudge_queue.enqueue(
        cfg, decision_id="dec-bbb222", reason="r2", source="manual"
    )
    rejudge_queue.enqueue(
        cfg, decision_id="dec-ccc333", reason="r3", source="cron"
    )
    removed = rejudge_queue.remove(cfg, ["dec-aaa111", "dec-ccc333"])
    assert removed == 2
    survivors = rejudge_queue.peek(cfg)
    assert len(survivors) == 1
    # Survivor keeps its fields (field-preserving, unlike drain+re-enqueue).
    assert survivors[0]["decision_id"] == "dec-bbb222"
    assert survivors[0]["reason"] == "r2"
    assert survivors[0]["source"] == "manual"


def test_remove_unknown_or_empty_ids_is_noop(cfg: Config) -> None:
    rejudge_queue.enqueue(
        cfg, decision_id="dec-aaa111", reason="r", source="manual"
    )
    assert rejudge_queue.remove(cfg, []) == 0
    assert rejudge_queue.remove(cfg, ["dec-zzz999", ""]) == 0
    assert len(rejudge_queue.peek(cfg)) == 1


def test_peek_does_not_clear(cfg: Config) -> None:
    rejudge_queue.enqueue(
        cfg, decision_id="dec-aaa111", reason="r", source="manual"
    )
    rejudge_queue.peek(cfg)
    rejudge_queue.peek(cfg)
    # Still there after two peeks.
    assert len(rejudge_queue.peek(cfg)) == 1


# ---------------------------------------------------------------------------
# pending_due
# ---------------------------------------------------------------------------


def _make_decision(
    vault: VaultManager, *, title: str, project: str = "t",
    prediction_match: str | None = None, judged_at: str | None = None,
    predicted_outcome: str = "",
) -> str:
    """Create a decision note with prediction frontmatter, return its id."""
    sess_path = vault.create_note(
        NoteType.SESSION, "S", body="## Summary\n", project=project,
    )
    sess_id = vault.read_note(sess_path).id
    extra: dict = {
        "status": "accepted",
        "committed": True,
        "source_session": sess_id,
        "derived_from": [sess_id],
    }
    if predicted_outcome:
        extra["predicted_outcome"] = predicted_outcome
    if prediction_match:
        extra["prediction_match"] = prediction_match
    if judged_at:
        extra["judged_at"] = judged_at
    dec_path = vault.create_note(
        NoteType.DECISION, title,
        body="## Context\n\n## Decision\n",
        project=project,
        extra_frontmatter=extra,
        output_dir=sess_path.parent,
    )
    return vault.read_note(dec_path).id


def _index(cfg: Config) -> None:
    idx = Indexer(config=cfg)
    idx.rebuild(full=True)
    idx.close()


def test_pending_due_finds_stale_pending(cfg: Config, vault: VaultManager) -> None:
    """Decision with prediction_match=pending and stale judged_at surfaces."""
    stale_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    dec_id = _make_decision(
        vault, title="D_stale",
        prediction_match="pending", judged_at=stale_ts,
        predicted_outcome="will land",
    )
    _index(cfg)

    due = rejudge_queue.pending_due(cfg, age_days=1)
    assert dec_id in due


def test_pending_due_ignores_recent_pending(cfg: Config, vault: VaultManager) -> None:
    """Recently-judged pending verdicts stay below the cutoff."""
    fresh_ts = datetime.now(timezone.utc).isoformat()
    dec_id = _make_decision(
        vault, title="D_fresh",
        prediction_match="pending", judged_at=fresh_ts,
        predicted_outcome="will land",
    )
    _index(cfg)

    due = rejudge_queue.pending_due(cfg, age_days=1)
    assert dec_id not in due


def test_pending_due_ignores_non_pending_verdicts(
    cfg: Config, vault: VaultManager
) -> None:
    """A confirmed/contradicted/unevaluable verdict doesn't surface."""
    stale_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    confirmed_id = _make_decision(
        vault, title="D_confirmed",
        prediction_match="confirmed", judged_at=stale_ts,
        predicted_outcome="will land",
    )
    contradicted_id = _make_decision(
        vault, title="D_contradicted",
        prediction_match="contradicted", judged_at=stale_ts,
        predicted_outcome="will land",
    )
    _index(cfg)

    due = rejudge_queue.pending_due(cfg, age_days=1)
    assert confirmed_id not in due
    assert contradicted_id not in due
