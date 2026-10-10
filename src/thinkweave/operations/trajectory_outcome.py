"""Deterministic outcome judge for issue-loop trajectory notes.

The reward signal for the self-improvement loop. `issue-loop-memory.md` names a
deterministic outcome judge as future work; the shape already exists — the
``dream-judge-worker`` appends ``prediction_history`` to *decisions*. This
module is the *task-grain* analog: it appends ``prediction_history``-shaped
entries to loop **trajectory notes** (``type: note``, tag ``loop-run``, carrying
``pr_url`` / ``run_id`` / ``outcome`` frontmatter — see ``build_trajectory()`` in
``scripts/issue_loop.py``) so ``weave rlvr export`` consumes tasks and decisions
identically.

Two judgments per trajectory, on a **closed horizon** (tasks have a natural
verdict window, unlike decisions which are revisited indefinitely):

- **Phase 1 — at merge/close.** Verdict ``merged-clean | reworked |
  closed-unmerged | routed-to-human`` from the PR's state + commit authorship.
- **Phase 2 — once, at +``dream.trajectory_phase2_days`` (default 14) after
  merge.** The delayed signals: **rework-blame** (fraction of the merged diff's
  lines rewritten by later commits) and **revert detection** (a later revert
  commit referencing the PR). The issue/bug-citation sweep (issue reopenings,
  follow-up bug issues citing the PR) is a documented, tested-as-absent seam —
  see :func:`fetch_delayed_signals`.

Design mirrors the dream-judge idiom's split:

- **Pure, unit-tested logic** lives here — :func:`classify_pr_outcome` (over
  pre-fetched PR JSON), :func:`compute_rework_blame`,
  :func:`classify_delayed_outcome`, :func:`phase2_due`, and the append-idempotency
  helpers. None of these touch the network.
- **The main flow** is :func:`judge_trajectories`, first below: per due
  trajectory, phase 1, phase 2, then its feedback handed to the task; last,
  the tracker-close sweep. It produces :class:`Feedback` records; the
  task-side writes are ``TaskStore``'s.
- **The ``gh``/``git`` seam** is isolated in :func:`fetch_pr_json` /
  :func:`fetch_delayed_signals`, at the bottom. The driver takes them as
  injectable parameters so tests feed fixtures and never hit the network or a
  real repo.
- **The worker agent** (``agents/dream-outcome-worker.md``) is a thin wrapper
  that runs ``weave trajectory judge`` and relays the JSON outcome.

Raw counts, never composite scores: phase-1 records ``human_commits``
/ ``fix_rounds`` and the human-feedback join ``review_comments`` /
``requested_changes_rounds`` (fetched from the PR's ``reviews`` and stamped on
the trajectory note); phase-2 records ``blame_total_lines`` /
``blame_surviving_lines`` / ``blame_fraction`` / ``reverted``. Normalization
belongs to the downstream learner, not this judge.
"""


from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from thinkweave.core.config import Config


# --- Phase-1 verdicts (closed-horizon, at merge/close) ---------------------
MERGED_CLEAN = "merged-clean"
REWORKED = "reworked"
CLOSED_UNMERGED = "closed-unmerged"
ROUTED_TO_HUMAN = "routed-to-human"
_MERGED_LABELS = frozenset({MERGED_CLEAN, REWORKED})

# --- Phase-2 verdicts (delayed signals, +window after merge) ---------------
STABLE = "stable"
REWORKED_POST_MERGE = "reworked-post-merge"
REVERTED = "reverted"

# Defaults; every one of these is a config knob (dream.trajectory_*) so cron
# tunes them without a code change. See core/config.py.
PHASE2_WINDOW_DAYS = 14
DEFAULT_AGENT_IDENTITIES = ("claude", "noreply@anthropic.com")
DEFAULT_REWORK_THRESHOLD = 0.5



# ---------------------------------------------------------------------------
# The judge visit — the main flow
# ---------------------------------------------------------------------------


