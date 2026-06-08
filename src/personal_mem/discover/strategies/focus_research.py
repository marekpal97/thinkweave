"""Focus-research strategy — declared focus concepts, substrate-graded exemplars.

For each concept in ``focus.research_concepts`` (PRIORITIES.yaml), emit
a gap descriptor with three pieces of evidence:

- **Substrate exemplars** — top-N notes tagged with the concept, ranked
  by served-count across the last ``window_days`` (any of
  ``startup``/``onthefly``/``prompttime`` in ``context_served``).
  Tie-break favours ``prompttime`` source rows since those are
  system-pushed nudges that the agent didn't request — the highest-
  signal layer for "what's actually load-bearing right now."
- **Probe-tied exemplars** — notes tagged with the concept that were
  served in the same session as a probe-classified prompt whose text
  mentions the concept slug. Captures "what the user was looking at
  while wondering about this."
- **Source coverage** — count of ``type=source`` notes tagged with the
  concept, partitioned by ``source_type``. Surfaces gaps in the
  evidence base ("focus concept X has 3 papers but no repos").

The strategy is the consumer the deleted ``concept_coverage`` strategy
(commit f7f1116, 2026-06-06) left behind: declared focus concepts had
no consumer between f7f1116 and this restoration. The substrate +
probe legs are the upgrade over the original — the original ranked
concepts but never surfaced exemplar notes per concept.

Opt-in: not in the default ``discover_strategies`` list. Add
``focus_research`` to ``vault/.mem/sources.yaml`` ``discover_strategies:``
to enable per-project.

Emitted descriptor shape::

    {
        "strategy": "focus_research",
        "concept": "agent-harness",
        "exemplar_served": ["n-abc123", "n-def456"],
        "exemplar_probed": ["n-jkl012"],
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
            from personal_mem.sources.priorities import (
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

        # Probe-tied exemplars need a project scope (query_prompts is
        # project-scoped). Without one, fall back to default_project; if
        # neither, skip the probe leg (substrate + coverage still ship).
        probe_project = project or getattr(cfg, "default_project", None)
        probe_sessions_by_concept: dict[str, set[str]] = {}
        if probe_project:
            try:
                probe_sessions_by_concept = self._probe_sessions_by_concept(
                    cfg, probe_project, concepts, params["window_days"]
                )
            except Exception:
                probe_sessions_by_concept = {}

        cutoff_iso = (
            datetime.now(timezone.utc) - timedelta(days=params["window_days"])
        ).isoformat()

        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        try:
            return [
                self._descriptor_for(
                    db,
                    concept,
                    cutoff_iso,
                    probe_sessions_by_concept.get(concept, set()),
                    params,
                )
                for concept in concepts
            ]
        finally:
            db.close()

    def _descriptor_for(
        self,
        db: sqlite3.Connection,
        concept: str,
        cutoff_iso: str,
        probe_sessions: set[str],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        substrate_exemplars = self._substrate_exemplars(
            db, concept, cutoff_iso, params["exemplar_limit"]
        )
        probe_exemplars = self._probe_exemplars(
            db, concept, probe_sessions, params["probe_exemplar_limit"]
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
            "exemplar_probed": probe_exemplars,
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

    def _probe_exemplars(
        self,
        db: sqlite3.Connection,
        concept: str,
        probe_sessions: set[str],
        limit: int,
    ) -> list[str]:
        """Notes tagged with `concept` served in any probe session."""
        if not probe_sessions:
            return []
        placeholders = ",".join("?" * len(probe_sessions))
        sql = f"""
            SELECT DISTINCT cs.note_id
            FROM context_served cs
            JOIN note_concepts nc ON nc.note_id = cs.note_id
            WHERE nc.concept = ?
              AND cs.session_id IN ({placeholders})
            ORDER BY cs.note_id
            LIMIT ?
        """
        params = [concept, *sorted(probe_sessions), limit]
        rows = db.execute(sql, params).fetchall()
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

    def _probe_sessions_by_concept(
        self,
        cfg: Any,
        project: str,
        concepts: list[str],
        window_days: int,
    ) -> dict[str, set[str]]:
        """Map each focus concept → set of vault session note ids whose
        ``events.jsonl`` carries a probe-classified prompt mentioning the
        concept slug.

        Walks ``vault/projects/<project>/sessions/<dir>/events.jsonl``
        directly rather than going through ``query_prompts`` — the latter
        returns Claude Code session UUIDs, but ``context_served`` keys by
        the vault session note id (``ses-xxx``). Reading the sibling
        ``session.md`` frontmatter inside each session folder bridges the
        two without a separate UUID→ses-id lookup.

        Buffer-side (live, unprocessed) sessions are NOT walked here —
        substrate-derived exemplars surface what's *already happened*;
        active sessions get folded in by the next /dream cycle.
        """
        from personal_mem.core.events import extract_prompts
        from personal_mem.core.vault import VaultManager

        by_concept: dict[str, set[str]] = {c: set() for c in concepts}
        lowered = [(c, c.lower()) for c in concepts if c]
        if not lowered:
            return by_concept

        sessions_root = cfg.vault_root / "projects" / project / "sessions"
        if not sessions_root.exists():
            return by_concept

        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        vm = VaultManager(config=cfg)

        for sess_dir in sessions_root.iterdir():
            if not sess_dir.is_dir():
                continue
            events_file = sess_dir / "events.jsonl"
            session_file = sess_dir / "session.md"
            if not events_file.exists() or not session_file.exists():
                continue
            try:
                ses_id = vm.read_note(session_file).id
            except Exception:
                continue
            if not ses_id:
                continue

            for prompt in extract_prompts(events_file):
                if prompt.classification != "probe":
                    continue
                if (
                    prompt.ts != datetime.min
                    and prompt.ts.tzinfo is not None
                    and prompt.ts < cutoff
                ):
                    continue
                text = (prompt.text or "").lower()
                if not text:
                    continue
                for canonical, slug_lower in lowered:
                    if slug_lower in text:
                        by_concept[canonical].add(ses_id)
        return by_concept

    def _params(self, config: dict[str, Any]) -> dict[str, Any]:
        strategies_cfg = (
            config.get("projects", {})
            .get("default", {})
            .get("focus_research", {})
        )
        return {
            "window_days": int(strategies_cfg.get("window_days", 14)),
            "exemplar_limit": int(strategies_cfg.get("exemplar_limit", 5)),
            "probe_exemplar_limit": int(
                strategies_cfg.get("probe_exemplar_limit", 3)
            ),
        }


STRATEGY = FocusResearchStrategy()
