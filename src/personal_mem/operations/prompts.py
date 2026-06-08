"""Probe-pressure helper — aggregates probe-classified prompts into
per-concept pressure scores.

Output is consumed as an additive bias by gap-emitting discover
strategies (``decision_review``, ``prompt_gap``), plus ``/dream``'s
scan phase to seed ``priority_signals``. Concept
substrate stays the same; the helper turns "what the user has been
asking about" into a dict the downstream consumers can multiply
against their existing scoring.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from personal_mem.core.config import Config


def recent_probe_pressure(
    cfg: Config,
    project: str | None = None,
    window_days: int = 14,
) -> dict[str, int]:
    """Aggregate probe-classified prompts into per-concept pressure.

    Walks recent probes via :func:`personal_mem.operations.search.query_prompts`
    (``classified_as="probe"``), tokenises each prompt against the merged
    set of canonical-ontology + indexed proposed concepts, and returns a
    frequency count per matching concept slug.

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

    Returns:
        Dict ``{concept_slug: probe_count}``. Empty when no probes in
        window or no vocabulary loaded.
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

    pressure: dict[str, int] = {}
    for row in probes:
        text_lower = (row.get("text") or "").lower()
        if not text_lower:
            continue
        # Tokenise against the vocabulary as a whole rather than splitting
        # words first — concepts like ``write-ahead-log`` or ``concept-edge``
        # contain hyphens and would be split apart by naive whitespace
        # tokenisation. Substring match catches them.
        matched: set[str] = set()
        for concept in vocabulary:
            # ``len(concept) >= 3`` guards against the single/2-char
            # garbage pool described in the docstring — those slugs
            # match every probe and drown real signal.
            if concept and len(concept) >= 3 and concept in text_lower:
                matched.add(concept)
        for concept in matched:
            pressure[concept] = pressure.get(concept, 0) + 1

    return pressure
