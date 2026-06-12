"""``mem hubs {plan,run,status,repair,link}`` — concept-hub backfill orchestration.

Heavy lifting (per-batch backfill loop, linkage builders) lives in
``operations/hubs_batch.py``. The ``link`` action's OpenAI Batches loop is
extracted into ``_hubs_link.py`` because it's long and stateful.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from personal_mem.core.config import load_config
from personal_mem.surfaces.cli._hubs_link import (
    hubs_apply_linkage as _hubs_apply_linkage,
)
from personal_mem.surfaces.cli._hubs_link import hubs_link as _hubs_link


def cmd_hubs(args: argparse.Namespace) -> None:
    """Concept hub page management — plan, status, repair, link.

    The ``run`` deprecation alias (folded into ``mem drain --target hubs
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
    from personal_mem.synthesis.concept_hub import build_plan, plan_to_dict

    plans = build_plan(
        cfg,
        project=args.project,
        note_type=args.note_type,
        concept_filter=args.concept,
        limit_notes_per_concept=args.limit_notes,
        limit_concepts=args.limit_concepts,
    )

    payload = plan_to_dict(plans)
    out_path = Path(args.out) if args.out else (cfg.mem_dir / "hubs_plan.json")
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
    from personal_mem.synthesis.concept_hub import (
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
    """Retroactive fix: swap backfill dates for source-note dates, strip
    duplicated inline wikilink citations. No LLM calls.
    """
    from personal_mem.core.indexer import Indexer
    from personal_mem.synthesis.concept_hub import (
        _strip_inline_wikilinks,
        parse_concept_hub,
        topics_dir,
        write_concept_hub,
    )
    from personal_mem.synthesis.hub import build_id_path_map, build_id_title_map

    topics = topics_dir(cfg)
    if not topics.exists():
        print(f"No concept-hub topics directory at {topics}.")
        return

    idx = Indexer(config=cfg)
    id_to_date: dict[str, str] = {}
    for row in idx.db.execute("SELECT id, date FROM notes WHERE date IS NOT NULL AND date != ''"):
        id_to_date[row["id"]] = str(row["date"])[:10]
    # Path/title maps so the full re-render keeps citations path-based with
    # title aliases; path->id inverse so the parse recovers ids from those
    # links (else the entry.citation date lookup silently no-ops).
    idmap = build_id_path_map(idx.db)
    title_map = build_id_title_map(idx.db)
    path_to_id = {path: nid for nid, path in idmap.items()}
    idx.close()

    hub_files = sorted(topics.glob("*.md"))
    if args.concept:
        target = args.concept.lower()
        hub_files = [p for p in hub_files if p.stem == target]

    changed_hubs = 0
    changed_entries = 0
    citation_cleanups = 0
    date_updates = 0

    for hub_path in hub_files:
        hub = parse_concept_hub(hub_path, path_to_id=path_to_id)
        if not hub.log_entries:
            continue
        dirty = False
        for entry in hub.log_entries:
            new_date = id_to_date.get(entry.citation, entry.date)
            new_text = _strip_inline_wikilinks(entry.text) if entry.text else entry.text
            if new_date != entry.date:
                entry.date = new_date
                date_updates += 1
                dirty = True
            if new_text != entry.text:
                entry.text = new_text
                citation_cleanups += 1
                dirty = True
        if dirty:
            changed_hubs += 1
            changed_entries += sum(
                1 for e in hub.log_entries
                if id_to_date.get(e.citation, e.date) == e.date
            )
            if args.dry_run:
                print(f"[dry-run] would rewrite {hub_path.name}")
            else:
                write_concept_hub(hub, idmap=idmap, title_map=title_map)

    print(
        f"Repaired {changed_hubs} hub(s) — "
        f"{date_updates} date swap(s), {citation_cleanups} citation cleanup(s)."
    )
    if args.dry_run:
        print("(dry-run: no files written)")
        return

    import sqlite3 as _sqlite3

    idx = Indexer(config=cfg)
    reindex_failures = 0
    for hub_path in hub_files:
        if not hub_path.exists():
            continue
        try:
            idx.index_file(hub_path)
        except _sqlite3.OperationalError as e:
            reindex_failures += 1
            if reindex_failures == 1:
                print(f"  warning: reindex hit SQLite contention ({e}); continuing")
    idx.close()
    if reindex_failures:
        print(
            f"  {reindex_failures} hub(s) couldn't be reindexed due to DB "
            f"contention. Run `uv run mem index` once the contending process "
            f"releases the lock."
        )


# Backwards-compatibility shims — these helpers moved to operations/hubs_batch.py
# (formerly operations/drain.py). Tests still import them under their
# underscore-prefixed names.
def _validate_linkage_revision(entry_date: str, flag: str, ref: str, **kwargs):
    from personal_mem.operations.hubs_batch import validate_linkage_revision

    return validate_linkage_revision(entry_date, flag, ref, **kwargs)


def _build_linkage_user_prompt(
    concept: str, essence: str, entries: list, **kwargs
) -> str:
    from personal_mem.operations.hubs_batch import build_linkage_user_prompt

    return build_linkage_user_prompt(concept, essence, entries, **kwargs)


def _parse_linkage_response(raw: str) -> list[dict]:
    from personal_mem.operations.hubs_batch import parse_linkage_response

    return parse_linkage_response(raw)
