"""``weave wrap-finalize`` — deterministic post-extraction tail of ``/wrap``.

The LLM phase of ``/wrap`` (digest distillation + writing the session's
insights/decisions via ``weave_extract``) hands off to this. One Bash call
replaces the ``index → judge → landing → drift → prune`` chain that used to be
~5 separate MCP round-trips, each a model turn.

Used both interactively (the wrap skill runs it after the extraction subagent
returns) and headless (a cron ``claude -p "/wrap"`` catch-up run ends with
``weave wrap-finalize <session_id> --json``).
"""

from __future__ import annotations

import argparse
import json
import sys

from thinkweave.core.config import load_config


def cmd_wrap_finalize(args: argparse.Namespace) -> None:
    from thinkweave.operations.wrap import finalize_wrap

    cfg = load_config()
    project = args.project or cfg.default_project or ""
    if not project:
        print(
            "error: project required — pass --project or set THINKWEAVE_PROJECT.",
            file=sys.stderr,
        )
        sys.exit(2)

    verdicts: list[dict] = []
    if getattr(args, "verdicts", ""):
        try:
            verdicts = json.loads(args.verdicts)
        except json.JSONDecodeError as e:
            print(f"error: --verdicts is not valid JSON: {e}", file=sys.stderr)
            sys.exit(2)
        if not isinstance(verdicts, list) or not all(
            isinstance(v, dict) for v in verdicts
        ):
            print(
                "error: --verdicts must be a JSON list of "
                '{"prompt": ..., "register": ...} objects.',
                file=sys.stderr,
            )
            sys.exit(2)

    result = finalize_wrap(
        cfg,
        session_id=args.session_id,
        project=project,
        prune=not args.no_prune,
        verdicts=verdicts,
    )

    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
        sys.exit(1 if result.errors else 0)

    print(f"wrap-finalize · session {result.session_id} · project {project}")
    if result.verdicts_written or result.verdicts_skipped or result.verdicts_unmatched:
        print(
            f"  verdicts: {result.verdicts_written} written, "
            f"{result.verdicts_skipped} skipped, "
            f"{result.verdicts_unmatched} unmatched"
        )
    if result.orphans_pruned:
        mb = result.orphans_freed_bytes / (1024 * 1024)
        print(f"  prune:   {result.orphans_pruned} orphan folder(s), {mb:.1f} MB freed")
    else:
        print("  prune:   no orphans")
    print(f"  index:   {result.indexed} indexed, {result.removed} removed, {result.edges} edges")
    if result.decisions_judged:
        verdicts = ", ".join(f"{v}×{n}" for v, n in sorted(result.verdicts.items()))
        print(f"  judge:   {result.decisions_judged} decision(s) — {verdicts}")
    else:
        print("  judge:   no decisions to judge")
    print(f"  landing: {', '.join(result.landing_written) or '(none)'}")
    if result.drift_text:
        print("  drift (advisory):")
        for line in result.drift_text.splitlines():
            print(f"    {line}")
    if result.timings:
        parts = " · ".join(
            f"{k} {result.timings[k]:.1f}s"
            for k in ("verdicts", "prune", "index", "judge", "landing", "drift")
            if k in result.timings
        )
        if parts:
            print(f"  timing:  {parts}")
    if result.warnings:
        print("  warnings:")
        for w in result.warnings:
            print(f"    ~ {w}")
    if result.errors:
        print("  errors:")
        for e in result.errors:
            print(f"    ! {e}")
        sys.exit(1)
