"""``mem intake`` — drop-folder enumerate / archive helpers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def cmd_intake(args: argparse.Namespace) -> None:
    """Drop-folder intake helpers — enumerate / archive.

    Intentionally narrow: just enumeration + archival. LLM-driven work
    (frontmatter parsing, brief writing, concept mapping) stays in the
    skill that calls these. Image backfill is platform-specific and lives
    with each importer skill.
    """
    from personal_mem.sources.intake import (
        archive_to_processed,
        enumerate_inbox,
    )

    action = args.intake_action
    if not action:
        print("Usage: mem intake enumerate <path> | archive <entry> --inbox <root>")
        sys.exit(1)

    if action == "enumerate":
        inbox = Path(args.path).expanduser()
        entries = enumerate_inbox(inbox, archive_name=args.archive_name)
        payload = [
            {
                "path": str(e.path),
                "kind": e.kind,
                "companion_dir": str(e.companion_dir) if e.companion_dir else None,
            }
            for e in entries
        ]
        print(json.dumps(payload))
        return

    if action == "archive":
        entry = Path(args.entry).expanduser()
        inbox_root = Path(args.inbox).expanduser()
        try:
            final_path = archive_to_processed(entry, inbox_root)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(2)
        print(str(final_path))
        return

    print(f"Unknown intake action: {action}", file=sys.stderr)
    sys.exit(1)
