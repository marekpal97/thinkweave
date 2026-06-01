"""Anthropic Batches enrichment for materialized claude-code sessions.

Sibling to ``operations.drain.run_hubs_batch`` (which uses OpenAI
Batches for concept-hub backfill). Same shape — build per-item
requests, submit, poll, write back — but the work item is a
materialized session whose ``enrichment_status: pending``, and the
writeback updates the session's frontmatter + creates derived decision
notes.

Triggered by ``mem import claude-code --enrich --via batch``. Requires
``ANTHROPIC_API_KEY`` and the ``[seed]`` extra (``anthropic`` SDK).

Inline alternative: the ``/seed-enrich`` skill loops pending sessions
and calls ``mem_extract`` per-session via the running Claude Code
model — no API key, but no parallelism either. Both paths consume the
same ``enrichment_status: pending`` discriminator.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from personal_mem.core.config import Config

ENRICHMENT_SYSTEM = """You extract structured memory artifacts from a Claude Code session transcript.

Return ONLY valid JSON with this shape:
{
  "decisions": [
    {"title": "<imperative, <80 chars>", "narrative": "<2-4 sentence rationale>", "files_touched": ["<path>", ...], "outcome": "committed|abandoned|partial"}
  ],
  "insights": [
    {"type": "gotcha|pattern|trade-off|discovery", "narrative": "<1-3 sentence summary>"}
  ],
  "concepts": ["<existing-or-new ontology term>", ...]
}

