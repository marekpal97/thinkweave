"""Install/uninstall thinkweave hooks into Claude Code settings.

Merge pattern — reads existing settings, appends hooks,
preserves permissions. Non-destructive.
"""

from __future__ import annotations

import copy
import difflib
import json
import shutil
import sys
from pathlib import Path

# Substrings that identify a thinkweave hook command in settings,
# across every historical form this project has written. On reinstall,
# any stored command matching one of these gets rewritten in place to
# the current absolute-path form — so upgrading is always a single
# `weave hooks install` away regardless of which variant is stuck in the
# file.
#
#   - `weave-hook`: console-script name, current form (and the brief
#     bare-name form shipped before absolute-path resolution).
#   - `run_hook.sh`: the original bash wrapper, deleted in favor of the
#     entry point.
#   - `thinkweave.surfaces.hooks.handler`: `python -m thinkweave.surfaces.hooks.handler`
#     entries from even earlier installs.
HOOK_MARKERS = (
    "weave-hook",
    "run_hook.sh",
    "thinkweave.surfaces.hooks.handler",
    "thinkweave.hooks.handler",  # legacy (pre-restructure)
)


def _resolve_hook_cmd() -> str:
    """Return an absolute path to the `weave-hook` console script.

    Resolution order:
      1. `shutil.which("weave-hook")` — finds it via PATH and picks up the
         right extension (`.exe` on Windows) automatically.
      2. `Path(sys.executable).parent / "weave-hook"[.exe]` — pip/uv install
         console scripts alongside the python that ran the install, so
         the bin/Scripts directory of the current interpreter is the
         canonical fallback when PATH is sparse.
      3. Bare `"weave-hook"` — last-resort, relies on whatever shell
         Claude Code spawns hooks through finding it on PATH at fire
         time. Only reached if the entry point isn't installed anywhere
         discoverable, which shouldn't happen if `weave hooks install`
         itself resolved.

    The stored command is an absolute path, so Claude Code's hook
    dispatch (which goes through `/bin/sh` on Unix or `cmd.exe` on
    Windows) never depends on the shell's PATH inheriting whatever
    environment the install ran under.
    """
    resolved = shutil.which("weave-hook")
    if resolved:
        return resolved

    script_dir = Path(sys.executable).parent
    for name in ("weave-hook", "weave-hook.exe"):
        candidate = script_dir / name
        if candidate.exists():
            return str(candidate)

    return "weave-hook"


def _settings_path(project_dir: str = "") -> Path:
    """Find the settings.local.json path."""
    if project_dir:
        return Path(project_dir) / ".claude" / "settings.local.json"
    # Default: current working directory
    return Path.cwd() / ".claude" / "settings.local.json"


def _settings_path_for_scope(scope: str, project_dir: str = "") -> Path:
    """Dispatch the settings.json target by install scope.

    ``project`` — the legacy default. Writes to ``<cwd or project_dir>/.claude/
    settings.local.json``. Fires only inside that project tree.

    ``user`` — machine-scope. Writes to ``~/.claude/settings.json`` (note:
    non-local, the per-user file). Fires in every Claude Code session on
    this machine. Used by the legacy `/onboard` install path to mirror
    what the plugin manifest provides for free.
    """
    if scope == "project":
        return _settings_path(project_dir)
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    raise ValueError(
        f"unknown scope {scope!r}; expected one of: 'project', 'user'"
    )


