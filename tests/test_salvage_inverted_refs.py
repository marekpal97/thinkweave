"""Regression tests for scripts/salvage_inverted_hub_refs.py.

Locks the swap rule for the inverted-ref salvage script (n-8645c889).
The script's logic is non-trivial and has several edge-case fallbacks
(same-day, target missing, target ambiguous, target already classified)
— each path needs to be exercised so the contract doesn't regress on a
future "small refactor".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from personal_mem.synthesis.concept_hub import LogEntry  # noqa: E402

from salvage_inverted_hub_refs import salvage_hub  # noqa: E402


def _e(date: str, flag: str = "new", ref: str = "", text: str = "x") -> LogEntry:
    return LogEntry(date=date, flag=flag, ref=ref, text=text, citation="n-test")


class TestSalvageHub:
    def test_simple_swap_extends(self):
        # Pre: X (Mar) extends Y (Apr) — direction inverted
        # Post: Y extends X
        x = _e("2026-03-01", flag="extends", ref="2026-04-22")
        y = _e("2026-04-22", flag="new")
        swapped, cleared, reasons = salvage_hub([x, y])
        assert swapped == 1
        assert cleared == 0
        assert x.flag == "new"
        assert x.ref == ""
        assert y.flag == "extends"
        assert y.ref == "2026-03-01"

    def test_simple_swap_contradicts(self):
        x = _e("2026-03-01", flag="contradicts", ref="2026-04-22")
        y = _e("2026-04-22", flag="new")
        swapped, _, _ = salvage_hub([x, y])
        assert swapped == 1
        assert y.flag == "contradicts"
        assert y.ref == "2026-03-01"

    def test_simple_swap_agrees(self):
        # agrees with explicit ref also gets swapped — the agreement
        # direction reverses too.
        x = _e("2026-03-01", flag="agrees", ref="2026-04-22")
        y = _e("2026-04-22", flag="new")
        swapped, _, _ = salvage_hub([x, y])
        assert swapped == 1
        assert y.flag == "agrees"
        assert y.ref == "2026-03-01"

    def test_same_day_ref_clears_to_new(self):
        x = _e("2026-03-01", flag="extends", ref="2026-03-01")
        swapped, cleared, reasons = salvage_hub([x])
        assert swapped == 0
        assert cleared == 1
        assert reasons == {"same_day": 1}
        assert x.flag == "new"
        assert x.ref == ""

    def test_target_missing_clears_to_new(self):
        # X claims to extend Y on a date no entry in this hub holds.
        x = _e("2026-03-01", flag="extends", ref="2026-04-22")
        swapped, cleared, reasons = salvage_hub([x])
        assert swapped == 0
        assert cleared == 1
        assert reasons == {"target_missing": 1}
        assert x.flag == "new"

    def test_target_ambiguous_clears_to_new(self):
        # Two entries share the ref date — script refuses to guess.
        x = _e("2026-03-01", flag="extends", ref="2026-04-22")
        y1 = _e("2026-04-22", flag="new", text="first")
        y2 = _e("2026-04-22", flag="new", text="second")
        swapped, cleared, reasons = salvage_hub([x, y1, y2])
        assert swapped == 0
        assert cleared == 1
        assert reasons == {"target_ambiguous": 1}
        assert x.flag == "new"
        # Both Y candidates are untouched.
        assert y1.flag == "new" and y1.ref == ""
        assert y2.flag == "new" and y2.ref == ""

    def test_target_already_classified_clears_only_x(self):
        # Y already has a real classification — swap would overwrite it.
        # X gets cleared, Y is preserved.
        x = _e("2026-03-01", flag="extends", ref="2026-04-22")
        y = _e("2026-04-22", flag="agrees", ref="2026-02-15")
        swapped, cleared, reasons = salvage_hub([x, y])
        assert swapped == 0
        assert cleared == 1
        assert reasons == {"target_already_classified": 1}
        assert x.flag == "new" and x.ref == ""
        # Y is preserved exactly.
        assert y.flag == "agrees"
        assert y.ref == "2026-02-15"

    def test_valid_backward_ref_left_alone(self):
        # X (Apr) extends Y (Mar) — already correct direction.
        x = _e("2026-04-22", flag="extends", ref="2026-03-01")
        y = _e("2026-03-01", flag="new")
        swapped, cleared, _ = salvage_hub([x, y])
        assert swapped == 0
        assert cleared == 0
        assert x.flag == "extends"
        assert x.ref == "2026-03-01"

    def test_no_ref_left_alone(self):
        # Plain "new" entries don't get touched.
        a = _e("2026-03-01", flag="new")
        b = _e("2026-04-22", flag="new")
        swapped, cleared, _ = salvage_hub([a, b])
        assert swapped == 0
        assert cleared == 0

    def test_mixed_outcomes_in_one_hub(self):
        # All four outcomes coexisting on one hub.
        ok = _e("2026-04-22", flag="extends", ref="2026-03-01", text="valid")
        ok_target = _e("2026-03-01", flag="new", text="ok-target")
        swap_x = _e("2026-02-01", flag="extends", ref="2026-05-10", text="swap-x")
        swap_y = _e("2026-05-10", flag="new", text="swap-y")
        same_day = _e("2026-06-01", flag="agrees", ref="2026-06-01", text="same-day")
        missing = _e("2026-07-01", flag="extends", ref="2026-12-31", text="missing")
        swapped, cleared, reasons = salvage_hub(
            [ok, ok_target, swap_x, swap_y, same_day, missing]
        )
        assert swapped == 1
        assert cleared == 2
        assert reasons == {"same_day": 1, "target_missing": 1}
        # Valid entry untouched.
        assert ok.flag == "extends" and ok.ref == "2026-03-01"
        # Swap completed.
        assert swap_x.flag == "new" and swap_x.ref == ""
        assert swap_y.flag == "extends" and swap_y.ref == "2026-02-01"
        # Same-day cleared.
        assert same_day.flag == "new" and same_day.ref == ""
        # Missing cleared.
        assert missing.flag == "new" and missing.ref == ""
