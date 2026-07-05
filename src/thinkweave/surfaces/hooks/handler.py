"""Claude Code Pre/PostToolUse/Stop/SessionStart/UserPromptSubmit hook handler.

Invoked as the `weave-hook` console script (declared in pyproject.toml).
pip/uv materialize this as a cross-platform executable, so Claude Code
calls it directly from settings.local.json with no shell wrapper.

Input: JSON via stdin (tool_name, tool_input, session_id, etc.)
Output: JSON to stdout following Claude Code hook protocol.
Exit 0 = success.

SessionStart: Injects ~7–10k tokens of structured project context
  (recent sessions, STATE, backlog, decisions, tool manifest) so Claude
  wakes up oriented. Never blocks — always exits 0.
UserPromptSubmit: Captures every user prompt as a structured "prompt"
  event in the JSONL buffer. Promotes user prompts into a first-class
  primitive (`Prompt`) — replaces the heuristic `probe`-tag flow.
PostToolUse (Write|Edit|Bash): Buffers events to JSONL. Session note
  materialization is deferred to Stop hook.
Stop: Reconstructs session from buffer, writes summary, indexes once.

Note: an earlier PreToolUse(Write|Edit) handler injected "Related vault
notes" before each file edit. It was redundant with SessionStart context
and the filename-stem heuristic produced noisy hits, so it was removed.
Re-running `weave hooks install` strips any stale PreToolUse entry from
existing settings.
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


def _log_error(hook_type: str, error: Exception) -> None:
    """Log hook errors to file. Never blocks Claude Code."""
    try:
        import traceback

        from thinkweave.core.config import load_config

        cfg = load_config()
        log_path = cfg.weave_dir / "hooks.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{now}] {hook_type}: {error}\n")
            traceback.print_exc(file=f)
            f.write("\n")
    except Exception:
        pass  # Last resort: silent failure on logging itself


def _log_info(hook_type: str, message: str) -> None:
    """Log non-error hook telemetry (e.g. an R2 deadline miss) to file.

    Sibling to :func:`_log_error` minus the traceback — for events that are
    expected/handled outcomes, not failures. Never blocks Claude Code.
    """
    try:
        from thinkweave.core.config import load_config

        cfg = load_config()
        log_path = cfg.weave_dir / "hooks.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{now}] {hook_type}: {message}\n")
    except Exception:
        pass  # Last resort: silent failure on logging itself


def main() -> None:
    hook_type = sys.argv[1] if len(sys.argv) > 1 else ""
    hook_input = _read_stdin()
    tool_name = hook_input.get("tool_name", "")

    # Early-return gate: if the vault hasn't been initialised yet, every
    # hook is a no-op. Replaces the bash gate in hooks/hooks.json — that
    # gate (a) checked a stale Phase-3.1 path and (b) used bash idioms
    # that don't parse under cmd.exe. Matches the "fail-silent, exit 0"
    # posture of the existing try/except below.
    try:
        from thinkweave.core.config import is_vault_initialized, load_config

        if not is_vault_initialized(load_config()):
            _output()
            return
    except Exception as e:
        _log_error(hook_type, e)
        _output()
        return

    try:
        if hook_type == "post_tool_use":
            _handle_post(tool_name, hook_input)
        elif hook_type == "stop":
            _handle_stop(hook_input)
        elif hook_type == "session_start":
            _handle_session_start(hook_input)
        elif hook_type == "user_prompt_submit":
            _handle_user_prompt_submit(hook_input)
        else:
            # Includes legacy `pre_tool_use` invocations from settings.json
            # entries written before that hook was retired. Falls through
            # to an empty {} payload, which Claude Code treats as a no-op.
            _output()
    except Exception as e:
        _log_error(hook_type, e)
        _output()


def _handle_post(tool_name: str, hook_input: dict) -> None:
    """PostToolUse: buffer event to JSONL and ensure session note exists.

    Lean by design — all heavy work (frontmatter updates, summary,
    FTS indexing) is deferred to the Stop hook. The JSONL buffer is
    the source of truth during the session.

    Gated to Write/Edit/Bash (file/command activity) plus the closed set
    of thinkweave MCP retrieval tools. Retrieval events feed the RLVR
    decision-context substrate — see ``operations/retrieval_log.py``.

    Performance note: on retrieval-tool calls (``mcp__thinkweave__weave_search``
    etc.) we deliberately *skip* the ``_ensure_session`` materialisation that
    action tools trigger. The session note will be lazily created by the
    next action/prompt event (already cheap there), or by the Stop hook's
    fallback path for retrieval-only sessions. Without this skip, every
    MCP retrieval call would pay an ``rglob(*.md)`` scan over the entire
    vault — for large vaults that blows past Claude Code's 5s hook timeout
    and the hook is cancelled before the buffer write lands, dropping the
    retrieval event entirely. Measured 2026-05-26 against a ~1.5k-note
    vault on WSL→9P: every ``weave_search`` PostToolUse hook was cancelled.
    """
    from thinkweave.operations.retrieval_log import RETRIEVAL_TOOLS

    is_action_tool = tool_name in ("Write", "Edit", "Bash")
    is_retrieval_tool = tool_name in RETRIEVAL_TOOLS
    if not (is_action_tool or is_retrieval_tool):
        _output()
        return

    tool_input = hook_input.get("tool_input", {})
    # Claude Code's PostToolUse payload uses ``tool_response`` (newer, an
    # object with stdout/stderr) or ``tool_output`` (older string form).
    # _extract_tool_output_text normalises both to a single string so
    # downstream parsers (_parse_commit_from_output, build_retrieval_event,
    # etc.) don't need provider-version awareness.
    tool_output = _extract_tool_output_text(hook_input)

    try:
        from thinkweave.core.config import load_config

        cfg = load_config()

        session_id = hook_input.get("session_id", os.environ.get("CLAUDE_SESSION_ID", ""))
        now = datetime.now(timezone.utc).isoformat()

        # Buffer the event (crash-safe, append-only). Retrieval and action
        # events are kept in the same buffer file — the Stop-time finalizer
        # partitions them into events.jsonl vs retrieval_log.jsonl.
        if is_action_tool:
            event = _build_event(tool_name, tool_input, tool_output, now)
        else:
            from thinkweave.operations.retrieval_log import build_retrieval_event

            event = build_retrieval_event(tool_name, tool_input, tool_output, now)
        if event:
            _buffer_event(cfg.weave_dir, session_id, event)

        # Action-tool path materialises the session note (so MCP tools can
        # discover it mid-conversation). Retrieval path defers — Stop hook
        # creates one from the buffer if nothing else does. Keeps the
        # retrieval hook latency O(buffer-append) rather than O(vault-scan).
        if is_action_tool:
            _ensure_session(cfg, session_id, hook_input)

        _output()
    except Exception as e:
        _log_error("post_tool_use", e)
        _output()


def _handle_user_prompt_submit(hook_input: dict) -> None:
    """UserPromptSubmit: append a structured prompt event to the JSONL buffer.

    Schema written to ``buffer/<session_id>.jsonl``::

        {"ts": "...", "type": "prompt", "text": "...",
         "session_id": "...", "cwd": "..."}

    Promotes user prompts into a first-class primitive that ``extract.py``
    can lift into ``Prompt`` objects + classify as probes — replacing the
    older heuristic ``probe`` tag flow. Never blocks Claude Code; failures
    are logged silently.
    """
    try:
        from thinkweave.core.config import load_config

        cfg = load_config()

        session_id = hook_input.get(
            "session_id", os.environ.get("CLAUDE_SESSION_ID", "")
        )
        prompt_text = hook_input.get("prompt", hook_input.get("user_prompt", ""))
        if not session_id or not prompt_text:
            _output()
            return

        now = datetime.now(timezone.utc).isoformat()
        cwd = hook_input.get("cwd", "")
        event = {
            "ts": now,
            "type": "prompt",
            "text": prompt_text,
            "session_id": session_id,
            "cwd": cwd,
        }
        _buffer_event(cfg.weave_dir, session_id, event)

        # Eagerly create the session note too, so a buffer that begins
        # with prompts (no Edit/Bash yet) still has a note to attach to.
        _ensure_session(cfg, session_id, hook_input)

        # R2 — prompt-time retrieval enrichment. Bounded, deduped against the
        # live buffer, hard-capped. Any failure here must fall through to a
        # plain (empty) response — never break the user's turn.
        block = _prompt_time_enrichment(cfg, session_id, prompt_text, now)
        if block:
            _output(
                additional_context=block,
                hook_event_name="UserPromptSubmit",
            )
            return

        _output()
    except Exception as e:
        _log_error("user_prompt_submit", e)
        _output()


def _prompt_time_enrichment(
    cfg, session_id: str, prompt_text: str, now: str
) -> str | None:
    """Build the R2 enrichment block and record the outcome to the buffer.

    Returns the block to inject, or ``None`` to no-op. Self-contained: on any
    error it logs and returns ``None`` so the caller emits a plain response.
    ``build_enrichment`` is pure (read-only) — this function owns every
    buffer write-back on its behalf:

    - On a fresh block: a ``retrieval`` event tagged with ``PROMPT_TIME_TOOL``
      so (1) the next turn's dedup sees these ids and (2) the indexer
      projects them to ``context_served`` with ``source='prompttime'``.
    - On a deadline miss: a distinct ``prompt_time_miss`` telemetry event
      (never tagged ``PROMPT_TIME_TOOL``, never typed ``retrieval`` — see
      ``operations/prompt_time_retrieval``'s module docstring for why that
      distinction matters) plus an info line in the hooks log, so a run of
      misses is visible instead of silently re-paying the embedding deadline
      every turn.
    """
    try:
        from thinkweave.operations.prompt_time_retrieval import (
            PROMPT_TIME_MISS,
            PROMPT_TIME_TOOL,
            build_enrichment,
        )

        block, served_ids, missed = build_enrichment(cfg, session_id, prompt_text)

        if missed:
            _buffer_event(
                cfg.weave_dir,
                session_id,
                {"ts": now, "type": PROMPT_TIME_MISS, "session_id": session_id},
            )
            _log_info(
                "prompt_time_enrichment",
                f"deadline miss for session {session_id}",
            )

        if not block:
            return None

        _buffer_event(
            cfg.weave_dir,
            session_id,
            {
                "ts": now,
                "type": "retrieval",
                "tool": PROMPT_TIME_TOOL,
                "returned_ids": served_ids,
                "chars": len(block),
                "token_est": len(block) // 4,
            },
        )
        return block
    except Exception as e:
        _log_error("prompt_time_enrichment", e)
        return None


def _find_session_note(vm, session_id: str) -> Path | None:
    """Find an existing session note for this Claude Code session.

    Fast path: SQL probe against the indexer's ``notes`` table for any
    ``type='session'`` row whose ``frontmatter`` blob contains
    ``"source_session": "<id>"``. O(rows-with-type-session) substring
    match, no markdown reads, no rglob.

    Slow path: a bounded, sessions-only glob —
    ``projects/*/sessions/*/session.md`` — never a vault-wide walk.
    Candidates are checked newest-first with a hard cap: this path is only
    reached when the index DB is missing, locked, or stale (session note was
    just created and hasn't been indexed yet), and in that just-created case
    the note we want is the most recently modified one, so the common case
    is a single frontmatter read. Session folder names are ``<slug>-<date>``
    (see ``VaultManager``), never derived from the Claude Code session UUID,
    so a name match is impossible — frontmatter is the only place the id
    lives.

    Measured 16s for the previous fallback (``vm.list_notes(note_type=
    SESSION, limit=20)``) on a ~1k-note vault over WSL2's 9P filesystem —
    that helper's ``rglob("*.md")`` reads and parses EVERY note's
    frontmatter across the whole vault (decisions, sources, themes, ...)
    until it accumulates ``limit`` session matches. Scoping the glob to the
    ``sessions/<id>/session.md`` shape skips every non-session note's
    content entirely, bounding the scan to the sessions that actually exist
    instead of the whole vault.
    """
    if not session_id:
        return None

    # Fast path — SQLite probe. Substring LIKE on frontmatter is fine here:
    # ``type='session'`` filter is selective (sessions are a tiny fraction
    # of the notes table) and ``source_session`` values are UUIDs, so the
    # match is unambiguous. We open a read-only connection so a contended
    # write lock (e.g. ``weave index`` running concurrently) never blocks us.
    try:
        import sqlite3

        cfg = vm.config
        if cfg.index_db.exists():
            uri = f"file:{cfg.index_db}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1.0) as db:
                row = db.execute(
                    "SELECT path FROM notes "
                    "WHERE type='session' AND frontmatter LIKE ? "
                    "LIMIT 1",
                    (f'%"source_session": "{session_id}"%',),
                ).fetchone()
                if row and row[0]:
                    p = Path(row[0])
                    abs_p = p if p.is_absolute() else vm.root / p
                    if abs_p.exists():
                        return abs_p
    except Exception:
        # Fall through to the bounded glob on any DB issue.
        pass

    # Slow path — sessions-only glob, no vault-wide rglob of any kind.
    # Newest-first, capped: the stale-index window this backstop covers is
    # "created moments ago", so the target is at (or near) the front. A miss
    # under the cap means "not found" — creation dedupes on source_session,
    # so the worst case is a rare duplicate session note, not data loss.
    try:
        candidates = sorted(
            vm.root.glob("projects/*/sessions/*/session.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for note_path in candidates[:15]:
        try:
            note = vm.read_note(note_path)
            if note.frontmatter.get("source_session") == session_id:
                return note_path
        except Exception:
            continue
    return None


def _ensure_session(cfg, session_id: str, hook_input: dict) -> None:
    """Create session note on first event, index it once for MCP discoverability."""
    if not session_id:
        return

    from thinkweave.core.schemas import NoteType
    from thinkweave.core.vault import VaultManager

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

    # Index once so MCP tools (weave_search) can find this session mid-conversation
    from thinkweave.core.indexer import Indexer

    idx = Indexer(config=cfg)
    idx.index_file(session_path)
    idx.close()


_EPHEMERAL_CWD_RE = re.compile(r"^(agent-[a-f0-9]{12,}|[a-f0-9-]{32,})$")


def _detect_project(hook_input: dict) -> str:
    """Detect the current project from env var, git, or cwd.

    Priority: THINKWEAVE_PROJECT env var > git repo name > cwd directory name.

    When cwd looks ephemeral (e.g. ``agent-a4701018f1189051e/`` from a
    cloud-agent run, or a bare UUID), fall through to ``_unscoped`` instead
    of letting the runtime's session-id leak in as a project name.
    """
    # PERSONAL_MEM_PROJECT: pre-rename migration fallback (→ thinkweave 2026-06-13).
    env_proj = os.environ.get("THINKWEAVE_PROJECT") or os.environ.get("PERSONAL_MEM_PROJECT")
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

    if _EPHEMERAL_CWD_RE.match(cwd_path.name):
        return "_unscoped"
    return cwd_path.name


def _is_internal(path: str) -> bool:
    """Check if a path is an internal/config file we should ignore."""
    p = path.lower()
    return any(
        x in p
        for x in (".claude/", "claude.md", "claude.local.md", ".weave/", "settings.json")
    )


# ---------------------------------------------------------------------------
# Event buffer — crash-safe append-only JSONL
# ---------------------------------------------------------------------------


def _buffer_event(weave_dir: Path, session_id: str, event: dict) -> None:
    """Append a single event to the JSONL buffer. Atomic at OS level."""
    buf_dir = weave_dir / "buffer"
    buf_dir.mkdir(parents=True, exist_ok=True)
    with open(buf_dir / f"{session_id}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _extract_tool_output_text(hook_input: dict) -> str:
    """Pull the tool's output as a text string from a PostToolUse payload.

    Claude Code's PostToolUse hook delivers the tool result under the
    ``tool_response`` key (not ``tool_output``). For the ``Bash`` tool
    specifically, ``tool_response`` is an *object* shaped like::

        {"stdout": "...", "stderr": "...", "interrupted": false, "isImage": false}

    For other tools (Write/Edit/MCP) it can be a string or an object with
    tool-specific fields. This helper normalises any of those into a single
    text blob the downstream parsers (``_parse_commit_from_output``,
    ``_parse_test_result``, ``_extract_insight_blocks``, retrieval-event
    builder) can scan with regex.

    Order of preference:

    1. ``tool_response`` — current Claude Code key. When a dict, concatenate
       ``stdout`` + ``stderr`` (``git commit`` prints to stdout; ``pytest``
       splits between the two; both regexes are fine on the concatenation).
       When a string, use as-is.
    2. ``tool_output`` — legacy key, kept for back-compat with any older
       harness build or test fixture that still uses it.

    Returns an empty string when nothing usable is present, which downstream
    parsers already treat as a clean no-op.

    Root-cause note: until this normalisation landed, ``_handle_post`` read
    ``tool_output`` and got ``""`` for every Bash invocation — which meant
    ``_parse_commit_from_output`` returned ``None`` and the ``commit``
    subfield was never written. Empirically 0/405 native hook-emitted
    sessions ever carried ``commits[]``. Audit item A1.
    """
    raw = hook_input.get("tool_response")
    if raw is None:
        raw = hook_input.get("tool_output", "")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        stdout = raw.get("stdout", "") or ""
        stderr = raw.get("stderr", "") or ""
        if not isinstance(stdout, str):
            stdout = str(stdout)
        if not isinstance(stderr, str):
            stderr = str(stderr)
        if stdout and stderr:
            return stdout + "\n" + stderr
        return stdout or stderr or ""
    return ""


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


def _read_buffer(weave_dir: Path, session_id: str) -> list[dict]:
    """Read all events from the JSONL buffer for a session."""
    buf_file = weave_dir / "buffer" / f"{session_id}.jsonl"
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


# Buffer I/O lives in thinkweave.core.buffer so MCP tools can call it
# without crossing the surfaces/ → surfaces/ boundary. Re-exported here so
# legacy imports (`from thinkweave.surfaces.hooks.handler import ...`)
# keep working.
from thinkweave.core.buffer import archive_buffer, cleanup_buffer  # noqa: E402, F401


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
    """Check if a bash command runs tests.

    Handles chained commands like ``cd foo && uv run pytest -x``
    by splitting on shell operators and checking each segment.
    """
    _TEST_PREFIXES = ("pytest", "python -m pytest", "uv run pytest", "uv run python -m pytest")
    # Split on shell chain operators (&&, ||, ;) and check each segment
    segments = re.split(r"\s*(?:&&|\|\||;)\s*", command.strip().lower())
    return any(seg.startswith(p) for seg in segments for p in _TEST_PREFIXES)


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

    try:
        from thinkweave.core.config import load_config
        from thinkweave.core.indexer import Indexer
        from thinkweave.core.vault import (
            VaultManager,
            render_frontmatter,
        )

        cfg = load_config()
        vm = VaultManager(config=cfg)

        session_path = _find_session_note(vm, session_id)
        if not session_path:
            # Retrieval-only fallback: there's a buffer (e.g. just retrieval
            # events from an MCP-only agent turn) but no session note yet.
            # Materialise one so ``archive_buffer`` has somewhere to land
            # the retrieval log — without this the buffer would be orphaned
            # and ``context_served`` would never receive an ``onthefly`` row.
            buf_file = cfg.weave_dir / "buffer" / f"{session_id}.jsonl"
            if buf_file.exists() and buf_file.stat().st_size > 0:
                _ensure_session(cfg, session_id, hook_input)
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
        events = _read_buffer(cfg.weave_dir, source_session)

        if not events:
            _output()
            return

        from thinkweave.core.events import extract_deterministic

        result = extract_deterministic(events)

        # Build final frontmatter
        fm = note.frontmatter
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fm["processed"] = True
        fm["processed_at"] = today
        fm["auto_extracted"] = True
        if result.files_touched:
            fm["files_touched"] = result.files_touched
        if result.commits:
            fm["commits"] = result.commits
        if result.test_runs:
            fm["test_runs"] = result.test_runs
        if result.git_branch:
            fm["git_branch"] = result.git_branch
        if result.concepts:
            fm["concepts"] = result.concepts
        if result.decision_skeletons:
            fm["candidate_decisions"] = len(result.decision_skeletons)
        if result.failure_signals:
            fm["has_failures"] = True

        # Build body — summary + candidate insights + decision skeletons
        body_parts = [f"## Summary\n{result.summary}"]
        if result.insights:
            insight_lines = "\n".join(f"\n{ins}" for ins in result.insights)
            body_parts.append(f"## Candidate Insights{insight_lines}")
        if result.decision_skeletons:
            dec_lines = []
            for sk in result.decision_skeletons:
                files = ", ".join(sk.file_paths[:5])
                concepts = f" [{', '.join(sk.concepts)}]" if sk.concepts else ""
                dec_lines.append(f"- **{sk.title}** ({files}){concepts}")
            body_parts.append(f"## Candidate Decisions\n" + "\n".join(dec_lines))
        if result.failure_signals:
            fail_lines = [f"- {fs.title}" for fs in result.failure_signals]
            body_parts.append(f"## Failure Signals\n" + "\n".join(fail_lines))

        session_path.write_text(
            render_frontmatter(fm) + "\n\n"
            + "\n\n".join(body_parts) + "\n",
            encoding="utf-8",
        )

        # Archive buffer → events.jsonl in session folder
        archive_buffer(cfg.weave_dir, source_session, session_path.parent)

        # Index once
        try:
            idx = Indexer(config=cfg)
            idx.index_file(session_path)
            idx.close()
        except Exception as e:
            _log_error("stop/index", e)

        # Stop-hook opportunistic embed deleted 2026-06-06 (plan A1,
        # go-back-to-the-scalable-firefly.md). Embeddings are now driven
        # exclusively by the cron path (`weave index --embed --only-new`);
        # query-time similarity retrieval reads the same cache.
        _output()
    except Exception as e:
        _log_error("stop", e)
        _output()


def _handle_session_start(hook_input: dict) -> None:
    """SessionStart: inject structured project context before the first user turn.

    Emits a ``hookSpecificOutput.additionalContext`` payload (~7–10k tokens)
    built by ``thinkweave.retrieval.context.build_project_context``. Never blocks;
    all exceptions fall through to an empty response.

    Also records a single ``type: startup`` event in the session buffer with
    the set of note IDs the payload contains and the token estimate. This
    feeds the RLVR substrate's ``startup`` source — distinct from
    ``onthefly`` retrievals, and per the design weighted *lower* than
    on-the-fly hits when computing context value (a decision citing a note
    that was only in the startup payload is a weaker "context helped"
    signal than one that fetched the note mid-session).
    """
    try:
        from thinkweave.core.config import load_config
        from thinkweave.retrieval.context import build_project_context

        from thinkweave.operations.retrieval_log import parse_returned_ids

        cfg = load_config()
        project = _detect_project(hook_input)
        payload = build_project_context(cfg, project, budget_tokens=10000)

        # Served note ids — computed once, reused for the RLVR startup event
        # AND the memory-seam guard. (Parsed from the payload *before* the
        # guard is prepended, so the guard's own [[twin]] wikilinks — which
        # reference already-served notes — don't double-count.)
        served_ids = parse_returned_ids(payload)

        # Memory-seam serving lens — NOT a whole-seam dump. Cross-matches the
        # served notes against the flagged-twin index and injects a small
        # guard ONLY when a note in this session's context is the twin of a
        # durable CC memory flagged stale/diverged. Empty string = inject
        # nothing (the common case). Best-effort; never blocks the payload.
        try:
            from thinkweave.synthesis.memory_seam import session_guard_section

            guard = session_guard_section(cfg, served_ids)
        except Exception as e:
            _log_error("session_start_seam", e)
            guard = ""

        # Record the startup event regardless of whether we emit the payload —
        # an empty payload (cold vault) is itself a fact the RLVR row should
        # carry (n_retrievals_onthefly stays 0, startup_token_est = 0).
        try:
            session_id = hook_input.get(
                "session_id", os.environ.get("CLAUDE_SESSION_ID", "")
            )
            if session_id:
                event = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "type": "startup",
                    "returned_ids": served_ids,
                    # Rough token estimate — matches the SessionStart budget
                    # math (CHARS_PER_TOKEN ≈ 4 in retrieval/context.py).
                    "token_est": len(payload) // 4,
                }
                _buffer_event(cfg.weave_dir, session_id, event)
        except Exception as e:
            # Capture is best-effort; never block the payload injection.
            _log_error("session_start_capture", e)

        if not payload.strip() and not guard:
            _output()
            return

        # Guard rides at the TOP — it's a correctness interrupt on notes the
        # model is about to rely on, so it must be seen before the context.
        full = f"{guard}\n{payload}" if guard else payload
        _output(
            additional_context=full,
            hook_event_name="SessionStart",
        )
    except Exception as e:
        _log_error("session_start", e)
        _output()


def _read_stdin() -> dict:
    try:
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except (json.JSONDecodeError, EOFError):
        return {}


def _output(
    system_message: str = "",
    additional_context: str = "",
    hook_event_name: str = "",
) -> None:
    """Write hook response to stdout.

    Args:
        system_message: Legacy ``systemMessage`` channel used by PreToolUse.
        additional_context: Payload for ``hookSpecificOutput.additionalContext``
            (SessionStart). Injected as a system message before the first turn.
        hook_event_name: The Claude Code hook event name (e.g. ``SessionStart``).
            Required when ``additional_context`` is set.
    """
    result: dict = {}
    if system_message:
        result["systemMessage"] = system_message
    if additional_context and hook_event_name:
        result["hookSpecificOutput"] = {
            "hookEventName": hook_event_name,
            "additionalContext": additional_context,
        }
    json.dump(result, sys.stdout)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
