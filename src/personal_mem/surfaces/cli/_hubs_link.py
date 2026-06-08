"""``mem hubs link`` — temporal-DAG linkage pass.

Rewrites flat `new` flags on concept hubs into agrees/contradicts/extends
relationships. The OpenAI Batches submission/poll/fetch dance was
deleted 2026-06-06 (plan B4, ``go-back-to-the-scalable-firefly.md``);
this now flows through
:func:`personal_mem.core.agent_client.batch_completions_sync`. The
``mem hubs link --via inline`` route dispatches to the
``/hubs-link`` CC skill instead.
"""

from __future__ import annotations

import argparse
import sys


def hubs_link(cfg, args: argparse.Namespace) -> None:
    """Temporal-DAG linkage: rewrite flat `new` flags based on chronological
    relationships between entries on the same hub. One LLM request per hub
    via the OpenAI Batches API.
    """
    from personal_mem.core.indexer import Indexer
    from personal_mem.synthesis.concept_hub import (
        LogEntry,
        concept_hub_path,
        parse_concept_hub,
        topics_dir,
        write_concept_hub,
    )

    topics = topics_dir(cfg)
    hub_files = sorted(topics.glob("*.md"))
    if args.concept:
        target = args.concept.lower()
        hub_files = [p for p in hub_files if p.stem == target]

    # id->path / id->title for re-rendering title-aliased citations, and the
    # path->id inverse so parsing recovers ids from those links (else cited_ids
    # and date lookups would key on paths). Soft-fails to bare links if the DB
    # is unavailable.
    from personal_mem.synthesis.concept_hub import _safe_hub_maps

    _link_idmap, _link_titles, _link_path_id = _safe_hub_maps(cfg)

    work: list[tuple[str, list[LogEntry], str]] = []
    for hub_path in hub_files:
        hub = parse_concept_hub(hub_path, path_to_id=_link_path_id)
        if len(hub.log_entries) < args.min_entries:
            continue
        entries_sorted = sorted(hub.log_entries, key=lambda e: (e.date, e.citation))
        work.append((hub.concept, entries_sorted, hub.essence))

    if not work:
        print(f"No hubs with ≥{args.min_entries} entries found.")
        return

    print(f"Building linkage requests for {len(work)} hub(s)...")

    from personal_mem.operations.hubs_batch import (
        HUB_LINKAGE_SYSTEM,
        build_linkage_user_prompt,
        parse_linkage_response,
        validate_linkage_revision,
    )

    titles_by_id = _load_titles_for_citations(cfg, work)

    system_prompt = HUB_LINKAGE_SYSTEM
    requests_to_send: list[dict] = []
    for concept, entries, essence in work:
        user_prompt = build_linkage_user_prompt(
            concept, essence, entries, titles_by_id=titles_by_id
        )
        requests_to_send.append({
            "concept": concept,
            "system": system_prompt,
            "user": user_prompt,
            "entry_count": len(entries),
        })

    print(f"Built {len(requests_to_send)} request(s).")

    if args.dry_run:
        print("\n--- DRY RUN: first request preview ---")
        r = requests_to_send[0]
        print(f"concept: {r['concept']}  entries: {r['entry_count']}")
        print(f"system: {len(r['system'])} chars  user: {len(r['user'])} chars")
        print("\n--- user prompt (first 1200 chars) ---")
        print(r["user"][:1200])
        return

    if args.max_input_tokens > 0:
        budget = args.max_input_tokens
        capped: list[dict] = []
        total_tokens = 0
        for r in requests_to_send:
            est = (len(r["system"]) + len(r["user"])) // 4
            if total_tokens + est > budget:
                break
            capped.append(r)
            total_tokens += est
        if len(capped) < len(requests_to_send):
            deferred = len(requests_to_send) - len(capped)
            print(
                f"Capping at {len(capped)} hub(s) (~{total_tokens:,} input tokens); "
                f"{deferred} deferred to a subsequent run."
            )
        requests_to_send = capped

    # B4: route decision — inline dispatches to /hubs-link CC skill.
    from personal_mem.operations._backfill_route import choose_route
    decision = choose_route(
        via=getattr(args, "via", None),
        n_items=len(requests_to_send),
    )
    if decision.route == "inline":
        print(
            f"Inline hubs link ({len(requests_to_send)} hub(s); "
            f"{decision.reason}).\n"
            f"  Run:  /hubs-link"
            + (f" --concept {args.concept}" if args.concept else "")
            + "\n  (skill walks hubs via the running model, no provider "
            f"key required)."
        )
        return

    # Batch path: fan out via the wrapper. Resolve provider + model from
    # api.yaml::overrides.hubs_link (default openai / gpt-5-mini).
    from personal_mem.core.agent_client import batch_completions_sync
    from personal_mem.core.api_config import load_api_config, resolve_for_op

    op_cfg = resolve_for_op(load_api_config(cfg.vault_root), "hubs_link")
    provider = op_cfg["provider"]
    effective_model = args.model or op_cfg["model"]
    concurrency = int(op_cfg.get("batch_concurrency", 20))

    prompts = [r["user"] for r in requests_to_send]
    print(
        f"Issuing {len(prompts)} request(s) to {provider}/{effective_model} "
        f"(concurrency={concurrency})..."
    )
    completions = batch_completions_sync(
        prompts,
        provider=provider,
        model=effective_model,
        op="hubs_link",
        max_tokens=args.max_tokens,
        system=HUB_LINKAGE_SYSTEM,
        concurrency=concurrency,
        mode="cron",
        return_exceptions=True,
        response_format={"type": "json_object"},
    )

    applied_hubs = 0
    applied_entries = 0
    request_errors = 0
    touched_concepts: set[str] = set()
    for req, result in zip(requests_to_send, completions):
        concept = req["concept"]
        touched_concepts.add(concept)
        if isinstance(result, BaseException):
            request_errors += 1
            continue
        raw, _usage = result
        if not raw:
            continue
        revisions = parse_linkage_response(raw)
        if not revisions:
            continue

        hub_path = concept_hub_path(cfg, concept)
        hub = parse_concept_hub(hub_path, concept=concept, path_to_id=_link_path_id)
        entries_sorted = sorted(hub.log_entries, key=lambda e: (e.date, e.citation))

        # Length tolerance: gpt-5-mini occasionally truncates very long hubs.
        # Apply revisions for the first min(len(revisions), len(entries))
        # entries — they're in input order — and leave any tail unchanged.
        # We refuse only when the response is structurally empty.
        if not revisions:
            continue
        if len(revisions) != len(entries_sorted):
            print(
                f"  {concept}: response had {len(revisions)} revisions for "
                f"{len(entries_sorted)} entries — applying first "
                f"{min(len(revisions), len(entries_sorted))}, leaving the rest unchanged."
            )

        by_date_texts: dict[str, list[str]] = {}
        for e in entries_sorted:
            by_date_texts.setdefault(e.date, []).append(e.text)

        any_change = False
        pairs = list(zip(entries_sorted, revisions))
        for entry, rev in pairs:
            new_flag, new_ref, _quote = validate_linkage_revision(
                entry_date=entry.date,
                flag=str(rev.get("flag", "new")).lower(),
                ref=str(rev.get("ref") or "").strip(),
                ref_quote=str(rev.get("ref_quote") or "").strip(),
                by_date_texts=by_date_texts,
            )
            if new_flag is None:
                continue
            if new_flag != entry.flag or new_ref != entry.ref:
                entry.flag = new_flag
                entry.ref = new_ref
                any_change = True
                applied_entries += 1

        if any_change:
            hub.log_entries = sorted(hub.log_entries, key=lambda e: (e.date, e.citation))
            write_concept_hub(hub, idmap=_link_idmap, title_map=_link_titles)
            applied_hubs += 1

    if request_errors:
        print(
            f"  warning: {request_errors} request(s) failed; rerun to retry "
            f"the rest"
        )

    print(f"\nApplied linkage revisions to {applied_hubs} hub(s), {applied_entries} entries updated.")

    import sqlite3 as _sqlite3

    idx = Indexer(config=cfg)
    reindex_failures = 0
    for concept in touched_concepts:
        p = concept_hub_path(cfg, concept)
        if not p.exists():
            continue
        try:
            idx.index_file(p)
        except _sqlite3.OperationalError as e:
            reindex_failures += 1
            if reindex_failures == 1:
                print(f"  warning: reindex hit SQLite contention ({e}); continuing")
    idx.close()
    if reindex_failures:
        print(
            f"  {reindex_failures} hub(s) couldn't be reindexed. "
            f"Run `uv run mem index` to catch up."
        )


def _load_titles_for_citations(cfg, work) -> dict[str, str]:
    """Bulk-resolve note titles for every citation across every hub.

    The linkage prompt decorates each entry with `[from: "<title>"]` so the
    model has more than the distilled artifact line to reason about. One
    SELECT covers every citation; missing ids fall back to no decoration.
    """
    citation_ids: set[str] = set()
    for _concept, entries, _essence in work:
        for e in entries:
            if e.citation:
                citation_ids.add(e.citation)
    if not citation_ids or not cfg.index_db.exists():
        return {}

    import sqlite3

    titles: dict[str, str] = {}
    db = sqlite3.connect(cfg.index_db)
    try:
        db.row_factory = sqlite3.Row
        chunk = 500
        ids = list(citation_ids)
        for i in range(0, len(ids), chunk):
            batch = ids[i : i + chunk]
            placeholders = ",".join("?" * len(batch))
            rows = db.execute(
                f"SELECT id, title FROM notes WHERE id IN ({placeholders})",
                batch,
            ).fetchall()
            for r in rows:
                t = (r["title"] or "").strip()
                if t:
                    titles[r["id"]] = t
    finally:
        db.close()
    return titles
