"""Decision-review strategy — surface stalled decisions.

Walks the decisions table, finds entries with status ``proposed`` or
``accepted`` whose ``date`` (or last-edit timestamp) is older than
``stale_days`` (default 30) AND that have no implementing catalyst
mentioning them since.

Each emitted item is a review prompt — the caller can route them to
BACKLOG (the typical destination) or to a per-project review queue.
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

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            return self._gather(db, project, cutoff, params)
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
            out.append(
                {
                    "strategy": self.name,
                    "decision_id": row["id"],
                    "title": f"Re-review {row['title']} ({status}, {row['date']})",
                    "decision_status": status,
                    "decision_date": row["date"] or "",
                    "kind": "review",
                    "queue": "backlog",
                }
            )
            if len(out) >= params["limit"]:
                break
        return out


STRATEGY = DecisionReviewStrategy()