Rules:
- decisions: capture explicit choices the user made or the assistant proposed and the user accepted. Skip exploratory chatter.
- insights: surprises, gotchas, recurring patterns, trade-offs the session surfaced. Empty list if none stand out.
- concepts: 2-6 specific terms (e.g. "fts5", "anthropic-batches"). Prefer terms that are likely to recur in other sessions.
- If the transcript is too thin to extract anything meaningful, return all three lists empty: {"decisions": [], "insights": [], "concepts": []}.
- Output JSON only, no prose, no fences."""


@dataclass
class PendingSession:
    """One materialized session ready for enrichment."""

    note_id: str
    project: str
    note_path: Path
    transcript: str
    title: str


def find_pending_sessions(
    cfg: Config,
    *,
    project_filter: str = "",
    limit: int = 0,
) -> list[PendingSession]:
    """Scan the index for sessions with ``enrichment_status: pending``.

    Walks the on-disk session notes (cheap — they're small) rather than
    re-querying the index, because frontmatter holds the discriminator.
    """
    from personal_mem.core.vault import VaultManager, parse_frontmatter

    vm = VaultManager(config=cfg)
    pending: list[PendingSession] = []

    sessions_root = vm.root / "projects"
    if not sessions_root.exists():
        return pending

    for project_dir in sorted(sessions_root.iterdir()):
        if not project_dir.is_dir():
            continue
        project = project_dir.name
        if project_filter and project != project_filter:
            continue
        sessions_dir = project_dir / "sessions"
        if not sessions_dir.exists():
            continue
        for session_subdir in sorted(sessions_dir.iterdir()):
            session_md = session_subdir / "session.md"
            if not session_md.exists():
                continue
            try:
                fm, body = parse_frontmatter(session_md.read_text(encoding="utf-8"))
            except Exception:
                continue
            if fm.get("imported_from") != "claude-code":
                continue
            if fm.get("enrichment_status") != "pending":
                continue
            pending.append(
                PendingSession(
                    note_id=fm.get("id", ""),
                    project=project,
                    note_path=session_md,
                    transcript=body,
                    title=fm.get("title", session_md.parent.name),
                )
            )
            if limit and len(pending) >= limit:
                return pending
    return pending


def _build_request_jsonl(
    pending: list[PendingSession],
    *,
    model: str,
    max_tokens: int,
) -> tuple[str, dict[str, str]]:
    """Render the JSONL Batches input + a custom_id → note_id mapping."""
    id_to_note: dict[str, str] = {}
    lines: list[str] = []
    for i, sess in enumerate(pending):
        custom_id = f"sess-{i:05d}"
        id_to_note[custom_id] = sess.note_id
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": ENRICHMENT_SYSTEM,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Project: {sess.project}\nSession title: {sess.title}\n\n"
                        f"--- transcript ---\n{sess.transcript}"
                    ),
                }
            ],
        }
        lines.append(
            json.dumps({"custom_id": custom_id, "params": body})
        )
    return "\n".join(lines) + "\n", id_to_note


def _writeback_one(
    cfg: Config,
    note_id: str,
    enrichment: dict,
) -> dict:
    """Update session frontmatter with enrichment + create decision notes.

    Returns counts: ``{decisions_created, insights_appended, concepts_added}``.
    """
    from personal_mem.core.indexer import Indexer
    from personal_mem.core.schemas import NoteType
    from personal_mem.core.vault import VaultManager

    vm = VaultManager(config=cfg)
    idx = Indexer(config=cfg)
    counts = {"decisions_created": 0, "insights_appended": 0, "concepts_added": 0}

    row = idx.db.execute(
        "SELECT path FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    if not row:
        idx.close()
        return counts
    session_path = vm.root / row["path"]

    fm_updates: dict = {
        "enrichment_status": "enriched",
        "enriched_at": datetime.now(timezone.utc).isoformat(),
    }
    concepts = [c for c in (enrichment.get("concepts") or []) if isinstance(c, str)]
    if concepts:
        fm_updates["concepts"] = concepts
        counts["concepts_added"] = len(concepts)

    insights = enrichment.get("insights") or []
    insights_block = ""
    if insights:
        lines = ["## Insights (extracted)", ""]
        for ins in insights:
            if not isinstance(ins, dict):
                continue
            t = ins.get("type", "discovery")
            n = ins.get("narrative", "")
            if n:
                lines.append(f"- **{t}** — {n}")
                counts["insights_appended"] += 1
        if counts["insights_appended"]:
            insights_block = "\n".join(lines) + "\n"

    vm.update_note(
        session_path,
        frontmatter_updates=fm_updates,
        body_append=insights_block or None,
    )
    idx.index_file(session_path)

    fm_session = next(
        (
            r for r in idx.db.execute(
                "SELECT project, frontmatter FROM notes WHERE id = ?", (note_id,)
            )
        ),
        None,
    )
    project = fm_session["project"] if fm_session else ""

    for dec in (enrichment.get("decisions") or []):
        if not isinstance(dec, dict):
            continue
        title = dec.get("title", "").strip()
        if not title:
            continue
        narrative = dec.get("narrative", "").strip()
        files_touched = [f for f in (dec.get("files_touched") or []) if isinstance(f, str)]
        outcome = dec.get("outcome", "")
        body_parts = []
        if narrative:
            body_parts += ["## Context", "", narrative, ""]
        body_parts += ["## Decision", "", title, ""]
        if files_touched:
            body_parts += ["## Files", "", ", ".join(files_touched), ""]
        body = "\n".join(body_parts).rstrip() + "\n"
        status = "accepted" if outcome == "committed" else "proposed"
        dec_path = vm.create_note(
            note_type=NoteType.DECISION,
            title=title,
            body=body,
            project=project,
            tags=["claude-code-seed"],
            extra_frontmatter={
                "source_session": note_id,
                "derived_from": note_id,
                "status": status,
                "outcome": outcome,
                "file_paths": files_touched,
                "concepts": concepts[:5] if concepts else [],
            },
            session_id=note_id,
        )
        idx.index_file(dec_path)
        counts["decisions_created"] += 1

    idx.close()
    return counts


def run_enrichment_batch(
    cfg: Config,
    *,
    project_filter: str = "",
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 1024,
    poll_interval: int = 60,
    limit: int = 0,
    dry_run: bool = False,
) -> dict:
    """Enrich pending materialized sessions via Anthropic Batches.

    Lifecycle: find pending → build JSONL → submit → poll → writeback.
    """
    pending = find_pending_sessions(cfg, project_filter=project_filter, limit=limit)
    stats: dict = {
        "pending": len(pending),
        "submitted": 0,
        "enriched": 0,
        "decisions_created": 0,
        "insights_appended": 0,
        "errors": [],
    }
    if not pending:
        print("No pending claude-code sessions found. Nothing to enrich.")
        return stats

    jsonl, id_to_note = _build_request_jsonl(
        pending, model=model, max_tokens=max_tokens
    )

    if dry_run:
        sample = pending[0]
        print(f"--- DRY RUN: would submit {len(pending)} request(s) to {model} ---")
        print(f"  first session: {sample.note_id} ({sample.project})")
        print(f"  transcript chars: {len(sample.transcript)}")
        print(f"  estimated input tokens (all sessions): "
              f"~{sum(len(s.transcript) for s in pending) // 4:,}")
        print()
        print("--- first request body (truncated) ---")
        print(jsonl.split('\n', 1)[0][:600] + "...")
        return stats

    try:
        import anthropic
    except ImportError:
        print(
            "mem import claude-code --enrich --via batch requires the "
            "Anthropic SDK.\n"
            "Install with: uv add --optional seed anthropic  (or `pip install anthropic`)"
        )
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY is not set in the environment.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    requests = []
    for line in jsonl.strip().split("\n"):
        requests.append(json.loads(line))

    print(f"Submitting batch of {len(requests)} session(s) to {model}...")
    batch = client.messages.batches.create(requests=requests)
    stats["submitted"] = len(requests)
    batch_id = batch.id
    print(f"Batch ID: {batch_id}")
    (cfg.mem_dir / "claude_code_seed_last_batch").write_text(
        json.dumps({"batch_id": batch_id, "submitted_at": datetime.now(timezone.utc).isoformat()}, indent=2),
        encoding="utf-8",
    )

    while True:
        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        print(
            f"  status={batch.processing_status} "
            f"succeeded={counts.succeeded} failed={counts.errored} "
            f"processing={counts.processing}"
        )
        if batch.processing_status == "ended":
            break
        time.sleep(poll_interval)

    print("Streaming results...")
    _spend_in = _spend_out = _spend_cr = _spend_cw = 0
    _spend_model = ""
    for result in client.messages.batches.results(batch_id):
        custom_id = result.custom_id
        note_id = id_to_note.get(custom_id, "")
        if not note_id:
            continue
        if result.result.type != "succeeded":
            stats["errors"].append(f"{custom_id}: {result.result.type}")
            continue
        message = result.result.message
        _mu = getattr(message, "usage", None)
        if _mu is not None:
            _spend_in += getattr(_mu, "input_tokens", 0) or 0
            _spend_out += getattr(_mu, "output_tokens", 0) or 0
            _spend_cr += getattr(_mu, "cache_read_input_tokens", 0) or 0
            _spend_cw += getattr(_mu, "cache_creation_input_tokens", 0) or 0
            _spend_model = getattr(message, "model", "") or _spend_model
        text = ""
        for block in message.content:
            if getattr(block, "type", "") == "text":
                text += block.text
        try:
            enrichment = json.loads(text.strip())
        except json.JSONDecodeError as e:
            stats["errors"].append(f"{note_id}: bad JSON ({e})")
            continue
        wb_counts = _writeback_one(cfg, note_id, enrichment)
        stats["enriched"] += 1
        stats["decisions_created"] += wb_counts["decisions_created"]
        stats["insights_appended"] += wb_counts["insights_appended"]

    if _spend_in or _spend_out:
        from personal_mem.core.spend import record_spend

        record_spend(
            "anthropic", _spend_model or "claude-opus-4", "onboard_enrich",
            _spend_in, _spend_out,
            tokens_cache_read=_spend_cr, tokens_cache_write=_spend_cw,
            mode="cli",
        )

    print(
        f"\nEnriched {stats['enriched']} session(s); "
        f"created {stats['decisions_created']} decision(s); "
        f"appended {stats['insights_appended']} insight(s)."
    )
    if stats["errors"]:
        print(f"  errors ({len(stats['errors'])}):")
        for err in stats["errors"][:10]:
            print(f"    {err}")
    return stats
