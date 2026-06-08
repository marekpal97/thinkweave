"""``mem pause`` / ``mem resume`` — temporarily disable personal_mem touchpoints.

Plugin disable only pauses the plugin-managed bits (its MCP entry,
commands). The hooks installed by ``mem hooks install`` and the
machine-scope MCP entry / ``~/.claude/CLAUDE.md`` block written by
``mem install`` live outside the plugin manager's control and keep
firing. This pair fills that gap.

Hard-disable: pause physically removes user-scope hooks, the MCP entry,
and the CLAUDE.md block (resume re-runs the idempotent installers
rather than restoring saved bytes, so an upgrade mid-pause doesn't
strand stale config). Vault contents are never touched. Project-scope
hooks are out of scope — ``cd <repo> && mem hooks uninstall`` covers
that case.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from personal_mem.surfaces.cli.install import (
    MARKER,
    _install_claude_md_block,
    _remove_claude_md_block,
    _remove_mcp_entry,
    _restore_mcp_entry,
)
from personal_mem.surfaces.hooks.install import install_hooks, uninstall_hooks


def cmd_pause(args: argparse.Namespace) -> None:
    if args.status:
        if MARKER.exists():
            data = json.loads(MARKER.read_text(encoding="utf-8"))
            print(f"personal_mem is PAUSED (since {data.get('paused_at', '?')}).")
            print(f"  removed: {', '.join(data.get('removed', [])) or '(nothing)'}")
            print(f"  marker:  {MARKER}")
            print("  resume:  mem resume")
        else:
            print("personal_mem is active (no pause marker).")
        return

    if MARKER.exists():
        print("personal_mem is already paused. Run `mem resume` first.")
        sys.exit(1)

    removed: list[str] = []
    uninstall_hooks(project_dir="", scope="user", dry_run=False)
    removed.append("user-scope hooks")
    if _remove_mcp_entry():
        removed.append("MCP entry")
    if _remove_claude_md_block():
        removed.append("CLAUDE.md block")

    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(
        json.dumps(
            {
                "paused_at": datetime.now(timezone.utc).isoformat(),
                "removed": removed,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print()
    print("Paused personal_mem:")
    for item in removed:
        print(f"  - {item}: removed")
    print(f"  - marker: {MARKER}")
    print()
    print("Restart Claude Code for changes to take effect. `mem resume` to undo.")
    print("Note: project-scope hooks (in <repo>/.claude/settings.local.json) survive —")
    print("      cd to the repo and `mem hooks uninstall` if you want those gone too.")


def cmd_resume(args: argparse.Namespace) -> None:
    if not MARKER.exists():
        print("personal_mem is not paused (no marker found).")
        return
    data = json.loads(MARKER.read_text(encoding="utf-8"))
    removed = data.get("removed", [])

    if "user-scope hooks" in removed:
        install_hooks(project_dir="", scope="user", dry_run=False)
    if "MCP entry" in removed:
        _restore_mcp_entry()
    if "CLAUDE.md block" in removed:
        _install_claude_md_block(yes=True)

    MARKER.unlink()
    print()
    print("Resumed personal_mem. Restart Claude Code for changes to take effect.")
