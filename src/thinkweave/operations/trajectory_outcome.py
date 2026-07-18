"""Deterministic outcome judge for issue-loop trajectory notes (issue #60).

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

Design mirrors the dream-judge idiom's split (orchestrator note on #60):

- **Pure, unit-tested logic** lives here — :func:`classify_pr_outcome` (over
  pre-fetched PR JSON), :func:`compute_rework_blame`,
  :func:`classify_delayed_outcome`, :func:`phase2_due`, and the append-idempotency
  helpers. None of these touch the network.
- **The ``gh``/``git`` seam** is isolated in :func:`fetch_pr_json` /
  :func:`fetch_delayed_signals`. The driver :func:`judge_trajectories` takes them
  as injectable parameters so tests feed fixtures and never hit the network or a
  real repo.
- **The worker agent** (``agents/dream-outcome-worker.md``) is a thin wrapper
  that runs ``weave trajectory judge`` and relays the JSON outcome.

Raw counts, never composite scores (per #60): phase-1 records ``human_commits``
/ ``fix_rounds`` and #71's human-feedback join ``review_comments`` /
``requested_changes_rounds`` (fetched from the PR's ``reviews`` and stamped on
the trajectory note); phase-2 records ``blame_total_lines`` /
``blame_surviving_lines`` / ``blame_fraction`` / ``reverted``. Normalization
belongs to the downstream learner, not this judge.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
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
    """Count raw human review-feedback signals from pre-fetched PR JSON (issue #71). Pure.

    Over the ``reviews`` array ``gh pr view --json reviews`` emits — each
    ``{author, authorAssociation, body, state, submittedAt}``:

    - ``review_comments`` — reviews carrying a written body (a substantive
      review comment). A bare approval (empty body) or a PR with no reviews
      contributes nothing.
    - ``requested_changes_rounds`` — reviews with ``state ==
      'CHANGES_REQUESTED'`` (the owner's rework turns / review turns).

    Raw counts, never a composite score (per #60): review turns confound task
    difficulty with implementation quality; normalization is the downstream
    learner's job. Returns zeros for a **fetched** PR with no feedback (a clean
    merge) — the phase-1 driver only stamps this when the PR was fetched, so a
    None pr at the call site means 'could not fetch', which stays DISTINCT from
    'clean PR = 0' (that case leaves the fields absent).

    DEFERRED (documented seam): the finer-grained inline review-*thread* comment
    count (``gh api .../pulls/N/comments``) and a condensed digest of review
    bodies — both need a second fetch beyond ``gh pr view --json reviews``.
    Raw submission-level counts are the #71 acceptance criteria and the
    right-sized surface; when a deeper count is wanted, extend this function's
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
    #59's triage calibration reads) and its timestamp. Mirrors
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
# drive the phase-1 verdict; mergeCommit.oid seeds phase-2 blame; `reviews`
# carries the human-feedback join (#71 — state + body per review submission).
_PR_JSON_FIELDS = "number,state,mergedAt,mergeCommit,commits,reviews"


def _run(args: list[str], *, cwd: str | None = None) -> str:
    return subprocess.run(
        args, capture_output=True, text=True, check=True, cwd=cwd
    ).stdout


def fetch_pr_json(pr_url: str) -> Optional[dict]:
    """Fetch PR state + commits via ``gh``. Network seam — returns ``None`` on any error.

    Kept dead-simple and total so the driver's per-note loop never raises on a
    stale/deleted PR URL; classification is a pure function over what this
    returns.
    """
    if not pr_url:
        return None
    try:
        out = _run(["gh", "pr", "view", pr_url, "--json", _PR_JSON_FIELDS])
    except Exception:
        return None
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def fetch_delayed_signals(pr: dict, *, repo_dir: str | None = None) -> dict:
    """Fetch phase-2 delayed signals for a merged PR. ``git``/``gh`` seam.

    Returns ``{total_lines, surviving_lines, reverted}`` — the raw inputs to
    :func:`compute_rework_blame` / :func:`classify_delayed_outcome`. Total on any
    error so the driver degrades to a ``stable`` verdict rather than raising.

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
    signals = {"total_lines": 0, "surviving_lines": 0, "reverted": False}
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
    return signals


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
# Driver — fetch + classify + write, idempotent, per-phase
# ---------------------------------------------------------------------------


def _phase1_extra(fm: dict, pr: Optional[dict], identities: tuple[str, ...]) -> dict:
    """Raw phase-1 counts for the history entry (no composite scores).

    ``fix_rounds`` from the trajectory; ``human_commits`` and #71's
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
) -> dict:
    """Judge every due trajectory once per phase. Idempotent; write-with-receipt.

    ``phase`` ∈ ``{"both", "1", "2"}``. Returns
    ``{judged: [...], skipped: [...], errors: [...]}`` — one ``judged`` entry
    per history append (``{id, phase, outcome}``); a re-run over already-judged
    trajectories returns empty ``judged``.

    The ``pr_fetcher`` / ``signals_fetcher`` seams default to the real ``gh`` /
    ``git`` functions; tests inject fixtures so no network / repo is touched.
    """
    from thinkweave.core.vault import VaultManager

    # Resolve the seams at call time (not as def-time defaults) so
    # ``monkeypatch.setattr(trajectory_outcome, "fetch_pr_json", …)`` reaches
    # them — the standard way tests keep this off the network.
    pr_fetcher = pr_fetcher or fetch_pr_json
    signals_fetcher = signals_fetcher or fetch_delayed_signals

    now = now or datetime.now(timezone.utc)
    identities = identities or _cfg_identities(cfg)
    window_days = window_days if window_days is not None else _cfg_window(cfg)
    rework_threshold = rework_threshold if rework_threshold is not None else _cfg_rework_threshold(cfg)
    do1 = phase in ("both", "1", 1)
    do2 = phase in ("both", "2", 2)
    judged_at = now.isoformat(timespec="seconds")

    vm = VaultManager(config=cfg)
    judged: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    seen = 0

    for note_id, rel in _candidate_trajectories(cfg):
        if limit is not None and seen >= limit:
            break
        seen += 1
        path = vm.root / rel
        try:
            note = vm.read_note(path)
        except Exception as e:  # noqa: BLE001
            errors.append({"id": note_id, "reason": f"read failed: {e}"})
            continue
        fm = note.frontmatter
        history = read_history(fm)
        pr_url = fm.get("pr_url", "") or ""
        traj_outcome = fm.get("outcome", "") or ""

        # --- Phase 1: at merge/close ---------------------------------------
        if do1 and not has_phase_entry(history, 1):
            errored = False
            try:
                pr = pr_fetcher(pr_url)
                verdict = classify_pr_outcome(pr, trajectory_outcome=traj_outcome, identities=identities)
            except Exception as e:  # noqa: BLE001
                errors.append({"id": note_id, "phase": 1, "reason": f"classify failed: {e}"})
                pr, verdict, errored = None, None, True
            if errored:
                # A classify/fetch exception is recorded under errors only —
                # do NOT also record it as a skip (one bucket per note).
                pass
            elif verdict is None:
                skipped.append({"id": note_id, "phase": 1, "reason": "not at verdict window (PR open / no PR)"})
            else:
                label, reason = verdict
                extra = _phase1_extra(fm, pr, identities)
                delta = append_outcome(fm, outcome=label, reason=reason, phase=1, judged_at=judged_at, extra=extra)
                # #71: stamp the human-feedback counts as TOP-LEVEL frontmatter
                # so the trajectory note itself carries them (and rlvr export
                # surfaces them via the history entry). Only when the PR was
                # fetched — a None pr means 'could not fetch', which must stay
                # DISTINCT from 'clean PR = 0' (so we leave the fields absent).
                if pr is not None:
                    delta.update(count_review_feedback(pr))
                # Stamp merged_at so phase-2's window arithmetic is
                # self-contained. Prefer the PR's own mergedAt; if a merged
                # verdict lacks it (anomalous — GitHub normally always sets it),
                # anchor to the phase-1 judgment time so phase-2 still becomes
                # due rather than being blocked forever. Non-merged verdicts
                # (routed-to-human / closed-unmerged) intentionally get no
                # merged_at — they never take a phase-2 pass.
                if label in _MERGED_LABELS:
                    delta["merged_at"] = (pr.get("mergedAt") if pr else "") or judged_at
                try:
                    vm.update_note(path, frontmatter_updates=delta)
                except Exception as e:  # noqa: BLE001
                    errors.append({"id": note_id, "phase": 1, "reason": f"write failed: {e}"})
                else:
                    judged.append({"id": note_id, "phase": 1, "outcome": label})
                    fm.update(delta)
                    history = read_history(fm)

        # --- Phase 2: once, at +window after merge -------------------------
        if do2 and not has_phase_entry(history, 2):
            p1 = phase_entry(history, 1)
            merged_at = fm.get("merged_at") or ""
            if not (p1 and p1.get("outcome") in _MERGED_LABELS):
                pass  # only merged trajectories get a phase-2 pass
            elif not phase2_due(merged_at, now=now, window_days=window_days):
                skipped.append({"id": note_id, "phase": 2, "reason": "phase-2 window not elapsed"})
            else:
                try:
                    pr = pr_fetcher(pr_url)
                    signals = signals_fetcher(pr) if pr else {"total_lines": 0, "surviving_lines": 0, "reverted": False}
                    frac = compute_rework_blame(signals.get("total_lines", 0), signals.get("surviving_lines", 0))
                    label, reason = classify_delayed_outcome(
                        blame_fraction=frac, reverted=bool(signals.get("reverted")), rework_threshold=rework_threshold
                    )
                except Exception as e:  # noqa: BLE001
                    errors.append({"id": note_id, "phase": 2, "reason": f"delayed-signal failed: {e}"})
                else:
                    extra = {
                        "blame_total_lines": int(signals.get("total_lines", 0) or 0),
                        "blame_surviving_lines": int(signals.get("surviving_lines", 0) or 0),
                        "blame_fraction": frac,
                        "reverted": bool(signals.get("reverted")),
                    }
                    delta = append_outcome(fm, outcome=label, reason=reason, phase=2, judged_at=judged_at, extra=extra)
                    try:
                        vm.update_note(path, frontmatter_updates=delta)
                    except Exception as e:  # noqa: BLE001
                        errors.append({"id": note_id, "phase": 2, "reason": f"write failed: {e}"})
                    else:
                        judged.append({"id": note_id, "phase": 2, "outcome": label})

    return {"judged": judged, "skipped": skipped, "errors": errors}
