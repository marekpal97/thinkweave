"""Claude Code Pre/PostToolUse hook handler.

Called via run_hook.sh which sets PYTHONPATH.

Input: JSON via stdin (tool_name, tool_input, session_id, etc.)
Output: JSON to stdout following Claude Code hook protocol.
Exit 0 = success.

PreToolUse (Write|Edit): Injects context from vault FTS index.
PostToolUse (Write|Edit|Bash): Buffers events to JSONL. Session note
  materialization is deferred to Stop hook.
Stop: Reconstructs session from buffer, writes summary, indexes once.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Lazy imports to keep hook startup fast


def main() -> None:
    hook_type = sys.argv[1] if len(sys.argv) > 1 else ""
    hook_input = _read_stdin()
    tool_name = hook_input.get("tool_name", "")

    try:
        if hook_type == "pre_tool_use":
            _handle_pre(tool_name, hook_input)
        elif hook_type == "post_tool_use":
            _handle_post(tool_name, hook_input)
        elif hook_type == "stop":
            _handle_stop(hook_input)
        else:
            _output()
    except Exception:
        # Hooks must never block Claude Code — fail silently
        _output()


def _handle_pre(tool_name: str, hook_input: dict) -> None:
    """PreToolUse: inject relevant vault context as a system message."""
    if tool_name not in ("Write", "Edit"):
        _output()
        return

    tool_input = hook_input.get("tool_input", {})
    file_path = tool_input.get("file_path", tool_input.get("path", ""))

    if not file_path or _is_internal(file_path):
        _output()
        return

    # Check if hive_swarm is active — if so, defer
    if os.environ.get("HIVE_STATE_DIR"):
        _output()
        return

    try:
        from personal_mem.config import load_config
        from personal_mem.search import Search

        cfg = load_config()
        if not cfg.index_db.exists():
            _output()
            return

        s = Search(config=cfg)

        # Search for notes related to this file
        filename = Path(file_path).stem
        results = s.get_context(query=filename, limit=3)
        s.close()

        if not results:
            _output()
            return

        # Build context message
        lines = ["[mem] Related vault notes:"]
        for r in results[:3]:
            tags = f" [{', '.join(r.tags[:3])}]" if r.tags else ""
            lines.append(f"  - [{r.type}] {r.title}{tags}")
        msg = "\n".join(lines)

        _output(system_message=msg)
    except Exception:
        _output()


def _handle_post(tool_name: str, hook_input: dict) -> None:
    """PostToolUse: buffer event to JSONL and ensure session note exists.

    Lean by design — all heavy work (frontmatter updates, summary,
    FTS indexing) is deferred to the Stop hook. The JSONL buffer is
    the source of truth during the session.
    """
    if tool_name not in ("Write", "Edit", "Bash"):
        _output()
        return

    # Check if hive_swarm is active — defer session management
    if os.environ.get("HIVE_STATE_DIR"):
        _output()
        return

    tool_input = hook_input.get("tool_input", {})
    tool_output = hook_input.get("tool_output", "")

    try:
        from personal_mem.config import load_config

        cfg = load_config()

        session_id = hook_input.get("session_id", os.environ.get("CLAUDE_SESSION_ID", ""))
        now = datetime.now(timezone.utc).isoformat()

        # Buffer the event (crash-safe, append-only)
        event = _build_event(tool_name, tool_input, tool_output, now)
        if event:
            _buffer_event(cfg.mem_dir, session_id, event)

        # Ensure session note exists (creates + indexes once on first event)
        _ensure_session(cfg, session_id, hook_input)

        _output()
    except Exception:
        _output()


def _find_session_note(vm, session_id: str) -> Path | None:
    """Find an existing session note for this Claude Code session."""
    if not session_id:
        return None
    from personal_mem.schemas import NoteType

    for note in vm.list_notes(note_type=NoteType.SESSION, limit=20):
        if note.frontmatter.get("source_session") == session_id:
            return vm.root / note.path
    return None


def _ensure_session(cfg, session_id: str, hook_input: dict) -> None:
    """Create session note on first event, index it once for MCP discoverability."""
    if not session_id:
        return

    from personal_mem.schemas import NoteType
    from personal_mem.vault import VaultManager

    vm = VaultManager(config=cfg)
    vm.ensure_dirs()

    if _find_session_note(vm, session_id):
        return

    project = _detect_project(hook_input)
    session_path = vm.create_note(
        NoteType.SESSION,
        f"Session {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        project=project,
        extra_frontmatter={"source_session": session_id},
    )

    # Index once so MCP tools (mem_search) can find this session mid-conversation
    from personal_mem.indexer import Indexer

    idx = Indexer(config=cfg)
    idx.index_file(session_path)
    idx.close()


def _detect_project(hook_input: dict) -> str:
    """Detect the current project from env var, git, or cwd.

    Priority: PERSONAL_MEM_PROJECT env var > git repo name > cwd directory name.
    """
    env_proj = os.environ.get("PERSONAL_MEM_PROJECT")
    if env_proj:
        return env_proj

    cwd = hook_input.get("cwd", os.getcwd())
    cwd_path = Path(cwd)

    # Walk up to find a .git directory — use that repo's directory name
    for parent in [cwd_path, *cwd_path.parents]:
        if (parent / ".git").exists():
            return parent.name
        if parent == parent.parent:
            break

    return cwd_path.name


def _is_internal(path: str) -> bool:
    """Check if a path is an internal/config file we should ignore."""
    p = path.lower()
    return any(
        x in p
        for x in (".claude/", "claude.md", "claude.local.md", ".mem/", "settings.json")
    )


# ---------------------------------------------------------------------------
# Event buffer — crash-safe append-only JSONL
# ---------------------------------------------------------------------------


def _buffer_event(mem_dir: Path, session_id: str, event: dict) -> None:
    """Append a single event to the JSONL buffer. Atomic at OS level."""
    buf_dir = mem_dir / "buffer"
    buf_dir.mkdir(parents=True, exist_ok=True)
    with open(buf_dir / f"{session_id}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _build_event(tool_name: str, tool_input: dict, tool_output, now: str) -> dict | None:
    """Build a structured event dict for the buffer.

    Enriches Bash events with parsed commit/test/push metadata so the
    Stop hook can reconstruct session frontmatter from the buffer alone.
    """
    output_str = tool_output if isinstance(tool_output, str) else ""

    if tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", tool_input.get("path", ""))
        if not file_path or _is_internal(file_path):
            return None
        context = _diff_context(tool_name, tool_input)
        event: dict = {"ts": now, "tool": tool_name, "file": file_path, "context": context}
    elif tool_name == "Bash":
        command = tool_input.get("command", "")
        if not _is_significant_command(command):
            return None
        event = {"ts": now, "tool": "Bash", "command": command[:80]}

        # Enrich with structured metadata
        if _is_git_commit(command):
            commit_info = _parse_commit_from_output(command, output_str)
            if commit_info:
                if commit_info.get("hash"):
                    files = _get_commit_files(commit_info["hash"])
                    if files:
                        commit_info["files"] = files
                event["commit"] = commit_info
        if _is_test_command(command):
            test_info = _parse_test_result(command, output_str)
            if test_info:
                event["test_run"] = test_info
        if "git push" in command.lower():
            branch = _parse_push_branch(command)
            if branch:
                event["git_branch"] = branch
    else:
        return None

    # Capture ★ Insight blocks from tool output
    if output_str:
        insights = _extract_insight_blocks(output_str)
        if insights:
            event["insights"] = insights

    return event


def _read_buffer(mem_dir: Path, session_id: str) -> list[dict]:
    """Read all events from the JSONL buffer for a session."""
    buf_file = mem_dir / "buffer" / f"{session_id}.jsonl"
    if not buf_file.exists():
        return []
    events = []
    for line in buf_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _summarize_events(events: list[dict]) -> dict:
    """Extract structured metadata from buffered events.

    Returns dict with files_touched, commits, test_runs, insights, git_branch.
    """
    files: list[str] = []
    commits: list[dict] = []
    test_runs: list[dict] = []
    insights: list[str] = []
    git_branch = ""

    for ev in events:
        if "file" in ev:
            files.append(ev["file"])
        if "commit" in ev:
            commits.append(ev["commit"])
        if "test_run" in ev:
            test_runs.append(ev["test_run"])
        if "insights" in ev:
            insights.extend(ev["insights"])
        if "git_branch" in ev:
            git_branch = ev["git_branch"]

    # Deduplicate files preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            deduped.append(f)

    return {
        "files_touched": deduped,
        "commits": commits,
        "test_runs": test_runs,
        "insights": insights,
        "git_branch": git_branch,
    }


def _build_auto_summary(
    files_touched: list[str],
    commits: list[dict],
    test_runs: list[dict],
    event_count: int,
) -> str:
    """Build a metadata-based auto-summary for the Stop hook."""
    parts: list[str] = []
    if files_touched:
        basenames = [Path(f).name for f in files_touched[:5]]
        more = f" (+{len(files_touched) - 5} more)" if len(files_touched) > 5 else ""
        parts.append(f"Edited {len(files_touched)} files: {', '.join(basenames)}{more}")
    if commits:
        msgs = []
        for c in commits[:3]:
            if isinstance(c, dict):
                msgs.append(c.get("message", "")[:60])
            else:
                msgs.append(str(c)[:60])
        parts.append(f"Commits: {'; '.join(msgs)}")
    if test_runs:
        for tr in test_runs[:2]:
            if isinstance(tr, dict):
                p = tr.get("passed", 0)
                f = tr.get("failed", 0)
                parts.append(f"Tests: {p} passed, {f} failed")
    if not parts:
        parts.append(f"{event_count} tool events recorded")
    return ". ".join(parts) + "."


def cleanup_buffer(mem_dir: Path, session_id: str) -> None:
    """Delete the buffer file after successful extraction."""
    buf_file = mem_dir / "buffer" / f"{session_id}.jsonl"
    buf_file.unlink(missing_ok=True)


def archive_buffer(mem_dir: Path, session_id: str, session_dir: Path) -> None:
    """Move the buffer file to events.jsonl in the session directory."""
    buf_file = mem_dir / "buffer" / f"{session_id}.jsonl"
    if not buf_file.exists():
        return
    dest = session_dir / "events.jsonl"
    try:
        import shutil
        shutil.move(str(buf_file), str(dest))
    except Exception:
        # Fallback: just delete
        buf_file.unlink(missing_ok=True)


def _is_significant_command(command: str) -> bool:
    """Only capture meaningful bash commands, not noise."""
    significant = ["git commit", "git push", "pytest", "python", "uv run", "make", "npm", "deploy"]
    cmd_lower = command.lower().strip()
    return any(cmd_lower.startswith(s) for s in significant)


def _diff_context(tool_name: str, tool_input: dict) -> str:
    """Extract brief diff context from tool_input for enriched event lines."""
    if tool_name == "Edit":
        old = tool_input.get("old_string", "")[:80].replace("\n", " ").strip()
        new = tool_input.get("new_string", "")[:80].replace("\n", " ").strip()
        if old and new:
            return f" — `{old}` → `{new}`"
    elif tool_name == "Write":
        content = tool_input.get("content", "")
        first = _first_meaningful_line(content)
        if first:
            return f" — {first[:80]}"
    return ""


def _first_meaningful_line(text: str) -> str:
    """Return first non-blank, non-comment line from text."""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("//"):
            return stripped
    return ""


def _is_git_commit(command: str) -> bool:
    """Check if a bash command is a git commit."""
    cmd = command.strip().lower()
    return cmd.startswith("git commit") and "--amend" not in cmd


def _parse_commit_from_output(command: str, output: str) -> dict | None:
    """Extract commit info from git commit output.

    Git commit output looks like:
      [branch abc1234] Commit message
       N files changed, M insertions(+), K deletions(-)
    """
    if not output:
        return None

    info: dict = {}

    # Extract hash from [branch hash] pattern
    m = re.search(r"\[[\w/.-]+\s+([0-9a-f]{7,})\]", output)
    if m:
        info["hash"] = m.group(1)

    # Extract message from -m flag or from output
    m_flag = re.search(r'-m\s+["\'](.+?)["\']', command)
    if m_flag:
        info["message"] = m_flag.group(1)[:120]
    else:
        # Message is after the hash bracket
        m_msg = re.search(r"\[[^\]]+\]\s+(.+)", output)
        if m_msg:
            info["message"] = m_msg.group(1).strip()[:120]

    # Extract files from "N file(s) changed" line
    m_files = re.search(r"(\d+)\s+files?\s+changed", output)
    if m_files:
        info["files_changed"] = int(m_files.group(1))

    return info if info else None


def _get_commit_files(commit_hash: str) -> list[str]:
    """Get the list of files changed in a specific commit via git diff-tree."""
    try:
        result = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return [f for f in result.stdout.strip().splitlines() if f.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return []


def _is_test_command(command: str) -> bool:
    """Check if a bash command runs tests."""
    cmd = command.strip().lower()
    return any(
        cmd.startswith(p)
        for p in ("pytest", "python -m pytest", "uv run pytest", "uv run python -m pytest")
    )


def _parse_test_result(command: str, output: str) -> dict | None:
    """Extract pass/fail counts from pytest output.

    Pytest summary line looks like: "12 passed, 1 failed" or "12 passed"
    """
    if not output:
        return None

    info: dict = {"command": command[:80]}

    # Match pytest summary: "N passed", "N failed", "N error"
    m_passed = re.search(r"(\d+)\s+passed", output)
    m_failed = re.search(r"(\d+)\s+failed", output)
    m_error = re.search(r"(\d+)\s+error", output)

    if m_passed:
        info["passed"] = int(m_passed.group(1))
    if m_failed:
        info["failed"] = int(m_failed.group(1))
    if m_error:
        info["errors"] = int(m_error.group(1))

    # Only return if we found at least a pass/fail count
    return info if ("passed" in info or "failed" in info) else None


def _parse_push_branch(command: str) -> str | None:
    """Extract branch name from a git push command."""
    # git push origin branch-name
    parts = command.strip().split()
    if len(parts) >= 3 and parts[0] == "git" and parts[1] == "push":
        # Skip flags
        for p in parts[2:]:
            if not p.startswith("-"):
                # Could be remote or branch — take the last non-flag arg
                pass
        # Simple heuristic: last non-flag argument
        non_flags = [p for p in parts[2:] if not p.startswith("-")]
        if len(non_flags) >= 2:
            return non_flags[1]  # git push <remote> <branch>
        elif len(non_flags) == 1:
            return non_flags[0]  # git push <remote-or-branch>
    return None


_INSIGHT_RE = re.compile(
    r"★ Insight[─ ]+\n(.*?)\n─+",
    re.DOTALL,
)


def _extract_insight_blocks(text: str) -> list[str]:
    """Extract ★ Insight blocks from Claude output."""
    return _INSIGHT_RE.findall(text)


def _handle_stop(hook_input: dict) -> None:
    """Stop hook: reconstruct session from JSONL buffer and finalize.

    Reads all buffered events, extracts metadata (files, commits, tests,
    insights), writes the session note once, archives the buffer, and
    indexes once. This is the only place that materializes buffer → note.
    """
    session_id = hook_input.get("session_id", os.environ.get("CLAUDE_SESSION_ID", ""))
    if not session_id:
        _output()
        return

    # Check if hive_swarm is active — defer
    if os.environ.get("HIVE_STATE_DIR"):
        _output()
        return

    try:
        from personal_mem.config import load_config
        from personal_mem.indexer import Indexer
        from personal_mem.vault import (
            VaultManager,
            render_frontmatter,
        )

        cfg = load_config()
        vm = VaultManager(config=cfg)

        session_path = _find_session_note(vm, session_id)
        if not session_path:
            _output()
            return

        note = vm.read_note(session_path)

        # Already processed → nothing to do
        if note.frontmatter.get("processed"):
            _output()
            return

        # Reconstruct session from JSONL buffer
        source_session = note.frontmatter.get("source_session", session_id)
        events = _read_buffer(cfg.mem_dir, source_session)

        if not events:
            _output()
            return

        meta = _summarize_events(events)
        auto_summary = _build_auto_summary(
            meta["files_touched"], meta["commits"], meta["test_runs"], len(events),
        )

        # Build final frontmatter
        fm = note.frontmatter
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fm["processed"] = True
        fm["processed_at"] = today
        fm["auto_extracted"] = True
        if meta["files_touched"]:
            fm["files_touched"] = meta["files_touched"]
        if meta["commits"]:
            fm["commits"] = meta["commits"]
        if meta["test_runs"]:
            fm["test_runs"] = meta["test_runs"]
        if meta["git_branch"]:
            fm["git_branch"] = meta["git_branch"]

        # Build body — summary + candidate insights for /mem-wrap enrichment
        body_parts = [f"## Summary\n{auto_summary}"]
        if meta["insights"]:
            insight_lines = "\n".join(f"\n{ins}" for ins in meta["insights"])
            body_parts.append(f"## Candidate Insights{insight_lines}")

        session_path.write_text(
            render_frontmatter(fm) + "\n\n"
            + "\n\n".join(body_parts) + "\n",
            encoding="utf-8",
        )

        # Archive buffer → events.jsonl in session folder
        archive_buffer(cfg.mem_dir, source_session, session_path.parent)

        # Index once
        try:
            idx = Indexer(config=cfg)
            idx.index_file(session_path)
            idx.close()
        except Exception:
            pass

        _output()
    except Exception:
        _output()


def _read_stdin() -> dict:
    try:
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except (json.JSONDecodeError, EOFError):
        return {}


def _output(system_message: str = "") -> None:
    """Write hook response to stdout."""
    result: dict = {}
    if system_message:
        result["systemMessage"] = system_message
    json.dump(result, sys.stdout)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
