"""Tests for the supersession → rejudge-queue trigger.

Three paths can write a decision with ``supersedes: [dec-X]``:

1. ``operations.extract.extract_session`` (wrap context)
2. ``operations.notes.create_note`` (headless ``mem_create``)
3. ``operations.notes.update_note`` (headless ``mem_update`` extending fm)

All three must enqueue the predecessor for re-judgment. The headless paths
deliberately skip the structural ``status: superseded`` flip — that's only
done in the wrap context where the new decision is being extracted from a
session. Headless writes only signal the verdict pipeline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager
from personal_mem.operations import notes as ops_notes
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
