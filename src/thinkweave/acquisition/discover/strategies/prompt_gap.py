"""Residual probe-gap strategy — surfaces probed concepts not yet in
the ontology.

The cross-cutting probe-pressure bias on ``decision_review`` already
handles probes about *known* concepts.  This strategy handles the
residual case: hyphenated-compound terms the
user has been asking about that don't appear in the ontology
(canonical OR proposed) at all. ``/discover`` routes these to ontology
proposal instead of source research.

Conservatively scoped: only hyphenated multi-word kebab tokens count.
They have the right shape for concept slugs and are unlikely to be
generic stopwords. Single-word probes about new terms slip through
this filter by design — too noisy to surface autonomously.

    {
        "strategy": "prompt_gap",
        "concept": "dynamic-batching",
        "concept_status": "unknown",
        "probe_pressure": 3,
        "title": "Probe gap: dynamic-batching (3 probes)",
        "kind": "gap",
        "queue": "ontology",
    }
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any


# At least one hyphen, 2+ kebab segments, lowercase letters/digits only.
_KEBAB_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9]+(?:-[a-z0-9]+)+\b")


class PromptGapStrategy:
    name = "prompt_gap"

    def run(
        self,
        vault: Any,
        project: str | None,
        config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        config = config or {}
        params = self._params(config)
        cfg = getattr(vault, "config", None) or vault

        scope_project = project or getattr(cfg, "default_project", None)
        if not scope_project:
            return []

        # Lazy imports — this strategy is loaded at /discover boot and
        # we don't want to pay for ontology/indexer parsing unless the
        # strategy actually fires.
        from thinkweave.operations.search import query_prompts
        from thinkweave.synthesis.concepts import (
            build_keep_set,
            get_all_proposed_concepts,
            load_ontology,
        )

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=params["window_days"])
        ).isoformat()
        probes = query_prompts(
            cfg,
            project=scope_project,
            since=cutoff,
            limit=500,
            classified_as="probe",
        )
        if not probes:
            return []

        known: set[str] = build_keep_set(load_ontology())
        try:
            from thinkweave.core.indexer import Indexer

            idx = Indexer(config=cfg)
            try:
                known.update(get_all_proposed_concepts(idx.db).keys())
            finally:
                idx.close()
        except Exception:
            # Unindexed vault: canonical-only vocabulary is still useful.
            pass

        unknown_counts: dict[str, int] = {}
        for row in probes:
            text_lower = (row.get("text") or "").lower()
            if not text_lower:
                continue
            seen: set[str] = set()
            for match in _KEBAB_TOKEN_RE.finditer(text_lower):
                term = match.group(0)
                if term in known:
                    continue
                seen.add(term)
            for term in seen:
                unknown_counts[term] = unknown_counts.get(term, 0) + 1

        candidates = [
            (term, count)
            for term, count in unknown_counts.items()
            if count >= params["min_pressure"]
        ]
        candidates.sort(key=lambda x: x[1], reverse=True)
        candidates = candidates[: params["limit"]]

        return [
            {
                "strategy": self.name,
                "concept": term,
                "concept_status": "unknown",
                "probe_pressure": count,
                "title": (
                    f"Probe gap: {term} "
                    f"({count} probe{'s' if count > 1 else ''})"
                ),
                "kind": "gap",
                "queue": "ontology",
            }
            for term, count in candidates
        ]

    def _params(self, config: dict[str, Any]) -> dict[str, Any]:
        strategies_cfg = (
            config.get("projects", {})
            .get("default", {})
            .get("prompt_gap", {})
        )
        return {
            "window_days": int(strategies_cfg.get("window_days", 14)),
            "min_pressure": int(strategies_cfg.get("min_pressure", 2)),
            "limit": int(strategies_cfg.get("limit", 5)),
        }


STRATEGY = PromptGapStrategy()
