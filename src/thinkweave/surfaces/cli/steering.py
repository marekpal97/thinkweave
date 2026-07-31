"""``weave steering`` — the evidence gate the slow self-improvement loop calls (issue #62).

Two read-only-plus-gate actions, the surface #61 (the weekly improve-arch /
ponytail-audit Routine, not yet built) invokes:

- ``weave steering evidence [--module PATH] [--json]`` — show the computed
  per-module evidence signals (rework/churn, superseded-decision density,
  gate-failure hotspots, hub pressure) from the index. Read-only.
- ``weave steering gate --proposals-json <file> [--json]`` — run a batch of
  candidate proposals through :func:`operations.steering.gate_proposals` and
  print ``{filed, dropped}``. #61 files ONLY what ``filed`` returns; every
  filed proposal already carries its machine-readable evidence block.

All logic lives in ``operations/steering`` (pure functions + one read-only
index scan). This CLI is the thin surface; the ``[steering]`` config knobs steer
the budget + signal weights.
"""

from __future__ import annotations

import argparse
import json
import sys

from thinkweave.core.config import load_config


def cmd_steering(args: argparse.Namespace) -> None:
    """Dispatch ``weave steering <action>``."""
    action = getattr(args, "steering_action", None)
    if action == "evidence":
        _cmd_evidence(args)
    elif action == "gate":
        _cmd_gate(args)
    else:
        print(
            "usage: weave steering {evidence [--module PATH] | "
            "gate --proposals-json FILE} [--json]",
            file=sys.stderr,
        )
        sys.exit(2)


def _cmd_evidence(args: argparse.Namespace) -> None:
    from thinkweave.operations.steering import evidence_signals

    cfg = load_config()
    result = evidence_signals(cfg, module=getattr(args, "module", "") or "")

    if getattr(args, "json", False):
        print(json.dumps(result))
        return

    if "module" in result:
        block = result["evidence"]
        print(f"steering evidence · {result['module']}")
        _print_block(block, indent="  ")
        return

    modules = result.get("modules", [])
    print(f"steering evidence · {len(modules)} module(s) with signal")
    for block in modules:
        print(f"  {block['module']}  (weight {block['weight']})")
        _print_block(block, indent="    ")
    hub = result.get("hub_pressure") or {}
    if hub:
        print("  hub pressure (concept → centrality):")
        for concept, score in hub.items():
            print(f"    {concept}: {score}")


def _print_block(block: dict, *, indent: str) -> None:
    print(
        f"{indent}rework={block['rework_count']} "
        f"fix_rounds={block['fix_rounds']} "
        f"superseded={block['superseded_decisions']} "
        f"gate_failures={block['gate_failures']} "
        f"hub_pressure={block['hub_pressure']}"
    )


def _cmd_gate(args: argparse.Namespace) -> None:
    from pathlib import Path

    from thinkweave.operations.steering import build_evidence_index, gate_proposals

    cfg = load_config()

    raw = Path(args.proposals_json).read_text(encoding="utf-8")
    candidates = json.loads(raw)
    # Accept either a bare list of candidates or a {"candidates": [...]} wrapper.
    if isinstance(candidates, dict):
        candidates = candidates.get("candidates", [])
    if not isinstance(candidates, list):
        print("proposals-json must be a list of candidates (or {candidates: [...]})", file=sys.stderr)
        sys.exit(2)

    index = build_evidence_index(cfg)
    result = gate_proposals(candidates, index, cfg)

    if getattr(args, "json", False):
        print(json.dumps(result))
        return

    filed = result.get("filed", [])
    dropped = result.get("dropped", [])
    print(f"steering gate · {len(filed)} filed · {len(dropped)} dropped")
    for f in filed:
        print(f"  + {f['module']}  (weight {f['weight']})")
    for d in dropped:
        print(f"  - {d['module']}: {d['reason']}")
