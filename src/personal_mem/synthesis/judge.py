"""Structural decision judgment — no LLM, pure evidence-based evaluation.

Evaluates decision notes against downstream evidence:
- Was the file committed? (from session frontmatter + git log)
- Were tests passing? (from session frontmatter)
- Was the file re-edited by a later decision? (supersession check)

The three-stage temporal model:
1. Hooks capture events DURING session → SESSION frontmatter
2. mem_extract creates decisions with best-available info
3. mem_judge reconciles with git reality ANY TIME LATER
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from personal_mem.core._utils import as_list
from personal_mem.core.schemas import NoteMeta
from personal_mem.core.vault import VaultManager

log = logging.getLogger(__name__)


def find_decisions(
    db: sqlite3.Connection,
    vm: VaultManager,
    session_id: str = "",
    project: str = "",
) -> list[NoteMeta]:
    """Look up decision notes via the SQLite index — no filesystem walk.

    Matches session decisions via frontmatter `source_session` *or*
    `derived_from`, so decisions written by `mem_extract` (which sets both)
    are found regardless of which field the caller populates.

    When both `session_id` and `project` are empty, returns every decision.
    """
    if session_id:
        rows = db.execute(
            "SELECT path FROM notes WHERE type = 'decision' "
            "AND (frontmatter LIKE ? OR frontmatter LIKE ?)",
            (f'%"source_session": "{session_id}"%', f'%"{session_id}"%'),
        ).fetchall()
    elif project:
        rows = db.execute(
            "SELECT path FROM notes WHERE type = 'decision' AND project = ?",
            (project,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT path FROM notes WHERE type = 'decision'"
        ).fetchall()

    notes: list[NoteMeta] = []
    for row in rows:
        p = vm.root / row["path"]
        if not p.exists():
            continue
        try:
            note = vm.read_note(p)
        except (ValueError, KeyError):
            continue
        if session_id:
            fm = note.frontmatter
            derived = fm.get("derived_from", [])
            if isinstance(derived, str):
                derived = [derived]
            if fm.get("source_session") != session_id and session_id not in derived:
                continue
        notes.append(note)
    return notes


def evaluate_decision(
    decision: NoteMeta,
    all_decisions: list[NoteMeta],
    session_meta: NoteMeta | None = None,
) -> dict:
    """Evaluate a decision based on downstream evidence. Pure data, no LLM.

    Returns dict with verdict (``kept``/``superseded``/``reverted``/``unknown``),
    confidence (0.0-1.0), and evidence string. Prediction verdicts live in
    the ``/judge-prediction`` skill now — this evaluator only emits the
    structural verdict.
    """
    judged_at = datetime.now(timezone.utc).isoformat()

    fm = decision.frontmatter
    committed = fm.get("committed", False)
    file_paths = as_list(fm.get("file_paths"))

    # Collect commit_refs: start from frontmatter, enrich from git
    commit_refs: list[str] = as_list(fm.get("commit_refs"))
    seen_refs = set(commit_refs)

    # Check if superseded by a later decision on same files
    superseder = _check_re_edited(decision, file_paths, all_decisions)

    # Check if committed via git (catches post-session commits)
    # hash_to_files maps each discovered hash to the files it touched,
    # enabling narrower blame checks downstream.
    hash_to_files: dict[str, list[str]] = {}
    if file_paths:
        hash_to_files = _check_committed_via_git(file_paths, decision.date)
        if hash_to_files:
            committed = True
            for h in hash_to_files:
                if h not in seen_refs:
                    seen_refs.add(h)
                    commit_refs.append(h)

    # Check blame survival — how many lines this decision still owns.
    # P0-9 guard: skip blame entirely when the decision isn't committed.
    # An uncommitted decision has nothing to attribute blame against; the
    # per-file `git blame --porcelain` calls are the dominant judge cost
    # in /mem-wrap. Inside _check_blame_survival the empty commit_refs
    # branch returns -1 immediately, but reaching that branch still costs
    # the function-call dispatch + (with the new ThreadPoolExecutor) the
    # cost of forming the task list — cheap, but free is cheaper.
    if committed:
        blame_lines = _check_blame_survival(file_paths, commit_refs, hash_to_files)
    else:
        blame_lines = -1

    # Check if files still exist (not reverted/deleted)
    files_exist = all(Path(fp).exists() for fp in file_paths) if file_paths else True

    # Check test status from source session
    tested = _check_tested(session_meta, file_paths) if session_meta else False

    # Plan context (informational, doesn't affect verdict)
    plan_ref = fm.get("plan_ref", "")
    plan_note = f" (plan: {plan_ref})" if plan_ref else ""

    # Base result fields shared by all verdict paths
    base = {
        "commit_refs": commit_refs,
        "judged_at": judged_at,
        "blame_lines": blame_lines,
    }

    # Choose verdict from the evidence ladder. One assembly point at the end
    # so prediction_match writeback (and any future cross-cutting field) has
    # exactly one place to attach.
    if superseder:
        if blame_lines > 0:
            # Lines survive despite a later decision on the same file —
            # co-contributor, not truly superseded.
            verdict, confidence, evidence = (
                "kept", 0.5,
                f"Re-edited by {superseder} but {blame_lines} lines survive{plan_note}",
            )
        else:
            verdict, confidence, evidence = (
                "superseded", 0.7, f"Re-edited by {superseder}{plan_note}",
            )
    elif committed and tested:
        verdict, confidence, evidence = (
            "kept", 0.9, f"Committed and tests pass{plan_note}",
        )
    elif committed and not files_exist:
        verdict, confidence, evidence = (
            "reverted", 0.6, f"Committed but files removed{plan_note}",
        )
    elif committed:
        verdict, confidence, evidence = (
            "kept", 0.6, f"Committed, not tested{plan_note}",
        )
    else:
        verdict, confidence, evidence = (
            "unknown", 0.0, f"Not committed{plan_note}",
        )

    return {
        **base,
        "verdict": verdict,
        "confidence": confidence,
        "evidence": evidence,
    }


def _check_re_edited(
    decision: NoteMeta,
    file_paths: list[str],
    all_decisions: list[NoteMeta],
) -> str | None:
    """Check if any later decision modifies the same files.

    **Same-session sibling guard.** Decisions extracted in one ``/mem-wrap``
    batch are co-equal siblings, not supersessions — they routinely share
    ``file_paths`` (the same module touched by several facets of one feature)
    and carry near-identical timestamps. The file-overlap heuristic alone
    would flag the earlier siblings as "re-edited" by the later ones, and
    when the work isn't committed yet the blame-survival co-contributor
    rescue can't fire (no commit to blame), so they'd fall through to a
    *false* ``superseded`` verdict. Decisions sharing ``source_session`` are
    therefore skipped here — only a different-session re-edit (or an explicit
    ``supersedes:`` frontmatter declaration, handled on its own path) marks a
    genuine supersession. Guards against the unvalidatable-lifecycle trap:
    never write a status transition without an evidence source.
    """
    if not file_paths:
        return None
    decision_files = set(file_paths)
    my_session = str(decision.frontmatter.get("source_session") or "")
    for other in all_decisions:
        if other.id == decision.id:
            continue
        other_session = str(other.frontmatter.get("source_session") or "")
        if my_session and other_session and my_session == other_session:
            continue  # sibling from the same wrap — never a supersession
        if other.date and decision.date and other.date > decision.date:
            other_files = set(as_list(other.frontmatter.get("file_paths")))
            if decision_files & other_files:
                return other.id
    return None


def _check_committed_via_git(
    file_paths: list[str], since_date: str,
) -> dict[str, list[str]]:
    """Find commit hashes touching these files after the decision date.

    Returns a dict mapping each short hash to the file_paths it touched.
    This enables narrower blame checks (only blame files a commit actually
    changed) and catches post-session commits that the hooks didn't capture.

    Implementation: a single ``git log --since=<date> --name-only --pretty=…``
    call replaces the per-file fanout. We then intersect each commit's
    touched files against ``file_paths`` to build the same hash→files map.
    For a decision touching N files this is O(1) subprocess calls instead
    of O(N). The window is bounded by --since, so the parsed stream is
    proportional to "commits since the decision", which is small for live
    judging and reasonable even for backfill.
    """
    if not since_date or not file_paths:
        return {}
    # Bare dates like "2026-04-05" need explicit time for reliable git --since
    if "T" not in since_date:
        since_date = f"{since_date}T00:00:00"

    try:
        result = subprocess.run(
            [
                "git", "log",
                f"--since={since_date}",
                "--name-only",
                "--pretty=format:%h",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return {}
    if result.returncode != 0 or not result.stdout.strip():
        return {}

    # Format with `--pretty=format:%h\n` + `--name-only`:
    #   <hash>
    #   <file>
    #   <file>
    #
    #   <hash>
    #   <file>
    #   ...
    # Blank lines separate commit blocks. The %h line is always non-empty.
    target = set(file_paths)
    hash_to_files: dict[str, list[str]] = {}
    current_hash: str | None = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            # Blank line — end of current commit block.
            current_hash = None
            continue
        if current_hash is None:
            # First non-blank line after a separator is the commit hash.
            current_hash = stripped
            continue
        # Subsequent lines are file paths touched by current_hash.
        if stripped in target:
            entry = hash_to_files.setdefault(current_hash, [])
            if stripped not in entry:
                entry.append(stripped)

    return hash_to_files


def _check_tested(session_meta: NoteMeta | None, file_paths: list[str]) -> bool:
    """Check if test runs in the session suggest coverage.

    Approximate: if the session has passing tests and no failures,
    assume the decision's files are covered. Precise file-level
    coverage would require deeper analysis (e.g., coverage.py data).
    """
    if not session_meta:
        return False
    test_runs = session_meta.frontmatter.get("test_runs", [])
    for run in test_runs:
        passed = run.get("passed", 0)
        failed = run.get("failed", 0)
        if passed > 0 and failed == 0:
            return True
    return False


def _blame_one_file(fp: str, relevant_refs: list[str]) -> tuple[bool, int]:
    """Run `git blame --porcelain` for a single file.

    Returns ``(checked, count)`` — ``checked`` flags whether blame ran
    successfully (so the outer caller can decide between -1 and a real
    total when every file fails).
    """
    if not Path(fp).exists() or not relevant_refs:
        return False, 0
    try:
        result = subprocess.run(
            ["git", "blame", "--porcelain", fp],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False, 0
    if result.returncode != 0:
        return False, 0
    total = 0
    for line in result.stdout.splitlines():
        if not line or line[0] == "\t":
            continue
        full_hash = line.split()[0]
        for ref in relevant_refs:
            if full_hash.startswith(ref):
                total += 1
                break
    return True, total


def _check_blame_survival(
    file_paths: list[str],
    commit_refs: list[str],
    hash_to_files: dict[str, list[str]] | None = None,
) -> int:
    """Count lines in files still attributed to these commits via git blame.

    When *hash_to_files* is provided (mapping hash → files it touched),
    blame for each file only counts lines from commits that actually changed
    that file. This prevents cross-contamination from large commits that
    touch many files but are only relevant to one decision's file_paths.

    Per-file blame runs in a small thread pool (max 4 workers) so a
    decision touching multiple files doesn't pay sequential subprocess
    latency for each one (P0-9 defense-in-depth). Pool size is capped
    to avoid starving git on disk-bound forks.

    Returns total surviving line count across all files, or -1 if blame
    cannot be determined (e.g., file deleted, git unavailable).
    """
    if not file_paths or not commit_refs:
        return -1

    # Compute per-file relevant_refs once.
    tasks: list[tuple[str, list[str]]] = []
    for fp in file_paths:
        if hash_to_files:
            relevant_refs = [r for r in commit_refs if fp in hash_to_files.get(r, [])]
        else:
            relevant_refs = list(commit_refs)
        tasks.append((fp, relevant_refs))

    # Single-file fast path keeps test mocks (patch subprocess.run with
    # side_effect/return_value) simple — they expect synchronous, in-thread
    # invocation. For multi-file we still call the per-file helper directly
    # so subprocess.run mocks bound at module scope remain intercepted.
    if len(tasks) <= 1:
        results = [_blame_one_file(fp, refs) for fp, refs in tasks]
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(tasks))) as ex:
            results = list(ex.map(lambda t: _blame_one_file(*t), tasks))

    total = 0
    any_checked = False
    for checked, count in results:
        if checked:
            any_checked = True
            total += count
    return total if any_checked else -1
