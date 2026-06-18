"""Focus-research strategy — declared focus concepts, substrate-graded exemplars.

For each concept in ``focus.research_concepts`` (PRIORITIES.yaml), emit a
gap descriptor with two pieces of evidence:

- **Substrate exemplars** — top-N notes tagged with the concept, ranked
  by served-count across the last ``window_days`` (any of
  ``startup``/``onthefly``/``prompttime`` in ``context_served``).
  Tie-break favours ``prompttime`` source rows since those are
  system-pushed nudges that the agent didn't request — the highest-
  signal layer for "what's actually load-bearing right now."
- **Source coverage** — count of ``type=source`` notes tagged with the
  concept, partitioned by ``source_type``. Surfaces gaps in the
  evidence base ("focus concept X has 3 papers but no repos").

This is the **declared-floor rail**: it covers topics the user named in
``focus.research_concepts`` even when they haven't probed them recently.
The behavioural / probe-driven rail (questions the user actually asked →
research leads) is owned by the ``/dream`` probe-distillation worker
(``dream-priority-worker``); this strategy deliberately carries NO probe
input, so the two rails don't duplicate (2026-06-17 — the probe-tightening
leg was removed once the probe worker became the single owner of
probe→research).

The strategy is the consumer the deleted ``concept_coverage`` strategy
(commit f7f1116, 2026-06-06) left behind: declared focus concepts had no
consumer between f7f1116 and this restoration.

Emitted descriptor shape::

    {
        "strategy": "focus_research",
        "concept": "agent-harness",
        "exemplar_served": ["n-abc123", "n-def456"],
        "source_coverage": {"paper": 3, "repo": 1, "article": 5},
        "kind": "research_focus",
        "title": "Research focus: agent-harness (...)",
        "queue": "research",
    }
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any


class FocusResearchStrategy:
    name = "focus_research"

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

        # Fail-open on PRIORITIES.yaml absence / parse error — empty
        # focus_concepts returns no gaps, matching the pre-strategy
        # behaviour of "nothing happens."
        try:
            from thinkweave.acquisition.sources.priorities import (
                focus_concepts,
                load_priorities,
            )

            concepts = focus_concepts(
                load_priorities(getattr(cfg, "vault_root", None))
            )
        except Exception:
            concepts = []
        if not concepts:
            return []

        cutoff_iso = (
            datetime.now(timezone.utc) - timedelta(days=params["window_days"])
        ).isoformat()

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            return [
                self._descriptor_for(db, concept, cutoff_iso, params)
                for concept in concepts
            ]
        finally:
            db.close()

    def _descriptor_for(
        self,
        db: sqlite3.Connection,
        concept: str,
        cutoff_iso: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        substrate_exemplars = self._substrate_exemplars(
            db, concept, cutoff_iso, params["exemplar_limit"]
        )
        coverage = self._source_coverage(db, concept)

        coverage_summary = ", ".join(
            f"{cnt} {st}{'s' if cnt != 1 else ''}"
            for st, cnt in sorted(coverage.items())
        ) or "no sources"
        return {
            "strategy": self.name,
            "concept": concept,
            "exemplar_served": substrate_exemplars,
            "source_coverage": coverage,
            "kind": "research_focus",
            "title": f"Research focus: {concept} ({coverage_summary})",
            "queue": "research",
        }

    def _substrate_exemplars(
        self,
        db: sqlite3.Connection,
        concept: str,
        cutoff_iso: str,
        limit: int,
    ) -> list[str]:
        """Top-N notes tagged with `concept`, ranked by served-count.

        Ranking weight: total served-count over the window, with a +0.5
        boost per ``prompttime`` row (system-pushed nudges are the
        highest-quality signal — see ``prompt_time_retrieval`` design).
        Tie-break by note-id for determinism.
        """
        sql = """
            SELECT cs.note_id,
                   COUNT(*) AS served_count,
                   SUM(CASE WHEN cs.source = 'prompttime' THEN 1 ELSE 0 END)
                     AS prompttime_count
            FROM context_served cs
            JOIN note_concepts nc ON nc.note_id = cs.note_id
            WHERE nc.concept = ?
              AND (cs.ts = '' OR cs.ts >= ?)
            GROUP BY cs.note_id
            ORDER BY (served_count + 0.5 * prompttime_count) DESC, cs.note_id
            LIMIT ?
        """
        rows = db.execute(sql, (concept, cutoff_iso, limit)).fetchall()
        return [r["note_id"] for r in rows]

    def _source_coverage(
        self, db: sqlite3.Connection, concept: str
    ) -> dict[str, int]:
        """Count of source notes tagged with `concept`, by source_type.

        The source_type lives in the frontmatter JSON; we approximate it
        by reading the JSON blob and counting per-type. Falls back to
        ``"unknown"`` bucket when frontmatter lacks ``source_type``.
        """
        import json

        sql = """
            SELECT n.id, n.frontmatter
            FROM notes n
            JOIN note_concepts nc ON nc.note_id = n.id
            WHERE nc.concept = ? AND n.type = 'source'
        """
        rows = db.execute(sql, (concept,)).fetchall()
        coverage: dict[str, int] = {}
        for r in rows:
            fm_raw = r["frontmatter"] or ""
            try:
                fm = json.loads(fm_raw) if fm_raw else {}
            except (TypeError, ValueError):
                fm = {}
            stype = str(fm.get("source_type") or "unknown")
            coverage[stype] = coverage.get(stype, 0) + 1
        return coverage

    def _params(self, config: dict[str, Any]) -> dict[str, Any]:
        strategies_cfg = (
            config.get("projects", {})
            .get("default", {})
            .get("focus_research", {})
        )
        return {
            "window_days": int(strategies_cfg.get("window_days", 14)),
            "exemplar_limit": int(strategies_cfg.get("exemplar_limit", 5)),
        }


STRATEGY = FocusResearchStrategy()
