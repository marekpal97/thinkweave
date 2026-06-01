"""Probe-pressure helper — aggregates probe-classified prompts into
per-concept pressure scores.

Output is consumed as an additive bias by every existing discover
strategy (``concept_coverage``, ``decision_review``, ``theme_drift``),
plus ``/dream``'s scan phase to seed ``priority_signals``. Concept
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

    Matching is case-insensitive substring — a probe like
    "How does FTS5 tokenize?" pressures both ``fts5`` and any other
    concept whose slug appears in the text. A single probe contributes
    pressure +1 per distinct concept it mentions (not per occurrence).
    Concepts the user has never explicitly asked about return zero
    pressure (callers should treat missing keys as 0).

    Args:
        cfg: vault config (drives ontology + index paths).
        project: project to scope prompts to. ``None`` uses
            ``cfg.default_project``.
        window_days: lookback window in days. Default 14 matches the
            audit's "recent" framing.

    Returns:
        Dict ``{concept_slug: probe_count}``. Empty when no probes in
        window or no vocabulary loaded.
    """
    scope_project = project or cfg.default_project
    if not scope_project:
        return {}

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
    probes = query_prompts(
        cfg,
        project=scope_project,
        since=cutoff,
        limit=500,
        classified_as="probe",
    )
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
            if concept and concept in text_lower:
                matched.add(concept)
        for concept in matched:
            pressure[concept] = pressure.get(concept, 0) + 1

    return pressure
