"""Install/uninstall personal_mem hooks into Claude Code settings.

Follows hive_swarm's merge pattern — reads existing settings,
appends hooks, preserves permissions. Non-destructive.
"""

from __future__ import annotations

import json
from pathlib import Path


def _get_hook_command() -> str:
    """Get the absolute path to the run_hook.sh script."""
    hook_sh = Path(__file__).parent / "run_hook.sh"
    return str(hook_sh.resolve())


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
    hook_cmd = _get_hook_command()

    # PreToolUse hook
    pre_hooks = hooks.setdefault("PreToolUse", [])
    _ensure_hook(pre_hooks, "Write|Edit", f"{hook_cmd} pre_tool_use")

    # PostToolUse hook
    post_hooks = hooks.setdefault("PostToolUse", [])
    _ensure_hook(post_hooks, "Write|Edit|Bash", f"{hook_cmd} post_tool_use")

    # Stop hook — gates session exit for knowledge extraction
    stop_hooks = hooks.setdefault("Stop", [])
    _ensure_hook(stop_hooks, "", f"{hook_cmd} stop")

    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"Hooks installed at {settings_path}")


def uninstall_hooks(project_dir: str = "") -> None:
    """Remove personal_mem hooks from .claude/settings.local.json."""
    settings_path = _settings_path(project_dir)
    if not settings_path.exists():
        print("No settings file found.")
        return

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    hooks = settings.get("hooks", {})
    hook_cmd = _get_hook_command()

    for hook_type in ("PreToolUse", "PostToolUse", "Stop"):
        entries = hooks.get(hook_type, [])
        hooks[hook_type] = [
            entry
            for entry in entries
            if not any(
                h.get("command", "").startswith(hook_cmd)
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


def _ensure_hook(entries: list, matcher: str, command: str) -> None:
    """Add a hook entry if it doesn't already exist."""
    # Check if our hook is already installed
    for entry in entries:
        for hook in entry.get("hooks", []):
            if hook.get("command", "").startswith(command.split()[0]):
                # Already installed — update command
                hook["command"] = command
                return

    # Add new entry
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
