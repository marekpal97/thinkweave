"""Orphan session cleanup.

An *orphan* session is a folder under ``vault/projects/*/sessions/`` that
accumulated no meaningful content — no derived notes/decisions, no real
event log, no touched files, no commits — and is old enough that no
in-flight wrap can be processing it.

These get created when hooks fire in test invocations, subagent stubs, or
aborted sessions. Left alone they accumulate forever and pollute the
index, landing pages, and SessionStart context. This module is the
garbage collector.

All functions here are pure + testable except ``prune_orphans`` which
performs destructive ``shutil.rmtree`` — keep those separate.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from personal_mem.config import Config, load_config
from personal_mem.vault import VaultManager, parse_frontmatter

# Minimum events.jsonl size in bytes for a session to count as "substantive".
# Below this, the only events are likely PreToolUse stubs with no real work.
EVENTS_MIN_BYTES = 500

# Minimum age in seconds for an orphan to be safe to delete. Protects
# sessions currently being processed. 1 hour is a wide safety margin.
ORPHAN_MIN_AGE_SECONDS = 3600


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class PruneResult:
    """Return value from ``prune_orphans``."""

    deleted: int
    freed_bytes: int
    paths: list[str]

    def as_dict(self) -> dict:
        return {
            "deleted": self.deleted,
            "freed_bytes": self.freed_bytes,
            "paths": self.paths,
        }


def is_orphan(
    session_dir: Path,
    *,
    current_session_id: str = "",
    min_age_seconds: int = ORPHAN_MIN_AGE_SECONDS,
    now: float | None = None,
) -> bool:
    """Return True if a session folder is a safe-to-delete orphan.

    All seven conditions must hold:

    1. Folder contains ``session.md``
    2. No sibling .md files other than ``session.md``
    3. ``events.jsonl`` is missing OR smaller than ``EVENTS_MIN_BYTES``
    4. Frontmatter ``files_touched`` is missing or empty
    5. Frontmatter ``commits`` is missing or empty
    6. Folder age > ``min_age_seconds`` (by frontmatter date or mtime)
    7. Frontmatter ``source_session`` does not match ``current_session_id``

    Returns False on any IO error — conservative: if we can't tell, don't delete.
    """
    try:
        if not session_dir.is_dir():
            return False

        session_md = session_dir / "session.md"
        if not session_md.exists():
            return False

        # Condition 2: no sibling .md files other than session.md
        md_files = list(session_dir.glob("*.md"))
        if len(md_files) != 1 or md_files[0].name != "session.md":
            return False

        # Condition 3: events.jsonl missing or too small
        events_file = session_dir / "events.jsonl"
        if events_file.exists() and events_file.stat().st_size >= EVENTS_MIN_BYTES:
            return False

        # Parse frontmatter once for conditions 4, 5, 6, 7
        text = session_md.read_text(encoding="utf-8")
        fm, _body = parse_frontmatter(text)

        # Condition 4: empty files_touched
        files_touched = fm.get("files_touched") or []
        if files_touched:
            return False

        # Condition 5: empty commits
        commits = fm.get("commits") or []
        if commits:
            return False

        # Condition 7: not the current wrap
        if current_session_id and fm.get("source_session") == current_session_id:
            return False

        # Condition 6: old enough
        if now is None:
            now = time.time()
        age = _folder_age_seconds(session_dir, fm, now)
        if age < min_age_seconds:
            return False

        return True
    except OSError:
        # Permission error, stat failure — fall back to safe (not orphan)
        return False


def find_orphans(
    cfg: Config | None = None,
    *,
    project: str = "",
    current_session_id: str = "",
    min_age_seconds: int = ORPHAN_MIN_AGE_SECONDS,
    now: float | None = None,
) -> list[Path]:
    """Return every orphan session folder under the vault, optionally scoped to a project."""
    cfg = cfg or load_config()
    if now is None:
        now = time.time()

    orphans: list[Path] = []
    for session_dir in iter_session_folders(cfg, project=project):
        if is_orphan(
            session_dir,
            current_session_id=current_session_id,
            min_age_seconds=min_age_seconds,
            now=now,
        ):
            orphans.append(session_dir)
    return orphans


def iter_session_folders(cfg: Config, *, project: str = "") -> Iterable[Path]:
    """Yield every session folder under ``vault/projects/<proj>/sessions/``.

    Skips the ``misc/`` catch-all (which holds standalone notes, not sessions).
    """
    projects_dir = cfg.vault_root / "projects"
    if not projects_dir.exists():
        return

    if project:
        project_dirs = [projects_dir / project]
    else:
        project_dirs = [p for p in projects_dir.iterdir() if p.is_dir()]

    for proj_dir in project_dirs:
        sessions_dir = proj_dir / "sessions"
        if not sessions_dir.is_dir():
            continue
        for entry in sessions_dir.iterdir():
            if not entry.is_dir():
                continue
            if entry.name == "misc":
                continue
            yield entry


def prune_orphans(
    orphans: list[Path],
    *,
    dry_run: bool = False,
) -> PruneResult:
    """Delete orphan session folders. Caller is responsible for running ``find_orphans`` first.

    Returns a PruneResult with counts, freed bytes, and relative paths for
    reporting.
    """
    deleted = 0
    freed = 0
    paths: list[str] = []

    for session_dir in orphans:
        try:
            size = _folder_size_bytes(session_dir)
        except OSError:
            size = 0

        paths.append(str(session_dir))
        freed += size
        if dry_run:
            continue

        try:
            shutil.rmtree(session_dir)
            deleted += 1
        except OSError:
            # Skip folders we can't delete; don't abort the whole pass.
            continue

    return PruneResult(
        deleted=deleted if not dry_run else len(orphans),
        freed_bytes=freed,
        paths=paths,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _folder_age_seconds(session_dir: Path, fm: dict, now: float) -> float:
    """Compute the age of a session in seconds.

    Prefers frontmatter ``processed_at`` then ``date``, falls back to
    directory mtime. Returns ``float('inf')`` if nothing is parseable —
    ancient folder, definitely old enough.
    """
    for key in ("processed_at", "date"):
        raw = fm.get(key)
        if not raw:
            continue
        try:
            ts = _parse_iso(str(raw))
        except ValueError:
            continue
        return max(0.0, now - ts)

    # Fall back to the folder's own mtime
    try:
        return max(0.0, now - session_dir.stat().st_mtime)
    except OSError:
        return float("inf")


def _parse_iso(value: str) -> float:
    """Parse an ISO 8601 timestamp into a POSIX seconds-since-epoch float.

    Accepts full ISO timestamps (``2026-04-06T21:08:09.993889+00:00``) and
    bare dates (``2026-04-06``). Bare dates are interpreted as midnight UTC.
    """
    s = value.strip()
    if not s:
        raise ValueError("empty timestamp")

    # Bare date — midnight UTC
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt.timestamp()

    # Handle trailing Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _folder_size_bytes(path: Path) -> int:
    """Sum file sizes in a folder (non-recursive is fine — sessions are shallow)."""
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total
