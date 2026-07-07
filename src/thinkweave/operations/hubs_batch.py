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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from thinkweave.core.config import Config


class PlanNotFoundError(FileNotFoundError):
    """Raised by :func:`run_hubs_batch` when the backfill plan is missing.

    The surface (``surfaces/cli/drain.py``) catches this, prints the
    ``weave hubs plan`` hint, and chooses the exit code — the operation
    stays pure (no print / no ``sys.exit``).
    """


@dataclass
class HubsBatchResult:
    """Structured outcome of :func:`run_hubs_batch`.

    Operations return data; the CLI surface formats the human-readable
    progress report (matching the legacy streamed output) and picks the
    exit code. Mirrors the ``wrap.py`` pattern.
    """

    concepts: int = 0
    requests_built: int = 0
    applied: int = 0
    essence_flagged: list[str] = field(default_factory=list)
    touched: int = 0
    errors: int = 0
    reindex_failures: int = 0
    reindex_contention_msg: str = ""
    # Capping (max-input-tokens budget) — populated only when a cap fires.
    capped: int = 0
    capped_tokens: int = 0
    deferred: int = 0
    budget: int = 0
    # Provider/model the fan-out was issued to (empty when nothing issued).
    provider: str = ""
    model: str = ""
    concurrency: int = 0
    issued: int = 0
    # Dry-run preview of the first request (None outside dry-run).
    dry_run: bool = False
    preview: dict | None = None


@dataclass
class RepairResult:
    """Structured outcome of :func:`repair_hubs`.

    The operation returns data; ``surfaces/cli/hubs.py`` formats the report
    and picks stdout / exit behaviour.
    """

    topics_missing: bool = False
    topics_dir: str = ""
    changed_hubs: int = 0
    date_updates: int = 0
    citation_cleanups: int = 0
    dry_run: bool = False
    would_rewrite: list[str] = field(default_factory=list)  # dry-run: hub filenames
    reindex_failures: int = 0
    reindex_contention_msg: str = ""


def repair_hubs(
    cfg: Config, *, concept: str | None = None, dry_run: bool = False
) -> RepairResult:
    """Retroactive fix: swap backfill dates for source-note dates, strip
    duplicated inline wikilink citations. No LLM calls.

    Pure operation — returns a :class:`RepairResult`; the CLI surface renders
    the human-readable summary and warnings. Lifted out of
    ``surfaces/cli/hubs.py::_hubs_repair`` (C2 purity sweep) so a second
    adapter can reuse it without capturing stdout.
    """
    import sqlite3 as _sqlite3

    from thinkweave.core.indexer import Indexer
    from thinkweave.synthesis.concept_hub import (
        _strip_inline_wikilinks,
        parse_concept_hub,
        topics_dir,
        write_concept_hub,
    )
    from thinkweave.synthesis.hub import build_id_path_map, build_id_title_map

    result = RepairResult(dry_run=dry_run)

    topics = topics_dir(cfg)
    if not topics.exists():
        result.topics_missing = True
        result.topics_dir = str(topics)
        return result

    idx = Indexer(config=cfg)
    id_to_date: dict[str, str] = {}
    for row in idx.db.execute(
        "SELECT id, date FROM notes WHERE date IS NOT NULL AND date != ''"
    ):
        id_to_date[row["id"]] = str(row["date"])[:10]
    # Path/title maps so the full re-render keeps citations path-based with
    # title aliases; path->id inverse so the parse recovers ids from those
    # links (else the entry.citation date lookup silently no-ops).
    idmap = build_id_path_map(idx.db)
    title_map = build_id_title_map(idx.db)
    path_to_id = {path: nid for nid, path in idmap.items()}
    idx.close()

    hub_files = sorted(topics.glob("*.md"))
    if concept:
        target = concept.lower()
        hub_files = [p for p in hub_files if p.stem == target]

    for hub_path in hub_files:
        hub = parse_concept_hub(hub_path, path_to_id=path_to_id)
        if not hub.log_entries:
            continue
        dirty = False
        for entry in hub.log_entries:
            new_date = id_to_date.get(entry.citation, entry.date)
            new_text = (
                _strip_inline_wikilinks(entry.text) if entry.text else entry.text
            )
            if new_date != entry.date:
                entry.date = new_date
                result.date_updates += 1
                dirty = True
            if new_text != entry.text:
                entry.text = new_text
                result.citation_cleanups += 1
                dirty = True
        if dirty:
            result.changed_hubs += 1
            if dry_run:
                result.would_rewrite.append(hub_path.name)
            else:
                write_concept_hub(hub, idmap=idmap, title_map=title_map)

    if dry_run:
        return result

    idx = Indexer(config=cfg)
    for hub_path in hub_files:
        if not hub_path.exists():
            continue
        try:
            idx.index_file(hub_path)
        except _sqlite3.OperationalError as e:
            result.reindex_failures += 1
            if result.reindex_failures == 1:
                result.reindex_contention_msg = str(e)
    idx.close()
    return result


def run_hubs_batch(
    cfg: Config,
    *,
    plan_path: Path | None = None,
    model: str | None = None,
    max_tokens: int = 1024,
    poll_interval: int = 30,
    max_input_tokens: int = 4_500_000,
    dry_run: bool = False,
) -> HubsBatchResult:
    """Execute a concept-hub backfill plan via the wrapper's async fan-out.

    Returns a :class:`HubsBatchResult`; the CLI surface renders progress and
    chooses the exit code. Raises :class:`PlanNotFoundError` when the plan
    file is missing (the surface turns that into the hint + non-zero exit).

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
        raise PlanNotFoundError(str(plan_path))

    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    concept_plans = payload.get("concepts", [])
    if not concept_plans:
        return HubsBatchResult(concepts=0, applied=0)

    result = HubsBatchResult(concepts=len(concept_plans))
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

    result.requests_built = len(requests_to_send)

    if dry_run:
        result.dry_run = True
        if requests_to_send:
            r = requests_to_send[0]
            result.preview = {
                "concept": r["concept"],
                "note_id": r["note_id"],
                "system_chars": len(r["system"]),
                "user_chars": len(r["user"]),
                "user_head": r["user"][:800],
            }
        return result

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
            result.capped = len(capped)
            result.capped_tokens = total_tokens
            result.deferred = deferred
            result.budget = budget
        sorted_requests = capped

    if not sorted_requests:
        return result

    result.issued = len(sorted_requests)
    result.provider = provider
    result.model = effective_model
    result.concurrency = concurrency

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
    for req, res in zip(sorted_requests, results):
        if isinstance(res, BaseException):
            errors += 1
            continue
        text, _usage = res
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

    result.applied = applied
    result.essence_flagged = sorted(essence_flagged)
    result.errors = errors

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
                result.reindex_contention_msg = str(e)
    idx.close()
    result.touched = len(touched_concepts)
    result.reindex_failures = reindex_failures

    return result


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
