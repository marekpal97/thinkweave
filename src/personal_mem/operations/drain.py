"""Drain orchestration — the OpenAI Batches API path for concept-hub backfill.

This module holds the 250-LOC monolith previously inlined as ``_hubs_run`` in
``surfaces/cli/__init__.py``. CLI ``mem drain --target hubs --via batch`` (and
its deprecated alias ``mem hubs run``) both call into ``run_hubs_batch``.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from personal_mem.core.config import Config


def run_hubs_batch(
    cfg: Config,
    *,
    plan_path: Path | None = None,
    model: str = "gpt-5-mini",
    max_tokens: int = 1024,
    poll_interval: int = 30,
    max_input_tokens: int = 4_500_000,
    dry_run: bool = False,
) -> dict:
    """Execute a concept-hub backfill plan via OpenAI Batches API.

    Returns a stats dict; prints progress to stdout (legacy behaviour preserved).
    Exits the process via sys.exit on hard errors (missing plan, missing key,
    failed batch).
    """
    from personal_mem.core.indexer import Indexer
    from personal_mem.core.vault import VaultManager, parse_frontmatter
    from personal_mem.synthesis.concept_hub import (
        HUB_EXTRACTION_SYSTEM,
        append_log_entries,
        build_extraction_user_prompt,
        concept_hub_path,
        parse_concept_hub,
        parse_llm_response,
    )

    plan_path = plan_path or (cfg.mem_dir / "hubs_plan.json")
    if not plan_path.exists():
        print(f"Plan file not found: {plan_path}")
        print("Run `mem hubs plan` first.")
        sys.exit(1)

    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    concept_plans = payload.get("concepts", [])
    if not concept_plans:
        print("Plan is empty.")
        return {"applied": 0, "concepts": 0}

    vm = VaultManager(config=cfg)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    requests_to_send: list[dict] = []
    for cp in concept_plans:
        concept = cp["concept"]
        hub_path = concept_hub_path(cfg, concept)
        hub = parse_concept_hub(hub_path, concept=concept)

        for note_entry in cp["unprocessed_notes"]:
            note_path = vm.root / note_entry["path"]
            if not note_path.exists():
                continue
            note_text = note_path.read_text(encoding="utf-8")
            _, body = parse_frontmatter(note_text)

            user_prompt = build_extraction_user_prompt(
                concept=concept,
                essence=hub.essence,
                recent_entries=hub.log_entries,
                note_id=note_entry["id"],
                note_type=note_entry.get("type", "note"),
                project=note_entry.get("project", ""),
                date=note_entry.get("date", ""),
                title=note_entry.get("title", ""),
                body=body,
            )
            note_date = (note_entry.get("date") or today)[:10]
            requests_to_send.append(
                {
                    "concept": concept,
                    "note_id": note_entry["id"],
                    "note_date": note_date,
                    "system": HUB_EXTRACTION_SYSTEM,
                    "user": user_prompt,
                    "cache_key": concept,
                }
            )

    print(f"Built {len(requests_to_send)} request(s) across {len(concept_plans)} concept(s).")

    if dry_run:
        print("\n--- DRY RUN: first request preview ---")
        if requests_to_send:
            r = requests_to_send[0]
            print(f"concept: {r['concept']}")
            print(f"note_id: {r['note_id']}")
            print(f"system: {len(r['system'])} chars")
            print(f"user: {len(r['user'])} chars")
            print("\n--- user prompt (first 800 chars) ---")
            print(r["user"][:800])
        return {"applied": 0, "concepts": len(concept_plans), "dry_run": True}

    try:
        from openai import OpenAI
    except ImportError:
        print(
            "mem hubs run requires the OpenAI SDK.\n"
            "Install with: uv add --optional hubs openai  (or `pip install openai`)"
        )
        sys.exit(1)

    from personal_mem.enrich import load_openai_api_key

    api_key = load_openai_api_key()
    if not api_key:
        print(
            "OPENAI_API_KEY is not set (neither in env nor in the project .env)."
            " Export it or add OPENAI_API_KEY=sk-... to the repo .env."
        )
        sys.exit(1)
    os.environ["OPENAI_API_KEY"] = api_key

    client = OpenAI()

    sorted_requests = sorted(
        requests_to_send, key=lambda r: (r["cache_key"], r["note_id"])
    )

    if max_input_tokens > 0:
        budget = max_input_tokens
        capped: list[dict] = []
        total_tokens = 0
        for r in sorted_requests:
            est = (len(r["system"]) + len(r["user"])) // 4
            if total_tokens + est > budget:
                break
            capped.append(r)
            total_tokens += est
        deferred = len(sorted_requests) - len(capped)
        if deferred > 0:
            print(
                f"Capping at {len(capped)} request(s) (~{total_tokens:,} input "
                f"tokens) to stay under --max-input-tokens={budget:,}. "
                f"{deferred} request(s) deferred — rerun `mem hubs plan` + "
                f"`mem hubs run` after this batch completes."
            )
        sorted_requests = capped

    id_to_key: dict[str, tuple[str, str, str]] = {}
    jsonl_lines: list[str] = []
    for i, r in enumerate(sorted_requests):
        custom_id = f"req-{i:05d}"
        id_to_key[custom_id] = (r["concept"], r["note_id"], r["note_date"])
        body = {
            "model": model,
            "max_completion_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": r["system"]},
                {"role": "user", "content": r["user"]},
            ],
        }
        jsonl_lines.append(
            json.dumps(
                {
                    "custom_id": custom_id,
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": body,
                }
            )
        )

    batch_input_path = cfg.mem_dir / "hubs_batch_input.jsonl"
    batch_input_path.parent.mkdir(parents=True, exist_ok=True)
    batch_input_path.write_text("\n".join(jsonl_lines) + "\n", encoding="utf-8")
    print(f"Wrote batch input: {batch_input_path} ({len(jsonl_lines)} line(s))")

    print("Uploading batch input to OpenAI Files API...")
    with batch_input_path.open("rb") as f:
        input_file = client.files.create(file=f, purpose="batch")
    print(f"Input file ID: {input_file.id}")

    print(f"Submitting batch of {len(jsonl_lines)} request(s) to {model}...")
    batch = client.batches.create(
        input_file_id=input_file.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
        metadata={"source": "personal-mem.hubs"},
    )
    print(f"Batch ID: {batch.id}")
    (cfg.mem_dir / "hubs_last_run").write_text(
        json.dumps({"batch_id": batch.id, "input_file_id": input_file.id}, indent=2),
        encoding="utf-8",
    )

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
        time.sleep(poll_interval)

    if batch.status != "completed":
        print(f"Batch did not complete cleanly: status={batch.status}")
        if batch.errors:
            print(f"Errors: {batch.errors}")
        sys.exit(1)

    if not batch.output_file_id:
        print("Batch completed but has no output_file_id.")
        sys.exit(1)

    print(f"Downloading results from output file {batch.output_file_id}...")
    output_content = client.files.content(batch.output_file_id).text

    applied = 0
    essence_flagged: set[str] = set()
    for line in output_content.splitlines():
        if not line.strip():
            continue
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        custom_id = result.get("custom_id", "")
        concept, note_id, note_date = id_to_key.get(custom_id, ("", "", ""))
        if not concept:
            continue
        if result.get("error"):
            continue
        response = result.get("response", {})
        if response.get("status_code") != 200:
            continue
        body = response.get("body", {})
        choices = body.get("choices", [])
        if not choices:
            continue
        raw = choices[0].get("message", {}).get("content", "")
        if not raw:
            continue
        entries, needs_essence = parse_llm_response(
            raw, note_id=note_id, run_date=note_date or today
        )
        if entries:
            append_log_entries(cfg, concept, entries)
            applied += len(entries)
        if needs_essence:
            essence_flagged.add(concept)

    print(f"\nApplied {applied} new log entries.")
    if essence_flagged:
        print(f"Essence revision flagged for {len(essence_flagged)} concept(s):")
        for c in sorted(essence_flagged):
            print(f"  {c}")
        print("Run /mem-resolve-concepts to review flagged essences.")

    import sqlite3 as _sqlite3

    idx = Indexer(config=cfg)
    touched_concepts = {c for c, _, _ in id_to_key.values()}
    reindex_failures = 0
    for concept in touched_concepts:
        path = concept_hub_path(cfg, concept)
        if not path.exists():
            continue
        try:
            idx.index_file(path)
        except _sqlite3.OperationalError as e:
            reindex_failures += 1
            if reindex_failures == 1:
                print(f"  warning: reindex hit SQLite contention ({e}); continuing")
    idx.close()
    print(
        f"Reindexed {len(touched_concepts) - reindex_failures} of "
        f"{len(touched_concepts)} hub page(s)."
    )
    if reindex_failures:
        print(
            f"  {reindex_failures} hub(s) couldn't be reindexed due to DB "
            f"contention. Run `uv run mem index` once the contending process "
            f"releases the lock."
        )

    return {
        "applied": applied,
        "concepts": len(concept_plans),
        "essence_flagged": list(essence_flagged),
        "touched": len(touched_concepts),
    }


# ---------------------------------------------------------------------------
# Hub linkage — temporal-DAG rewrite of `new` flags
# ---------------------------------------------------------------------------


def validate_linkage_revision(
    entry_date: str, flag: str, ref: str
) -> tuple[str | None, str]:
    """Validate a single linkage revision against the temporal-DAG contract."""
    from personal_mem.synthesis.concept_hub import ALLOWED_FLAGS

    if flag not in ALLOWED_FLAGS:
        return None, ""

    if flag == "new":
        return "new", ""

    if ref and not re.match(r"^\d{4}-\d{2}-\d{2}$", ref):
        ref = ""

    if ref and ref >= entry_date:
        ref = ""

    if not ref and flag in {"extends", "contradicts"}:
        flag = "new"

    return flag, ref


HUB_LINKAGE_SYSTEM = """You are revising a learning log for one concept in a personal knowledge vault. Each entry is a distilled learning artifact captured from a note. Entries are listed oldest-first; the entry's date is its line prefix.