def _build_installed_settings(existing: dict) -> dict:
    """Return a settings dict with thinkweave hooks merged in.

    Pure function — operates on a deep copy of ``existing``, never mutates
    the input. The full body of the historic merge (ensure SessionStart /
    UserPromptSubmit / PostToolUse {action, mcp} / Stop, strip retired
    PreToolUse) lives here so it can be exercised both for the real write
    and for the dry-run diff with no behavioural drift between them.
    """
    settings = copy.deepcopy(existing)
    hooks = settings.setdefault("hooks", {})
    hook_cmd = _resolve_hook_cmd()

    # SessionStart hook — injects project context before the first user turn
    session_start_hooks = hooks.setdefault("SessionStart", [])
    _ensure_hook(session_start_hooks, "", f"{hook_cmd} session_start")

    # UserPromptSubmit hook — captures every user prompt as a JSONL event,
    # promoting prompts into a first-class primitive (Phase 4 E), and runs R2
    # prompt-time retrieval enrichment. Timeout raised to 10s (above the default
    # 5s) to cover the bounded embedding deadline (~3s) + render + write-back;
    # the handler degrades to FTS / silent no-op well inside this budget.
    user_prompt_hooks = hooks.setdefault("UserPromptSubmit", [])
    _ensure_hook(
        user_prompt_hooks, "", f"{hook_cmd} user_prompt_submit", timeout=10
    )

    # PreToolUse: previously injected "related vault notes" before each
    # Write/Edit. Retired — redundant with SessionStart context and the
    # filename-stem heuristic produced noisy hits. Strip any stale entry
    # left over from earlier installs.
    _strip_thinkweave_hooks(hooks, "PreToolUse")

    # PostToolUse hook — two matchers:
    #   1. ``Write|Edit|Bash`` — the action-tool gate that feeds the
    #      session-event buffer (files touched, commits, test runs).
    #   2. ``mcp__thinkweave__.*`` — every thinkweave MCP call. The
    #      retrieval-only subset (``weave_search``, ``weave_context``,
    #      ``weave_graph``, ``weave_read``, ``weave_timeline``,
    #      ``weave_project_snapshot``) feeds the RLVR context-served
    #      substrate via ``operations/retrieval_log.RETRIEVAL_TOOLS``.
    #      The handler's in-process gate (``_handle_post``:97–100)
    #      filters out non-retrieval MCP tools (``weave_create`` etc.) so
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
        "mcp__thinkweave__.*",
        f"{hook_cmd} post_tool_use",
        slot="mcp",
    )

    # Stop hook — gates session exit for knowledge extraction
    stop_hooks = hooks.setdefault("Stop", [])
    _ensure_hook(stop_hooks, "", f"{hook_cmd} stop")

    return settings


def _build_uninstalled_settings(existing: dict) -> dict:
    """Return a settings dict with thinkweave hooks removed.

    Pure function — mirrors ``_build_installed_settings`` so uninstall and
    its dry-run share one implementation.
    """
    settings = copy.deepcopy(existing)
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
                _is_thinkweave_hook(h.get("command", ""))
                for h in entry.get("hooks", [])
            )
        ]
        # Clean up empty arrays
        if not hooks[hook_type]:
            del hooks[hook_type]

    if "hooks" in settings and not hooks:
        del settings["hooks"]

    return settings


def _settings_diff(before: dict, after: dict, target: Path) -> str:
    """Unified diff over pretty-printed JSON, suitable for printing."""
    before_text = json.dumps(before, indent=2, sort_keys=False) + "\n"
    after_text = json.dumps(after, indent=2, sort_keys=False) + "\n"
    diff = difflib.unified_diff(
        before_text.splitlines(keepends=True),
        after_text.splitlines(keepends=True),
        fromfile=f"a/{target}",
        tofile=f"b/{target}",
        n=3,
    )
    return "".join(diff)


def install_hooks(
    project_dir: str = "",
    scope: str = "project",
    dry_run: bool = False,
) -> None:
    """Install thinkweave hooks into the chosen Claude Code settings file.

    ``scope`` selects the settings file: ``"project"`` (default — preserves
    the legacy behaviour) writes to ``<project_dir or cwd>/.claude/
    settings.local.json``; ``"user"`` writes to ``~/.claude/settings.json``
    so hooks fire in every Claude Code session on this machine. The
    plugin install path gets global hooks via the plugin manifest; this
    flag is the legacy install path's equivalent.

    ``dry_run=True`` prints the planned unified diff against the current
    settings file and returns without writing.
    """
    settings_path = _settings_path_for_scope(scope, project_dir)

    # Read existing settings (no parent mkdir yet — dry-run must not
    # create the .claude directory either).
    existing: dict = {}
    if settings_path.exists():
        existing = json.loads(settings_path.read_text(encoding="utf-8"))

    planned = _build_installed_settings(existing)

    if dry_run:
        print(f"Would write: {settings_path}")
        diff = _settings_diff(existing, planned, settings_path)
        if diff:
            print(diff, end="")
        else:
            print("(no changes)")
        return

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(planned, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Hooks installed at {settings_path} (scope={scope})\n"
        "  SessionStart hook will inject ~7–10k tokens of project context "
        "on the next Claude Code session."
    )


