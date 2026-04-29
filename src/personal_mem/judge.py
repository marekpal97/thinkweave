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
from datetime import datetime, timezone
from pathlib import Path

from personal_mem.schemas import NoteMeta
from personal_mem.vault import VaultManager

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

    Returns dict with verdict, confidence (0.0-1.0), and evidence string.
    """
    judged_at = datetime.now(timezone.utc).isoformat()

    fm = decision.frontmatter
    committed = fm.get("committed", False)
    file_paths = fm.get("file_paths", [])

    # Collect commit_refs: start from frontmatter, enrich from git
    commit_refs: list[str] = list(fm.get("commit_refs", []))
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

    # Check blame survival — how many lines this decision still owns
    blame_lines = _check_blame_survival(file_paths, commit_refs, hash_to_files)

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

    if superseder:
        if blame_lines > 0:
            # Lines survive despite a later decision on the same file —
            # co-contributor, not truly superseded.
            return {
                **base,
                "verdict": "kept",
                "confidence": 0.5,
                "evidence": f"Re-edited by {superseder} but {blame_lines} lines survive{plan_note}",
            }
        return {
            **base,
            "verdict": "superseded",
            "confidence": 0.7,
            "evidence": f"Re-edited by {superseder}{plan_note}",
        }
    if committed and tested:
        return {
            **base,
            "verdict": "kept",
            "confidence": 0.9,
            "evidence": f"Committed and tests pass{plan_note}",
        }
    if committed and not files_exist:
        return {
            **base,
            "verdict": "reverted",
            "confidence": 0.6,
            "evidence": f"Committed but files removed{plan_note}",
        }
    if committed:
        return {
            **base,
            "verdict": "kept",
            "confidence": 0.6,
            "evidence": f"Committed, not tested{plan_note}",
        }
    return {
        **base,
        "verdict": "unknown",
        "confidence": 0.0,
        "evidence": f"Not committed{plan_note}",
    }


def _check_re_edited(
    decision: NoteMeta,
    file_paths: list[str],
    all_decisions: list[NoteMeta],
) -> str | None:
    """Check if any later decision modifies the same files."""
    if not file_paths:
        return None
    decision_files = set(file_paths)
    for other in all_decisions:
        if other.id == decision.id:
            continue
        if other.date and decision.date and other.date > decision.date:
            other_files = set(other.frontmatter.get("file_paths", []))
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
    """
    if not since_date:
        return {}
    # Bare dates like "2026-04-05" need explicit time for reliable git --since
    if "T" not in since_date:
        since_date = f"{since_date}T00:00:00"
    hash_to_files: dict[str, list[str]] = {}
    for fp in file_paths:
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", f"--since={since_date}", "--", fp],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().splitlines():
                    parts = line.split(None, 1)
                    if parts:
                        h = parts[0]
                        hash_to_files.setdefault(h, [])
                        if fp not in hash_to_files[h]:
                            hash_to_files[h].append(fp)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
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

    Returns total surviving line count across all files, or -1 if blame
    cannot be determined (e.g., file deleted, git unavailable).
    """
    if not file_paths or not commit_refs:
        return -1
    total = 0
    checked = False
    for fp in file_paths:
        if not Path(fp).exists():
            continue
        # Narrow refs to those that actually touched this file
        if hash_to_files:
            relevant_refs = [r for r in commit_refs if fp in hash_to_files.get(r, [])]
        else:
            relevant_refs = commit_refs
        if not relevant_refs:
            continue
        try:
            result = subprocess.run(
                ["git", "blame", "--porcelain", fp],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                continue
            checked = True
            for line in result.stdout.splitlines():
                if not line or line[0] == "\t":
                    continue
                full_hash = line.split()[0]
                for ref in relevant_refs:
                    if full_hash.startswith(ref):
                        total += 1
                        break
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
    return total if checked else -1
