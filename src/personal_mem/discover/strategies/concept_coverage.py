"""Concept-coverage strategy — the original ``/discover`` default.

Walks the ``note_concepts`` index, ranks load-bearing concepts by
mention count, and surfaces concepts where the source-note coverage
falls below a configurable threshold (``min_sources``, default 2).

This strategy emits **gap descriptors**, not finished search hits — the
companion ``/discover`` skill reads the descriptors and runs WebSearch
queries inline to populate them. Each emitted dict is shaped like a
queue item placeholder:

    {
        "strategy": "concept_coverage",
        "concept": "graph-memory",
        "domains": ["ai/memory"],
        "mention_count": 14,
        "source_count": 1,
        "title": "Gap: graph-memory (1 source / 14 mentions)",
        "kind": "gap",
        "queue": "research",
    }

The CLI's ``mem discover`` then routes these into the appropriate
queue (the project's backlog or research-focus landing doc, or a
per-source-type JSONL queue) per the project config.
"""

from __future__ import annotations

import sqlite3
from typing import Any


class ConceptCoverageStrategy:
    name = "concept_coverage"

    def run(
        self,
        vault: Any,
        project: str | None,
        config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        config = config or {}
        params = self._params(config)

        cfg = getattr(vault, "config", None) or vault
        db_path = getattr(cfg, "index_db", None)
        if db_path is None or not db_path.exists():
            return []

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            return self._gather(db, project, params)
        finally:
            db.close()

    def _params(self, config: dict[str, Any]) -> dict[str, Any]:
        strategies_cfg = (
            config.get("projects", {})
            .get("default", {})
            .get("concept_coverage", {})
        )
        return {
            "min_mentions": int(strategies_cfg.get("min_mentions", 3)),
            "min_sources": int(strategies_cfg.get("min_sources", 2)),
            "limit": int(strategies_cfg.get("limit", 5)),
        }

    def _gather(
        self,
        db: sqlite3.Connection,
        project: str | None,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        # Mention counts — restrict to project if given.
        if project:
            rows = db.execute(
                """
                SELECT nc.concept, nc.domain, COUNT(*) AS n
                FROM note_concepts nc
                JOIN notes n ON n.id = nc.note_id
                WHERE n.project = ?
                GROUP BY nc.concept
                ORDER BY n DESC
                """,
                (project,),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT nc.concept, nc.domain, COUNT(*) AS n
                FROM note_concepts nc
                GROUP BY nc.concept
                ORDER BY n DESC
                """,
            ).fetchall()

        candidates: list[dict[str, Any]] = []
        for row in rows:
            mentions = int(row["n"] or 0)
            if mentions < params["min_mentions"]:
                continue
            concept = row["concept"]
            source_count = self._count_sources(db, concept)
            if source_count >= params["min_sources"]:
                continue
            domains = [row["domain"]] if row["domain"] else []
            candidates.append(
                {
                    "strategy": self.name,
                    "concept": concept,
                    "domains": domains,
                    "mention_count": mentions,
                    "source_count": source_count,
                    "title": (
                        f"Gap: {concept} "
                        f"({source_count} source / {mentions} mentions)"
                    ),
                    "kind": "gap",
                    "queue": "research",
                }
            )
            if len(candidates) >= params["limit"]:
                break
        return candidates

    @staticmethod
    def _count_sources(db: sqlite3.Connection, concept: str) -> int:
        row = db.execute(
            """
            SELECT COUNT(DISTINCT n.id) AS c
            FROM note_concepts nc
            JOIN notes n ON n.id = nc.note_id
            WHERE nc.concept = ? AND n.type = 'source'
            """,
            (concept,),
        ).fetchone()
        return int(row["c"] or 0) if row else 0


STRATEGY = ConceptCoverageStrategy()