def uninstall_hooks(
    project_dir: str = "",
    scope: str = "project",
    dry_run: bool = False,
) -> None:
    """Remove thinkweave hooks from the scoped Claude Code settings file."""
    settings_path = _settings_path_for_scope(scope, project_dir)
    if not settings_path.exists():
        print("No settings file found.")
        return

    existing = json.loads(settings_path.read_text(encoding="utf-8"))
    planned = _build_uninstalled_settings(existing)

    if dry_run:
        print(f"Would write: {settings_path}")
        diff = _settings_diff(existing, planned, settings_path)
        if diff:
            print(diff, end="")
        else:
            print("(no changes)")
        return

    settings_path.write_text(
        json.dumps(planned, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Hooks removed from {settings_path} (scope={scope})")


def _is_thinkweave_hook(command: str) -> bool:
    """True if `command` is a thinkweave hook in any historical form."""
    if not command:
        return False
    return any(marker in command for marker in HOOK_MARKERS)


def _strip_thinkweave_hooks(hooks: dict, hook_type: str) -> None:
    """Remove every thinkweave entry under `hook_type`, leaving foreign
    hooks untouched. Used to retire a hook phase we no longer install
    (currently PreToolUse) — re-running `weave hooks install` cleans the
    stale entry out of existing settings without disturbing anything else.
    """
    entries = hooks.get(hook_type, [])
    pruned = [
        entry
        for entry in entries
        if not any(
            _is_thinkweave_hook(h.get("command", ""))
            for h in entry.get("hooks", [])
        )
    ]
    if pruned:
        hooks[hook_type] = pruned
    elif hook_type in hooks:
        del hooks[hook_type]


def _ensure_hook(
    entries: list,
    matcher: str,
    command: str,
    *,
    slot: str | None = None,
    timeout: int = 5,
) -> None:
    """Add a hook entry, or rewrite any existing thinkweave hook in place.

    Matches any form this project has ever written (see HOOK_MARKERS),
    so reinstalling always converges to the current absolute-path form
    regardless of which historical variant is stored in the file.

    ``slot``: when a single hook phase has more than one thinkweave
    entry (PostToolUse owns two — the ``Write|Edit|Bash`` action gate
    and the ``mcp__thinkweave__.*`` MCP gate), we need to disambiguate
    which existing entry to rewrite. The slot is matched by the entry's
    ``matcher`` string, so legacy single-entry installs migrate cleanly:
    the first thinkweave entry that matches the requested slot's
    matcher is rewritten in place; any unmatched-slot install appends a
    fresh entry. With no slot, the first thinkweave entry found is
    rewritten — preserving the original single-matcher contract for
    SessionStart / UserPromptSubmit / Stop, which only ever own one
    entry per phase.
    """
    for entry in entries:
        entry_matcher = entry.get("matcher", "")
        # Slot-aware: only rewrite an entry that already targets this
        # slot. Lets a single phase own multiple thinkweave entries
        # (PostToolUse: action gate + MCP gate) without each install
        # call clobbering the other.
        if slot is not None and entry_matcher != matcher:
            continue
        for hook in entry.get("hooks", []):
            if _is_thinkweave_hook(hook.get("command", "")):
                hook["command"] = command
                # Snap matcher + timeout to the canonical values too, in case a
                # legacy install wrote a stale matcher or the default timeout.
                hook["timeout"] = timeout
                entry["matcher"] = matcher
                return

    entries.append(
        {
            "matcher": matcher,
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": timeout,
                }
            ],
        }
    )