def judge_trajectories(
    cfg: Config,
    *,
    phase: str = "both",
    limit: int | None = None,
    now: datetime | None = None,
    identities: tuple[str, ...] | None = None,
    window_days: int | None = None,
    rework_threshold: float | None = None,
    pr_fetcher: Callable[[str], Optional[dict]] | None = None,
    signals_fetcher: Callable[..., dict] | None = None,
    issue_state: Callable[[str, str], str] | None = None,
) -> dict:
    """Judge every due trajectory once per phase, file each judged phase's
    human feedback as notes on the trajectory's task, then close every open
    task whose tracker issue has closed. Idempotent; write-with-receipt.

    ``phase`` ∈ ``{"both", "1", "2"}``. Returns
    ``{judged: [...], skipped: [...], errors: [...], feedback: [...],
    closed_tasks: [...]}`` — one ``judged`` entry per history append (``{id,
    phase, outcome}``) and one ``feedback`` entry per note written (``{id,
    task, source}``); a re-run over already-judged trajectories returns
    empty ``judged`` and ``feedback``.

    The ``pr_fetcher`` / ``signals_fetcher`` / ``issue_state`` seams default to
    the real ``gh`` / ``git`` functions; tests inject fixtures so no network /
    repo is touched.
    """
    visit = _JudgeVisit(
        cfg,
        now=now or datetime.now(timezone.utc),
        identities=identities or _cfg_identities(cfg),
        window_days=window_days if window_days is not None else _cfg_window(cfg),
        rework_threshold=(
            rework_threshold if rework_threshold is not None else _cfg_rework_threshold(cfg)
        ),
        # Resolved at call time (not as def-time defaults) so
        # ``monkeypatch.setattr(trajectory_outcome, "fetch_pr_json", …)``
        # reaches them — the standard way tests keep this off the network.
        pr_fetcher=pr_fetcher or fetch_pr_json,
        signals_fetcher=signals_fetcher or fetch_delayed_signals,
    )
    for note_id, rel in _candidate_trajectories(cfg)[:limit]:
        trajectory = visit.read(note_id, rel)
        if trajectory is None:
            continue
        if phase in ("both", "1", 1):
            visit.phase1(trajectory)
        if phase in ("both", "2", 2):
            visit.phase2(trajectory)
        visit.file_feedback(trajectory)
    visit.close_tracked(issue_state)
    return visit.result()


@dataclass
class _Trajectory:
    """One trajectory note under judgment, and the feedback its judged
    phases gathered for the task."""

    id: str
    path: Path
    fm: dict
    feedback: list[Feedback] = field(default_factory=list)

    @property
    def history(self) -> list[dict]:
        return read_history(self.fm)

    @property
    def pr_url(self) -> str:
        return self.fm.get("pr_url", "") or ""


@dataclass(frozen=True)
class _Verdict:
    """One phase's classification: the history entry's label, reason and
    raw counts, the top-level fields stamped beside it, and the feedback
    the phase surfaced."""

    label: str
    reason: str
    extra: dict
    stamp: dict = field(default_factory=dict)
    feedback: tuple[Feedback, ...] = ()


