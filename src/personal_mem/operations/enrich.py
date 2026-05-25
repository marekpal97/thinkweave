"""Concept-enrichment operation — thin orchestrator over the root-level
``personal_mem.enrich`` module.

Wraps the LLM concept-tagging pass and the post-enrichment reindex /
wikilink-materialization step that the MCP ``mem_enrich`` tool needs to run
once concepts have actually been written to disk. Pure: returns stats, never
prints.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.enrich import enrich as _enrich


@dataclass
class EnrichResult:
    """Stats from :func:`run_enrich`."""

    stats: dict = field(default_factory=dict)
    reindex_stats: dict | None = None
    wikilink_stats: dict | None = None
    dry_run: bool = False


def run_enrich(
    cfg: Config,
    *,
    project: str = "",
    note_types: list[str] | None = None,
    limit: int = 0,
    force: bool = False,
    dry_run: bool = False,
) -> EnrichResult:
    """Run LLM concept enrichment, then (unless dry-run) reindex + materialize links."""
    types = note_types or ["session", "note", "decision", "source"]
    stats = _enrich(
        cfg,
        project=project,
        note_types=types,
        limit=limit,
        force=force,
        dry_run=dry_run,
    )
    out = EnrichResult(stats=stats, dry_run=dry_run)
    if not dry_run and stats.get("enriched", 0) > 0:
        idx = Indexer(config=cfg)
        out.reindex_stats = idx.rebuild(full=True)
        out.wikilink_stats = idx.materialize_links(max_links=5)
        idx.rebuild(full=False)
        idx.close()
    return out
