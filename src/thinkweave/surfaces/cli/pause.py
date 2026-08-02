"""``weave pause`` / ``weave resume`` — temporarily disable thinkweave touchpoints.

Plugin disable only pauses the plugin-managed bits (its MCP entry,
commands). The hooks installed by ``weave hooks install`` and the
machine-scope MCP entry / ``~/.claude/CLAUDE.md`` block written by
``weave install`` live outside the plugin manager's control and keep
firing. This pair fills that gap.

Hard-disable: pause physically removes user-scope hooks, the MCP entry,
and the CLAUDE.md block (resume re-runs the idempotent installers
rather than restoring saved bytes, so an upgrade mid-pause doesn't
strand stale config). Vault contents are never touched. Project-scope
hooks are out of scope — ``cd <repo> && weave hooks uninstall`` covers
that case.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from thinkweave.core.harness import active as _profile
from thinkweave.surfaces.cli.install import (
    _install_claude_md_block,
    _marker,
    _remove_claude_md_block,
    _remove_mcp_entry,
    _restore_mcp_entry,
)
from thinkweave.surfaces.hooks.install import install_hooks, uninstall_hooks

#: How the marker names the instructions-file block. Harness-neutral, because
#: the file it lives in is per-harness (CLAUDE.md / AGENTS.md).
INSTRUCTIONS_BLOCK = "instructions block"


def cmd_pause(args: argparse.Namespace) -> None:
    if args.status:
        if _marker().exists():
            data = json.loads(_marker().read_text(encoding="utf-8"))
            print(f"thinkweave is PAUSED (since {data.get('paused_at', '?')}).")
            print(f"  removed: {', '.join(data.get('removed', [])) or '(nothing)'}")
            print(f"  marker:  {_marker()}")
            print("  resume:  weave resume")
        else:
            print("thinkweave is active (no pause marker).")
        return

    if _marker().exists():
        print("thinkweave is already paused. Run `weave resume` first.")
        sys.exit(1)

    removed: list[str] = []
    # `removed` is resume's instruction list, so it must record only what was
    # actually taken away. On a harness with no lifecycle hooks the uninstall
    # is a no-op — claiming it anyway would send `weave resume` into the hooks
    # installer, which refuses, stranding the resume half-done.
    if _profile().hooks:
        uninstall_hooks(project_dir="", scope="user", dry_run=False)
        removed.append("user-scope hooks")
    if _remove_mcp_entry():
        removed.append("MCP entry")
    if _remove_claude_md_block():
        removed.append(INSTRUCTIONS_BLOCK)

    _marker().parent.mkdir(parents=True, exist_ok=True)
    _marker().write_text(
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
    print("Paused thinkweave:")
    for item in removed:
        print(f"  - {item}: removed")
    print(f"  - marker: {_marker()}")
    print()
    print(f"Restart {_profile().cli_bin} for changes to take effect. `weave resume` to undo.")
    if _profile().hooks:
        rel = _profile().project_settings_relpath.as_posix()
        print(f"Note: project-scope hooks (in <repo>/{rel}) survive —")
        print("      cd to the repo and `weave hooks uninstall` if you want those gone too.")


def cmd_resume(args: argparse.Namespace) -> None:
    if not _marker().exists():
        print("thinkweave is not paused (no marker found).")
        return
    data = json.loads(_marker().read_text(encoding="utf-8"))
    removed = data.get("removed", [])

    if "user-scope hooks" in removed:
        install_hooks(project_dir="", scope="user", dry_run=False)
    if "MCP entry" in removed:
        _restore_mcp_entry()
    # "CLAUDE.md block" is the pre-#106 spelling — markers written by an
    # older version are still on disk on paused machines.
    if {INSTRUCTIONS_BLOCK, "CLAUDE.md block"} & set(removed):
        _install_claude_md_block(yes=True)

    _marker().unlink()
    print()
    print(f"Resumed thinkweave. Restart {_profile().cli_bin} for changes to take effect.")
