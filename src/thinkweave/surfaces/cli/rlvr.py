"""``weave rlvr export`` — stream the RLVR decision-context export as JSONL.

One line per decision (default) or one line per prediction-history entry
(``--explode-history``). Schema defined in
``thinkweave.operations.rlvr_export.RLVRRow``. Composable from the shell::

    weave rlvr export --project thinkweave --committed-only > train.jsonl
    weave rlvr export | jq 'select(.prediction.match == "confirmed")'
    weave rlvr export --explode-history > trajectories.jsonl

``--explode-history`` emits the trajectory shape for RL training: one row per
prediction-history entry, with ``prediction.match``/``judged_at`` carrying
the per-entry values, a new ``prediction.reason`` field, and a 0-based
``prediction.entry_index``. A decision with no history still emits exactly
one row (preserves the "one decision = at least one row" invariant).

No MCP parity — exports are batch shell operations, not query primitives
agents reach for mid-conversation. Agents that want one row should use the
Python API directly (``rlvr_export.assemble_row``).
"""

from __future__ import annotations

import argparse
import json
import sys

from thinkweave.core.config import load_config


def cmd_rlvr(args: argparse.Namespace) -> None:
    """Dispatch the ``weave rlvr <action>`` subcommand."""
    action = getattr(args, "rlvr_action", None)
    if action == "export":
        _cmd_export(args)
    else:
        # No action given — print help.
        print("usage: weave rlvr {export} [args...]", file=sys.stderr)
        sys.exit(2)


def _cmd_export(args: argparse.Namespace) -> None:
    from thinkweave.operations.rlvr_export import export_rows, export_trajectory_rows

    cfg = load_config()
    project = args.project or ""
    explode = bool(getattr(args, "explode_history", False))

    def _emit(row: dict) -> int:
        if explode:
            n = 0
            for sub_row in _explode_row(row):
                print(json.dumps(sub_row))
                n += 1
            return n
        print(json.dumps(row))
        return 1

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
        count += _emit(row)

    # Loop task-trajectories (issue #60) stream after decisions, same schema.
    # `--committed-only` is a decision-only filter (trajectories have no
    # commit gate) so it does not apply here.
    for row in export_trajectory_rows(
        cfg,
        project=project,
        since=args.since or "",
        until=args.until or "",
    ):
        count += _emit(row)

    # Status line goes to stderr so the JSONL on stdout stays clean for
    # downstream pipes. Match the shell convention "stdout is data; stderr
    # is meta".
    if args.verbose:
        print(f"rlvr export: {count} row(s) emitted", file=sys.stderr)


def _explode_row(row: dict) -> list[dict]:
    """Explode one decision row into one row per prediction-history entry.

    Decisions without history still emit exactly one row (empty prediction
    fields) so the "one decision = at least one row" invariant holds. The
    ``prediction.history`` list is omitted in exploded rows (redundant — the
    consumer can reconstruct it from ``entry_index``).
    """
    prediction = row.get("prediction") or {}
    text = prediction.get("text", "") or ""
    history = prediction.get("history") or []

    # Carry over everything except the prediction block, which we rebuild
    # per-entry. ``dict(row)`` is shallow; the nested outcome/context dicts
    # are denormalized (shared by reference) across exploded rows — fine for
    # JSON serialization.
    base = {k: v for k, v in row.items() if k != "prediction"}

    if not history:
        return [
            {
                **base,
                "prediction": {
                    "text": text,
                    "match": "",
                    "judged_at": "",
                    "reason": "",
                    "entry_index": 0,
                },
            }
        ]

    out: list[dict] = []
    for i, entry in enumerate(history):
        out.append(
            {
                **base,
                "prediction": {
                    "text": text,
                    "match": entry.get("match", "") or "",
                    "judged_at": entry.get("judged_at", "") or "",
                    "reason": entry.get("reason", "") or "",
                    "entry_index": i,
                },
            }
        )
    return out
