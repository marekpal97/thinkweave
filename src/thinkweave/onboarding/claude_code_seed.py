"""Seed the vault from prior Claude Code conversations.

Walks ``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`` files,
derives the canonical project name from each session's ``cwd`` field
(authoritative — the directory name encoding is lossy on ``_`` vs
``-``), and materialises one ``session`` note per JSONL.

This module does **not** call any LLM. Materialisation is pure
file-walk + frontmatter assembly. Optional enrichment (LLM extraction
of decisions and concepts) is a separate pass — inline via
``/weave-wrap`` per-session, or in bulk via Anthropic Batches (see
``operations.hubs_batch.run_claude_code_batch``).

Idempotency: tracked via ``vault/.weave/onboarding/claude_code.json``
(maps Claude Code session UUID → vault note id + project + import ts).
Re-runs skip UUIDs already in the manifest.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator

from thinkweave.core.config import Config, load_config
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager

DEFAULT_CC_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
MANIFEST_REL = ".weave/onboarding/claude_code.json"
UNSCOPED_PROJECT = "_unscoped"


@dataclass
class ClaudeCodeSession:
    """Parsed view of one Claude Code session JSONL."""

    uuid: str
    project: str
    cwd: str
    git_branch: str
    started_at: datetime | None
    ended_at: datetime | None
    user_turns: list[str] = field(default_factory=list)
    assistant_turns: list[str] = field(default_factory=list)
    file_path: Path | None = None

    @property
    def turn_count(self) -> int:
        return len(self.user_turns) + len(self.assistant_turns)


# ── Project resolution ─────────────────────────────────────────────────


def normalize_project(cwd: str) -> str:
    """Derive a vault project name from a session's cwd.

    Three steps:

    1. Strip a trailing ``.claude/worktrees/<branch>`` (Claude Code
       worktree sessions; we want the repo root, not the branch name).
    2. Drop sessions whose cwd is the homedir or ``~/.claude`` itself —
       they map to ``_unscoped``.
    3. Take basename → lowercase → ``-`` → ``_``.
    """
    if not cwd:
        return UNSCOPED_PROJECT

    # Normalize separators up front so a Windows cwd (``C:\\Users\\x\\repo``)
    # and a POSIX cwd parse identically, regardless of which OS runs the
    # import. PurePosixPath then gives consistent ``.parts`` semantics on
    # the normalized string (a drive like ``C:`` becomes a leading part).
    parts = list(PurePosixPath(cwd.replace("\\", "/").rstrip("/")).parts)

    # Strip a trailing ``.claude/worktrees/<branch>[/...]`` — we want the
    # repo root, not the worktree branch dir.
    for i in range(len(parts) - 1):
        if parts[i] == ".claude" and parts[i + 1] == "worktrees":
            parts = parts[:i]
            break

    # Drop sessions whose cwd is the homedir, ``~/.claude``, or the root.
    home_parts = PurePosixPath(str(Path.home()).replace("\\", "/")).parts
    if (
        not parts
        or tuple(parts) == home_parts
        or tuple(parts) == home_parts + (".claude",)
    ):
        return UNSCOPED_PROJECT

    name = parts[-1]
    # Guard a bare drive root (``C:``) and dotfile dirs.
    if not name or name.startswith(".") or name.endswith(":"):
        return UNSCOPED_PROJECT

    name = name.lower().replace("-", "_")
    name = re.sub(r"_{2,}", "_", name).strip("_")
    return name or UNSCOPED_PROJECT


# ── Walker + parser ────────────────────────────────────────────────────


def discover_sessions(
    claude_projects_root: Path = DEFAULT_CC_PROJECTS_ROOT,
) -> Iterator[Path]:
    """Yield every ``*.jsonl`` session file under the Claude Code
    projects root. Stable ordering for reproducible manifests."""
    if not claude_projects_root.exists():
        return
    for project_dir in sorted(claude_projects_root.iterdir()):
        if not project_dir.is_dir():
            continue
        for jsonl in sorted(project_dir.glob("*.jsonl")):
            yield jsonl


def _extract_text_from_message(msg: dict | str) -> str:
    """Pull plain text from a CC message payload.

    User messages have ``message.content`` as a string. Assistant
    messages have it as a list of typed blocks; we keep ``text`` blocks
    and drop ``thinking`` / ``tool_use`` / ``tool_result`` (those are
    operational noise in the historical-seed context).
    """
    if isinstance(msg, str):
        return msg.strip()
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def parse_session(jsonl_path: Path) -> ClaudeCodeSession | None:
    """Parse a JSONL into a session. Returns None if no user/assistant
    events were found (some files contain only metadata events)."""
    uuid = jsonl_path.stem
    cwd = ""
    git_branch = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    user_turns: list[str] = []
    assistant_turns: list[str] = []

    with jsonl_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev_type = ev.get("type", "")

            if not cwd:
                ev_cwd = ev.get("cwd")
                if isinstance(ev_cwd, str) and ev_cwd:
                    cwd = ev_cwd
            if not git_branch:
                ev_branch = ev.get("gitBranch")
                if isinstance(ev_branch, str) and ev_branch:
                    git_branch = ev_branch

            ts = _parse_ts(ev.get("timestamp"))
            if ts and (started_at is None or ts < started_at):
                started_at = ts
            if ts and (ended_at is None or ts > ended_at):
                ended_at = ts

            if ev_type == "user":
                text = _extract_text_from_message(ev.get("message", {}))
                if text:
                    user_turns.append(text)
            elif ev_type == "assistant":
                text = _extract_text_from_message(ev.get("message", {}))
                if text:
                    assistant_turns.append(text)

    if not user_turns and not assistant_turns:
        return None

    return ClaudeCodeSession(
        uuid=uuid,
        project=normalize_project(cwd),
        cwd=cwd,
        git_branch=git_branch,
        started_at=started_at,
        ended_at=ended_at,
        user_turns=user_turns,
        assistant_turns=assistant_turns,
        file_path=jsonl_path,
    )


# ── Manifest ───────────────────────────────────────────────────────────


def _manifest_path(cfg: Config) -> Path:
    return Path(cfg.vault_root) / MANIFEST_REL


def load_manifest(cfg: Config) -> dict:
    path = _manifest_path(cfg)
    if not path.exists():
        return {"version": 1, "imported_uuids": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "imported_uuids": {}}


def save_manifest(cfg: Config, manifest: dict) -> None:
    path = _manifest_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    import os

    os.replace(tmp, path)


# ── Materialisation ────────────────────────────────────────────────────


def _build_session_body(session: ClaudeCodeSession) -> str:
    """Render the session note's markdown body.

    The transcript is preserved verbatim (interleaved user/assistant
    turns). Enrichment passes downstream read this body to extract
    decisions/concepts; we don't pre-summarise here.
    """
    lines: list[str] = []
    lines.append("## Source")
    lines.append("")
    lines.append(f"Imported from Claude Code session `{session.uuid}`.")
    if session.cwd:
        lines.append(f"Original cwd: `{session.cwd}`")
    if session.git_branch:
        lines.append(f"Git branch at session start: `{session.git_branch}`")
    lines.append("")
    lines.append("## Transcript")
    lines.append("")
    n_user = len(session.user_turns)
    n_asst = len(session.assistant_turns)
    for i in range(max(n_user, n_asst)):
        if i < n_user:
            lines.append(f"### User (turn {i + 1})")
            lines.append("")
            lines.append(session.user_turns[i])
            lines.append("")
        if i < n_asst:
            lines.append(f"### Assistant (turn {i + 1})")
            lines.append("")
            lines.append(session.assistant_turns[i])
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def materialize_session(
    cfg: Config,
    vm: VaultManager,
    session: ClaudeCodeSession,
    *,
    manifest: dict,
    dry_run: bool = False,
) -> str | None:
    """Write one session note. Returns vault note id, or None if skipped
    (already in manifest, or dry_run)."""
    imported = manifest.setdefault("imported_uuids", {})
    if session.uuid in imported:
        return None
    if dry_run:
        return None

    title_ts = (session.started_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M")
    title = f"Claude Code session {title_ts} ({session.project})"

    extra_fm: dict = {
        "imported_from": "claude-code",
        "claude_session_uuid": session.uuid,
        "original_jsonl": str(session.file_path) if session.file_path else "",
        "user_turn_count": len(session.user_turns),
        "assistant_turn_count": len(session.assistant_turns),
        "enrichment_status": "pending",
    }
    if session.cwd:
        extra_fm["source_cwd"] = session.cwd
    if session.git_branch:
        extra_fm["git_branch"] = session.git_branch
    if session.started_at:
        extra_fm["started_at"] = session.started_at.isoformat()
    if session.ended_at:
        extra_fm["ended_at"] = session.ended_at.isoformat()

    body = _build_session_body(session)

    path = vm.create_note(
        note_type=NoteType.SESSION,
        title=title,
        body=body,
        project=session.project,
        tags=["imported", "claude-code"],
        extra_frontmatter=extra_fm,
    )

    note_id = extra_fm.get("id")
    if not note_id:
        from thinkweave.core.vault import parse_frontmatter

        fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        note_id = fm.get("id", "")

    imported[session.uuid] = {
        "note_id": note_id,
        "project": session.project,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    return note_id


# ── Top-level entry ────────────────────────────────────────────────────


def import_claude_code(
    cfg: Config | None = None,
    *,
    project_filter: str = "",
    dry_run: bool = False,
    claude_projects_root: Path | None = None,
    since: str = "",
    limit: int = 0,
) -> dict:
    """Walk Claude Code session histories and materialise them as vault
    session notes.

    Args:
        cfg: Vault config; loaded from defaults if None.
        project_filter: If non-empty, only import sessions whose
            *normalized* project matches this name.
        dry_run: If True, returns per-project counts without writing.
        claude_projects_root: Override for the CC projects root
            (default ``~/.claude/projects``).
        since: ISO date (``YYYY-MM-DD``). Sessions whose ``started_at``
            is older are tallied as ``skipped_since`` and not imported.
            Empty string disables the filter.
        limit: Cap on materialised session count (0 = unbounded). When
            set, sessions are processed **newest-first** so the cap
            keeps the most recent work — the natural shape for
            ``--sample-only`` previews from the onboard flow.

    Returns:
        Stats dict: ``{
            "discovered": N, "skipped_no_content": N, "skipped_filter": N,
            "skipped_already_imported": N, "skipped_since": N,
            "materialized": N, "per_project": {...}, "errors": [...],
        }``
    """
    cfg = cfg or load_config()
    root = claude_projects_root or DEFAULT_CC_PROJECTS_ROOT

    stats: dict = {
        "discovered": 0,
        "skipped_no_content": 0,
        "skipped_filter": 0,
        "skipped_already_imported": 0,
        "skipped_since": 0,
        "materialized": 0,
        "per_project": {},
        "errors": [],
    }

    if not root.exists():
        stats["errors"].append(f"Claude Code projects root not found: {root}")
        return stats

    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            stats["errors"].append(f"--since must be YYYY-MM-DD, got {since!r}")
            return stats

    vm = VaultManager(config=cfg) if not dry_run else None
    if vm:
        vm.ensure_dirs()

    manifest = load_manifest(cfg)

    # When `limit` is set, parse every session up-front, sort newest-first,
    # then materialise the head. Without `limit` we keep the streaming path
    # (manifest order, lower memory) — the cost of a full pre-pass on small
    # imports is negligible but unnecessary.
    if limit > 0:
        parsed: list[ClaudeCodeSession] = []
        for jsonl in discover_sessions(root):
            stats["discovered"] += 1
            try:
                session = parse_session(jsonl)
            except Exception as e:
                stats["errors"].append(f"{jsonl}: {type(e).__name__}: {e}")
                continue
            if session is None:
                stats["skipped_no_content"] += 1
                continue
            parsed.append(session)
        parsed.sort(key=lambda s: s.started_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        sessions_iter: Iterator[ClaudeCodeSession] = iter(parsed)
    else:
        def _stream() -> Iterator[ClaudeCodeSession]:
            for jsonl in discover_sessions(root):
                stats["discovered"] += 1
                try:
                    session = parse_session(jsonl)
                except Exception as e:
                    stats["errors"].append(f"{jsonl}: {type(e).__name__}: {e}")
                    continue
                if session is None:
                    stats["skipped_no_content"] += 1
                    continue
                yield session
        sessions_iter = _stream()

    for session in sessions_iter:
        if project_filter and session.project != project_filter:
            stats["skipped_filter"] += 1
            continue

        if since_dt is not None:
            if session.started_at is None or session.started_at < since_dt:
                stats["skipped_since"] += 1
                continue

        if session.uuid in manifest.get("imported_uuids", {}):
            stats["skipped_already_imported"] += 1
            continue

        per_proj = stats["per_project"].setdefault(
            session.project, {"materialized": 0, "discovered": 0}
        )
        per_proj["discovered"] += 1

        if dry_run:
            per_proj["materialized"] += 1
            stats["materialized"] += 1
            if limit and stats["materialized"] >= limit:
                break
            continue

        try:
            note_id = materialize_session(
                cfg, vm, session, manifest=manifest, dry_run=False
            )
            if note_id:
                per_proj["materialized"] += 1
                stats["materialized"] += 1
        except Exception as e:
            src = session.file_path or session.uuid
            stats["errors"].append(f"{src}: {type(e).__name__}: {e}")

        if limit and stats["materialized"] >= limit:
            break

    if not dry_run:
        save_manifest(cfg, manifest)

    return stats
