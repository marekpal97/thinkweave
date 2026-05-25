"""``mem dream`` — periodic vault-hygiene cycle CLI.

Two actions, mirroring the scan / apply phases of the cycle:

- ``mem dream scan [--json]`` — read-only; emit the action plan. Default
  formats a compact table for interactive inspection; ``--json`` emits
  the raw :class:`DreamCycleScan` payload for skill consumption.
- ``mem dream apply --plan <path>|- [--dry-run] [--json]`` — execute the
  LLM-judged plan. Reads JSON from a file path or stdin. ``--dry-run``
  parses + validates the plan but skips writes.

The intermediate LLM judgment phase lives in the ``/dream`` skill —
``commands/dream.md``. This surface is just the two endpoints.
"""

from __future__ import annotations

import argparse
import json
import sys

from personal_mem.core.config import load_config


def cmd_dream(args: argparse.Namespace) -> None:
    action = getattr(args, "dream_action", None)
    if action == "scan":
        _cmd_scan(args)
    elif action == "apply":
        _cmd_apply(args)
    else:
        print("Usage: mem dream {scan|apply}", file=sys.stderr)
        sys.exit(2)


def _cmd_scan(args: argparse.Namespace) -> None:
    from personal_mem.operations.dream import scan

    cfg = load_config()
    project = args.project or cfg.default_project or ""

    result = scan(
        cfg,
        project=project,
        promotion_cap=args.promotion_cap,
        promotion_threshold=args.promotion_threshold,
    )

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
        sys.exit(1 if result.errors else 0)

    # Human-readable summary — for eyeballing what a cycle would consider.
    print(f"dream scan · {result.cycle_id} · project {project or '(none)'}")
    s = result.stats
    print(
        f"  found: {s.get('drift_pairs', 0)} drift · "
        f"{s.get('promotion_candidates', 0)} promotions "
        f"(cap {result.promotion_cap}) · "
        f"{s.get('theme_candidates', 0)} theme-candidates · "
        f"{s.get('dormant_themes', 0)} dormant · "
        f"{s.get('resolved_themes', 0)} resolved"
    )
    if result.promotion_candidates:
        print("  promotion candidates:")
        for p in result.promotion_candidates[:20]:
            print(f"    {p['count']:>3}  {p['concept']}")
    if result.drift_pairs:
        print("  drift pairs (post-filter):")
        for d in result.drift_pairs:
            print(f"    {d['from']} → {d['to']}  ({d.get('reason', '')})")
    if result.theme_candidates:
        print(f"  theme candidates: {len(result.theme_candidates)}")
        for tc in result.theme_candidates[:10]:
            concepts = ", ".join(tc.get("cluster_concepts") or [])
            print(f"    {tc['candidate_id']}: {concepts}")
    if result.dormant_themes:
        print("  dormant themes:")
        for dt in result.dormant_themes:
            print(f"    {dt['theme_id']}: {dt.get('title', '')}")
    if result.resolved_themes:
        print("  resolved themes:")
        for rt in result.resolved_themes:
            print(f"    {rt['theme_id']}: {rt.get('title', '')}")
    if result.timings:
        parts = " · ".join(
            f"{k} {v:.2f}s" for k, v in sorted(result.timings.items())
        )
        print(f"  timing: {parts}")
    if result.errors:
        print("  errors:")
        for e in result.errors:
            print(f"    ! {e}")
        sys.exit(1)


def _cmd_apply(args: argparse.Namespace) -> None:
    from personal_mem.operations.dream import apply

    # Read plan from file or stdin.
    raw: str
    if args.plan == "-":
        raw = sys.stdin.read()
    else:
        with open(args.plan, encoding="utf-8") as f:
            raw = f.read()
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON plan — {e}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(plan, dict):
        print("error: plan must be a JSON object", file=sys.stderr)
        sys.exit(2)

    if args.dry_run:
        # Validation-only: count what would happen, never touch the vault.
        summary = {
            "merges": len(plan.get("merges") or []),
            "promotions": len(plan.get("promotions") or []),
            "theme_promotions": len(plan.get("theme_promotions") or []),
            "candidates_archived": len(plan.get("candidates_archived") or []),
            "theme_status_changes": len(plan.get("theme_status_changes") or []),
            "essence_rewrites": len(plan.get("essence_rewrites") or []),
        }
        if args.json:
            print(json.dumps({"dry_run": True, "would_apply": summary}, indent=2))
        else:
            print("dream apply (dry-run) — would apply:")
            for k, v in summary.items():
                print(f"  {k}: {v}")
        sys.exit(0)

    cfg = load_config()
    project = args.project or cfg.default_project or ""
    cycle_id = plan.get("cycle_id") or None

    result = apply(cfg, plan=plan, project=project, cycle_id=cycle_id)

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
        sys.exit(1 if result.errors else 0)

    print(f"dream apply · {result.cycle_id} · project {project or '(none)'}")
    print(
        f"  applied: {result.merges_applied} merges · "
        f"{result.promotions_applied} promotions · "
        f"{result.candidates_promoted} theme-promotions · "
        f"{result.candidates_archived} candidates-archived · "
        f"{result.theme_status_changes} theme-status · "
        f"{result.essence_rewrites_logged} essence-rewrites (log-only)"
    )
    print(
        f"  index:   {result.indexed} indexed, "
        f"{result.removed} removed, {result.edges} edges"
    )
    if result.log_path:
        print(f"  log:     {result.log_path}")
    if result.timings:
        parts = " · ".join(
            f"{k} {v:.2f}s" for k, v in sorted(result.timings.items())
        )
        print(f"  timing: {parts}")
    if result.errors:
        print("  errors:")
        for e in result.errors:
            print(f"    ! {e}")
        sys.exit(1)
