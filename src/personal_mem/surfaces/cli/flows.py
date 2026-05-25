"""``mem flow`` — list / show / run named workflow pipelines."""

from __future__ import annotations

import argparse
import sys

from personal_mem.core.config import load_config


def cmd_flow(args: argparse.Namespace) -> None:
    """Run a named workflow pipeline."""
    from personal_mem.flows import flows_path, load_flows, run_flow

    cfg = load_config()
    flows = load_flows(cfg)

    action = args.flow_action or "list"

    if action == "list":
        if not flows:
            print(f"No flows defined. Create {flows_path(cfg)} to add one.")
            return
        print(f"Flows ({len(flows)}):\n")
        for name, spec in sorted(flows.items()):
            desc = spec.description or "(no description)"
            print(f"  {name:24s} {desc}")
        return

    if action == "show":
        if args.name not in flows:
            print(f"Unknown flow: {args.name}")
            sys.exit(1)
        spec = flows[args.name]
        print(f"{spec.name}: {spec.description}")
        print(f"  on_error: {spec.on_error}")
        if spec.log:
            print(f"  log: {spec.log}")
        for i, stage in enumerate(spec.stages):
            print(f"  stage {i + 1}: {stage.run}")
            if stage.sleep:
                print(f"    sleep {stage.sleep}s")
        return

    if action == "run":
        if args.name not in flows:
            print(f"Unknown flow: {args.name}")
            sys.exit(1)
        code = run_flow(flows[args.name], dry_run=args.dry_run)
        sys.exit(code if not args.dry_run else 0)
