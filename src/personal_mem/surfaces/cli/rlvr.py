"""``mem rlvr export`` — stream the RLVR decision-context export as JSONL.

One line per decision, schema defined in
``personal_mem.operations.rlvr_export.RLVRRow``. Composable from the shell::

    mem rlvr export --project personal_mem --committed-only > train.jsonl
    mem rlvr export | jq 'select(.prediction.match == "confirmed")'

No MCP parity — exports are batch shell operations, not query primitives
agents reach for mid-conversation. Agents that want one row should use the
Python API directly (``rlvr_export.assemble_row``).
"""

from __future__ import annotations

import argparse
import json
import sys

from personal_mem.core.config import load_config


def cmd_rlvr(args: argparse.Namespace) -> None:
    """Dispatch the ``mem rlvr <action>`` subcommand."""
    action = getattr(args, "rlvr_action", None)
    if action == "export":
        _cmd_export(args)
    else:
        # No action given — print help.
        print("usage: mem rlvr {export} [args...]", file=sys.stderr)
        sys.exit(2)


def _cmd_export(args: argparse.Namespace) -> None:
    from personal_mem.operations.rlvr_export import export_rows

    cfg = load_config()
    project = args.project or ""
    # Stream — no buffering. A vault with thousands of decisions should
    # still produce its first row immediately.
    count = 0
    for row in export_rows(
        cfg,
        project=project,
        since=args.since or "",
        until=args.until or "",
        committed_only=bool(args.committed_only),
    ):
        print(json.dumps(row))
        count += 1

    # Status line goes to stderr so the JSONL on stdout stays clean for
    # downstream pipes. Match the shell convention "stdout is data; stderr
    # is meta".
    if args.verbose:
        print(f"rlvr export: {count} row(s) emitted", file=sys.stderr)
