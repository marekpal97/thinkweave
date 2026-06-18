"""Claude-code session synthesis — the ``--via batch`` backend.

One of two interchangeable backends behind "generation is synthesis" for
imported sessions (the other is the keyless ``/synthesize-sessions`` inline
skill). Both run the *same* spec — :data:`thinkweave.synthesis.
session_synthesis.SYNTHESIS_SYSTEM` — and both write back through
:func:`thinkweave.operations.extract.extract_session`, so an imported-then-
synthesised session is the same shape as a live ``/wrap`` session:
ontology-gated concepts, commit-evidence decision flips, the lot.

This backend fans the prompt out across pending sessions via
:func:`thinkweave.core.agent_client.batch_completions_sync` (N async
completions in parallel — the Anthropic Batches dance was deleted 2026-06-06,
see [[feedback_unified_wrapper_no_batches_apis]]).

Provider + model are resolved from ``vault/config/api.yaml`` via
``resolve_for_op(..., "claude_code_enrich")``. With the seeded ``overrides: {}``
that falls straight through to ``completion.provider`` — there is **no**
hardcoded provider. Set an ``overrides.claude_code_enrich`` block only to
pin a different provider/model for this op specifically.

Triggered by ``weave import claude-code --enrich [--via batch]``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from thinkweave.core.config import Config


@dataclass
class PendingSession:
    """One imported session awaiting synthesis."""

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
    """Scan on-disk session notes for imported-but-not-yet-synthesised ones.

    The gate is ``imported_from == "claude-code"`` AND not ``processed`` —
    ``processed: true`` is the canonical "synthesised" marker (stamped by
    ``extract_session``), identical to a live-wrapped session. The legacy
    ``enrichment_status: pending`` discriminator was retired with the
    transcript→companion move.

    Walks the disk (not the index) because the discriminator is frontmatter;
    transcript is read from the ``transcript.md`` companion if it's already
    been archived (a mid-run re-entry), else from the still-inline body.
    """
    from thinkweave.core.vault import VaultManager, parse_frontmatter
    from thinkweave.synthesis.session_synthesis import TRANSCRIPT_COMPANION

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
            if fm.get("processed"):
                continue
            # Prefer the archived companion (re-entry after a partial run);
            # fall back to the still-inline transcript body.
            transcript = body
            companion = session_subdir / TRANSCRIPT_COMPANION
            if "## Transcript" not in body and companion.exists():
                try:
                    transcript = companion.read_text(encoding="utf-8")
                except OSError:
                    transcript = body
            pending.append(
                PendingSession(
                    note_id=fm.get("id", ""),
                    project=project,
                    note_path=session_md,
                    transcript=transcript,
                    title=fm.get("title", session_subdir.name),
                )
            )
            if limit and len(pending) >= limit:
                return pending
    return pending


def _synthesize_one(cfg: Config, sess: PendingSession, inputs: dict) -> dict:
    """Apply one parsed synthesis: archive transcript, set session concepts,
    then create insight/decision notes + summary via ``extract_session``.

    Returns ``{decisions_created, insights_created, concepts_added}``.
    """
    from thinkweave.core.indexer import Indexer
    from thinkweave.core.vault import VaultManager
    from thinkweave.operations.extract import extract_session
    from thinkweave.synthesis.concepts import (
        build_keep_set,
        load_ontology,
        split_concepts_by_ontology,
    )

    vm = VaultManager(config=cfg)

    # 1. Session-level concepts, ontology-gated (canonical → `concepts:`, the
    #    rest → `proposed_concepts:` for /tighten to review later). Set on the
    #    session note before extraction; the inline skill mirrors this via
    #    weave_update so both backends tag the session identically.
    canonical, proposed = split_concepts_by_ontology(
        inputs["concepts"], ontology_keep=build_keep_set(load_ontology())
    )
    fm_updates: dict = {}
    if canonical:
        fm_updates["concepts"] = canonical
    if proposed:
        fm_updates["proposed_concepts"] = proposed
    if fm_updates:
        vm.update_note(sess.note_path, frontmatter_updates=fm_updates)

    # 2. Ensure the session is indexed so extract_session resolves it by id
    #    (a standalone `--enrich` run may follow a materialize that never
    #    indexed). Without this, get_note_by_id misses → a duplicate session.
    idx = Indexer(config=cfg)
    idx.index_file(sess.note_path)
    idx.close()

    # 3. The shared writeback — same path /wrap uses. extract_session
    #    archives the verbatim transcript to a companion (transcript → summary
    #    body) as part of synthesising an import.
    outcome = extract_session(
        cfg,
        session_id=sess.note_id,
        summary=inputs["summary"],
        insights=inputs["insights"],
        decisions=inputs["decisions"],
    )
    return {
        "decisions_created": len(outcome.created_decisions),
        "insights_created": len(outcome.created_notes),
        "concepts_added": len(canonical),
    }


def fanout_plan(cfg: Config, n_pending: int) -> dict:
    """How the inline ``/seed-enrich`` skill should process ``n_pending`` sessions.

    ``mode='inline'`` when ``n_pending <= enrich_fanout_threshold`` (synthesise
    in-process — no subagent spawn for a small backlog, per the "no subagent for
    small wraps" finding). Otherwise ``mode='fanout'``: batch into groups of
    ``enrich_batch_size`` and spawn up to ``enrich_parallelism`` workers at a
    time. The skill reads this off the ``--dry-run`` ``FANOUT`` line so the
    decision is deterministic and config-driven, not improvised by the model.
    """
    threshold = cfg.enrich_fanout_threshold
    if n_pending <= threshold:
        return {"mode": "inline", "threshold": threshold,
                "batch_size": 0, "parallelism": 0}
    return {
        "mode": "fanout",
        "threshold": threshold,
        "batch_size": cfg.enrich_batch_size,
        "parallelism": cfg.enrich_parallelism,
    }


def run_enrichment_batch(
    cfg: Config,
    *,
    project_filter: str = "",
    model: str | None = None,
    max_tokens: int = 2048,
    poll_interval: int = 60,
    limit: int = 0,
    dry_run: bool = False,
    finalize: bool = True,
) -> dict:
    """Synthesise pending imported sessions via the wrapper's async fan-out.

    Lifecycle: find pending → build prompts → ``batch_completions_sync`` →
    per result: ``extract_session`` writeback.

    Provider / model: when ``model`` is ``None`` (typical), reads
    ``api.yaml`` via ``resolve_for_op(..., "claude_code_enrich")``, which
    falls through to ``completion.provider`` unless an override pins one.
    ``poll_interval`` is accepted for CLI back-compat — there's no polling.
    """
    del poll_interval  # back-compat only; no polling under the wrapper path

    pending = find_pending_sessions(cfg, project_filter=project_filter, limit=limit)
    stats: dict = {
        "pending": len(pending),
        "submitted": 0,
        "synthesized": 0,
        "decisions_created": 0,
        "insights_created": 0,
        "concepts_added": 0,
        "errors": [],
    }
    if not pending:
        print("No pending claude-code sessions found. Nothing to synthesise.")
        return stats

    from thinkweave.core.api_config import load_api_config, resolve_for_op
    from thinkweave.synthesis.session_synthesis import (
        SYNTHESIS_SYSTEM,
        build_user_prompt,
        parse_synthesis,
        to_extract_inputs,
    )

    op_cfg = resolve_for_op(load_api_config(cfg.vault_root), "claude_code_enrich")
    provider = op_cfg["provider"]
    effective_model = model or op_cfg["model"]
    concurrency = int(op_cfg.get("batch_concurrency", 20))

    prompts = [
        build_user_prompt(project=s.project, title=s.title, transcript=s.transcript)
        for s in pending
    ]

    if dry_run:
        print(
            f"--- DRY RUN: {len(pending)} pending session(s) would synthesise via "
            f"{provider}/{effective_model} ---"
        )
        print(
            f"  estimated input tokens (all sessions): "
            f"~{sum(len(s.transcript) for s in pending) // 4:,}"
        )
        # Machine-parseable plan + pending list — the inline /seed-enrich skill
        # reads the FANOUT line to decide inline-vs-fan-out deterministically,
        # then walks the PENDING lines as its worklist (one tab-separated each).
        plan = fanout_plan(cfg, len(pending))
        print(
            f"FANOUT\t{plan['mode']}\t{plan['threshold']}\t"
            f"{plan['batch_size']}\t{plan['parallelism']}"
        )
        for s in pending:
            print(f"PENDING\t{s.note_id}\t{s.project}\t{s.title}")
        return stats

    print(
        f"Issuing {len(prompts)} request(s) to {provider}/{effective_model} "
        f"(concurrency={concurrency})..."
    )
    stats["submitted"] = len(prompts)

    from thinkweave.core.agent_client import batch_completions_sync

    results = batch_completions_sync(
        prompts,
        provider=provider,
        model=effective_model,
        max_tokens=max_tokens,
        system=SYNTHESIS_SYSTEM,
        concurrency=concurrency,
        return_exceptions=True,
    )

    for sess, result in zip(pending, results):
        if isinstance(result, BaseException):
            stats["errors"].append(f"{sess.note_id}: {result.__class__.__name__}: {result}")
            continue
        text, _usage = result
        parsed = parse_synthesis(text)
        if parsed is None:
            stats["errors"].append(f"{sess.note_id}: unparseable synthesis response")
            continue
        try:
            counts = _synthesize_one(cfg, sess, to_extract_inputs(parsed))
        except Exception as e:  # noqa: BLE001 — one bad session shouldn't kill the batch
            stats["errors"].append(f"{sess.note_id}: writeback {type(e).__name__}: {e}")
            continue
        stats["synthesized"] += 1
        stats["decisions_created"] += counts["decisions_created"]
        stats["insights_created"] += counts["insights_created"]
        stats["concepts_added"] += counts["concepts_added"]

    print(
        f"\nSynthesised {stats['synthesized']} session(s); "
        f"created {stats['decisions_created']} decision(s), "
        f"{stats['insights_created']} insight(s); "
        f"{stats['concepts_added']} concept(s) tagged."
    )
    if stats["errors"]:
        print(f"  errors ({len(stats['errors'])}):")
        for err in stats["errors"][:10]:
            print(f"    {err}")

    if finalize and stats["synthesized"] > 0:
        # Batch-grain finalize. Per-session `extract_session` wrote the notes
        # but skipped the deterministic tail — running prune/index/judge/landing
        # per session across a large backfill is quadratic. Index once +
        # refresh landing for the touched projects so the synthesised sessions
        # are immediately retrievable and visible in DECISIONS/BACKLOG. This is
        # the batch analogue of the per-session `weave wrap-finalize` that live
        # /wrap and dream-wrap-worker run; the inline /seed-enrich skill
        # does the same once after its fan-out. (Judge/drift are skipped:
        # imported historical decisions carry no forward prediction to judge.)
        from thinkweave.core.indexer import Indexer
        from thinkweave.operations.landing import render_landing

        idx = Indexer(config=cfg)
        istats = idx.rebuild(full=False)
        idx.close()
        touched = sorted({s.project for s in pending if s.project})
        for proj in touched:
            render_landing(cfg, project=proj, doc="all")
        print(
            f"Finalized: indexed {istats.get('indexed', 0)} note(s); "
            f"refreshed landing for {len(touched)} project(s)."
        )

    return stats
