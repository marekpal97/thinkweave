"""``mem hooks`` — install / uninstall / status."""

from __future__ import annotations

import argparse
import sys

from personal_mem.core.config import load_config


def cmd_hooks(args: argparse.Namespace) -> None:
    if not args.hooks_action:
        print("Usage: mem hooks install|uninstall")
        sys.exit(1)

    if args.hooks_action == "install":
        from personal_mem.surfaces.hooks.install import install_hooks

        project = args.project if hasattr(args, "project") else ""
        install_hooks(project_dir=project)
    elif args.hooks_action == "uninstall":
        from personal_mem.surfaces.hooks.install import uninstall_hooks

        uninstall_hooks()
    elif args.hooks_action == "status":
        cfg = load_config()
        log_path = cfg.mem_dir / "hooks.log"
        if not log_path.exists():
            print("No hook errors recorded.")
            return
        lines = log_path.read_text(encoding="utf-8").splitlines()
        limit = args.limit if hasattr(args, "limit") else 20
        recent = lines[-limit:] if len(lines) > limit else lines
        if not recent:
            print("Hook log is empty.")
        else:
            print(f"Last {len(recent)} lines from {log_path}:\n")
            for line in recent:
                print(line)