class _JudgeVisit:
    """One run of the judge: its knobs, its seams, and the buckets every
    step reports into — each trajectory lands in at most one of ``judged``,
    ``skipped`` or ``errors`` per phase."""

    def __init__(
        self,
        cfg: Config,
        *,
        now: datetime,
        identities: tuple[str, ...],
        window_days: int,
        rework_threshold: float,
        pr_fetcher: Callable[[str], Optional[dict]],
        signals_fetcher: Callable[..., dict],
    ) -> None:
        from thinkweave.core.vault import VaultManager
        from thinkweave.operations.tasks import TaskStore

        self.vm = VaultManager(config=cfg)
        self.store = TaskStore(cfg)
        self.now = now
        self.judged_at = now.isoformat(timespec="seconds")
        self.identities = identities
        self.window_days = window_days
        self.rework_threshold = rework_threshold
        self.pr_fetcher = pr_fetcher
        self.signals_fetcher = signals_fetcher
        self.judged: list[dict] = []
        self.skipped: list[dict] = []
        self.errors: list[dict] = []
        self.filed: list[dict] = []
        self.closed: list[str] = []

    def read(self, note_id: str, rel: str) -> _Trajectory | None:
        path = self.vm.root / rel
        try:
            note = self.vm.read_note(path)
        except Exception as e:  # noqa: BLE001
            self.errors.append({"id": note_id, "reason": f"read failed: {e}"})
            return None
        return _Trajectory(note_id, path, note.frontmatter)

    def phase1(self, t: _Trajectory) -> None:
        """At merge/close: the PR's verdict, once."""
        if has_phase_entry(t.history, 1):
            return
        self._judge(t, 1, self._classify_merge, failed="classify failed",
                    unripe="not at verdict window (PR open / no PR)")

    def phase2(self, t: _Trajectory) -> None:
        """Once, at +window after merge: the delayed signals. Only merged
        trajectories take this pass."""
        if has_phase_entry(t.history, 2):
            return
        p1 = phase_entry(t.history, 1)
        if not (p1 and p1.get("outcome") in _MERGED_LABELS):
            return
        if not phase2_due(t.fm.get("merged_at") or "", now=self.now, window_days=self.window_days):
            self.skipped.append({"id": t.id, "phase": 2, "reason": "phase-2 window not elapsed"})
            return
        self._judge(t, 2, self._classify_delayed, failed="delayed-signal failed")

    def file_feedback(self, t: _Trajectory) -> None:
        """Hand the gathered feedback to the task holding this trajectory's
        round."""
        if not t.feedback:
            return
        task = self.store.for_trajectory(t.id)
        if task is None:
            self.skipped.append({"id": t.id, "reason": "no task holds this trajectory's round; its feedback is not filed"})
            return
        for item in t.feedback:
            try:
                feedback_id = self.store.file_feedback(task, item)
            except Exception as e:  # noqa: BLE001
                self.errors.append({"id": t.id, "reason": f"feedback {item.ref} not filed: {e}"})
            else:
                if feedback_id:
                    self.filed.append({"id": feedback_id, "task": task.id, "source": item.source})

    def close_tracked(self, issue_state: Callable[[str, str], str] | None) -> None:
        tracked = self.store.close_tracked(issue_state)
        self.errors += [{"id": task_id, "reason": reason} for task_id, reason in tracked.errors.items()]
        self.closed = tracked.closed

    def result(self) -> dict:
        return {
            "judged": self.judged,
            "skipped": self.skipped,
            "errors": self.errors,
            "feedback": self.filed,
            "closed_tasks": self.closed,
        }

    def _judge(
        self,
        t: _Trajectory,
        phase: int,
        classify: Callable[[_Trajectory], Optional[_Verdict]],
        *,
        failed: str,
        unripe: str = "",
    ) -> None:
        """Fetch and classify, append the outcome, write the note, and
        report into one bucket. ``classify`` returns ``None`` when the
        phase is not at its verdict window (reported as ``unripe``)."""
        try:
            verdict = classify(t)
        except Exception as e:  # noqa: BLE001
            self.errors.append({"id": t.id, "phase": phase, "reason": f"{failed}: {e}"})
            return
        if verdict is None:
            self.skipped.append({"id": t.id, "phase": phase, "reason": unripe})
            return
        delta = append_outcome(
            t.fm, outcome=verdict.label, reason=verdict.reason, phase=phase,
            judged_at=self.judged_at, extra=verdict.extra,
        )
        delta.update(verdict.stamp)
        try:
            self.vm.update_note(t.path, frontmatter_updates=delta)
        except Exception as e:  # noqa: BLE001
            self.errors.append({"id": t.id, "phase": phase, "reason": f"write failed: {e}"})
            return
        self.judged.append({"id": t.id, "phase": phase, "outcome": verdict.label})
        t.feedback += verdict.feedback
        t.fm.update(delta)

    def _classify_merge(self, t: _Trajectory) -> Optional[_Verdict]:
        pr = self.pr_fetcher(t.pr_url)
        verdict = classify_pr_outcome(
            pr, trajectory_outcome=t.fm.get("outcome", "") or "", identities=self.identities
        )
        if verdict is None:
            return None
        label, reason = verdict
        # The human-feedback counts go TOP-LEVEL too, so the trajectory note
        # itself carries them — only when the PR was fetched: a None pr means
        # 'could not fetch', which must stay DISTINCT from 'clean PR = 0'.
        stamp = count_review_feedback(pr) if pr is not None else {}
        # merged_at makes phase-2's window arithmetic self-contained. Prefer
        # the PR's own mergedAt; a merged verdict lacking it (anomalous)
        # anchors to the judgment time so phase 2 still becomes due.
        # Non-merged verdicts get none — they never take a phase-2 pass.
        if label in _MERGED_LABELS:
            stamp["merged_at"] = (pr.get("mergedAt") if pr else "") or self.judged_at
        return _Verdict(
            label, reason, _phase1_extra(t.fm, pr, self.identities), stamp,
            tuple(review_feedback(pr)) if pr else (),
        )

    def _classify_delayed(self, t: _Trajectory) -> _Verdict:
        pr = self.pr_fetcher(t.pr_url)
        signals = (
            self.signals_fetcher(pr) if pr
            else {"total_lines": 0, "surviving_lines": 0, "reverted": False}
        )
        frac = compute_rework_blame(signals.get("total_lines", 0), signals.get("surviving_lines", 0))
        label, reason = classify_delayed_outcome(
            blame_fraction=frac, reverted=bool(signals.get("reverted")),
            rework_threshold=self.rework_threshold,
        )
        extra = {
            "blame_total_lines": int(signals.get("total_lines", 0) or 0),
            "blame_surviving_lines": int(signals.get("surviving_lines", 0) or 0),
            "blame_fraction": frac,
            "reverted": bool(signals.get("reverted")),
        }
        return _Verdict(
            label, reason, extra,
            feedback=tuple(post_merge_feedback(signals, t.pr_url, self.identities)),
        )


