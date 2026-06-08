"""Hub operations — plan, status, repair, link.

Wraps the concept-hub primitives in `synthesis/concept_hub.py` behind narrow
operation-level functions consumable by both the CLI and any future MCP path.
The OpenAI Batches execution paths live in ``operations/hubs_batch.py``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from personal_mem.core.config import Config


def plan(
    cfg: Config,
    *,
    project: str = "",
    note_type: str = "",
    concept_filter: str = "",
    limit_notes_per_concept: int = 0,
    limit_concepts: int = 0,
    out_path: Path | None = None,
):
    """Walk the vault and write a JSON backfill plan. Returns the dict payload."""
    from personal_mem.synthesis.concept_hub import build_plan, plan_to_dict

    plans = build_plan(
        cfg,
        project=project,
        note_type=note_type,
        concept_filter=concept_filter,
        limit_notes_per_concept=limit_notes_per_concept,
        limit_concepts=limit_concepts,
    )

    payload = plan_to_dict(plans)
    out_path = out_path or (cfg.mem_dir / "hubs_plan.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    payload["_out_path"] = str(out_path)
    payload["_plans"] = plans
    return payload


def status(cfg: Config, *, concept: str = "") -> list[tuple[str, int, int, int]]:
    """Return [(concept, total, cited, todo), …]."""
    from personal_mem.synthesis.concept_hub import (
        all_concepts_in_vault,
        concept_hub_path,
        parse_concept_hub,
    )

    counts = all_concepts_in_vault(cfg)
    if concept:
        counts = {c: n for c, n in counts.items() if c == concept.lower()}

    rows: list[tuple[str, int, int, int]] = []
    for c, total in sorted(counts.items(), key=lambda x: -x[1]):
        hub = parse_concept_hub(concept_hub_path(cfg, c), concept=c)
        cited = len(hub.cited_ids)
        rows.append((c, total, cited, total - cited))
    return rows


def repair(cfg: Config, *, concept: str = "", dry_run: bool = False) -> dict:
    """Retroactive fix for hub log entries. Returns stats dict."""
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
        return {"changed_hubs": 0, "date_updates": 0, "citation_cleanups": 0}

    idx = Indexer(config=cfg)
    id_to_date: dict[str, str] = {}
    for row in idx.db.execute(
        "SELECT id, date FROM notes WHERE date IS NOT NULL AND date != ''"
    ):
        id_to_date[row["id"]] = str(row["date"])[:10]
    # Maps so the full re-render below keeps citations path-based with title
    # aliases (not reverting to bare-id links), and the path->id inverse so the
    # parse recovers ids from title-aliased links (else the per-entry date
    # lookup keyed on entry.citation would silently no-op).
    idmap = build_id_path_map(idx.db)
    title_map = build_id_title_map(idx.db)
    path_to_id = {path: nid for nid, path in idmap.items()}
    idx.close()

    hub_files = sorted(topics.glob("*.md"))
    if concept:
        target = concept.lower()
        hub_files = [p for p in hub_files if p.stem == target]

    changed_hubs = 0
    date_updates = 0
    citation_cleanups = 0
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
            if not dry_run:
                write_concept_hub(hub, idmap=idmap, title_map=title_map)

    if not dry_run:
        idx = Indexer(config=cfg)
        for hub_path in hub_files:
            if not hub_path.exists():
                continue
            try:
                idx.index_file(hub_path)
            except sqlite3.OperationalError:
                pass
        idx.close()

    return {
        "changed_hubs": changed_hubs,
        "date_updates": date_updates,
        "citation_cleanups": citation_cleanups,
    }
