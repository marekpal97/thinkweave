"""Claude-code session enrichment orchestrator.

Sibling to :func:`personal_mem.operations.hubs_batch.run_hubs_batch`.
Same shape — find pending work items, build per-item prompts, write
results back — but the item is a materialized session whose
``enrichment_status: pending``, and the writeback updates the session
frontmatter + creates derived decision notes.

Triggered by ``mem import claude-code --enrich --via batch``.

Provider Batches dance deleted 2026-06-06 (plan:
``go-back-to-the-scalable-firefly.md`` step C2). Now delegates execution
to :func:`personal_mem.core.agent_client.batch_completions_sync`, which
fans out N async completions in parallel
([[feedback_unified_wrapper_no_batches_apis]]). The Anthropic Batches
50% discount is forfeited for one code path.

Provider + model are resolved from
``vault/config/api.yaml::overrides.claude_code_enrich`` (default
provider ``anthropic``, model ``claude-haiku-4-5-20251001``).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
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


def _build_user_prompt(sess: PendingSession) -> str:
    """Render a single session's user-prompt body."""
    return (
        f"Project: {sess.project}\nSession title: {sess.title}\n\n"
        f"--- transcript ---\n{sess.transcript}"
    )


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
    model: str | None = None,
    max_tokens: int = 1024,
    poll_interval: int = 60,
    limit: int = 0,
    dry_run: bool = False,
) -> dict:
    """Enrich pending materialized sessions via the wrapper's async fan-out.

    Lifecycle: find pending → build prompts → ``batch_completions_sync``
    → writeback per result.

    Provider / model resolution: when ``model`` is ``None`` (typical), reads
    ``vault/config/api.yaml::overrides.claude_code_enrich`` for the
    effective provider and model. Defaults are Anthropic /
    ``claude-haiku-4-5-20251001``. ``poll_interval`` is accepted for
    back-compat with the CLI flag — there's no polling anymore.
    """
    del poll_interval  # back-compat only; no polling under the new path

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

    # Resolve provider + model from api.yaml::overrides.claude_code_enrich.
    from personal_mem.core.api_config import load_api_config, resolve_for_op
    op_cfg = resolve_for_op(load_api_config(cfg.vault_root), "claude_code_enrich")
    provider = op_cfg["provider"]
    effective_model = model or op_cfg["model"]
    concurrency = int(op_cfg.get("batch_concurrency", 20))

    prompts = [_build_user_prompt(s) for s in pending]

    if dry_run:
        sample = pending[0]
        print(
            f"--- DRY RUN: would issue {len(pending)} request(s) to "
            f"{provider}/{effective_model} ---"
        )
        print(f"  first session: {sample.note_id} ({sample.project})")
        print(f"  transcript chars: {len(sample.transcript)}")
        print(
            f"  estimated input tokens (all sessions): "
            f"~{sum(len(s.transcript) for s in pending) // 4:,}"
        )
        print()
        print("--- first user prompt (truncated) ---")
        print(prompts[0][:600] + "...")
        return stats

    print(
        f"Issuing {len(prompts)} request(s) to {provider}/{effective_model} "
        f"(concurrency={concurrency})..."
    )
    stats["submitted"] = len(prompts)

    from personal_mem.core.agent_client import batch_completions_sync
    results = batch_completions_sync(
        prompts,
        provider=provider,
        model=effective_model,
        op="claude_code_enrich",
        max_tokens=max_tokens,
        system=ENRICHMENT_SYSTEM,
        concurrency=concurrency,
        mode="cli",
        return_exceptions=True,
    )

    for sess, result in zip(pending, results):
        if isinstance(result, BaseException):
            stats["errors"].append(f"{sess.note_id}: {result.__class__.__name__}: {result}")
            continue
        text, _usage = result
        if not text:
            stats["errors"].append(f"{sess.note_id}: empty response")
            continue
        try:
            enrichment = json.loads(text.strip())
        except json.JSONDecodeError as e:
            stats["errors"].append(f"{sess.note_id}: bad JSON ({e})")
            continue
        wb_counts = _writeback_one(cfg, sess.note_id, enrichment)
        stats["enriched"] += 1
        stats["decisions_created"] += wb_counts["decisions_created"]
        stats["insights_appended"] += wb_counts["insights_appended"]

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
