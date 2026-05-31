"""Decision-review strategy — surface stalled decisions.

Walks the decisions table, finds entries with status ``proposed`` or
``accepted`` whose ``date`` (or last-edit timestamp) is older than
``stale_days`` (default 30) AND that have no implementing catalyst
mentioning them since.

Each emitted item is a review prompt — the caller can route them to
the project's backlog landing doc (the typical destination) or to a
per-project review queue.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any


class DecisionReviewStrategy:
    name = "decision_review"

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

        cutoff = (date.today() - timedelta(days=params["stale_days"])).isoformat()

        # Probe-pressure bias (Slice 1.3): decisions touching concepts
        # the user has been probing about float to the top of the
        # stale-decision list. Fail-open to {} preserves pre-bias order
        # on missing ontology / unindexed vault / etc.
        try:
            from personal_mem.operations.prompts import recent_probe_pressure

            pressure = recent_probe_pressure(
                cfg, project=project, window_days=14
            )
        except Exception:
            pressure = {}

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            return self._gather(db, project, cutoff, params, pressure)
        finally:
            db.close()

    def _params(self, config: dict[str, Any]) -> dict[str, Any]:
        strategies_cfg = (
            config.get("projects", {})
            .get("default", {})
            .get("decision_review", {})
        )
        return {
            "stale_days": int(strategies_cfg.get("stale_days", 30)),
            "limit": int(strategies_cfg.get("limit", 10)),
        }

    def _gather(
        self,
        db: sqlite3.Connection,
        project: str | None,
        cutoff: str,
        params: dict[str, Any],
        pressure: dict[str, int],
    ) -> list[dict[str, Any]]:
        if project:
            sql = (
                "SELECT id, title, date, frontmatter FROM notes "
                "WHERE type = 'decision' AND project = ? "
                "AND date < ? ORDER BY date"
            )
            rows = db.execute(sql, (project, cutoff)).fetchall()
        else:
            sql = (
                "SELECT id, title, date, frontmatter FROM notes "
                "WHERE type = 'decision' AND date < ? ORDER BY date"
            )
            rows = db.execute(sql, (cutoff,)).fetchall()

        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
            except (TypeError, json.JSONDecodeError):
                fm = {}
            status = fm.get("status", "proposed")
            if status not in ("proposed", "accepted"):
                continue
            decision_concepts: list[str] = []
            for key in ("concepts", "proposed_concepts"):
                values = fm.get(key) or []
                if isinstance(values, list):
                    decision_concepts.extend(str(c) for c in values)
            probe_pressure = sum(
                pressure.get(c.lower(), 0) for c in decision_concepts
            )
            out.append(
                {
                    "strategy": self.name,
                    "decision_id": row["id"],
                    "title": f"Re-review {row['title']} ({status}, {row['date']})",
                    "decision_status": status,
                    "decision_date": row["date"] or "",
                    "probe_pressure": probe_pressure,
                    "kind": "review",
                    "queue": "backlog",
                }
            )
        # Pressure-first, then stalest-first (existing tie-break). The
        # SQL already returned rows by date ASC; sorted is stable in
        # CPython, so equal-pressure entries keep date-asc ordering.
        out.sort(key=lambda d: d["probe_pressure"], reverse=True)
        return out[: params["limit"]]


STRATEGY = DecisionReviewStrategy()
