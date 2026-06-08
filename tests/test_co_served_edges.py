"""Tests for the ``co_served`` edge projection in the indexer.

Co-served pairs are aggregated from ``context_served`` at full-rebuild
time: every pair of notes served in the same session contributes +1 to
the pair's count; pairs reaching ``threshold`` (default 2) materialise
as edges with ``edge_type='co_served'``, ``weight=co_served_count``, and
``metadata={"co_served_count": N}``. Behavioural overlay; doesn't
disturb the existing concept/wikilink edge set.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
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


@pytest.fixture
def indexer(config: Config):
    idx = Indexer(config=config)
    yield idx
    idx.close()


def _seed_session(vault: VaultManager, project: str, returned_ids: list[str]) -> str:
    sess_path = vault.create_note(
        NoteType.SESSION,
        "S",
        body="## Summary\nseed\n",
        project=project,
        extra_frontmatter={"processed": True},
    )
    ts = datetime.now(timezone.utc).isoformat()
    (sess_path.parent / "retrieval_log.jsonl").write_text(
        json.dumps({
            "ts": ts,
            "type": "retrieval",
            "tool": "mem_search",
            "returned_ids": returned_ids,
        }) + "\n",
        encoding="utf-8",
    )
    return vault.read_note(sess_path).id


def _co_served_rows(idx: Indexer) -> list[dict]:
    rows = idx.db.execute(
        "SELECT source, target, weight, metadata FROM edges "
        "WHERE edge_type = 'co_served' "
        "ORDER BY source, target"
    ).fetchall()
    return [dict(r) for r in rows]


class TestCoServedProjection:
    def test_pair_below_threshold_is_dropped(
        self, vault: VaultManager, indexer: Indexer
    ):
        a = vault.read_note(vault.create_note(NoteType.NOTE, "A", project="t")).id
        b = vault.read_note(vault.create_note(NoteType.NOTE, "B", project="t")).id
        # Only one session, so co_served_count = 1 — below threshold 2.
        _seed_session(vault, "t", [a, b])
        indexer.rebuild(full=True)
        assert _co_served_rows(indexer) == []

    def test_pair_meeting_threshold_materialises(
        self, vault: VaultManager, indexer: Indexer
    ):
        a = vault.read_note(vault.create_note(NoteType.NOTE, "A", project="t")).id
        b = vault.read_note(vault.create_note(NoteType.NOTE, "B", project="t")).id
        _seed_session(vault, "t", [a, b])
        _seed_session(vault, "t", [a, b])
        indexer.rebuild(full=True)
        rows = _co_served_rows(indexer)
        assert len(rows) == 1
        edge = rows[0]
        assert {edge["source"], edge["target"]} == {a, b}
        assert edge["weight"] == 2.0
        meta = json.loads(edge["metadata"])
        assert meta["co_served_count"] == 2

    def test_weight_tracks_count(
        self, vault: VaultManager, indexer: Indexer
    ):
        a = vault.read_note(vault.create_note(NoteType.NOTE, "A", project="t")).id
        b = vault.read_note(vault.create_note(NoteType.NOTE, "B", project="t")).id
        for _ in range(5):
            _seed_session(vault, "t", [a, b])
        indexer.rebuild(full=True)
        rows = _co_served_rows(indexer)
        assert rows[0]["weight"] == 5.0
        assert json.loads(rows[0]["metadata"])["co_served_count"] == 5

    def test_idempotent_across_rebuilds(
        self, vault: VaultManager, indexer: Indexer
    ):
        a = vault.read_note(vault.create_note(NoteType.NOTE, "A", project="t")).id
        b = vault.read_note(vault.create_note(NoteType.NOTE, "B", project="t")).id
        _seed_session(vault, "t", [a, b])
        _seed_session(vault, "t", [a, b])
        indexer.rebuild(full=True)
        first = _co_served_rows(indexer)
        indexer.rebuild(full=True)
        second = _co_served_rows(indexer)
        assert first == second

    def test_independent_pairs_per_session(
        self, vault: VaultManager, indexer: Indexer
    ):
        a = vault.read_note(vault.create_note(NoteType.NOTE, "A", project="t")).id
        b = vault.read_note(vault.create_note(NoteType.NOTE, "B", project="t")).id
        c = vault.read_note(vault.create_note(NoteType.NOTE, "C", project="t")).id
        # Two sessions where A+B+C all appear → C(3,2) = 3 pairs each at count 2.
        _seed_session(vault, "t", [a, b, c])
        _seed_session(vault, "t", [a, b, c])
        indexer.rebuild(full=True)
        rows = _co_served_rows(indexer)
        assert len(rows) == 3
        for r in rows:
            assert r["weight"] == 2.0

    def test_self_pairs_excluded(
        self, vault: VaultManager, indexer: Indexer
    ):
        a = vault.read_note(vault.create_note(NoteType.NOTE, "A", project="t")).id
        # Session serves the same note twice — should not produce a self-edge.
        _seed_session(vault, "t", [a, a])
        _seed_session(vault, "t", [a, a])
        indexer.rebuild(full=True)
        rows = _co_served_rows(indexer)
        # No (a, a) pair, no other pairs — empty.
        assert rows == []

    def test_does_not_disturb_other_edge_types(
        self, vault: VaultManager, indexer: Indexer
    ):
        # Concept edges should still exist regardless of co_served projection.
        vault.create_note(
            NoteType.NOTE, "A", project="t",
            extra_frontmatter={"concepts": ["shared-x", "shared-y"]},
        )
        vault.create_note(
            NoteType.NOTE, "B", project="t",
            extra_frontmatter={"concepts": ["shared-x", "shared-y"]},
        )
        indexer.rebuild(full=True)
        # A↔B concept edges (relates_to) should exist; no co_served (no sessions).
        non_co_rows = indexer.db.execute(
            "SELECT edge_type FROM edges WHERE edge_type != 'co_served'"
        ).fetchall()
        assert len(non_co_rows) > 0
        assert _co_served_rows(indexer) == []


class TestEdgeTypesPassthrough:
    """Verify mem_graph honors edge_types filter for co_served."""

    def test_get_related_excludes_co_served_when_not_requested(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        from personal_mem.retrieval.search import Search

        a = vault.read_note(vault.create_note(NoteType.NOTE, "A", project="t")).id
        b = vault.read_note(vault.create_note(NoteType.NOTE, "B", project="t")).id
        _seed_session(vault, "t", [a, b])
        _seed_session(vault, "t", [a, b])
        indexer.rebuild(full=True)

        s = Search(config=config)
        try:
            # Walk from A asking only for relates_to edges — B should not show
            # up via co_served.
            only_relates = s.get_related(a, depth=1, edge_types=["relates_to"])
            assert all(n.id != b for n in only_relates)

            # Walk again including co_served — B is now reachable.
            with_co = s.get_related(a, depth=1, edge_types=["relates_to", "co_served"])
            assert any(n.id == b for n in with_co)
        finally:
            s.close()
