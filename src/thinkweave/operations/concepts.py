"""Concept operations — unified action dispatcher for the `weave_concepts` MCP tool.

Folds five previously separate MCP tools into one action-dispatched call:
- ``action='list'`` (default — was weave_concepts)
- ``action='tighten'`` (was weave_concepts_tighten)
- ``action='merge'`` (was weave_concepts_merge)
- ``action='search'`` (was weave_concept_search — now delegates to graph.walk)
- ``action='source_counts'`` (was weave_concept_source_counts)
- ``action='drift'`` (was weave_concepts_drift)
"""

from __future__ import annotations

from collections import defaultdict
import json

from thinkweave.core._utils import as_list
from thinkweave.core.config import Config


def list_concepts(cfg: Config, *, prefix: str = "", min_count: int = 1):
    """Return [(concept, count), …] sorted by count desc then name."""
    from thinkweave.core.indexer import Indexer

    idx = Indexer(config=cfg)
    counts: dict[str, int] = defaultdict(int)
    for row in idx.db.execute("SELECT frontmatter FROM notes"):
        fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
        for c in as_list(fm.get("concepts")):
            counts[c.lower()] += 1
    idx.close()

    p = (prefix or "").lower()
    return sorted(
        ((c, n) for c, n in counts.items() if n >= min_count and c.startswith(p)),
        key=lambda x: (-x[1], x[0]),
    )


def tighten(cfg: Config):
    """Find near-duplicate concepts via Levenshtein. Returns (duplicates, aliases, counts)."""
    from thinkweave.core.indexer import Indexer
    from thinkweave.synthesis.concepts import (
        find_near_duplicates,
        get_all_concepts,
        load_aliases,
    )

    idx = Indexer(config=cfg)
    counts = get_all_concepts(idx.db)
    idx.close()
    aliases = load_aliases(cfg)
    duplicates = find_near_duplicates(list(counts.keys()))
    return duplicates, aliases, counts


def merge(cfg: Config, from_concept: str, to_concept: str) -> dict:
    """Rename concept across all notes; persist alias; rebuild index. Returns stats.

    The losing concept's hub is FOLDED into the winner's and archived
    with a ``merged-into:`` tombstone (``fold_concept_hub_on_merge`` —
    same path the ``/dream`` apply step uses), never deleted. When the
    fold moved log entries, the winner lands on the seam-link queue for
    the next dream cycle's cross-parent linkage pass.
    """
    from thinkweave.core.indexer import Indexer
    from thinkweave.synthesis.concepts import (
        fold_concept_hub_on_merge,
        load_aliases,
        merge_concept_in_notes,
        save_aliases,
    )

    from_c = from_concept.lower()
    to_c = to_concept.lower()
    if from_c == to_c:
        raise ValueError("from_concept and to_concept are the same.")

    changed = merge_concept_in_notes(cfg.vault_root, from_c, to_c)

    aliases = load_aliases(cfg)
    existing = aliases.get(to_c, [])
    if from_c not in existing:
        existing.append(from_c)
    if from_c in aliases:
        for old in aliases.pop(from_c):
            if old != to_c and old not in existing:
                existing.append(old)
    aliases[to_c] = existing
    save_aliases(cfg, aliases)

    fold_stats = fold_concept_hub_on_merge(cfg, from_c, to_c)

    idx = Indexer(config=cfg)
    idx.rebuild(full=True)
    idx.close()

    return {"changed": changed, "hub_fold": fold_stats}


def source_counts(cfg: Config, concepts: list[str]) -> dict:
    from thinkweave.retrieval.search import Search

    s = Search(config=cfg)
    try:
        return s.get_concept_source_counts(concepts)
    finally:
        s.close()


def drift(
    cfg: Config,
    *,
    project: str = "",
    threshold: int = 5,
    max_items: int = 5,
    include_hubs: bool = False,
    hub_jaccard: float = 0.4,
):
    from thinkweave.synthesis.concepts import (
        drift_report,
        find_redundant_hub_candidates,
        format_drift_report,
    )

    report = drift_report(
        cfg, project=project, threshold=threshold, max_items=max_items
    )
    text = format_drift_report(report)

    candidates = []
    if include_hubs:
        candidates = find_redundant_hub_candidates(cfg, min_jaccard=hub_jaccard)

    return {"report": report, "text": text, "hub_candidates": candidates}


def project_concepts(cfg: Config, project: str) -> dict:
    from thinkweave.retrieval.search import Search

    s = Search(config=cfg)
    try:
        return s.get_project_concepts(project)
    finally:
        s.close()


def cooccurrence(cfg: Config, concept: str, *, limit: int = 10):
    from thinkweave.retrieval.search import Search

    s = Search(config=cfg)
    try:
        return s.get_concept_cooccurrence(concept, limit=limit)
    finally:
        s.close()
