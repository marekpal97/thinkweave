"""``weave hubs {plan,run,status,repair,link}`` — concept-hub backfill orchestration.

Heavy lifting (per-batch backfill loop, linkage builders) lives in
``operations/hubs_batch.py``. The ``link`` action's OpenAI Batches loop is
extracted into ``_hubs_link.py`` because it's long and stateful.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from thinkweave.core.config import load_config
from thinkweave.surfaces.cli._hubs_link import (
    hubs_apply_linkage as _hubs_apply_linkage,
)
from thinkweave.surfaces.cli._hubs_link import hubs_link as _hubs_link


def cmd_hubs(args: argparse.Namespace) -> None:
    """Concept hub page management — plan, status, repair, link.

    The ``run`` deprecation alias (folded into ``weave drain --target hubs
    --via batch``) was removed 2026-05-21; agents should call the canonical
    form directly.
    """
    cfg = load_config()
    action = args.hubs_action or "status"

    if action == "plan":
        _hubs_plan(cfg, args)
    elif action == "status":
        _hubs_status(cfg, args)
    elif action == "repair":
        _hubs_repair(cfg, args)
    elif action == "link":
        _hubs_link(cfg, args)
    elif action == "apply-linkage":
        _hubs_apply_linkage(cfg, args)
    else:
        print(f"Unknown hubs action: {action}")
        sys.exit(1)


def _hubs_plan(cfg, args: argparse.Namespace) -> None:
    from thinkweave.synthesis.concept_hub import build_plan, plan_to_dict

    plans = build_plan(
        cfg,
        project=args.project,
        note_type=args.note_type,
        concept_filter=args.concept,
        limit_notes_per_concept=args.limit_notes,
        limit_concepts=args.limit_concepts,
    )

    payload = plan_to_dict(plans)
    out_path = Path(args.out) if args.out else (cfg.weave_dir / "hubs_plan.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"Plan: {out_path}")
    print(f"  concepts: {payload['total_concepts']}")
    print(f"  unprocessed notes: {payload['total_notes']}")
    print(f"  est input tokens: {payload['est_input_tokens']:,}")
    if not plans:
        print("  (nothing to process — all hubs are caught up)")
        return
    print("\n  Top concepts by unprocessed note count:")
    for p in plans[:10]:
        dom = f" [{', '.join(p.domains)}]" if p.domains else ""
        print(f"    {len(p.unprocessed_notes):4d}  {p.concept}{dom}")


def _hubs_status(cfg, args: argparse.Namespace) -> None:
    from thinkweave.synthesis.concept_hub import (
        all_concepts_in_vault,
        concept_hub_path,
        parse_concept_hub,
    )

    counts = all_concepts_in_vault(cfg)
    if args.concept:
        counts = {c: n for c, n in counts.items() if c == args.concept.lower()}
    if not counts:
        print("No concepts found in the vault index.")
        return

    rows: list[tuple[str, int, int, int]] = []
    for concept, total in sorted(counts.items(), key=lambda x: -x[1]):
        hub = parse_concept_hub(concept_hub_path(cfg, concept), concept=concept)
        cited = len(hub.cited_ids)
        unprocessed = total - cited
        rows.append((concept, total, cited, unprocessed))

    print(f"{'concept':<40} {'total':>6} {'cited':>6} {'todo':>6}")
    print("-" * 62)
    for concept, total, cited, todo in rows:
        print(f"{concept:<40} {total:>6} {cited:>6} {todo:>6}")
    print(f"\n{len(rows)} concept(s), {sum(r[3] for r in rows)} unprocessed note-citations total.")


def _hubs_repair(cfg, args: argparse.Namespace) -> None:
    """Thin surface over ``operations.hubs_batch.repair_hubs`` — the heavy
    lifting (SQL, hub parsing, date-swap + citation-cleanup rewrites) lives in
    the operation; this only formats the report.
    """
    from thinkweave.operations.hubs_batch import repair_hubs

    result = repair_hubs(cfg, concept=args.concept, dry_run=args.dry_run)

    if result.topics_missing:
        print(f"No concept-hub topics directory at {result.topics_dir}.")
        return

    for name in result.would_rewrite:
        print(f"[dry-run] would rewrite {name}")
    print(
        f"Repaired {result.changed_hubs} hub(s) — "
        f"{result.date_updates} date swap(s), "
        f"{result.citation_cleanups} citation cleanup(s)."
    )
    if result.dry_run:
        print("(dry-run: no files written)")
        return

    if result.reindex_contention_msg:
        print(
            f"  warning: reindex hit SQLite contention "
            f"({result.reindex_contention_msg}); continuing"
        )
    if result.reindex_failures:
        print(
            f"  {result.reindex_failures} hub(s) couldn't be reindexed due to DB "
            f"contention. Run `uv run weave index` once the contending process "
            f"releases the lock."
        )
