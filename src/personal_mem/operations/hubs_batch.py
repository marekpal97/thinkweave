"""Hub-backfill batch orchestration — the OpenAI Batches API path for concept hubs.

Renamed from ``operations/drain.py`` to disambiguate from the ``/drain`` skill,
which drains per-source-type acquisition queues — a different object entirely.
This module holds the 250-LOC monolith previously inlined as ``_hubs_run`` in
``surfaces/cli/__init__.py``. CLI ``mem drain --target hubs --via batch`` calls
into ``run_hubs_batch``.
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
            note_date = _resolve_note_date(vm, note_path, note_entry)
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
        concept, note_id, note_date = id_to_key.get(custom_id, ("", "", ""))
        if not concept:
            continue
        if result.get("error"):
            continue
        response = result.get("response", {})
        if response.get("status_code") != 200:
            continue
        body = response.get("body", {})
        _u = body.get("usage") or {}
        _spend_in += _u.get("prompt_tokens", 0) or 0
        _spend_out += _u.get("completion_tokens", 0) or 0
        _spend_model = body.get("model") or _spend_model
        choices = body.get("choices", [])
        if not choices:
            continue
        raw = choices[0].get("message", {}).get("content", "")
        if not raw:
            continue
        entries, needs_essence = parse_llm_response(
            raw, note_id=note_id, run_date=note_date
        )
        if entries:
            append_log_entries(cfg, concept, entries)
            applied += len(entries)
        if needs_essence:
            essence_flagged.add(concept)

    if _spend_in or _spend_out:
        from personal_mem.core.spend import record_spend

        record_spend(
            "openai", _spend_model or "gpt-5-mini", "hubs_backfill",
            _spend_in, _spend_out, mode="cron",
        )

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


def _resolve_note_date(vm, note_path: Path, note_entry: dict) -> str:
    """Pick a YYYY-MM-DD date for a backfill entry, biased toward the
    note's own timeline.

    Priority:
      1. ``note_entry["date"]`` from the SQLite index (frontmatter ``date``).
      2. Frontmatter ``created`` field, parsed off disk.
      3. The file's mtime.

    Today's date is *never* used — the catalyst log records when each
    artifact was actually learned, and stamping every backfilled entry
    with the run date flattens the temporal DAG into a single point.
    """
    indexed = (note_entry.get("date") or "").strip()
    if indexed:
        return indexed[:10]

    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if text:
        from personal_mem.core.vault import parse_frontmatter as _pf

        try:
            fm, _body = _pf(text)
        except Exception:
            fm = {}
        for key in ("created", "date"):
            value = (fm or {}).get(key)
            if isinstance(value, str) and len(value) >= 10:
                return value[:10]

    try:
        mtime = note_path.stat().st_mtime
    except OSError:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d")


def validate_linkage_revision(
    entry_date: str,
    flag: str,
    ref: str,
    *,
    ref_quote: str = "",
    by_date_texts: dict[str, list[str]] | None = None,
) -> tuple[str | None, str, str]:
    """Validate a single linkage revision against the temporal-DAG contract.

    Returns ``(flag, ref, ref_quote)``. ``flag`` is ``None`` only on an
    unknown flag value. Quote validation: when ``by_date_texts`` is
    provided and ``ref_quote`` is non-empty, the quote must be a
    substring (case-insensitive, ≥20 chars) of at least one entry's
    text on ``ref``'s date — otherwise the revision is downgraded to
    ``new``. This forces the model to anchor non-``new`` claims in the
    actual cited text rather than asserting a relationship abstractly.
    """
    from personal_mem.synthesis.concept_hub import ALLOWED_FLAGS

    if flag not in ALLOWED_FLAGS:
        return None, "", ""

    if flag == "new":
        return "new", "", ""

    if ref and not re.match(r"^\d{4}-\d{2}-\d{2}$", ref):
        ref = ""

    if ref and ref >= entry_date:
        ref = ""

    # Strict ref-required policy: every non-`new` flag must point to a
    # specific earlier entry. An "agrees" without a ref is structurally
    # indistinguishable from a "new" — the connection is just an unverifiable
    # assertion. Demote to keep the DAG honest.
    if not ref:
        return "new", "", ""

    quote = (ref_quote or "").strip()
    if by_date_texts is not None:
        candidates = by_date_texts.get(ref, [])
        if not candidates:
            return "new", "", ""
        if len(quote) < 20:
            return "new", "", ""
        ql = quote.lower()
        if not any(ql in text.lower() for text in candidates):
            return "new", "", ""

    return flag, ref, quote


HUB_LINKAGE_SYSTEM = """You are revising a learning log for one concept in a personal knowledge vault. Each entry is a distilled learning artifact captured from a source note. Entries are listed oldest-first; the entry's date is its line prefix; a `[from: …]` decoration shows the citing note's title.

