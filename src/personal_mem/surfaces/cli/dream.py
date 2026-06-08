"""``mem dream`` — periodic vault-hygiene cycle CLI.

Three actions, mirroring the scan / apply phases of the cycle plus the
new orchestrator selector:

- ``mem dream scan [--json]`` — read-only; emit the action plan. Default
  formats a compact table for interactive inspection; ``--json`` emits
  the raw :class:`DreamCycleScan` payload for skill consumption.
- ``mem dream apply --plan <path>|- [--dry-run] [--json]`` — execute the
  LLM-judged plan. Reads JSON from a file path or stdin. ``--dry-run``
  parses + validates the plan but skips writes.
- ``mem dream tasks --phase {1,2} [--scan <path>] [--apply-result <path>]
  [--json]`` — enumerate the subagent tasks the ``/dream`` orchestrator
  should spawn for the given phase. Reads the scan from disk if
  ``--scan`` is provided, otherwise runs ``scan(cfg)`` fresh.

The intermediate LLM judgment phase lives in the ``/dream`` skill —
``commands/dream.md``. This surface is just the three endpoints.
"""

from __future__ import annotations

import argparse
import json
import sys
from types import SimpleNamespace

from personal_mem.core.config import load_config


def cmd_dream(args: argparse.Namespace) -> None:
    action = getattr(args, "dream_action", None)
    if action == "scan":
        _cmd_scan(args)
    elif action == "apply":
        _cmd_apply(args)
    elif action == "tasks":
        cmd_dream_tasks(args)
    else:
        print("Usage: mem dream {scan|apply|tasks}", file=sys.stderr)
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
        f"{s.get('theme_cluster_signals', 0)} theme-signals · "
        f"{s.get('active_themes', 0)} active-themes"
    )
    if result.promotion_candidates:
        print("  promotion candidates:")
        for p in result.promotion_candidates[:20]:
            print(f"    {p['count']:>3}  {p['concept']}")
    if result.drift_pairs:
        print("  drift pairs (post-filter):")
        for d in result.drift_pairs:
            print(f"    {d['from']} → {d['to']}  ({d.get('reason', '')})")
    if result.theme_cluster_signals:
        print(f"  theme cluster signals: {len(result.theme_cluster_signals)}")
        for sig in result.theme_cluster_signals[:10]:
            concepts = ", ".join(sig.get("shared_concepts") or [])
            cov = sig.get("covering_themes") or []
            tag = f" → extend {cov[0]['slug']}" if cov else " → mint"
            names = ", ".join((sig.get("proposed_names") or {}).keys())
            print(f"    [{concepts}]{tag}  ({sig.get('source_count', 0)} src)")
            if names:
                print(f"      proposed: {names}")
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
    from personal_mem.operations.dream import (
        PlanValidationError,
        apply,
        validate_plan_fragment,
    )

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

    # Strict default: --no-strict turns it off. The flag lives on args; the
    # validation gate also re-runs inside ``apply`` so direct Python callers
    # get the same guarantee.
    strict = getattr(args, "strict", True)

    if args.dry_run:
        # Validation-only: count what would happen, never touch the vault.
        # In strict mode we still surface plan-fragment drift so the operator
        # sees the bad keys without having to run apply for real.
        warnings = validate_plan_fragment(plan)
        summary = {
            "merges": len(plan.get("merges") or []),
            "promotions": len(plan.get("promotions") or []),
            "theme_mints": len(plan.get("theme_mints") or []),
            "theme_extensions": len(plan.get("theme_extensions") or []),
            "essence_rewrites": len(plan.get("essence_rewrites") or []),
        }
        if args.json:
            payload = {"dry_run": True, "would_apply": summary}
            if warnings:
                payload["validation_warnings"] = warnings
            print(json.dumps(payload, indent=2))
        else:
            print("dream apply (dry-run) — would apply:")
            for k, v in summary.items():
                print(f"  {k}: {v}")
            if warnings:
                print("  validation warnings:")
                for w in warnings:
                    print(f"    ! {w}")
        # In strict mode, warnings cause a non-zero exit so cron / orchestrator
        # can fail-fast before invoking the real apply.
        sys.exit(1 if (warnings and strict) else 0)

    cfg = load_config()
    project = args.project or cfg.default_project or ""
    cycle_id = plan.get("cycle_id") or None

    try:
        result = apply(
            cfg, plan=plan, project=project, cycle_id=cycle_id, strict=strict,
        )
    except PlanValidationError as e:
        # Strict-mode failure: surface every warning on stderr and exit
        # non-zero. The orchestrator (commands/dream.md step 1.5) reads
        # the exit code to decide whether to re-prompt the offending worker.
        if args.json:
            print(json.dumps({
                "error": "plan_validation",
                "warnings": e.warnings,
            }, indent=2))
        else:
            print("dream apply — plan validation failed:", file=sys.stderr)
            for w in e.warnings:
                print(f"  ! {w}", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
        sys.exit(1 if result.errors else 0)

    print(f"dream apply · {result.cycle_id} · project {project or '(none)'}")
    print(
        f"  applied: {result.merges_applied} merges · "
        f"{result.promotions_applied} promotions · "
        f"{result.themes_minted} themes-minted · "
        f"{result.themes_extended} themes-extended · "
        f"{result.essence_rewrites_applied} essence-rewrites"
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


def cmd_dream_tasks(args: argparse.Namespace) -> None:
    """Enumerate the subagent tasks the ``/dream`` orchestrator should spawn.

    Reads a scan payload from ``--scan <path>`` if provided (the common
    cycle-time path — the orchestrator runs ``scan`` once, then asks
    ``tasks`` per phase); otherwise runs ``scan(cfg)`` fresh.
    ``--apply-result`` is accepted as a pass-through for phase 2 (some
    phase-2 surfaces eventually need the apply outcome bundle, but v1 only
    consults the scan).
    """
    from personal_mem.operations.dream import DreamCycleScan, scan
    from personal_mem.operations.dream_tasks import enabled_tasks

    cfg = load_config()
    project = getattr(args, "project", "") or cfg.default_project or ""

    scan_obj: DreamCycleScan | SimpleNamespace
    scan_path = getattr(args, "scan", None)
    if scan_path:
        with open(scan_path, encoding="utf-8") as f:
            payload = json.load(f)
        # ``DreamCycleScan`` is a flat dataclass — rehydrate by-keyword
        # against its declared fields. Unknown keys (e.g. phase-2 fields
        # landing in a concurrent change) flow through as plain attributes
        # so the ``has_signal`` predicates (``getattr``-based) keep working.
        from dataclasses import fields as _fields

        known = {f.name for f in _fields(DreamCycleScan)}
        try:
            scan_kwargs = {k: v for k, v in payload.items() if k in known}
            scan_obj = DreamCycleScan(**scan_kwargs)
            for k, v in payload.items():
                if k not in known:
                    setattr(scan_obj, k, v)
        except TypeError:
            scan_obj = SimpleNamespace(**payload)
    else:
        scan_obj = scan(cfg, project=project)

    # ``--apply-result`` is a pass-through for v1 — read + validate that
    # it's JSON if provided, but don't pipe it into the selector yet.
    apply_result_path = getattr(args, "apply_result", None)
    if apply_result_path:
        with open(apply_result_path, encoding="utf-8") as f:
            json.load(f)

    tasks = enabled_tasks(scan_obj, phase=args.phase)

    if getattr(args, "json", False):
        print(json.dumps(tasks, indent=2, sort_keys=True))
        sys.exit(0)

    if not tasks:
        print(f"dream tasks · phase {args.phase} · 0 tasks enabled")
        return

    print(f"dream tasks · phase {args.phase} · {len(tasks)} task(s)")
    print(f"  {'worker':<28} {'surface':<24} plan_keys")
    print(f"  {'-' * 28} {'-' * 24} {'-' * 30}")
    for t in tasks:
        plan_keys = ",".join(t["plan_keys"]) or "(direct-write)"
        deps = (
            f"  depends_on: {','.join(t['depends_on'])}"
            if t["depends_on"]
            else ""
        )
        print(f"  {t['worker_name']:<28} {t['surface_key']:<24} {plan_keys}{deps}")