def _phase1_extra(fm: dict, pr: Optional[dict], identities: tuple[str, ...]) -> dict:
    """Raw phase-1 counts for the history entry (no composite scores).

    ``fix_rounds`` from the trajectory; ``human_commits`` and the
    human-feedback counts (``review_comments`` / ``requested_changes_rounds``)
    computed from the fetched PR. When the PR could not be fetched (``pr is
    None`` — e.g. a routed-to-human trajectory that opened no PR) the
    PR-derived counts are omitted rather than zero-filled, so 'could not fetch'
    stays distinct from 'clean PR = 0'.
    """
    extra: dict[str, Any] = {"fix_rounds": int(fm.get("fix_rounds", 0) or 0)}
    if pr is not None:
        extra["human_commits"] = count_human_commits(pr, identities)
        extra.update(count_review_feedback(pr))
    return extra


# ---------------------------------------------------------------------------
# Candidate discovery — index-driven, never a filesystem crawl
# ---------------------------------------------------------------------------


def _candidate_trajectories(cfg: Config) -> list[tuple[str, str]]:
    """``(note_id, rel_path)`` for loop-run trajectory notes carrying a pr_url.

    Uses the SQLite index (``note_tags`` join + ``json_extract`` on the
    frontmatter blob) — no vault crawl, mirroring ``_collect_rejudge_queue``.
    """
    from thinkweave.core.indexer import Indexer

    idx = Indexer(config=cfg)
    try:
        # Admit any loop-run note with a pr_url OR one the loop routed to a
        # human (``outcome: routed-to-human``) — the latter has an empty pr_url
        # (the loop opened no PR) but is the most informative negative reward
        # signal, so it must still reach phase-1 judgment + RLVR export.
        rows = idx.db.execute(
            """
            SELECT DISTINCT n.id AS id, n.path AS path
              FROM notes n
              JOIN note_tags t ON t.note_id = n.id
             WHERE n.type = 'note'
               AND t.tag = 'loop-run'
               AND (
                     (json_extract(n.frontmatter, '$.pr_url') IS NOT NULL
                      AND json_extract(n.frontmatter, '$.pr_url') != '')
                  OR json_extract(n.frontmatter, '$.outcome') = 'routed-to-human'
                   )
             ORDER BY n.id
            """
        ).fetchall()
    finally:
        idx.close()
    out: list[tuple[str, str]] = []
    for r in rows:
        try:
            out.append((r["id"], r["path"]))
        except (KeyError, IndexError):
            out.append((r[0], r[1]))
    return out


def scan_trajectory_outcomes(cfg: Config, *, now: datetime | None = None, cap: int | None = None) -> list[dict]:
    """Read-only surface: trajectory notes with judgment due this cycle.

    Each entry: ``{id, path, pr_url, due_phases: [1|2...]}``. Phase 1 is due
    when no phase-1 entry exists yet; phase 2 when a merged phase-1 entry
    exists, the window has elapsed (``merged_at`` + window), and no phase-2
    entry exists. Powers the dream scan's ``has_signal`` — the worker (or
    ``weave trajectory judge``) does the actual fetch + classify + write.
    """
    from thinkweave.core.vault import VaultManager

    now = now or datetime.now(timezone.utc)
    window = _cfg_window(cfg)
    vm = VaultManager(config=cfg)
    out: list[dict] = []
    for note_id, rel in _candidate_trajectories(cfg):
        try:
            note = vm.read_note(vm.root / rel)
        except Exception:
            continue
        fm = note.frontmatter
        history = read_history(fm)
        due: list[int] = []
        if not has_phase_entry(history, 1):
            due.append(1)
        else:
            p1 = phase_entry(history, 1)
            merged_at = fm.get("merged_at") or ""
            if (
                p1
                and p1.get("outcome") in _MERGED_LABELS
                and not has_phase_entry(history, 2)
                and phase2_due(merged_at, now=now, window_days=window)
            ):
                due.append(2)
        if due:
            out.append({"id": note_id, "path": rel, "pr_url": fm.get("pr_url", ""), "due_phases": due})
        if cap and len(out) >= cap:
            break
    return out


# ---------------------------------------------------------------------------
# Config knob resolution
# ---------------------------------------------------------------------------


def _cfg_identities(cfg: Config) -> tuple[str, ...]:
    raw = getattr(cfg, "dream_trajectory_agent_identities", None)
    if not raw:
        return DEFAULT_AGENT_IDENTITIES
    if isinstance(raw, str):
        return tuple(t.strip() for t in raw.split(",") if t.strip())
    return tuple(raw)


