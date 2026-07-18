"""Tests for ``operations/trajectory_outcome.py`` — the deterministic outcome
judge for issue-loop trajectory notes (issue #60).

Coverage layers, mirroring the dream-judge-worker split (pure classification +
idempotent append + phase-window arithmetic in Python; the worker agent is a
thin wrapper):

- ``classify_pr_outcome`` — pure over pre-fetched ``gh`` PR JSON. Never touches
  the network; every fixture is hand-built from the shapes ``gh pr view --json``
  emits. Loop commits are authored by the human running git but carry the agent
  co-author (``Co-Authored-By: Claude``); a *pure-human* rework commit lacks it.
  That co-author presence/absence is the deterministic merged-clean vs reworked
  signal.
- ``compute_rework_blame`` / ``classify_delayed_outcome`` — pure phase-2 delayed
  signal classification (rework-blame fraction + revert flag).
- ``phase2_due`` / ``read_history`` / ``has_phase_entry`` / ``append_outcome`` —
  phase-window arithmetic + append idempotency.
- ``judge_trajectories`` — the driver, exercised against a tmp vault with
  injected (never-networked) fetchers. Asserts the two acceptance criteria:
  merged-clean vs reworked labels, and re-run adds no duplicate entry.

All vault state is tmp-path via ``vault_factory``; no ambient config, no real
vault, no ``gh``/``git`` subprocess.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from thinkweave.core.schemas import NoteType
from thinkweave.operations import trajectory_outcome as to


# ---------------------------------------------------------------------------
# Fixtures — PR JSON shapes (as `gh pr view --json ...` emits them)
# ---------------------------------------------------------------------------


def _agent_commit(oid: str = "a1") -> dict:
    """A loop commit: human git author + the Claude co-author trailer."""
    return {
        "oid": oid,
        "committedDate": "2026-07-01T12:00:00Z",
        "authors": [
            {"login": "marekpal97", "email": "marekpaluch97@gmail.com", "name": "marekpal97"},
            {"login": "", "email": "noreply@anthropic.com", "name": "Claude"},
        ],
    }


def _human_commit(oid: str = "h1") -> dict:
    """A pure-human rework commit: no agent co-author trailer."""
    return {
        "oid": oid,
        "committedDate": "2026-07-02T09:00:00Z",
        "authors": [
            {"login": "somebody", "email": "someone@example.com", "name": "Some Body"},
        ],
    }


def _merged_pr(commits: list[dict], merged_at: str = "2026-07-03T10:00:00Z") -> dict:
    return {
        "number": 60,
        "state": "MERGED",
        "mergedAt": merged_at,
        "mergeCommit": {"oid": "m0"},
        "commits": commits,
    }


def _review(state: str, body: str = "", login: str = "marekpal97") -> dict:
    """A review submission as ``gh pr view --json reviews`` emits it."""
    return {
        "author": {"login": login},
        "authorAssociation": "OWNER",
        "body": body,
        "state": state,
        "submittedAt": "2026-07-02T09:00:00Z",
    }


def _merged_pr_with_reviews(
    commits: list[dict], reviews: list[dict], merged_at: str = "2026-07-03T10:00:00Z"
) -> dict:
    pr = _merged_pr(commits, merged_at=merged_at)
    pr["reviews"] = reviews
    return pr


# ---------------------------------------------------------------------------
# classify_pr_outcome — pure phase-1 classification
# ---------------------------------------------------------------------------


class TestClassifyPrOutcome:
    def test_merged_all_agent_is_merged_clean(self):
        pr = _merged_pr([_agent_commit("a1"), _agent_commit("a2")])
        label, reason = to.classify_pr_outcome(pr)
        assert label == "merged-clean"
        assert "merged" in reason.lower()

    def test_merged_with_human_commit_is_reworked(self):
        # An agent commit then a human rework commit before merge.
        pr = _merged_pr([_agent_commit("a1"), _human_commit("h1")])
        label, reason = to.classify_pr_outcome(pr)
        assert label == "reworked"
        assert "1 human" in reason or "human commit" in reason

    def test_closed_unmerged(self):
        pr = {"number": 60, "state": "CLOSED", "mergedAt": None, "commits": [_agent_commit()]}
        label, _ = to.classify_pr_outcome(pr)
        assert label == "closed-unmerged"

    def test_open_pr_not_yet_due_returns_none(self):
        pr = {"number": 60, "state": "OPEN", "mergedAt": None, "commits": [_agent_commit()]}
        assert to.classify_pr_outcome(pr) is None

    def test_open_pr_but_loop_routed_to_human(self):
        pr = {"number": 60, "state": "OPEN", "mergedAt": None, "commits": [_agent_commit()]}
        label, _ = to.classify_pr_outcome(pr, trajectory_outcome="routed-to-human")
        assert label == "routed-to-human"

    def test_no_pr_but_routed_to_human(self):
        label, _ = to.classify_pr_outcome(None, trajectory_outcome="routed-to-human")
        assert label == "routed-to-human"

    def test_no_pr_and_not_routed_returns_none(self):
        assert to.classify_pr_outcome(None, trajectory_outcome="awaiting-approval") is None

    def test_custom_agent_identities(self):
        # A commit only recognised as agent under a custom identity set.
        bot = {"oid": "b1", "authors": [{"login": "loop-bot", "email": "", "name": "Loop Bot"}]}
        pr = _merged_pr([bot])
        # Default identities don't know "loop-bot" → looks human → reworked.
        assert to.classify_pr_outcome(pr)[0] == "reworked"
        # With the identity injected, it's an agent commit → merged-clean.
        assert to.classify_pr_outcome(pr, identities=("loop-bot",))[0] == "merged-clean"


# ---------------------------------------------------------------------------
# count_review_feedback — pure human-feedback join (issue #71)
# ---------------------------------------------------------------------------


class TestCountReviewFeedback:
    def test_no_reviews_key_is_zeros(self):
        assert to.count_review_feedback({"number": 1}) == {
            "review_comments": 0,
            "requested_changes_rounds": 0,
        }

    def test_empty_reviews_is_zeros(self):
        # A clean merge (owner clicked merge, left no review) → zeros, NOT
        # missing — this is the criteria's "clean-merged PR yields zeros".
        assert to.count_review_feedback({"reviews": []}) == {
            "review_comments": 0,
            "requested_changes_rounds": 0,
        }

    def test_none_pr_is_zeros(self):
        # Defensive: the pure counter tolerates None (the driver never calls it
        # with None — a None pr at the call site means 'could not fetch').
        assert to.count_review_feedback(None) == {
            "review_comments": 0,
            "requested_changes_rounds": 0,
        }

    def test_counts_bodies_and_changes_requested(self):
        reviews = [
            _review("CHANGES_REQUESTED", "please fix the seam"),
            _review("COMMENTED", "one more nit"),
            _review("APPROVED", ""),  # bare approval, no body → not a comment
        ]
        # Two reviews carry a written body; one round requested changes.
        assert to.count_review_feedback({"reviews": reviews}) == {
            "review_comments": 2,
            "requested_changes_rounds": 1,
        }

    def test_two_changes_requested_rounds(self):
        reviews = [
            _review("CHANGES_REQUESTED", "round 1"),
            _review("CHANGES_REQUESTED", "round 2"),
            _review("APPROVED", "lgtm"),
        ]
        # All three carry bodies; two are changes-requested rounds.
        assert to.count_review_feedback({"reviews": reviews}) == {
            "review_comments": 3,
            "requested_changes_rounds": 2,
        }

    def test_ignores_non_dict_entries(self):
        reviews = [None, "junk", _review("CHANGES_REQUESTED", "x")]
        assert to.count_review_feedback({"reviews": reviews}) == {
            "review_comments": 1,
            "requested_changes_rounds": 1,
        }

    def test_whitespace_only_body_is_not_a_comment(self):
        assert to.count_review_feedback({"reviews": [_review("COMMENTED", "   ")]}) == {
            "review_comments": 0,
            "requested_changes_rounds": 0,
        }


# ---------------------------------------------------------------------------
# Phase-2 delayed-signal classification — pure
# ---------------------------------------------------------------------------


class TestReworkBlame:
    def test_fraction_rewritten(self):
        # 59 merged lines, 22 still blame to the merge commit → 37/59 rewritten.
        assert to.compute_rework_blame(59, 22) == pytest.approx(0.6271, abs=1e-3)

    def test_zero_total_is_zero(self):
        assert to.compute_rework_blame(0, 0) == 0.0

    def test_all_surviving_is_zero(self):
        assert to.compute_rework_blame(40, 40) == 0.0

    def test_surviving_clamped_above_total(self):
        assert to.compute_rework_blame(10, 25) == 0.0


class TestClassifyDelayedOutcome:
    def test_revert_wins(self):
        label, reason = to.classify_delayed_outcome(blame_fraction=0.1, reverted=True)
        assert label == "reverted"
        assert "revert" in reason.lower()

    def test_high_rework_is_reworked_post_merge(self):
        label, reason = to.classify_delayed_outcome(
            blame_fraction=0.63, reverted=False, rework_threshold=0.5
        )
        assert label == "reworked-post-merge"
        assert "0.63" in reason

    def test_low_rework_is_stable(self):
        label, _ = to.classify_delayed_outcome(
            blame_fraction=0.1, reverted=False, rework_threshold=0.5
        )
        assert label == "stable"


# ---------------------------------------------------------------------------
# Phase-window arithmetic + append idempotency
# ---------------------------------------------------------------------------


class TestPhase2Due:
    def test_before_window(self):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        merged = (now - timedelta(days=10)).isoformat()
        assert to.phase2_due(merged, now=now, window_days=14) is False

    def test_after_window(self):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        merged = (now - timedelta(days=15)).isoformat()
        assert to.phase2_due(merged, now=now, window_days=14) is True

    def test_exactly_at_window(self):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        merged = (now - timedelta(days=14)).isoformat()
        assert to.phase2_due(merged, now=now, window_days=14) is True

    def test_missing_merged_at(self):
        assert to.phase2_due("", now=datetime.now(timezone.utc)) is False


class TestAppendIdempotency:
    def test_append_sets_label_and_history(self):
        fm: dict = {}
        delta = to.append_outcome(
            fm, outcome="merged-clean", reason="r", phase=1, judged_at="2026-07-18T00:00:00+00:00"
        )
        assert delta["outcome_label"] == "merged-clean"
        assert len(delta["prediction_history"]) == 1
        entry = delta["prediction_history"][0]
        assert entry["outcome"] == "merged-clean"
        assert entry["phase"] == 1
        assert entry["judged_at"] == "2026-07-18T00:00:00+00:00"

    def test_append_preserves_prior_entries(self):
        fm = {"prediction_history": [{"outcome": "merged-clean", "phase": 1, "judged_at": "x", "reason": "r"}]}
        delta = to.append_outcome(fm, outcome="stable", reason="r2", phase=2)
        assert len(delta["prediction_history"]) == 2
        assert [e["phase"] for e in delta["prediction_history"]] == [1, 2]

    def test_has_phase_entry(self):
        history = [{"outcome": "merged-clean", "phase": 1}]
        assert to.has_phase_entry(history, 1) is True
        assert to.has_phase_entry(history, 2) is False

    def test_extra_fields_recorded_raw(self):
        delta = to.append_outcome(
            fm={}, outcome="reworked-post-merge", reason="r", phase=2,
            extra={"blame_total_lines": 59, "blame_surviving_lines": 22, "reverted": False},
        )
        entry = delta["prediction_history"][0]
        assert entry["blame_total_lines"] == 59
        assert entry["blame_surviving_lines"] == 22
        assert entry["reverted"] is False


# ---------------------------------------------------------------------------
# Driver — judge_trajectories against a tmp vault, injected fetchers
# ---------------------------------------------------------------------------


def _make_trajectory(vault_factory, *, issue: int, pr_url: str, outcome: str = "shipped",
                     extra: dict | None = None):
    tv = vault_factory()
    fm = {"issue": issue, "pr_url": pr_url, "run_id": "run-x", "outcome": outcome,
          "fix_rounds": 0}
    if extra:
        fm.update(extra)
    tv.vault.create_note(
        note_type=NoteType.NOTE,
        title=f"loop trajectory #{issue}",
        tags=["loop-run"],
        extra_frontmatter=fm,
    )
    tv.indexed()
    return tv


class TestJudgeTrajectoriesPhase1:
    def test_merged_clean_labels_trajectory(self, vault_factory):
        tv = _make_trajectory(vault_factory, issue=60, pr_url="https://github.com/o/r/pull/60")
        pr = _merged_pr([_agent_commit("a1"), _agent_commit("a2")])
        result = to.judge_trajectories(
            tv.config, phase="1", pr_fetcher=lambda url: pr,
        )
        assert len(result["judged"]) == 1
        assert result["judged"][0]["outcome"] == "merged-clean"

        # Persisted: outcome_label frontmatter + one history entry.
        note = _reload_only_trajectory(tv)
        assert note.frontmatter.get("outcome_label") == "merged-clean"
        assert len(to.read_history(note.frontmatter)) == 1

    def test_reworked_when_human_commit_present(self, vault_factory):
        tv = _make_trajectory(vault_factory, issue=61, pr_url="https://github.com/o/r/pull/61")
        pr = _merged_pr([_agent_commit("a1"), _human_commit("h1")])
        result = to.judge_trajectories(tv.config, phase="1", pr_fetcher=lambda url: pr)
        assert result["judged"][0]["outcome"] == "reworked"
        note = _reload_only_trajectory(tv)
        assert note.frontmatter.get("outcome_label") == "reworked"

    def test_rerun_adds_no_duplicate(self, vault_factory):
        tv = _make_trajectory(vault_factory, issue=62, pr_url="https://github.com/o/r/pull/62")
        pr = _merged_pr([_agent_commit("a1")])
        to.judge_trajectories(tv.config, phase="1", pr_fetcher=lambda url: pr)
        tv.indexed()
        second = to.judge_trajectories(tv.config, phase="1", pr_fetcher=lambda url: pr)
        assert second["judged"] == []  # already judged; no re-append
        note = _reload_only_trajectory(tv)
        assert len(to.read_history(note.frontmatter)) == 1

    def test_open_pr_is_skipped_not_judged(self, vault_factory):
        tv = _make_trajectory(vault_factory, issue=63, pr_url="https://github.com/o/r/pull/63")
        pr = {"number": 63, "state": "OPEN", "mergedAt": None, "commits": [_agent_commit()]}
        result = to.judge_trajectories(tv.config, phase="1", pr_fetcher=lambda url: pr)
        assert result["judged"] == []
        note = _reload_only_trajectory(tv)
        assert not note.frontmatter.get("outcome_label")


class TestPhase1ReviewFeedbackStamp:
    """Issue #71: phase-1 judge fetches PR review data and stamps the raw
    human-feedback counts (``review_comments`` / ``requested_changes_rounds``)
    on the trajectory note.
    """

    def test_merged_pr_with_feedback_stamps_counts(self, vault_factory):
        tv = _make_trajectory(vault_factory, issue=71, pr_url="https://github.com/o/r/pull/71")
        pr = _merged_pr_with_reviews(
            [_agent_commit("a1")],
            [_review("CHANGES_REQUESTED", "fix this"), _review("APPROVED", "ok now")],
        )
        result = to.judge_trajectories(tv.config, phase="1", pr_fetcher=lambda url: pr)
        assert result["judged"][0]["outcome"] == "merged-clean"

        note = _reload_only_trajectory(tv)
        # Top-level frontmatter carries the counts (criteria: "on its trajectory note").
        assert note.frontmatter.get("review_comments") == 2
        assert note.frontmatter.get("requested_changes_rounds") == 1
        # Recorded raw on the phase-1 history entry too (rides the export row).
        p1 = to.phase_entry(to.read_history(note.frontmatter), 1)
        assert p1["review_comments"] == 2
        assert p1["requested_changes_rounds"] == 1

    def test_clean_merge_stamps_zeros_not_missing(self, vault_factory):
        tv = _make_trajectory(vault_factory, issue=72, pr_url="https://github.com/o/r/pull/72")
        pr = _merged_pr_with_reviews([_agent_commit("a1")], [])  # merged, no reviews
        to.judge_trajectories(tv.config, phase="1", pr_fetcher=lambda url: pr)
        note = _reload_only_trajectory(tv)
        # Zeros present, NOT missing — distinguishes a clean merge from a
        # fetch failure (which leaves the fields absent, tested below).
        assert note.frontmatter.get("review_comments") == 0
        assert note.frontmatter.get("requested_changes_rounds") == 0

    def test_fetch_failure_does_not_zero_fill(self, vault_factory):
        # Routed-to-human: the loop opened no PR (pr_url empty, fetcher → None).
        # A None pr means 'could not fetch' — the feedback fields must stay
        # ABSENT so a downstream learner can tell it apart from a clean 0.
        tv = _make_trajectory(vault_factory, issue=73, pr_url="", outcome="routed-to-human")
        result = to.judge_trajectories(tv.config, phase="1", pr_fetcher=lambda url: None)
        assert result["judged"][0]["outcome"] == "routed-to-human"
        note = _reload_only_trajectory(tv)
        assert "review_comments" not in note.frontmatter
        assert "requested_changes_rounds" not in note.frontmatter

    def test_rerun_is_idempotent(self, vault_factory):
        tv = _make_trajectory(vault_factory, issue=74, pr_url="https://github.com/o/r/pull/74")
        pr = _merged_pr_with_reviews(
            [_agent_commit("a1")], [_review("CHANGES_REQUESTED", "fix")]
        )
        to.judge_trajectories(tv.config, phase="1", pr_fetcher=lambda url: pr)
        tv.indexed()
        second = to.judge_trajectories(tv.config, phase="1", pr_fetcher=lambda url: pr)
        assert second["judged"] == []  # already judged; the join does not re-run
        note = _reload_only_trajectory(tv)
        assert note.frontmatter.get("review_comments") == 1
        assert note.frontmatter.get("requested_changes_rounds") == 1
        assert len(to.read_history(note.frontmatter)) == 1


class TestJudgeTrajectoriesPhase2:
    def test_phase2_appends_second_entry_after_window(self, vault_factory):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        merged_at = (now - timedelta(days=15)).isoformat()
        # Seed a trajectory already carrying a phase-1 merged entry + merged_at.
        p1_entry = {"outcome": "merged-clean", "phase": 1, "judged_at": merged_at, "reason": "r"}
        tv = _make_trajectory(
            vault_factory, issue=60, pr_url="https://github.com/o/r/pull/60",
            extra={"prediction_history": [p1_entry], "outcome_label": "merged-clean",
                   "merged_at": merged_at},
        )
        pr = _merged_pr([_agent_commit("a1")], merged_at=merged_at)
        signals = {"total_lines": 59, "surviving_lines": 22, "reverted": False}
        result = to.judge_trajectories(
            tv.config, phase="2", now=now,
            pr_fetcher=lambda url: pr,
            signals_fetcher=lambda pr_json, **kw: signals,
        )
        assert len(result["judged"]) == 1
        assert result["judged"][0]["outcome"] == "reworked-post-merge"

        note = _reload_only_trajectory(tv)
        history = to.read_history(note.frontmatter)
        assert len(history) == 2
        assert [e["phase"] for e in history] == [1, 2]
        p2 = history[1]
        assert p2["blame_total_lines"] == 59
        assert p2["blame_surviving_lines"] == 22

    def test_phase2_not_due_before_window(self, vault_factory):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        merged_at = (now - timedelta(days=5)).isoformat()
        p1_entry = {"outcome": "merged-clean", "phase": 1, "judged_at": merged_at, "reason": "r"}
        tv = _make_trajectory(
            vault_factory, issue=60, pr_url="https://github.com/o/r/pull/60",
            extra={"prediction_history": [p1_entry], "outcome_label": "merged-clean",
                   "merged_at": merged_at},
        )
        result = to.judge_trajectories(
            tv.config, phase="2", now=now,
            pr_fetcher=lambda url: _merged_pr([_agent_commit()], merged_at=merged_at),
            signals_fetcher=lambda pr_json, **kw: {"total_lines": 1, "surviving_lines": 0, "reverted": False},
        )
        assert result["judged"] == []
        note = _reload_only_trajectory(tv)
        assert len(to.read_history(note.frontmatter)) == 1  # phase-2 not appended

    def test_phase2_rerun_adds_no_duplicate(self, vault_factory):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        merged_at = (now - timedelta(days=15)).isoformat()
        p1 = {"outcome": "merged-clean", "phase": 1, "judged_at": merged_at, "reason": "r"}
        p2 = {"outcome": "stable", "phase": 2, "judged_at": now.isoformat(), "reason": "r2"}
        tv = _make_trajectory(
            vault_factory, issue=60, pr_url="https://github.com/o/r/pull/60",
            extra={"prediction_history": [p1, p2], "outcome_label": "stable",
                   "merged_at": merged_at},
        )
        result = to.judge_trajectories(
            tv.config, phase="2", now=now,
            pr_fetcher=lambda url: _merged_pr([_agent_commit()], merged_at=merged_at),
            signals_fetcher=lambda pr_json, **kw: {"total_lines": 1, "surviving_lines": 1, "reverted": False},
        )
        assert result["judged"] == []
        note = _reload_only_trajectory(tv)
        assert len(to.read_history(note.frontmatter)) == 2


class TestFixRound1RoutedToHuman:
    """Routed-to-human trajectories have no PR (pr_url empty), yet are the most
    informative negative reward signal — they must still be scanned and judged.
    """

    def test_scan_surfaces_routed_to_human_with_empty_pr_url(self, vault_factory):
        tv = _make_trajectory(vault_factory, issue=70, pr_url="", outcome="routed-to-human")
        surface = to.scan_trajectory_outcomes(tv.config)
        assert len(surface) == 1
        assert surface[0]["due_phases"] == [1]

    def test_driver_judges_routed_to_human_with_empty_pr_url(self, vault_factory):
        tv = _make_trajectory(vault_factory, issue=70, pr_url="", outcome="routed-to-human")
        # No PR to fetch — the fetcher returns None for the empty URL.
        result = to.judge_trajectories(tv.config, phase="1", pr_fetcher=lambda url: None)
        assert len(result["judged"]) == 1
        assert result["judged"][0]["outcome"] == "routed-to-human"
        note = _reload_only_trajectory(tv)
        assert note.frontmatter.get("outcome_label") == "routed-to-human"
        assert len(to.read_history(note.frontmatter)) == 1

    def test_routed_to_human_is_never_phase2_due(self, vault_factory):
        # No merge → no merged_at → intentionally never a phase-2 pass.
        now = datetime(2026, 8, 30, tzinfo=timezone.utc)
        p1 = {"outcome": "routed-to-human", "phase": 1, "judged_at": "2026-07-01T00:00:00+00:00", "reason": "r"}
        tv = _make_trajectory(
            vault_factory, issue=70, pr_url="", outcome="routed-to-human",
            extra={"prediction_history": [p1], "outcome_label": "routed-to-human"},
        )
        assert to.scan_trajectory_outcomes(tv.config, now=now) == []
        result = to.judge_trajectories(
            tv.config, phase="2", now=now, pr_fetcher=lambda url: None,
            signals_fetcher=lambda pr_json, **kw: {"total_lines": 1, "surviving_lines": 0, "reverted": False},
        )
        assert result["judged"] == []


class TestFixRound1ErrorBuckets:
    def test_classify_exception_records_error_not_skipped(self, vault_factory):
        tv = _make_trajectory(vault_factory, issue=71, pr_url="https://github.com/o/r/pull/71")

        def _boom(url):
            raise RuntimeError("gh exploded")

        result = to.judge_trajectories(tv.config, phase="1", pr_fetcher=_boom)
        assert result["judged"] == []
        # Recorded in exactly one bucket — errors, not skipped.
        assert len(result["errors"]) == 1
        assert "gh exploded" in result["errors"][0]["reason"]
        assert result["skipped"] == []


class TestFixRound1MergedWithoutMergedAt:
    def test_merged_state_without_mergedAt_anchors_to_judged_at(self, vault_factory):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        tv = _make_trajectory(vault_factory, issue=72, pr_url="https://github.com/o/r/pull/72")
        # Anomalous PR: state MERGED but mergedAt missing.
        pr = {"number": 72, "state": "MERGED", "mergedAt": None,
              "mergeCommit": {"oid": "m0"}, "commits": [_agent_commit("a1")]}
        result = to.judge_trajectories(tv.config, phase="1", now=now, pr_fetcher=lambda url: pr)
        assert result["judged"][0]["outcome"] == "merged-clean"
        note = _reload_only_trajectory(tv)
        # merged_at was anchored to the phase-1 judgment time so phase-2 is reachable.
        anchored = note.frontmatter.get("merged_at")
        assert anchored, "merged verdict must stamp merged_at even without PR mergedAt"
        assert to.phase2_due(
            anchored, now=now + timedelta(days=15), window_days=14
        ) is True


def _reload_only_trajectory(tv):
    """Read back the single loop-run trajectory note in the tmp vault."""
    from thinkweave.core.vault import VaultManager

    vm = VaultManager(config=tv.config)
    for md in vm.root.rglob("*.md"):
        note = vm.read_note(md)
        if "loop-run" in (note.frontmatter.get("tags") or []):
            return note
    raise AssertionError("no loop-run trajectory note found")


# ---------------------------------------------------------------------------
# RLVR export — trajectories flow into `weave rlvr export` alongside decisions
# ---------------------------------------------------------------------------


class TestScanTrajectoryOutcomes:
    """The dream-scan surface — which trajectories have judgment due."""

    def test_phase1_due_when_no_entry(self, vault_factory):
        tv = _make_trajectory(vault_factory, issue=60, pr_url="https://github.com/o/r/pull/60")
        surface = to.scan_trajectory_outcomes(tv.config)
        assert len(surface) == 1
        assert surface[0]["due_phases"] == [1]

    def test_phase2_due_after_window(self, vault_factory):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        merged_at = (now - timedelta(days=15)).isoformat()
        p1 = {"outcome": "merged-clean", "phase": 1, "judged_at": merged_at, "reason": "r"}
        tv = _make_trajectory(
            vault_factory, issue=60, pr_url="https://github.com/o/r/pull/60",
            extra={"prediction_history": [p1], "outcome_label": "merged-clean", "merged_at": merged_at},
        )
        surface = to.scan_trajectory_outcomes(tv.config, now=now)
        assert surface[0]["due_phases"] == [2]

    def test_nothing_due_when_fully_judged(self, vault_factory):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        merged_at = (now - timedelta(days=15)).isoformat()
        p1 = {"outcome": "merged-clean", "phase": 1, "judged_at": merged_at, "reason": "r"}
        p2 = {"outcome": "stable", "phase": 2, "judged_at": now.isoformat(), "reason": "r2"}
        tv = _make_trajectory(
            vault_factory, issue=60, pr_url="https://github.com/o/r/pull/60",
            extra={"prediction_history": [p1, p2], "outcome_label": "stable", "merged_at": merged_at},
        )
        assert to.scan_trajectory_outcomes(tv.config, now=now) == []


class TestRlvrTrajectoryExport:
    """Acceptance: `weave rlvr export` sees trajectory outcomes; phase-2 entries
    appear under `--explode-history`. Trajectory rows reuse the LOCKED decision
    row schema (same keyset) so a downstream learner consumes both identically.
    """

    def _seed_two_phase(self, vault_factory):
        p1 = {"outcome": "merged-clean", "phase": 1, "judged_at": "2026-07-03T10:00:00+00:00", "reason": "clean"}
        p2 = {"outcome": "reworked-post-merge", "phase": 2, "judged_at": "2026-07-18T10:00:00+00:00",
              "reason": "rework-blame 0.63", "blame_total_lines": 59, "blame_surviving_lines": 22,
              "blame_fraction": 0.6271, "reverted": False}
        return _make_trajectory(
            vault_factory, issue=60, pr_url="https://github.com/o/r/pull/60", outcome="shipped",
            extra={"prediction_history": [p1, p2], "outcome_label": "reworked-post-merge",
                   "merged_at": "2026-07-03T10:00:00+00:00"},
        )

    def test_export_row_matches_locked_schema(self, vault_factory):
        from thinkweave.operations.rlvr_export import export_trajectory_rows

        tv = self._seed_two_phase(vault_factory)
        rows = list(export_trajectory_rows(tv.config))
        assert len(rows) == 1
        d = rows[0]
        # Identical top-level keyset to a decision row (schema parity).
        assert set(d.keys()) == {
            "decision_id", "project", "session_id", "created_at",
            "prediction", "outcome", "context",
        }
        assert d["outcome"]["verdict"] == "reworked-post-merge"
        # The two outcome entries map into prediction.history with `match`.
        matches = [e["match"] for e in d["prediction"]["history"]]
        assert matches == ["merged-clean", "reworked-post-merge"]

    def test_explode_history_yields_one_row_per_entry(self, vault_factory):
        from thinkweave.operations.rlvr_export import export_trajectory_rows
        from thinkweave.surfaces.cli.rlvr import _explode_row

        tv = self._seed_two_phase(vault_factory)
        rows = list(export_trajectory_rows(tv.config))
        exploded = _explode_row(rows[0])
        assert len(exploded) == 2
        assert [r["prediction"]["match"] for r in exploded] == ["merged-clean", "reworked-post-merge"]
        assert [r["prediction"]["entry_index"] for r in exploded] == [0, 1]
        # The phase-2 entry is present in the exploded output.
        assert exploded[1]["prediction"]["reason"].startswith("rework-blame")

    def test_export_row_carries_review_feedback(self, vault_factory):
        # Issue #71: the human-feedback counts stamped by the phase-1 judge
        # surface in `weave rlvr export` rows — WITHOUT widening the locked
        # top-level keyset. They ride inside the free-shape phase-1 history
        # entry (same place #60's raw counts live), so the consumer contract
        # for the row envelope is unchanged.
        from thinkweave.operations.rlvr_export import export_trajectory_rows

        p1 = {
            "outcome": "merged-clean", "phase": 1,
            "judged_at": "2026-07-03T10:00:00+00:00", "reason": "clean",
            "human_commits": 0, "fix_rounds": 0,
            "review_comments": 3, "requested_changes_rounds": 1,
        }
        tv = _make_trajectory(
            vault_factory, issue=71, pr_url="https://github.com/o/r/pull/71", outcome="shipped",
            extra={"prediction_history": [p1], "outcome_label": "merged-clean",
                   "merged_at": "2026-07-03T10:00:00+00:00",
                   "review_comments": 3, "requested_changes_rounds": 1},
        )
        rows = list(export_trajectory_rows(tv.config))
        assert len(rows) == 1
        # Envelope keyset unchanged (locked contract intact).
        assert set(rows[0].keys()) == {
            "decision_id", "project", "session_id", "created_at",
            "prediction", "outcome", "context",
        }
        entry = rows[0]["prediction"]["history"][0]
        assert entry["match"] == "merged-clean"  # outcome→match rename survives
        assert entry["review_comments"] == 3
        assert entry["requested_changes_rounds"] == 1

    def test_cli_export_includes_trajectories_by_default(self, vault_factory, capsys):
        import argparse

        from thinkweave.surfaces.cli.rlvr import cmd_rlvr

        tv = self._seed_two_phase(vault_factory)
        args = argparse.Namespace(
            rlvr_action="export", project="", since="", until="",
            committed_only=False, explode_history=True, verbose=False,
        )
        with _use_config(tv.config):
            cmd_rlvr(args)
        out = capsys.readouterr().out.strip().splitlines()
        parsed = [__import__("json").loads(line) for line in out if line.strip()]
        verdicts = [p["prediction"]["match"] for p in parsed]
        assert "merged-clean" in verdicts
        assert "reworked-post-merge" in verdicts


def _use_config(cfg):
    """Patch load_config so the rlvr CLI resolves the tmp vault, not ambient."""
    from unittest.mock import patch

    return patch("thinkweave.surfaces.cli.rlvr.load_config", return_value=cfg)


class TestTrajectoryCli:
    def test_judge_json_output(self, vault_factory, monkeypatch, capsys):
        import argparse

        from thinkweave.operations import trajectory_outcome
        from thinkweave.surfaces.cli.trajectory import cmd_trajectory

        tv = _make_trajectory(vault_factory, issue=60, pr_url="https://github.com/o/r/pull/60")
        # Never touch the network: stub the gh fetcher at its module home.
        pr = _merged_pr([_agent_commit("a1")])
        monkeypatch.setattr(trajectory_outcome, "fetch_pr_json", lambda url: pr)

        args = argparse.Namespace(trajectory_action="judge", phase="1", limit=None, json=True)
        with patch("thinkweave.surfaces.cli.trajectory.load_config", return_value=tv.config):
            with pytest.raises(SystemExit) as exc:
                cmd_trajectory(args)
        assert exc.value.code == 0
        payload = __import__("json").loads(capsys.readouterr().out.strip())
        assert payload["judged"][0]["outcome"] == "merged-clean"
