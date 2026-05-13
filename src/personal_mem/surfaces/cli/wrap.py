"""``mem wrap-finalize`` — deterministic post-extraction tail of ``/mem-wrap``.

The LLM phase of ``/mem-wrap`` (digest distillation + writing the session's
insights/decisions via ``mem_extract``) hands off to this. One Bash call
replaces the ``index → judge → landing → drift → prune`` chain that used to be
~5 separate MCP round-trips, each a model turn.

Used both interactively (the wrap skill runs it after the extraction subagent
returns) and headless (a cron ``claude -p "/mem-wrap"`` catch-up run ends with
``mem wrap-finalize <session_id> --json``).
"""

from __future__ import annotations

import argparse
import json
import sys

from personal_mem.core.config import load_config


def cmd_wrap_finalize(args: argparse.Namespace) -> None:
    from personal_mem.operations.wrap import finalize_wrap

    cfg = load_config()
    project = args.project or cfg.default_project or ""
    if not project:
        print(
            "error: project required — pass --project or set PERSONAL_MEM_PROJECT.",
            file=sys.stderr,
        )
        sys.exit(2)

    result = finalize_wrap(
        cfg,
        session_id=args.session_id,
        project=project,
        prune=not args.no_prune,
    )

    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
        sys.exit(1 if result.errors else 0)

    print(f"wrap-finalize · session {result.session_id} · project {project}")
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
    if result.errors:
        print("  errors:")
        for e in result.errors:
            print(f"    ! {e}")
        sys.exit(1)