def _cfg_window(cfg: Config) -> int:
    return int(getattr(cfg, "dream_trajectory_phase2_days", PHASE2_WINDOW_DAYS) or PHASE2_WINDOW_DAYS)


def _cfg_rework_threshold(cfg: Config) -> float:
    return float(getattr(cfg, "dream_trajectory_rework_threshold", DEFAULT_REWORK_THRESHOLD) or DEFAULT_REWORK_THRESHOLD)


# ---------------------------------------------------------------------------
# Phase-1 classification — pure over pre-fetched `gh pr view --json` output
# ---------------------------------------------------------------------------


def _identity_match(author: dict, identities: tuple[str, ...]) -> bool:
    """True if any of the author's login/email/name contains an agent identity.

    Substring, case-insensitive. Loop commits carry the agent co-author
    (``Co-Authored-By: Claude <noreply@anthropic.com>``) even though the git
    *author* is the human running the loop — so the co-author's presence is
    what marks a commit as agent-produced.
    """
    hay = " ".join(str(author.get(k, "") or "") for k in ("login", "email", "name")).lower()
    return any(idn.lower() in hay for idn in identities if idn)


def is_agent_authored(commit: dict, identities: tuple[str, ...] = DEFAULT_AGENT_IDENTITIES) -> bool:
    """True if the commit carries an agent author/co-author.

    ``commit["authors"]`` is the list ``gh pr view --json commits`` emits —
    each ``{login, email, name}`` — and includes co-authors (trailers). A
    commit with the Claude co-author is agent-produced; a pure-human rework
    commit lacks it.
    """
    authors = commit.get("authors") or []
    return any(_identity_match(a, identities) for a in authors)


def count_human_commits(pr: dict, identities: tuple[str, ...] = DEFAULT_AGENT_IDENTITIES) -> int:
    """Number of PR commits with no agent author/co-author (pure-human rework)."""
    return sum(1 for c in (pr.get("commits") or []) if not is_agent_authored(c, identities))


def count_review_feedback(pr: Optional[dict]) -> dict:
    """Count raw human review-feedback signals from pre-fetched PR JSON. Pure.

    Over the ``reviews`` array ``gh api .../pulls/N/reviews`` emits — each
    ``{user, body, state, submitted_at, html_url}``:

    - ``review_comments`` — reviews carrying a written body (a substantive
      review comment). A bare approval (empty body) or a PR with no reviews
      contributes nothing.
    - ``requested_changes_rounds`` — reviews with ``state ==
      'CHANGES_REQUESTED'`` (the owner's rework turns / review turns).

    Raw counts, never a composite score: review turns confound task
    difficulty with implementation quality; normalization is the downstream
    learner's job. Returns zeros for a **fetched** PR with no feedback (a clean
    merge) — the phase-1 driver only stamps this when the PR was fetched, so a
    None pr at the call site means 'could not fetch', which stays DISTINCT from
    'clean PR = 0' (that case leaves the fields absent).

    The inline review comments (``reviewComments``) are filed as feedback
    notes by :func:`review_feedback`, not counted here. Raw submission-level
    counts are the right-sized surface; when a deeper count is wanted, extend this function's
    return dict and thread it through :func:`_phase1_extra` / the phase-1 stamp
    — this pure counter is the only place a new signal enters.
    """
    review_comments = 0
    requested_changes_rounds = 0
    for r in (pr or {}).get("reviews") or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("body") or "").strip():
            review_comments += 1
        if str(r.get("state") or "").upper() == "CHANGES_REQUESTED":
            requested_changes_rounds += 1
    return {
        "review_comments": review_comments,
        "requested_changes_rounds": requested_changes_rounds,
    }