Your job: turn this flat chronological list into a connected DAG. For each entry E, decide what E does relative to entries with STRICTLY EARLIER dates:
- "new" — E introduces something none of the earlier entries cover.
- "agrees" — E reinforces, restates, or empirically confirms a claim from an earlier entry.
- "extends" — E elaborates, refines, generalizes, or adds a corollary to an earlier entry.
- "contradicts" — E directly conflicts with an earlier entry.

E is the SUBJECT; the cited earlier entry is the OBJECT. Never invert. The first entry (no earlier entries exist) MUST be "new".

A coherent learning log accumulates relationships. If you flag most entries "new", you have under-connected the log — re-scan and look for the closest semantic predecessor. In a well-developed concept (≥10 entries) it is normal for half or more of the entries to be `agrees` / `extends` / `contradicts`. All-`new` is a failure mode, not a safe default.

Rules for non-"new" flags (they are STRICT):
- `ref` is REQUIRED and must be the YYYY-MM-DD date of an earlier entry that actually appears in the input.
- `ref_quote` is REQUIRED: a verbatim ≥20-character contiguous slice from the cited entry's text (the part after the date and the `[from: …]` decoration). This anchors your relationship claim to actual prior text. Quotes that don't match the cited entry will be downgraded to "new" silently.
- `note` is REQUIRED: a short (≤80 char) explanation of WHY this is the relationship. Forces you to articulate the connection. Will be discarded; it exists only as a self-discipline mechanism.

Pick ONE predecessor per entry — the closest semantic neighbor. Never cite a future or same-day entry. Never invent dates or quotes.

Example of a healthy DAG (3 entries):
```
Input:
1. 2025-09-12 — pytest-bdd lets you write Gherkin scenarios as tests  [from: "BDD intro"]
2. 2025-11-04 — pytest-bdd's @scenario binding is brittle when steps are reused  [from: "First gotcha"]
3. 2026-02-18 — switched off pytest-bdd; plain pytest with helper fns scales further  [from: "Decision: drop bdd"]

Output:
{"entries": [
  {"flag": "new",         "ref": "",           "ref_quote": "", "note": "first entry, no predecessor"},
  {"flag": "extends",     "ref": "2025-09-12", "ref_quote": "pytest-bdd lets you write Gherkin", "note": "elaborates pytest-bdd practical limits"},
  {"flag": "contradicts", "ref": "2025-09-12", "ref_quote": "pytest-bdd lets you write Gherkin", "note": "drops pytest-bdd as net-negative"}
]}
```

Output a single JSON object exactly like the example. The "entries" array length MUST exactly match the input length and preserve input order.
"""


def build_linkage_user_prompt(
    concept: str,
    essence: str,
    entries: list,
    *,
    titles_by_id: dict[str, str] | None = None,
) -> str:
    """Render the per-hub linkage prompt.

    When ``titles_by_id`` is provided, each entry line includes the citing
    note's title as a `[from: "…"]` decoration so the model has more than a
    distilled artifact line to reason about. Falls back to no decoration
    when the title is missing or unknown.
    """
    titles_by_id = titles_by_id or {}
    essence_text = essence.strip() or "*No synthesis yet.*"
    lines = [
        f"Concept: `{concept}`",
        "",
        f"Essence:\n{essence_text}",
        "",
        "Entries (chronological):",
    ]
    for i, e in enumerate(entries, start=1):
        title = titles_by_id.get(e.citation, "").strip()
        suffix = f"  [from: \"{title}\"]" if title else ""
        lines.append(f"{i}. {e.date} — {e.text}{suffix}")
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