For each entry E in the list, decide what E does relative to the entries that appear BEFORE it (the entries with strictly earlier dates):
- "new" — E introduces something not present in any earlier entry.
- "agrees" — E reinforces, restates, or confirms a claim from an earlier entry.
- "contradicts" — E directly conflicts with an earlier entry.
- "extends" — E elaborates on, refines, or adds a corollary to an earlier entry.

E is the SUBJECT. The earlier entry is the OBJECT. The verb describes what E does to the earlier entry — never what the earlier entry does to E. The first entry in the list (no earlier entries exist) MUST be flagged "new".

Rules for the ref field:
- flag "new" → ref MUST be empty.
- flag "agrees" → ref is optional (empty if no single earlier entry to cite; otherwise the date of that earlier entry).
- flag "contradicts" → ref is REQUIRED and must be the date of an earlier entry.
- flag "extends" → ref is REQUIRED and must be the date of an earlier entry.

The ref date MUST be strictly less than E's date. If you cannot find an earlier entry that fits, flag E as "new". Never cite a future or same-day entry. Never invent dates — only cite dates that appear in the input.

Be conservative: default to "new" unless the relationship is clear.

Return a single JSON object of the form:
  {"entries": [{"flag": "...", "ref": "YYYY-MM-DD or empty"}, ...]}
The array length must EXACTLY match the input. Preserve input order.
"""


def build_linkage_user_prompt(concept: str, essence: str, entries: list) -> str:
    essence_text = essence.strip() or "*No synthesis yet.*"
    lines = [
        f"Concept: `{concept}`",
        "",
        f"Essence:\n{essence_text}",
        "",
        "Entries (chronological):",
    ]
    for i, e in enumerate(entries, start=1):
        lines.append(f"{i}. {e.date} — {e.text}")
    lines.append("")
    lines.append("Output JSON only.")
    return "\n".join(lines)


def parse_linkage_response(raw: str) -> list[dict]:
    """Parse the linkage LLM response. Tolerates code-fenced JSON. Returns []."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]