def classify_pr_outcome(
    pr: Optional[dict],
    *,
    trajectory_outcome: str = "",
    identities: tuple[str, ...] = DEFAULT_AGENT_IDENTITIES,
) -> Optional[tuple[str, str]]:
    """Classify a trajectory's phase-1 outcome. Pure — no I/O.

    Returns ``(label, reason)`` or ``None`` when the trajectory is not yet at
    its verdict window (an open PR that the loop did not explicitly route to a
    human).

    - **MERGED** → ``merged-clean`` if every commit carries the agent
      co-author, else ``reworked`` (a human touched the branch between agent
      push and merge).
    - **CLOSED, not merged** → ``closed-unmerged``.
    - **OPEN / no PR** → ``routed-to-human`` iff the loop recorded
      ``outcome: routed-to-human`` (it handed the issue off); otherwise
      ``None`` — the horizon hasn't closed, re-check next cycle.
    """
    if pr is None:
        if trajectory_outcome == ROUTED_TO_HUMAN:
            return ROUTED_TO_HUMAN, "loop routed the issue to a human; no PR was opened"
        return None

    state = str(pr.get("state") or "").upper()
    merged = bool(pr.get("mergedAt")) or state == "MERGED"
    if merged:
        commits = pr.get("commits") or []
        humans = count_human_commits(pr, identities)
        if humans:
            return (
                REWORKED,
                f"PR merged with {humans} human commit(s) lacking the agent "
                f"co-author between agent push and merge",
            )
        return MERGED_CLEAN, f"PR merged; all {len(commits)} commit(s) carry the agent co-author"

    if state == "CLOSED":
        return CLOSED_UNMERGED, "PR closed without merging"

    # OPEN (or unknown-but-not-merged): only a verdict if the loop routed it.
    if trajectory_outcome == ROUTED_TO_HUMAN:
        return ROUTED_TO_HUMAN, "loop routed the issue to a human; PR still open"
    return None



# ---------------------------------------------------------------------------
# Phase-2 classification — pure over pre-fetched blame / revert signals
# ---------------------------------------------------------------------------


def compute_rework_blame(total_lines: int, surviving_lines: int) -> float:
    """Fraction of the merged diff's added lines rewritten by later commits.

    ``total_lines`` = lines the merge introduced; ``surviving_lines`` = of
    those, how many still blame to the merge commit at HEAD. Returns
    ``1 - surviving/total`` clamped to ``[0, 1]``; ``0.0`` when nothing was
    added (no signal). This is the task-grain analog of the decision
    substrate's ``blame_lines`` survival — the strongest delayed signal.
    """
    if total_lines <= 0:
        return 0.0
    surviving = max(0, min(int(surviving_lines), int(total_lines)))
    return round(1.0 - surviving / total_lines, 4)


def classify_delayed_outcome(
    *,
    blame_fraction: float,
    reverted: bool,
    rework_threshold: float = DEFAULT_REWORK_THRESHOLD,
) -> tuple[str, str]:
    """Phase-2 verdict from the delayed signals. Pure.

    ``reverted`` dominates (a revert is the loudest negative signal); above the
    rework threshold the merge was substantially rewritten; otherwise stable.
    The label is categorical — the raw ``blame_fraction`` and line counts are
    recorded separately on the history entry (no composite score).
    """
    if reverted:
        return REVERTED, "a later revert commit references this PR"
    if blame_fraction >= rework_threshold:
        return (
            REWORKED_POST_MERGE,
            f"rework-blame {blame_fraction:.2f} of merged lines rewritten within the window",
        )
    return STABLE, f"rework-blame {blame_fraction:.2f}; merged diff largely intact"


# ---------------------------------------------------------------------------
# Feedback capture — the PR's human feedback as notes on the task
# ---------------------------------------------------------------------------

PR_REVIEW = "pr-review"
POST_MERGE_COMMIT = "post-merge-commit"


@dataclass(frozen=True)
class Feedback:
    """One piece of human feedback on a PR; its ``ref`` URL is its identity.
    ``TaskStore.file_feedback`` writes it beside the task."""

    source: str
    ref: str
    files: tuple[str, ...]
    body: str

    @property
    def note_id(self) -> str:
        return "n-" + hashlib.sha1(self.ref.encode("utf-8")).hexdigest()[:8]


def review_feedback(pr: dict) -> list[Feedback]:
    """Each written review and each review comment on the PR. A review with
    no body is only the container of its comments, so it files nothing."""
    reviews = [
        Feedback(PR_REVIEW, r["html_url"], (), str(r["body"]))
        for r in pr.get("reviews") or []
        if str(r.get("body") or "").strip()
    ]
    comments = [
        Feedback(PR_REVIEW, c["html_url"], (c["path"],), str(c.get("body") or ""))
        for c in pr.get("reviewComments") or []
    ]
    return reviews + comments


@dataclass(frozen=True)
class FollowupCommit:
    """A commit after the merge that touches the PR's files; ``authors`` are
    ``{name, email}`` dicts, co-authors included."""

    sha: str
    subject: str
    authors: tuple[dict, ...]
    files: tuple[str, ...]

    @classmethod
    def from_log(cls, block: str) -> FollowupCommit:
        """One ``git log`` block: the header line, then the files it touched."""
        header, *files = block.strip("\n").split("\n")
        sha, subject, name, email, trailers = (header.split("\x1f") + [""] * 5)[:5]
        co_authors = [{"name": t.strip()} for t in trailers.split("\x1e") if t.strip()]
        return cls(sha, subject, ({"name": name, "email": email}, *co_authors),
                   tuple(f for f in files if f.strip()))


