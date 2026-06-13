"""Tests for operations/seam_link_queue.py — the fold→seam-link handoff."""

from __future__ import annotations

from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.operations import seam_link_queue as slq


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(vault_root=tmp_path / "vault")


class TestEnqueue:
    def test_enqueue_and_peek(self, config):
        slq.enqueue(
            config,
            hub_kind="concept",
            hub_id="derivatives",
            folded_from="derivative",
            fold_dates=["2026-05-05"],
            reason="concept_merged",
        )
        items = slq.peek(config)
        assert len(items) == 1
        it = items[0]
        assert it["hub_kind"] == "concept"
        assert it["hub_id"] == "derivatives"
        assert it["folded_from"] == "derivative"
        assert it["fold_dates"] == ["2026-05-05"]
        assert it["enqueued_at"]

    def test_reenqueue_unions_dates(self, config):
        slq.enqueue(
            config, hub_kind="concept", hub_id="x",
            fold_dates=["2026-05-05"],
        )
        slq.enqueue(
            config, hub_kind="concept", hub_id="x",
            fold_dates=["2026-05-09", "2026-05-05"],
        )
        items = slq.peek(config)
        assert len(items) == 1
        assert items[0]["fold_dates"] == ["2026-05-05", "2026-05-09"]

    def test_same_id_different_kind_coexist(self, config):
        slq.enqueue(config, hub_kind="concept", hub_id="x", fold_dates=["2026-01-01"])
        slq.enqueue(config, hub_kind="theme", hub_id="x", fold_dates=["2026-01-02"])
        assert len(slq.peek(config)) == 2

    def test_empty_hub_id_noop(self, config):
        slq.enqueue(config, hub_kind="concept", hub_id="", fold_dates=["2026-01-01"])
        assert slq.peek(config) == []


class TestDrainDequeue:
    def test_drain_cap_fifo(self, config):
        for i in range(3):
            slq.enqueue(
                config, hub_kind="concept", hub_id=f"c{i}",
                fold_dates=[f"2026-01-0{i + 1}"],
            )
        taken = slq.drain(config, cap=2)
        assert [t["hub_id"] for t in taken] == ["c0", "c1"]
        assert [t["hub_id"] for t in slq.peek(config)] == ["c2"]

    def test_drain_all(self, config):
        slq.enqueue(config, hub_kind="theme", hub_id="thm-a", fold_dates=["2026-01-01"])
        assert len(slq.drain(config)) == 1
        assert slq.peek(config) == []

    def test_dequeue_specific(self, config):
        slq.enqueue(config, hub_kind="concept", hub_id="a", fold_dates=["2026-01-01"])
        slq.enqueue(config, hub_kind="concept", hub_id="b", fold_dates=["2026-01-02"])
        assert slq.dequeue(config, hub_kind="concept", hub_id="a") is True
        assert slq.dequeue(config, hub_kind="concept", hub_id="a") is False
        assert [t["hub_id"] for t in slq.peek(config)] == ["b"]


class TestRobustness:
    def test_corrupt_lines_skipped(self, config):
        path = config.vault_root / ".weave" / "seam_link_queue.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('not json\n{"hub_kind": "theme", "hub_id": "thm-x"}\n')
        items = slq.peek(config)
        assert len(items) == 1 and items[0]["hub_id"] == "thm-x"
