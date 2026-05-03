"""Theme-drift strategy — flag silent themes.

Walks the themes table and surfaces ``status: active`` themes whose
``## Catalyst log`` hasn't received an entry in ``stale_days`` (default
60) days. Output is a candidate list for ``status: dormant``; the
caller (typically ``/themes-resolve``) decides which to flip.

A theme with no catalyst entries at all is also flagged — the
heuristic looks at its ``date`` frontmatter as a fallback.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from typing import Any

_DATE_RE = re.compile(r"^\s*[-*]?\s*(\d{4}-\d{2}-\d{2})\b")


class ThemeDriftStrategy:
    name = "theme_drift"

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

        cutoff = date.today() - timedelta(days=params["stale_days"])

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            return self._gather(db, cutoff, params)
        finally:
            db.close()

    def _params(self, config: dict[str, Any]) -> dict[str, Any]:
        strategies_cfg = (
            config.get("projects", {})
            .get("default", {})
            .get("theme_drift", {})
        )
        return {
            "stale_days": int(strategies_cfg.get("stale_days", 60)),
            "limit": int(strategies_cfg.get("limit", 20)),
        }

    def _gather(
        self,
        db: sqlite3.Connection,
        cutoff: date,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows = db.execute(
            "SELECT id, title, date, frontmatter, body_text "
            "FROM notes WHERE type = 'theme' ORDER BY date DESC"
        ).fetchall()

        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
            except (TypeError, json.JSONDecodeError):
                fm = {}
            status = str(fm.get("status", "active"))
            if status != "active":
                continue
            last_iso = self._last_catalyst(row["body_text"] or "") or (
                row["date"] or ""
            )[:10]
            try:
                last_date = date.fromisoformat(last_iso) if last_iso else None
            except ValueError:
                last_date = None
            if last_date is None or last_date < cutoff:
                out.append(
                    {
                        "strategy": self.name,
                        "theme_id": row["id"],
                        "title": (
                            f"Theme drift: {row['title']} — "
                            f"silent since {last_iso or 'creation'}"
                        ),
                        "last_catalyst": last_iso,
                        "kind": "drift",
                        "queue": "themes",
                    }
                )
                if len(out) >= params["limit"]:
                    break
        return out

    @staticmethod
    def _last_catalyst(body: str) -> str:
        in_log = False
        latest = ""
        for line in body.split("\n"):
            stripped = line.strip()
            if stripped.startswith("## "):
                in_log = "catalyst" in stripped.lower()
                continue
            if not in_log:
                continue
            m = _DATE_RE.match(line)
            if m:
                d = m.group(1)
                if d > latest:
                    latest = d
        return latest


STRATEGY = ThemeDriftStrategy()
