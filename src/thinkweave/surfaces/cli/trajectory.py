"""``weave trajectory judge`` — the deterministic outcome-judge rail (issue #60).

The Python side of the phase-2 ``dream-outcome-worker``. One action:

- ``weave trajectory judge [--phase both|1|2] [--limit N] [--json]`` — scan
  loop-run trajectory notes with a ``pr_url``, fetch each PR's state via ``gh``
  (and, for phase-2, ``git`` blame/revert signals), classify deterministically,
  and append a ``prediction_history``-shaped ``{outcome, judged_at, reason,
  phase}`` entry + an ``outcome_label`` frontmatter field. Idempotent: an
  already-judged phase is never re-appended.

All the classification/idempotency/window logic lives in
``operations/trajectory_outcome`` (pure, unit-tested); the ``gh``/``git`` calls
are isolated behind that module's fetchers. This CLI is the thin surface the
worker agent (``agents/dream-outcome-worker.md``) invokes and relays.
"""

from __future__ import annotations

import argparse
import json
import sys

from thinkweave.core.config import load_config


def cmd_trajectory(args: argparse.Namespace) -> None:
    """Dispatch the ``weave trajectory <action>`` subcommand."""
    action = getattr(args, "trajectory_action", None)
    if action == "judge":
        _cmd_judge(args)
    else:
        print("usage: weave trajectory judge [--phase both|1|2] [--limit N] [--json]", file=sys.stderr)
        sys.exit(2)


def _cmd_judge(args: argparse.Namespace) -> None:
    from thinkweave.operations.trajectory_outcome import judge_trajectories

    cfg = load_config()
    result = judge_trajectories(
        cfg,
        phase=getattr(args, "phase", "both"),
        limit=getattr(args, "limit", None),
    )

    if getattr(args, "json", False):
        print(json.dumps(result))
        sys.exit(1 if result.get("errors") else 0)

    judged = result.get("judged", [])
    skipped = result.get("skipped", [])
    errors = result.get("errors", [])
    print(f"trajectory judge · {len(judged)} judged · {len(skipped)} skipped · {len(errors)} errors")
    for j in judged:
        print(f"  {j['id']} · phase {j['phase']} → {j['outcome']}")
    for e in errors:
        print(f"  ! {e.get('id', '?')}: {e.get('reason', '')}", file=sys.stderr)
    if errors:
        sys.exit(1)