def post_merge_feedback(
    signals: dict, pr_url: str, identities: tuple[str, ...] = DEFAULT_AGENT_IDENTITIES
) -> list[Feedback]:
    """Each post-merge commit touching the PR's files that no agent authored."""
    repo_url = pr_url.split("/pull/")[0]
    return [
        Feedback(POST_MERGE_COMMIT, f"{repo_url}/commit/{c.sha}", c.files, c.subject)
        for c in signals.get("followup_commits") or []
        if not any(_identity_match(a, identities) for a in c.authors)
    ]


# ---------------------------------------------------------------------------
# Phase-window arithmetic + prediction_history append idempotency
# ---------------------------------------------------------------------------


def phase2_due(merged_at: str, *, now: datetime | None = None, window_days: int = PHASE2_WINDOW_DAYS) -> bool:
    """True once ``window_days`` have elapsed since ``merged_at`` (ISO string)."""
    if not merged_at:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        m = datetime.fromisoformat(str(merged_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if m.tzinfo is None:
        m = m.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - m) >= timedelta(days=window_days)


def read_history(fm: dict) -> list[dict]:
    """Return the trajectory's ``prediction_history`` list (dicts only).

    Trajectory entries use ``outcome`` (not the decision grammar's ``match``)
    as the verdict key, so no VERDICT clamp applies. Non-list / non-dict junk
    is filtered.
    """
    raw = fm.get("prediction_history")
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict)]


def phase_entry(history: list[dict], phase: int) -> Optional[dict]:
    """First history entry stamped with ``phase``, or ``None``."""
    for e in history:
        try:
            if int(e.get("phase", 0) or 0) == phase:
                return e
        except (TypeError, ValueError):
            continue
    return None


def has_phase_entry(history: list[dict], phase: int) -> bool:
    """True if the history already carries an entry for ``phase`` (idempotency)."""
    return phase_entry(history, phase) is not None


def append_outcome(
    fm: dict,
    *,
    outcome: str,
    reason: str,
    phase: int,
    judged_at: str | None = None,
    extra: dict | None = None,
) -> dict:
    """Compose a frontmatter delta appending one outcome entry to the history.

    Returns ``{prediction_history, outcome_label, outcome_judged_at}`` — the
    full appended list plus the denormalized tail label (the queryable field
    triage calibration reads) and its timestamp. Mirrors
    ``synthesis.prediction.append_verdict`` but with the ``outcome`` verdict
    key and no VERDICT clamp.
    """
    ts = judged_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry: dict[str, Any] = {"outcome": outcome, "judged_at": ts, "reason": reason, "phase": int(phase)}
    if extra:
        entry.update(extra)
    history = read_history(fm) + [entry]
    return {
        "prediction_history": history,
        "outcome_label": outcome,
        "outcome_judged_at": ts,
    }


# ---------------------------------------------------------------------------
# The `gh` / `git` seam — the ONLY network / subprocess surface
# ---------------------------------------------------------------------------

# gh's `commits` JSON field carries co-authors under `authors`; state/mergedAt
# drive the phase-1 verdict; mergeCommit.oid seeds phase-2 blame.
_PR_JSON_FIELDS = "number,state,mergedAt,mergeCommit,commits"
_PR_URL = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)")


def _run(args: list[str], *, cwd: str | None = None) -> str:
    return subprocess.run(
        args, capture_output=True, text=True, check=True, cwd=cwd
    ).stdout


def fetch_pr_json(pr_url: str) -> Optional[dict]:
    """Fetch PR state + commits via ``gh pr view``, plus its ``reviews`` and
    ``reviewComments`` via ``gh api`` (the REST shapes, which carry each
    one's ``html_url``). Network seam — returns ``None`` on any error.

    Kept dead-simple and total so the driver's per-note loop never raises on a
    stale/deleted PR URL; classification is a pure function over what this
    returns.
    """
    match = _PR_URL.search(pr_url or "")
    if not match:
        return None
    api = f"repos/{match[1]}/pulls/{match[2]}"
    try:
        data = json.loads(_run(["gh", "pr", "view", pr_url, "--json", _PR_JSON_FIELDS]))
        data["reviews"] = _gh_api_list(f"{api}/reviews")
        data["reviewComments"] = _gh_api_list(f"{api}/comments")
    except Exception:
        return None
    return data


