"""Hub-backfill orchestrator — concept-hub catalyst-log backfill.

Renamed from ``operations/drain.py`` to disambiguate from the ``/drain`` skill,
which drains per-source-type acquisition queues — a different object entirely.
CLI ``weave drain --target hubs --via batch`` calls into ``run_hubs_batch``.

The OpenAI Batches submission / polling / fetching dance was deleted
2026-06-06 (plan: ``go-back-to-the-scalable-firefly.md`` step C2). The
orchestrator now delegates execution to
:func:`thinkweave.core.agent_client.batch_completions_sync`, which fires
N async completions in parallel under a semaphore-capped concurrency budget
([[feedback_unified_wrapper_no_batches_apis]]). ~50% per-token discount
forfeited for one code path.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from thinkweave.core.config import Config


def run_hubs_batch(
    cfg: Config,
    *,
    plan_path: Path | None = None,
    model: str | None = None,
    max_tokens: int = 1024,
    poll_interval: int = 30,
    max_input_tokens: int = 4_500_000,
    dry_run: bool = False,
) -> dict:
    """Execute a concept-hub backfill plan via the wrapper's async fan-out.

    Returns a stats dict; prints progress to stdout (legacy behaviour preserved).
    Exits the process via sys.exit on hard errors (missing plan).

    Provider / model resolution: when ``model`` is ``None`` (typical), reads
    ``vault/config/api.yaml::overrides.hubs_run`` for the effective provider
    and model. Explicit ``model=`` (from a CLI flag) overrides the api.yaml
    model; provider always comes from api.yaml. ``poll_interval`` is kept in
    the signature for back-compat — there's no polling anymore.
    """
    del poll_interval  # accepted for back-compat with the CLI flag; unused
    from thinkweave.core.indexer import Indexer
    from thinkweave.core.vault import VaultManager, parse_frontmatter
    from thinkweave.synthesis.concept_hub import (
        HUB_EXTRACTION_SYSTEM,
        append_log_entries,
        build_extraction_user_prompt,
        concept_hub_path,
        parse_concept_hub,
        parse_llm_response,
    )

    plan_path = plan_path or (cfg.weave_dir / "hubs_plan.json")
    if not plan_path.exists():
        print(f"Plan file not found: {plan_path}")
        print("Run `weave hubs plan` first.")
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

    # Resolve provider + model from api.yaml::overrides.hubs_run.
    from thinkweave.core.api_config import load_api_config, resolve_for_op
    op_cfg = resolve_for_op(load_api_config(cfg.vault_root), "hubs_run")
    provider = op_cfg["provider"]
    effective_model = model or op_cfg["model"]
    concurrency = int(op_cfg.get("batch_concurrency", 20))

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
                f"{deferred} request(s) deferred — rerun `weave hubs plan` + "
                f"`weave drain --target hubs --via batch` after this batch "
                f"completes."
            )
        sorted_requests = capped

    if not sorted_requests:
        return {"applied": 0, "concepts": len(concept_plans)}

    print(
        f"Issuing {len(sorted_requests)} request(s) to {provider}/{effective_model} "
        f"(concurrency={concurrency})..."
    )

    # All hub requests share HUB_EXTRACTION_SYSTEM — pass it once as the
    # uniform system prompt for the batch.
    from thinkweave.core.agent_client import batch_completions_sync
    prompts = [r["user"] for r in sorted_requests]
    results = batch_completions_sync(
        prompts,
        provider=provider,
        model=effective_model,
        max_tokens=max_tokens,
        system=HUB_EXTRACTION_SYSTEM,
        concurrency=concurrency,
        return_exceptions=True,
    )

    applied = 0
    essence_flagged: set[str] = set()
    errors = 0
    for req, result in zip(sorted_requests, results):
        if isinstance(result, BaseException):
            errors += 1
            continue
        text, _usage = result
        if not text:
            continue
        entries, needs_essence = parse_llm_response(
            text, note_id=req["note_id"], run_date=req["note_date"]
        )
        if entries:
            append_log_entries(cfg, req["concept"], entries)
            applied += len(entries)
        if needs_essence:
            essence_flagged.add(req["concept"])

    if errors:
        print(f"  warning: {errors} request(s) failed; rerun to retry the rest")

    print(f"\nApplied {applied} new log entries.")
    if essence_flagged:
        print(f"Essence revision flagged for {len(essence_flagged)} concept(s):")
        for c in sorted(essence_flagged):
            print(f"  {c}")
        print("Run /weave-resolve-concepts to review flagged essences.")

    import sqlite3 as _sqlite3

    idx = Indexer(config=cfg)
    touched_concepts = {r["concept"] for r in sorted_requests}
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
            f"contention. Run `uv run weave index` once the contending process "
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
        from thinkweave.core.vault import parse_frontmatter as _pf

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
    from thinkweave.synthesis.concept_hub import ALLOWED_FLAGS

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
