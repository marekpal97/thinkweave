"""``mem hubs link`` — temporal-DAG linkage pass via OpenAI Batches.

Lives apart from the other ``mem hubs`` actions because the OpenAI
Batches loop (build requests → upload file → poll for completion → apply
revisions → reindex) is long and stateful.
"""

from __future__ import annotations

import argparse
import json
import os
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

    work: list[tuple[str, list[LogEntry], str]] = []
    for hub_path in hub_files:
        hub = parse_concept_hub(hub_path)
        if len(hub.log_entries) < args.min_entries:
            continue
        entries_sorted = sorted(hub.log_entries, key=lambda e: (e.date, e.citation))
        work.append((hub.concept, entries_sorted, hub.essence))

    if not work:
        print(f"No hubs with ≥{args.min_entries} entries found.")
        return

    print(f"Building linkage requests for {len(work)} hub(s)...")

    from personal_mem.operations.drain import (
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

    try:
        from openai import OpenAI
    except ImportError:
        print(
            "mem hubs link requires the OpenAI SDK.\n"
            "Install with: uv add --optional hubs openai"
        )
        sys.exit(1)

    from personal_mem.enrich import load_openai_api_key

    api_key = load_openai_api_key()
    if not api_key:
        print("OPENAI_API_KEY is not set.")
        sys.exit(1)
    os.environ["OPENAI_API_KEY"] = api_key
    client = OpenAI()

    id_to_concept: dict[str, str] = {}
    jsonl_lines: list[str] = []
    for i, r in enumerate(requests_to_send):
        custom_id = f"link-{i:05d}"
        id_to_concept[custom_id] = r["concept"]
        body = {
            "model": args.model,
            "max_completion_tokens": args.max_tokens,
            "messages": [
                {"role": "system", "content": r["system"]},
                {"role": "user", "content": r["user"]},
            ],
            "response_format": {"type": "json_object"},
        }
        jsonl_lines.append(json.dumps({
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": body,
        }))

    batch_input_path = cfg.mem_dir / "hubs_link_input.jsonl"
    batch_input_path.parent.mkdir(parents=True, exist_ok=True)
    batch_input_path.write_text("\n".join(jsonl_lines) + "\n", encoding="utf-8")
    print(f"Wrote batch input: {batch_input_path} ({len(jsonl_lines)} line(s))")

    with batch_input_path.open("rb") as f:
        input_file = client.files.create(file=f, purpose="batch")
    print(f"Input file ID: {input_file.id}")

    batch = client.batches.create(
        input_file_id=input_file.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
        metadata={"source": "personal-mem.hubs-link"},
    )
    print(f"Batch ID: {batch.id}")
    (cfg.mem_dir / "hubs_last_link_run").write_text(
        json.dumps({"batch_id": batch.id, "input_file_id": input_file.id}, indent=2),
        encoding="utf-8",
    )

    import time as _time
    terminal_statuses = {"completed", "failed", "expired", "cancelled"}
    while True:
        batch = client.batches.retrieve(batch.id)
        counts = batch.request_counts
        print(
            f"  status={batch.status} "
            f"completed={counts.completed if counts else 0} "
            f"failed={counts.failed if counts else 0} "
            f"total={counts.total if counts else 0}"
        )
        if batch.status in terminal_statuses:
            break
        _time.sleep(args.poll_interval)

    if batch.status != "completed" or not batch.output_file_id:
        print(f"Batch did not complete cleanly: status={batch.status}")
        if batch.errors:
            print(f"Errors: {batch.errors}")
        sys.exit(1)

    output_content = client.files.content(batch.output_file_id).text

    applied_hubs = 0
    applied_entries = 0
    _spend_in = _spend_out = 0
    _spend_model = ""
    for line in output_content.splitlines():
        if not line.strip():
            continue
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        custom_id = result.get("custom_id", "")
        concept = id_to_concept.get(custom_id, "")
        if not concept or result.get("error"):
            continue
        response = result.get("response", {})
        if response.get("status_code") != 200:
            continue
        _body = response.get("body", {})
        _u = _body.get("usage") or {}
        _spend_in += _u.get("prompt_tokens", 0) or 0
        _spend_out += _u.get("completion_tokens", 0) or 0
        _spend_model = _body.get("model") or _spend_model
        raw = (
            _body
            .get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        if not raw:
            continue
        revisions = parse_linkage_response(raw)
        if not revisions:
            continue

        hub_path = concept_hub_path(cfg, concept)
        hub = parse_concept_hub(hub_path, concept=concept)
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
            write_concept_hub(hub)
            applied_hubs += 1

    if _spend_in or _spend_out:
        from personal_mem.core.spend import record_spend

        record_spend(
            "openai", _spend_model or "gpt-5-mini", "hubs_link",
            _spend_in, _spend_out, mode="cron",
        )

    print(f"\nApplied linkage revisions to {applied_hubs} hub(s), {applied_entries} entries updated.")

    import sqlite3 as _sqlite3

    idx = Indexer(config=cfg)
    reindex_failures = 0
    for concept in set(id_to_concept.values()):
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