def _gh_api_list(path: str) -> list[dict]:
    """Every item of a paginated ``gh api`` list endpoint."""
    out = _run(["gh", "api", "--paginate", path, "--jq", ".[] | tojson"])
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def fetch_delayed_signals(pr: dict, *, repo_dir: str | None = None) -> dict:
    """Fetch phase-2 delayed signals for a merged PR. ``git``/``gh`` seam.

    Returns ``{total_lines, surviving_lines, reverted, followup_commits}`` —
    the raw inputs to :func:`compute_rework_blame` /
    :func:`classify_delayed_outcome`, and each later commit touching the
    merge's files as a :class:`FollowupCommit` for :func:`post_merge_feedback`.
    A failed blame or revert lookup degrades to zeros; a failed follow-up
    log raises, so the phase stays unjudged and its feedback is retried.

    Implemented:

    - **rework-blame**: ``git blame`` over the merge commit's added lines.
      ``total_lines`` = lines the merge added (``git diff --numstat
      <merge>^..<merge>``); ``surviving_lines`` = of the changed files, how many
      current lines still blame to the merge commit.
    - **revert detection**: a later commit whose subject references a revert of
      the merge commit / PR.

    **Squash-merge assumption (load-bearing).** The surviving-line attribution
    (a current line "survives" iff its ``git blame`` sha equals
    ``mergeCommit.oid``) is only correct when the PR landed as a **squash
    merge** — then the merge commit IS the single commit that authored every
    line of the PR diff, so unrewritten lines blame back to it. The issue-loop
    ships squash merges, so this holds here. On a **merge-commit** or
    **rebase-merge** repo the PR's content lines are authored by the branch
    commits, not the merge commit, so blame finds ~0 lines attributed to
    ``mergeCommit.oid`` → ``surviving_lines ≈ 0`` → ``rework-blame ≈ 1.0``: a
    **false POSITIVE** ("fully reworked"), NOT the harmless zeros the total-on-
    error path returns. A merge-strategy-robust version would blame against the
    PR's branch-tip commit (or the set of PR commit shas) instead; that is a
    deliberate future change, gated on the loop adopting a non-squash strategy.

    DEFERRED (documented seam, tested-as-absent in the driver): the
    issue-reopening / follow-up-bug-citation sweep. That needs the ``gh`` issue
    timeline + a search over issues citing the PR; it is out of scope for this
    slice and returns nothing here. When added, extend this function's return
    dict (e.g. ``reopened``, ``citing_bug_issues``) and thread it through
    :func:`classify_delayed_outcome` — the pure classifier is the only place a
    new signal changes the verdict.
    """
    signals = {"total_lines": 0, "surviving_lines": 0, "reverted": False, "followup_commits": []}
    merge_oid = (pr.get("mergeCommit") or {}).get("oid") or ""
    number = pr.get("number")
    if not merge_oid:
        return signals

    # rework-blame -----------------------------------------------------------
    try:
        numstat = _run(
            ["git", "diff", "--numstat", f"{merge_oid}^..{merge_oid}"], cwd=repo_dir
        )
    except Exception:
        numstat = ""
    changed_files: list[str] = []
    total_added = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, _deleted, path = parts
        try:
            total_added += int(added)
        except ValueError:
            continue  # binary files show "-"
        changed_files.append(path)
    signals["total_lines"] = total_added

    surviving = 0
    for path in changed_files:
        try:
            blame = _run(["git", "blame", "-l", "HEAD", "--", path], cwd=repo_dir)
        except Exception:
            continue
        for bl in blame.splitlines():
            # `git blame -l` prefixes each line with the 40-char commit sha.
            if bl[:40] == merge_oid[:40] and merge_oid:
                surviving += 1
    signals["surviving_lines"] = surviving

    # revert detection -------------------------------------------------------
    try:
        log = _run(
            ["git", "log", f"{merge_oid}..HEAD", "--format=%H%x09%s"], cwd=repo_dir
        )
    except Exception:
        log = ""
    short = merge_oid[:7]
    for line in log.splitlines():
        subject = line.split("\t", 1)[-1].lower()
        if "revert" not in subject:
            continue
        if short and short in line.lower():
            signals["reverted"] = True
            break
        if number and f"#{number}" in subject:
            signals["reverted"] = True
            break

    # follow-up commits ------------------------------------------------------
    if changed_files:
        followups = _run(
            ["git", "log", f"{merge_oid}..HEAD", "--no-merges", "--name-only",
             "--format=%x00%H%x1f%s%x1f%an%x1f%ae%x1f%(trailers:key=Co-authored-by,valueonly,separator=%x1e)",
             "--", *changed_files],
            cwd=repo_dir,
        )
        signals["followup_commits"] = [
            FollowupCommit.from_log(block) for block in followups.split("\0") if block.strip()
        ]
    return signals
