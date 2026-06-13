"""Tests for the supersession → rejudge-queue trigger and evidence-gated flip.

Three paths can write a decision with ``supersedes: [dec-X]``:

1. ``operations.extract.extract_session`` (wrap context)
2. ``operations.notes.create_note`` (headless ``mem_create``)
3. ``operations.notes.update_note`` (headless ``mem_update`` extending fm)

All three only **enqueue** the predecessor for re-judgment — *none* flip its
``status``. A ``supersedes:`` declaration is a re-judge trigger, not proof
(evidence-gated 2026-06-13; the old eager flip in the wrap path is gone). The
structural ``status: superseded`` flip is owned by
``decisions.rejudge_supersession_predecessors``, run in ``wrap-finalize`` and
``dream apply``, where git-blame survival decides ``superseded`` (lines
replaced) vs ``kept`` (still co-contributing).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager
from personal_mem.operations import notes as ops_notes
from personal_mem.operations import rejudge_queue
from personal_mem.operations.decisions import rejudge_supersession_predecessors


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


def _seed_predecessor(vault: VaultManager) -> str:
    sess_path = vault.create_note(
        NoteType.SESSION, "S0", body="## Summary\n", project="t",
    )
    sess_id = vault.read_note(sess_path).id
    pred_path = vault.create_note(
        NoteType.DECISION, "Original D",
        body="## Context\n\n## Decision\n",
        project="t",
        extra_frontmatter={
            "status": "accepted",
            "committed": True,
            "source_session": sess_id,
            "derived_from": [sess_id],
        },
        output_dir=sess_path.parent,
    )
    return vault.read_note(pred_path).id


def _index(cfg: Config) -> None:
    idx = Indexer(config=cfg)
    idx.rebuild(full=True)
    idx.close()


def test_extract_session_enqueues_supersedes(
    cfg: Config, vault: VaultManager
) -> None:
    """`mem_extract` writing a decision with supersedes:[X] enqueues X."""
    pred_id = _seed_predecessor(vault)
    _index(cfg)

    # Fresh session for the new decision.
    sess_path = vault.create_note(
        NoteType.SESSION, "S1", body="## Summary\n",
        project="t", extra_frontmatter={"processed": False},
    )
    sess_id = vault.read_note(sess_path).id
    _index(cfg)

    from personal_mem.operations.extract import extract_session
    out = extract_session(
        cfg,
        session_id=sess_id,
        project="t",
        summary="ok",
        insights=[],
        decisions=[{
            "title": "Replacement decision",
            "rationale": "doing it differently",
            "outcome": "committed",
            "concepts": ["sqlite", "memory-system"],
            "supersedes": [pred_id],
        }],
    )
    assert out.error == ""
    assert len(out.created_decisions) == 1

    items = rejudge_queue.peek(cfg)
    assert len(items) == 1
    assert items[0]["decision_id"] == pred_id
    assert items[0]["source"] == "supersession"
    # New decision's id is encoded in the reason for successor lookup later.
    assert out.created_decisions[0].id in items[0]["reason"]


def test_create_note_enqueues_supersedes(cfg: Config, vault: VaultManager) -> None:
    """`mem_create` (operations.notes.create_note) with supersedes enqueues."""
    pred_id = _seed_predecessor(vault)
    _index(cfg)

    result = ops_notes.create_note(
        cfg,
        note_type=NoteType.DECISION,
        title="Headless replacement",
        body="## Context\n\n## Decision\n",
        project="t",
        extra_frontmatter={
            "status": "accepted",
            "committed": True,
            "supersedes": [pred_id],
        },
    )
    assert result.existed is False

    items = rejudge_queue.peek(cfg)
    assert len(items) == 1
    assert items[0]["decision_id"] == pred_id
    assert items[0]["source"] == "supersession"
    assert result.note.id in items[0]["reason"]


def test_update_note_adding_supersedes_enqueues(
    cfg: Config, vault: VaultManager
) -> None:
    """`mem_update` extending supersedes:[] enqueues the newly-added entry."""
    pred_id = _seed_predecessor(vault)

    # Create a decision *without* supersedes, then add it via update.
    result = ops_notes.create_note(
        cfg,
        note_type=NoteType.DECISION,
        title="Later decision",
        body="## Context\n\n## Decision\n",
        project="t",
        extra_frontmatter={"status": "accepted", "committed": True},
    )
    # Pre-state: queue should be empty (no supersedes at create time).
    assert rejudge_queue.peek(cfg) == []

    ops_notes.update_note(
        cfg, result.note.id,
        frontmatter_updates={"supersedes": [pred_id]},
    )

    items = rejudge_queue.peek(cfg)
    assert len(items) == 1
    assert items[0]["decision_id"] == pred_id
    assert items[0]["source"] == "supersession"


def test_supersession_idempotent_across_writes(
    cfg: Config, vault: VaultManager
) -> None:
    """Two create+update calls naming the same predecessor → one queue entry."""
    pred_id = _seed_predecessor(vault)
    _index(cfg)

    ops_notes.create_note(
        cfg,
        note_type=NoteType.DECISION,
        title="First successor",
        body="## Context\n\n## Decision\n",
        project="t",
        extra_frontmatter={"status": "accepted", "supersedes": [pred_id]},
    )
    # Second successor naming the same predecessor — queue dedup wins.
    ops_notes.create_note(
        cfg,
        note_type=NoteType.DECISION,
        title="Second successor",
        body="## Context\n\n## Decision\n",
        project="t",
        extra_frontmatter={"status": "accepted", "supersedes": [pred_id]},
    )
    items = rejudge_queue.peek(cfg)
    assert len(items) == 1
    assert items[0]["decision_id"] == pred_id


# --- evidence-gated flip ------------------------------------------------


def test_extract_session_does_not_flip_predecessor(
    cfg: Config, vault: VaultManager
) -> None:
    """`mem_extract` is passive: it enqueues but never flips the predecessor.

    The old eager flip set ``status: superseded`` on the bare declaration;
    post evidence-gating, extract leaves the predecessor untouched (the flip
    now belongs to the structural judge in wrap-finalize / dream apply).
    """
    pred_id = _seed_predecessor(vault)  # seeded as status: accepted
    _index(cfg)

    sess_path = vault.create_note(
        NoteType.SESSION, "S1", body="## Summary\n",
        project="t", extra_frontmatter={"processed": False},
    )
    sess_id = vault.read_note(sess_path).id
    _index(cfg)

    from personal_mem.operations.extract import extract_session
    out = extract_session(
        cfg, session_id=sess_id, project="t", summary="ok", insights=[],
        decisions=[{
            "title": "Replacement decision",
            "rationale": "doing it differently",
            "outcome": "committed",
            "concepts": ["sqlite", "memory-system"],
            "supersedes": [pred_id],
        }],
    )
    assert out.error == ""

    # Predecessor status is unchanged — extract did not flip it.
    pred_row = ops_notes.read_note(cfg, pred_id)[0]
    assert pred_row is not None
    assert pred_row.frontmatter.get("status") == "accepted"


def _seed_pred_and_successor(vault: VaultManager, cfg: Config) -> tuple[str, str]:
    """Predecessor + a later, different-session successor sharing one file.

    Both committed, so the blame-survival check (mocked in the tests) is the
    only thing deciding kept-vs-superseded. Dates are set explicitly so the
    successor is unambiguously *later* (``_check_re_edited`` requires it).
    """
    fp = vault.root / "mod.py"
    fp.write_text("x = 1\n", encoding="utf-8")

    sess_a = vault.create_note(NoteType.SESSION, "SA", body="## Summary\n", project="t")
    sess_a_id = vault.read_note(sess_a).id
    pred = vault.create_note(
        NoteType.DECISION, "Original D", body="## Context\n\n## Decision\n",
        project="t",
        extra_frontmatter={
            "status": "accepted", "committed": True, "date": "2026-04-01",
            "source_session": sess_a_id, "derived_from": [sess_a_id],
            "file_paths": [str(fp)], "commit_refs": ["aaa1111"],
        },
        output_dir=sess_a.parent,
    )
    pred_id = vault.read_note(pred).id

    sess_b = vault.create_note(NoteType.SESSION, "SB", body="## Summary\n", project="t")
    sess_b_id = vault.read_note(sess_b).id
    succ = vault.create_note(
        NoteType.DECISION, "Replacement D", body="## Context\n\n## Decision\n",
        project="t",
        extra_frontmatter={
            "status": "accepted", "committed": True, "date": "2026-04-09",
            "source_session": sess_b_id, "derived_from": [sess_b_id],
            "file_paths": [str(fp)], "supersedes": [pred_id],
            "commit_refs": ["bbb2222"],
        },
        output_dir=sess_b.parent,
    )
    succ_id = vault.read_note(succ).id
    _index(cfg)
    return pred_id, succ_id


@patch("personal_mem.synthesis.judge._check_committed_via_git", return_value={})
@patch("personal_mem.synthesis.judge._check_blame_survival", return_value=0)
def test_predecessor_flips_when_lines_replaced(
    _mock_blame, _mock_git, cfg: Config, vault: VaultManager
) -> None:
    """Blame survival == 0 (lines replaced) → predecessor flips to superseded."""
    pred_id, _succ_id = _seed_pred_and_successor(vault, cfg)

    results = rejudge_supersession_predecessors(cfg, [pred_id])
    assert len(results) == 1
    assert results[0][1]["verdict"] == "superseded"

    pred_row = ops_notes.read_note(cfg, pred_id)[0]
    assert pred_row.frontmatter.get("status") == "superseded"


@patch("personal_mem.synthesis.judge._check_committed_via_git", return_value={})
@patch("personal_mem.synthesis.judge._check_blame_survival", return_value=12)
def test_predecessor_stays_when_lines_survive(
    _mock_blame, _mock_git, cfg: Config, vault: VaultManager
) -> None:
    """Surviving blame lines → co-contributor → verdict kept, NOT superseded."""
    pred_id, _succ_id = _seed_pred_and_successor(vault, cfg)

    results = rejudge_supersession_predecessors(cfg, [pred_id])
    assert len(results) == 1
    assert results[0][1]["verdict"] == "kept"

    pred_row = ops_notes.read_note(cfg, pred_id)[0]
    # Stays accepted (was accepted) — never spuriously superseded.
    assert pred_row.frontmatter.get("status") != "superseded"


def test_rejudge_predecessors_empty_is_noop(cfg: Config, vault: VaultManager) -> None:
    """No ids → empty result, no crash."""
    assert rejudge_supersession_predecessors(cfg, []) == []
    assert rejudge_supersession_predecessors(cfg, ["", None]) == []  # type: ignore[list-item]
