"""Install/uninstall personal_mem hooks into Claude Code settings.

Follows hive_swarm's merge pattern — reads existing settings,
appends hooks, preserves permissions. Non-destructive.
"""

from __future__ import annotations

import json
from pathlib import Path

# Console-script entry point declared in pyproject.toml as
# `mem-hook = personal_mem.hooks.handler:main`. pip/uv materialize this
# as `mem-hook` on Unix and `mem-hook.exe` on Windows, so writing the
# bare name into settings.local.json makes hook dispatch cross-platform
# without any shell wrapper.
HOOK_CMD = "mem-hook"

# Legacy hook command fragments that earlier installs may have written
# into settings.local.json. On reinstall we rewrite any entry whose
# command contains one of these to the modern `mem-hook` form.
LEGACY_HOOK_MARKERS = ("run_hook.sh", "personal_mem.hooks.handler")


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

    # SessionStart hook — injects project context before the first user turn
    session_start_hooks = hooks.setdefault("SessionStart", [])
    _ensure_hook(session_start_hooks, "", f"{HOOK_CMD} session_start")

    # PreToolUse hook
    pre_hooks = hooks.setdefault("PreToolUse", [])
    _ensure_hook(pre_hooks, "Write|Edit", f"{HOOK_CMD} pre_tool_use")

    # PostToolUse hook
    post_hooks = hooks.setdefault("PostToolUse", [])
    _ensure_hook(post_hooks, "Write|Edit|Bash", f"{HOOK_CMD} post_tool_use")

    # Stop hook — gates session exit for knowledge extraction
    stop_hooks = hooks.setdefault("Stop", [])
    _ensure_hook(stop_hooks, "", f"{HOOK_CMD} stop")

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

    for hook_type in ("SessionStart", "PreToolUse", "PostToolUse", "Stop"):
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
    """True if `command` is a personal_mem hook — current or legacy form."""
    if not command:
        return False
    if command.startswith(HOOK_CMD):
        return True
    return any(marker in command for marker in LEGACY_HOOK_MARKERS)


def _ensure_hook(entries: list, matcher: str, command: str) -> None:
    """Add a hook entry, or rewrite any existing personal_mem hook in place.

    Matches on both the modern `mem-hook` command and any legacy
    `run_hook.sh`/`-m personal_mem.hooks.handler` entries from earlier
    installs, so reinstalling after an upgrade migrates stale commands
    to the new entry-point form rather than appending duplicates.
    """
    for entry in entries:
        for hook in entry.get("hooks", []):
            if _is_personal_mem_hook(hook.get("command", "")):
                hook["command"] = command
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
