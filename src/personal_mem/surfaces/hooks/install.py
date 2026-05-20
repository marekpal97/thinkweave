"""Install/uninstall personal_mem hooks into Claude Code settings.

Merge pattern — reads existing settings, appends hooks,
preserves permissions. Non-destructive.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

# Substrings that identify a personal_mem hook command in settings,
# across every historical form this project has written. On reinstall,
# any stored command matching one of these gets rewritten in place to
# the current absolute-path form — so upgrading is always a single
# `mem hooks install` away regardless of which variant is stuck in the
# file.
#
#   - `mem-hook`: console-script name, current form (and the brief
#     bare-name form shipped before absolute-path resolution).
#   - `run_hook.sh`: the original bash wrapper, deleted in favor of the
#     entry point.
#   - `personal_mem.surfaces.hooks.handler`: `python -m personal_mem.surfaces.hooks.handler`
#     entries from even earlier installs.
HOOK_MARKERS = (
    "mem-hook",
    "run_hook.sh",
    "personal_mem.surfaces.hooks.handler",
    "personal_mem.hooks.handler",  # legacy (pre-restructure)
)


def _resolve_hook_cmd() -> str:
    """Return an absolute path to the `mem-hook` console script.

    Resolution order:
      1. `shutil.which("mem-hook")` — finds it via PATH and picks up the
         right extension (`.exe` on Windows) automatically.
      2. `Path(sys.executable).parent / "mem-hook"[.exe]` — pip/uv install
         console scripts alongside the python that ran the install, so
         the bin/Scripts directory of the current interpreter is the
         canonical fallback when PATH is sparse.
      3. Bare `"mem-hook"` — last-resort, relies on whatever shell
         Claude Code spawns hooks through finding it on PATH at fire
         time. Only reached if the entry point isn't installed anywhere
         discoverable, which shouldn't happen if `mem hooks install`
         itself resolved.

    The stored command is an absolute path, so Claude Code's hook
    dispatch (which goes through `/bin/sh` on Unix or `cmd.exe` on
    Windows) never depends on the shell's PATH inheriting whatever
    environment the install ran under.
    """
    resolved = shutil.which("mem-hook")
    if resolved:
        return resolved

    script_dir = Path(sys.executable).parent
    for name in ("mem-hook", "mem-hook.exe"):
        candidate = script_dir / name
        if candidate.exists():
            return str(candidate)

    return "mem-hook"


def _settings_path(project_dir: str = "") -> Path:
    """Find the settings.local.json path."""
    if project_dir:
        return Path(project_dir) / ".claude" / "settings.local.json"
    # Default: current working directory
    return Path.cwd() / ".claude" / "settings.local.json"


def install_hooks(project_dir: str = "") -> None:
    """Install personal_mem hooks into .claude/settings.local.json."""
    settings_path = _settings_path(project_dir)
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing settings
    settings: dict = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))

    hooks = settings.setdefault("hooks", {})
    hook_cmd = _resolve_hook_cmd()

    # SessionStart hook — injects project context before the first user turn
    session_start_hooks = hooks.setdefault("SessionStart", [])
    _ensure_hook(session_start_hooks, "", f"{hook_cmd} session_start")

    # UserPromptSubmit hook — captures every user prompt as a JSONL event,
    # promoting prompts into a first-class primitive (Phase 4 E).
    user_prompt_hooks = hooks.setdefault("UserPromptSubmit", [])
    _ensure_hook(user_prompt_hooks, "", f"{hook_cmd} user_prompt_submit")

    # PreToolUse: previously injected "related vault notes" before each
    # Write/Edit. Retired — redundant with SessionStart context and the
    # filename-stem heuristic produced noisy hits. Strip any stale entry
    # left over from earlier installs.
    _strip_personal_mem_hooks(hooks, "PreToolUse")

    # PostToolUse hook — two matchers:
    #   1. ``Write|Edit|Bash`` — the action-tool gate that feeds the
    #      session-event buffer (files touched, commits, test runs).
    #   2. ``mcp__personal-mem__.*`` — every personal_mem MCP call. The
    #      retrieval-only subset (``mem_search``, ``mem_context``,
    #      ``mem_graph``, ``mem_read``, ``mem_timeline``,
    #      ``mem_project_snapshot``) feeds the RLVR context-served
    #      substrate via ``operations/retrieval_log.RETRIEVAL_TOOLS``.
    #      The handler's in-process gate (``_handle_post``:97–100)
    #      filters out non-retrieval MCP tools (``mem_create`` etc.) so
    #      the regex matcher is safe and cheap — the cost of an unmatched
    #      MCP call is one no-op hook invocation. Claude Code's matcher
    #      string accepts regex; the dot-star form matches the
    #      ``mcp__<server>__<tool>`` naming convention emitted by the
    #      MCP transport.
    post_hooks = hooks.setdefault("PostToolUse", [])
    _ensure_hook(
        post_hooks,
        "Write|Edit|Bash",
        f"{hook_cmd} post_tool_use",
        slot="action",
    )
    _ensure_hook(
        post_hooks,
        "mcp__personal-mem__.*",
        f"{hook_cmd} post_tool_use",
        slot="mcp",
    )

    # Stop hook — gates session exit for knowledge extraction
    stop_hooks = hooks.setdefault("Stop", [])
    _ensure_hook(stop_hooks, "", f"{hook_cmd} stop")

    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(
        f"Hooks installed at {settings_path}\n"
        "  SessionStart hook will inject ~7–10k tokens of project context "
        "on the next Claude Code session."
    )


def uninstall_hooks(project_dir: str = "") -> None:
    """Remove personal_mem hooks from .claude/settings.local.json."""
    settings_path = _settings_path(project_dir)
    if not settings_path.exists():
        print("No settings file found.")
        return

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    hooks = settings.get("hooks", {})

    for hook_type in (
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
    ):
        entries = hooks.get(hook_type, [])
        hooks[hook_type] = [
            entry
            for entry in entries
            if not any(
                _is_personal_mem_hook(h.get("command", ""))
                for h in entry.get("hooks", [])
            )
        ]
        # Clean up empty arrays
        if not hooks[hook_type]:
            del hooks[hook_type]

    if not hooks:
        del settings["hooks"]

    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"Hooks removed from {settings_path}")


def _is_personal_mem_hook(command: str) -> bool:
    """True if `command` is a personal_mem hook in any historical form."""
    if not command:
        return False
    return any(marker in command for marker in HOOK_MARKERS)


def _strip_personal_mem_hooks(hooks: dict, hook_type: str) -> None:
    """Remove every personal_mem entry under `hook_type`, leaving foreign
    hooks untouched. Used to retire a hook phase we no longer install
    (currently PreToolUse) — re-running `mem hooks install` cleans the
    stale entry out of existing settings without disturbing anything else.
    """
    entries = hooks.get(hook_type, [])
    pruned = [
        entry
        for entry in entries
        if not any(
            _is_personal_mem_hook(h.get("command", ""))
            for h in entry.get("hooks", [])
        )
    ]
    if pruned:
        hooks[hook_type] = pruned
    elif hook_type in hooks:
        del hooks[hook_type]


def _ensure_hook(entries: list, matcher: str, command: str, *, slot: str | None = None) -> None:
    """Add a hook entry, or rewrite any existing personal_mem hook in place.

    Matches any form this project has ever written (see HOOK_MARKERS),
    so reinstalling always converges to the current absolute-path form
    regardless of which historical variant is stored in the file.

    ``slot``: when a single hook phase has more than one personal_mem
    entry (PostToolUse owns two — the ``Write|Edit|Bash`` action gate
    and the ``mcp__personal-mem__.*`` MCP gate), we need to disambiguate
    which existing entry to rewrite. The slot is matched by the entry's
    ``matcher`` string, so legacy single-entry installs migrate cleanly:
    the first personal_mem entry that matches the requested slot's
    matcher is rewritten in place; any unmatched-slot install appends a
    fresh entry. With no slot, the first personal_mem entry found is
    rewritten — preserving the original single-matcher contract for
    SessionStart / UserPromptSubmit / Stop, which only ever own one
    entry per phase.
    """
    for entry in entries:
        entry_matcher = entry.get("matcher", "")
        # Slot-aware: only rewrite an entry that already targets this
        # slot. Lets a single phase own multiple personal_mem entries
        # (PostToolUse: action gate + MCP gate) without each install
        # call clobbering the other.
        if slot is not None and entry_matcher != matcher:
            continue
        for hook in entry.get("hooks", []):
            if _is_personal_mem_hook(hook.get("command", "")):
                hook["command"] = command
                # Snap matcher to the canonical value too, in case a
                # legacy install wrote a stale matcher string.
                entry["matcher"] = matcher
                return

    entries.append(
        {
            "matcher": matcher,
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": 5,
                }
            ],
        }
    )
