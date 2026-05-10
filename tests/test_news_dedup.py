"""Tests for the concept-bundle dedup helpers used by the news pipeline.

Covers:
  1. ``jaccard`` correctness on overlap / no-overlap / partial / empty.
  2. ``find_near_duplicate_pairs`` tier-based winner selection.
  3. ``find_near_duplicate_pairs`` tie-break by created_at on equal tier.
  4. Threshold gating — sub-threshold pairs aren't returned.
  5. Three-note batch with one near-duplicate pair.
  6. CLI mode: stdin JSON → stdout JSON pair list.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from personal_mem.operations.news_dedup import (
    find_near_duplicate_pairs,
    jaccard,
)


# ---------------------------------------------------------------------------
# jaccard
# ---------------------------------------------------------------------------


def test_jaccard_identical():
    assert jaccard(["a", "b", "c"], ["a", "b", "c"]) == 1.0


def test_jaccard_disjoint():
    assert jaccard(["a", "b"], ["c", "d"]) == 0.0


def test_jaccard_partial():
    # |A∩B|/|A∪B| = 2/4 = 0.5
    assert jaccard(["a", "b", "c"], ["a", "b", "d"]) == pytest.approx(0.5)


def test_jaccard_empty_pair_is_identity():
    # Empty == empty == 1.0 — defensible since both bundles are
    # equivalently meaningless and shouldn't be flagged as different.
    assert jaccard([], []) == 1.0


def test_jaccard_case_folds():
    assert jaccard(["Finance-Macro"], ["finance-macro"]) == 1.0


def test_jaccard_strips_whitespace_and_empty_tokens():
    assert jaccard(["  a  ", "", "b"], ["a", "b"]) == 1.0


# ---------------------------------------------------------------------------
# find_near_duplicate_pairs — basic
# ---------------------------------------------------------------------------


def _note(
    id: str, concepts: list[str], tier: int = 1, created_at: str = ""
) -> dict:
    return {
        "id": id,
        "concepts": concepts,
        "tier": tier,
        "created_at": created_at,
    }


def test_high_overlap_pair_returns_one():
    notes = [
        _note("src-a", ["x", "y", "z", "w"], tier=1, created_at="t0"),
        _note("src-b", ["x", "y", "z", "v"], tier=1, created_at="t1"),
    ]
    # A∩B={x,y,z}=3, A∪B=5, J=0.6 — sub-threshold at 0.8
    pairs = find_near_duplicate_pairs(notes, threshold=0.8)
    assert pairs == []


def test_full_overlap_pair_returns_one():
    notes = [
        _note("src-a", ["x", "y", "z"], tier=1, created_at="t0"),
        _note("src-b", ["x", "y", "z"], tier=2, created_at="t1"),
    ]
    pairs = find_near_duplicate_pairs(notes, threshold=0.8)
    assert len(pairs) == 1
    loser, winner, j = pairs[0]
    assert winner == "src-a"  # tier 1 wins over tier 2
    assert loser == "src-b"
    assert j == 1.0


def test_low_overlap_keeps_both():
    notes = [
        _note("src-a", ["x", "y"], tier=1, created_at="t0"),
        _note("src-b", ["w", "z"], tier=1, created_at="t0"),
    ]
    pairs = find_near_duplicate_pairs(notes, threshold=0.8)
    assert pairs == []


# ---------------------------------------------------------------------------
# tier and created_at semantics
# ---------------------------------------------------------------------------


def test_tier_1_wins_over_tier_2():
    notes = [
        _note("src-tier2", ["a", "b", "c"], tier=2, created_at="2026-05-09T10:00:00Z"),
        _note("src-tier1", ["a", "b", "c"], tier=1, created_at="2026-05-09T10:01:00Z"),
    ]
    pairs = find_near_duplicate_pairs(notes, threshold=0.8)
    assert len(pairs) == 1
    loser, winner, _ = pairs[0]
    assert winner == "src-tier1"
    assert loser == "src-tier2"


def test_tier_tie_earlier_created_at_wins():
    # Both tier 1, but src-early was created first → it keeps, src-late loses.
    notes = [
        _note("src-late", ["a", "b", "c"], tier=1, created_at="2026-05-09T10:01:00Z"),
        _note("src-early", ["a", "b", "c"], tier=1, created_at="2026-05-09T10:00:00Z"),
    ]
    pairs = find_near_duplicate_pairs(notes, threshold=0.8)
    assert len(pairs) == 1
    loser, winner, _ = pairs[0]
    assert winner == "src-early"
    assert loser == "src-late"


# ---------------------------------------------------------------------------
# multi-note batch
# ---------------------------------------------------------------------------


def test_three_notes_one_pair_one_independent():
    notes = [
        _note("src-a", ["x", "y", "z"], tier=1, created_at="t0"),
        _note("src-b", ["x", "y", "z"], tier=2, created_at="t1"),  # near-dupe of A
        _note("src-c", ["q", "r", "s"], tier=1, created_at="t2"),  # totally different
    ]
    pairs = find_near_duplicate_pairs(notes, threshold=0.8)
    assert len(pairs) == 1
    loser, winner, _ = pairs[0]
    assert winner == "src-a"
    assert loser == "src-b"


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


def test_cli_mode_reads_stdin_writes_pairs():
    notes = [
        _note("src-a", ["x", "y", "z"], tier=1, created_at="t0"),
        _note("src-b", ["x", "y", "z"], tier=2, created_at="t1"),
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "personal_mem.operations.news_dedup", "--threshold", "0.8"],
        input=json.dumps(notes),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]["winner"] == "src-a"
    assert out[0]["loser"] == "src-b"
    assert out[0]["jaccard"] == 1.0
