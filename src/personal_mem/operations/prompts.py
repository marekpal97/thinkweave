"""Probe-pressure helper — aggregates probe-classified prompts into
per-concept pressure scores (and, for the detail variant, the probe
texts themselves).

The count projection (:func:`recent_probe_pressure`) is consumed as an
additive bias by gap-emitting discover strategies (``decision_review``,
``prompt_gap``) and landing's ``probe_matches_24h``. The detail variant
(:func:`recent_probe_details`) feeds ``/dream``'s scan phase to seed
``priority_signals`` — carrying the texts forward is what makes probes
first-class on the acquisition rail (queue items inherit them so
``/drain`` can tighten search queries to the user's actual questions).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from personal_mem.core.config import Config
from personal_mem.core.events import match_probe_concepts


# Probe texts carried per concept by :func:`recent_probe_details`.
# Three keeps the worker prompt lean while still showing the shape of
# what the user asked; 240 chars survives multi-sentence questions
# without dragging pasted code blocks along.
_TEXTS_PER_CONCEPT = 3
_TEXT_TRUNCATE = 240


def recent_probe_pressure(
    cfg: Config,
    project: str | None = None,
    window_days: int = 14,
) -> dict[str, int]:
    """Aggregate probe-classified prompts into per-concept pressure.

    Count-only projection of :func:`recent_probe_details` — kept for the
    consumers that multiply pressure against their own scoring
    (``decision_review`` bias, landing's ``probe_matches_24h``) and don't
    need the underlying probe texts.
    """
    return {
        concept: detail["count"]
        for concept, detail in recent_probe_details(
            cfg, project=project, window_days=window_days
        ).items()
    }


def recent_probe_details(
    cfg: Config,
    project: str | None = None,
    window_days: int = 14,
    texts_per_concept: int = _TEXTS_PER_CONCEPT,
) -> dict[str, dict]:
    """Aggregate probe-classified prompts into per-concept pressure + texts.

    Walks recent probes via :func:`personal_mem.operations.search.query_prompts`
    (``classified_as="probe"``), tokenises each prompt against the merged
    set of canonical-ontology + indexed proposed concepts, and returns,
    per matching concept slug, the frequency count **and** the most
    recent probe texts themselves. Keeping the texts is what lets the
    acquisition side (``priority_signals`` → queue items → ``/drain``)
    tighten its search queries to what the user actually asked, instead
    of working from the concept slug alone.

    Matching is case-insensitive substring with a 3-char minimum on the
    concept slug — a probe like "How does FTS5 tokenize?" pressures both
    ``fts5`` and any other concept whose slug appears in the text. A
    single probe contributes pressure +1 per distinct concept it
    mentions (not per occurrence). Concepts the user has never
    explicitly asked about return zero pressure (callers should treat
    missing keys as 0).

    The 3-char minimum defends against the single-char concept-pool
    pollution surfaced by the 2026-06-07 str-iter bug class (entries
    like ``-``, ``[``, ``]``, plus 18 individual letters survived as
    ``proposed_concepts``); these otherwise match every probe and drown
    real signal. Two-char concepts (``ai``, ``hf``) are real but rare
    enough that the false-positive cost dominates — they're filtered
    too; consumers needing 2-char terms should canonicalise them with
    longer aliases.

    Args:
        cfg: vault config (drives ontology + index paths).
        project: project to scope prompts to. ``None`` falls back to
            ``cfg.default_project``; empty string (or both empty) means
            **vault-wide** — probes from every project under
            ``vault/projects/`` are aggregated. Vault-wide is the
            common case: every other ``/dream`` scan surface is
            vault-global, and gap-emitter strategies don't always have
            a project context.
        window_days: lookback window in days. Default 14 matches the
            audit's "recent" framing.
        texts_per_concept: how many probe texts to keep per concept,
            most-recent-first. Texts are truncated to ~240 chars and
            exact duplicates are dropped (re-asking the same question
            still counts toward ``count``).

    Returns:
        Dict ``{concept_slug: {"count": int, "probes": [text, ...]}}``.
        Empty when no probes in window or no vocabulary loaded.
    """
    # Resolve scope: explicit project > cfg.default_project > vault-wide.
    # The vault-wide fallback (project=="") matters because dream.scan
    # and gap-emitter strategies often run without a project context;
    # every other scan surface is vault-global, so probe pressure should
    # match.
    scope_project = project if project is not None else cfg.default_project

    # Local imports avoid circular wiring: this module is imported by
    # discover strategies, which are imported during /discover, which
    # sits beneath the indexer + ontology layer.
    from personal_mem.core.indexer import Indexer
    from personal_mem.operations.search import query_prompts
    from personal_mem.synthesis.concepts import (
        build_keep_set,
        get_all_proposed_concepts,
        load_ontology,
    )

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=window_days)
    ).isoformat()

    if scope_project:
        probes = query_prompts(
            cfg,
            project=scope_project,
            since=cutoff,
            limit=500,
            classified_as="probe",
        )
    else:
        # Vault-wide: enumerate projects on disk and union their probes.
        # query_prompts is project-scoped (it walks
        # vault/projects/<p>/sessions/), so vault-wide reduces to a
        # per-project fan-in with a final recency sort + cap.
        probes = []
        projects_root = cfg.vault_root / "projects"
        if projects_root.exists():
            for proj_dir in projects_root.iterdir():
                if not proj_dir.is_dir():
                    continue
                probes.extend(
                    query_prompts(
                        cfg,
                        project=proj_dir.name,
                        since=cutoff,
                        limit=500,
                        classified_as="probe",
                    )
                )
        # Recency-sort + global cap so we don't over-weight projects
        # with deeper history.
        probes.sort(key=lambda r: r.get("ts") or "", reverse=True)
        probes = probes[:500]

    if not probes:
        return {}

    vocabulary: set[str] = build_keep_set(load_ontology())
    try:
        idx = Indexer(config=cfg)
        try:
            vocabulary.update(get_all_proposed_concepts(idx.db).keys())
        finally:
            idx.close()
    except Exception:
        # An unindexed vault is valid — fall back to canonical only.
        pass

    if not vocabulary:
        return {}

    # Most-recent-first so the texts kept per concept are the freshest
    # framing of the question (the vault-wide branch already sorted; the
    # project-scoped branch returns walk order).
    probes.sort(key=lambda r: r.get("ts") or "", reverse=True)

    details: dict[str, dict] = {}
    for row in probes:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        # Shared attribution rule with the prompt_concepts SQL projection
        # (substring match, 3-char slug minimum) — see
        # core.events.match_probe_concepts for the rationale.
        matched = match_probe_concepts(text, vocabulary)
        for concept in matched:
            detail = details.setdefault(concept, {"count": 0, "probes": []})
            detail["count"] += 1
            snippet = text[:_TEXT_TRUNCATE]
            if (
                len(detail["probes"]) < texts_per_concept
                and snippet not in detail["probes"]
            ):
                detail["probes"].append(snippet)

    return details
