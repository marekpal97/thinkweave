"""``weave drain`` — unified queue / hub backfill entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from thinkweave.core.config import load_config


def _print_hubs_batch(result) -> None:
    """Render a :class:`HubsBatchResult` as the legacy streamed progress report.

    The operation (``operations/hubs_batch.run_hubs_batch``) returns data; this
    surface owns all stdout so a second adapter can reuse the op silently.
    """
    if result.concepts == 0:
        print("Plan is empty.")
        return

    print(
        f"Built {result.requests_built} request(s) across "
        f"{result.concepts} concept(s)."
    )

    if result.dry_run:
        print("\n--- DRY RUN: first request preview ---")
        if result.preview:
            p = result.preview
            print(f"concept: {p['concept']}")
            print(f"note_id: {p['note_id']}")
            print(f"system: {p['system_chars']} chars")
            print(f"user: {p['user_chars']} chars")
            print("\n--- user prompt (first 800 chars) ---")
            print(p["user_head"])
        return

    if result.deferred:
        print(
            f"Capping at {result.capped} request(s) (~{result.capped_tokens:,} "
            f"input tokens) to stay under --max-input-tokens={result.budget:,}. "
            f"{result.deferred} request(s) deferred — rerun `weave hubs plan` + "
            f"`weave drain --target hubs --via batch` after this batch completes."
        )

    if result.issued == 0:
        return

    print(
        f"Issuing {result.issued} request(s) to {result.provider}/{result.model} "
        f"(concurrency={result.concurrency})..."
    )
    if result.errors:
        print(
            f"  warning: {result.errors} request(s) failed; rerun to retry the rest"
        )

    print(f"\nApplied {result.applied} new log entries.")
    if result.essence_flagged:
        print(
            f"Essence revision flagged for {len(result.essence_flagged)} concept(s):"
        )
        for c in result.essence_flagged:
            print(f"  {c}")
        print("Run /weave-resolve-concepts to review flagged essences.")

    if result.reindex_contention_msg:
        print(
            f"  warning: reindex hit SQLite contention "
            f"({result.reindex_contention_msg}); continuing"
        )
    print(
        f"Reindexed {result.touched - result.reindex_failures} of "
        f"{result.touched} hub page(s)."
    )
    if result.reindex_failures:
        print(
            f"  {result.reindex_failures} hub(s) couldn't be reindexed due to DB "
            f"contention. Run `uv run weave index` once the contending process "
            f"releases the lock."
        )


def cmd_drain(args: argparse.Namespace) -> None:
    """Drain queues or backfill concept hubs.

    Canonical entry point for batch hub backfill (use ``--target hubs
    --via batch``) and the inline hub-backfill skill (use ``--target
    hubs --via inline``). Replaces the now-deleted ``weave hubs run``
    alias.
    """
    cfg = load_config()

    if args.target == "hubs":
        if args.via == "batch":
            from thinkweave.operations.hubs_batch import (
                PlanNotFoundError,
                run_hubs_batch,
            )

            try:
                result = run_hubs_batch(
                    cfg,
                    plan_path=Path(args.plan) if args.plan else None,
                    model=args.model,
                    max_tokens=args.max_tokens,
                    poll_interval=args.poll_interval,
                    max_input_tokens=args.max_input_tokens,
                    dry_run=args.dry_run,
                )
            except PlanNotFoundError as e:
                print(f"Plan file not found: {e}")
                print("Run `weave hubs plan` first.")
                sys.exit(1)
            _print_hubs_batch(result)
            return
        print(
            "Inline hub drain runs as a Claude Code skill.\n"
            "  Run:  /drain --target hubs --via inline\n"
            "  (or /update-hubs for small daily deltas).\n"
            "This CLI prints this hint and exits — the actual extraction "
            "happens in the skill, with full weave_* tool access."
        )
        return

    if args.source_type:
        if args.via == "batch":
            print(
                f"Batch drain for source_type='{args.source_type}' is not yet "
                "implemented. Roadmap: anthropic_batch / openai_batch drivers "
                "picked from sources.yaml::sources.<type>.drain_strategy."
            )
            sys.exit(2)
        from thinkweave.acquisition.sources import load_user_config

        sources_cfg = load_user_config(cfg.vault_root).get("sources", {})
        skill = (
            sources_cfg.get(args.source_type, {}).get("research_skill")
            or f"research-{args.source_type}"
        )
        print(
            f"Inline drain for source_type='{args.source_type}' runs as a "
            f"Claude Code skill.\n"
            f"  Run:  /drain --source-type {args.source_type}\n"
            f"  Per-item skill: /{skill}\n"
            "This CLI prints this hint and exits — the actual fetch + "
            "summarize loop happens in the skill."
        )
        return

    if args.source == "claude-history":
        from thinkweave.acquisition.importers.claude_history import import_claude_history

        stats = import_claude_history(
            cfg, db_path=None, project_filter="", dry_run=args.dry_run
        )
        if "error" in stats:
            print(f"Error: {stats['error']}")
            sys.exit(1)
        if not args.dry_run:
            print(
                f"Imported: {stats['sessions']} sessions, "
                f"{stats['notes']} notes, {stats['decisions']} decisions"
            )
        return

    print(
        "Usage: weave drain --target hubs [--via inline|batch]\n"
        "       weave drain --source-type <slug> [--via inline]\n"
        "       weave drain --source claude-history"
    )
    sys.exit(1)


def cmd_discover(args: argparse.Namespace) -> None:
    """Run the configured discovery strategies for a project.

    Resolution order:

    1. ``--strategy NAME`` — explicit, runs only that strategy.
    2. ``sources.yaml: projects.<project>.discover_strategies`` — list.
    3. ``sources.yaml: projects.default.discover_strategies`` — fallback.

    Output is JSON on stdout: a list of gap-descriptor dicts as returned
    by each strategy's ``run`` method, with the ``strategy`` field
    stamped onto every entry. Callers (the ``/discover`` skill, cron
    flows) read this JSON and decide how to enqueue / write back.
    """
    import json

    from thinkweave.core.vault import VaultManager
    from thinkweave.acquisition.discover import get, names
    from thinkweave.acquisition.sources import load_user_config

    if getattr(args, "list", False):
        for n in names():
            print(n)
        return

    cfg = load_config()
    user_cfg = load_user_config(cfg.vault_root)
    project = args.project or cfg.default_project or ""

    # Surface CLI runtime params to strategies via a reserved config key.
    # Strategies that care (rss_poll, mail_poll) read _runtime.source_type;
    # the rest ignore it.
    source_type_filter = getattr(args, "source_type", "") or ""
    if source_type_filter:
        user_cfg = dict(user_cfg)
        user_cfg["_runtime"] = {"source_type": source_type_filter}

    if args.strategy:
        strategy_names = [args.strategy]
    else:
        projects_cfg = user_cfg.get("projects", {}) or {}
        scope = projects_cfg.get(project) if project else None
        if not scope:
            scope = projects_cfg.get("default", {})
        strategy_names = list(scope.get("discover_strategies", []))

    vm = VaultManager(config=cfg)
    all_items: list[dict] = []
    for sname in strategy_names:
        try:
            strategy = get(sname)
        except KeyError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        items = strategy.run(vm, project or None, user_cfg)
        for item in items:
            item.setdefault("strategy", sname)
            all_items.append(item)

    print(json.dumps(all_items, indent=2))
