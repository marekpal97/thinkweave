"""What the user cares about — the three-layer focus merge (#170).

One deterministic function, :func:`rank`, merges three substrates the vault
already keeps, with a fixed precedence:

| layer    | substrate                                          | precedence |
|----------|----------------------------------------------------|------------|
| asked    | probe-classified prompts (``recent_probe_details``) | leads      |
| done     | concept edges on sessions/decisions in the window   | second     |
| declared | ``RESEARCH_FOCUS.md`` gaps + PRIORITIES ``focus.*`` | floor      |

Behavioural-over-declared (dec-549194d3): asked/done rank; declared is only
guaranteed *present* (``apply_pins``) and flagged ``declared_only`` so a
consumer can say "you said you wanted X — here's what landed against it".
Also the home of dream's ``active_projects`` collector (moved here
behaviour-identical so ``/dream`` and ``/brief`` share one definition).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

from thinkweave.acquisition.sources.priorities import (
    apply_pins,
    focus_active_projects,
    focus_concepts,
    load_priorities,
)
from thinkweave.core.config import Config

_GAP_BULLET = re.compile(r"^\s*[-*]\s+`?([a-z0-9][a-z0-9-]*)")


def active_projects(
    db: sqlite3.Connection, *, now: datetime, window_days: int, pins: list[str]
) -> list[str]:
    """Projects with sessions in the last ``window_days`` (most active first).

    Meta buckets (``_unscoped``/``_personal``…) excluded; pins appended as a
    floor. Self-heals — a renamed or abandoned project just stops appearing.
    """
    cutoff = (now - timedelta(days=window_days)).date().isoformat()
    rows = db.execute(
        "SELECT project, COUNT(*) AS c FROM notes "
        "WHERE type = 'session' AND date >= ? GROUP BY project ORDER BY c DESC",
        (cutoff,),
    ).fetchall()
    ranked = [r["project"] for r in rows if r["project"] and not r["project"].startswith("_")][:8]
    return apply_pins(ranked, pins)


def concept_gaps(text: str) -> list[str]:
    """Concept slugs listed as bullets under ``## Concept Gaps``."""
    body = re.split(r"(?m)^## Concept Gaps\s*$", text, maxsplit=1)
    if len(body) < 2:
        return []
    section = re.split(r"(?m)^## ", body[1], maxsplit=1)[0]
    return [m.group(1) for line in section.splitlines() if (m := _GAP_BULLET.match(line))]


def rank(
    cfg: Config,
    *,
    now: datetime | None = None,
    window_days: int | None = None,
    db: sqlite3.Connection | None = None,
) -> dict:
    """The focus vector: ``{"concepts", "active_projects", "asked_below_floor"}``.

    ``asked_below_floor`` lists probed concepts whose pressure is under the
    entry floor — mentioned, not ranked.

    Each concept row: ``concept``, ``asked`` (probe count), ``probes``
    (verbatim texts), ``done`` (session/decision concept edges in window),
    ``declared`` (bool), ``declared_only`` (bool). Ordered asked desc (only
    counts ≥ ``cfg.brief_attention_pressure`` enter the asked tier), then
    done desc, then name; declared-only rows trail as the floor.
    """
    from thinkweave.core.indexer import Indexer
    from thinkweave.operations.prompts import recent_probe_details

    now = now or datetime.now(timezone.utc)
    window_days = window_days or cfg.salience_activity_window_days
    priorities = load_priorities(cfg.vault_root)
    declared = list(focus_concepts(priorities))
    from thinkweave.synthesis.landing import landing_filenames

    rf = cfg.vault_root / landing_filenames(cfg.vault_root)["research_focus"]
    if rf.exists():
        declared += concept_gaps(rf.read_text(encoding="utf-8"))

    try:
        asked = recent_probe_details(cfg, project="", window_days=window_days)
    except Exception:  # noqa: BLE001 — probe load is best-effort
        asked = {}

    own = db is None
    idx = Indexer(config=cfg) if own else None
    conn = idx.db if own else db
    try:
        cutoff = (now - timedelta(days=window_days)).isoformat()
        done = {
            r["concept"]: r["c"]
            for r in conn.execute(
                "SELECT nc.concept, COUNT(*) AS c FROM note_concepts nc "
                "JOIN notes n ON n.id = nc.note_id "
                "WHERE n.type IN ('session', 'decision') AND n.date >= ? "
                "GROUP BY nc.concept",
                (cutoff,),
            )
        }
        projects = active_projects(
            conn, now=now, window_days=window_days, pins=focus_active_projects(priorities)
        )
    finally:
        if idx is not None:
            idx.close()

    declared_set = set(declared)
    # Entry floor for the asked tier: below brief_attention_pressure a probe
    # is a mention, not a signal, and the concept ranks on done only.
    floor = cfg.brief_attention_pressure

    def _asked(c: str) -> int:
        n = asked.get(c, {}).get("count", 0)
        return n if n >= floor else 0

    behavioural = sorted(
        set(asked) | set(done), key=lambda c: (-_asked(c), -done.get(c, 0), c)
    )
    concepts = [
        {
            "concept": c,
            "asked": asked.get(c, {}).get("count", 0),
            "probes": list(asked.get(c, {}).get("probes", [])),
            "done": done.get(c, 0),
            "declared": c in declared_set,
            "declared_only": c not in asked and c not in done,
        }
        for c in apply_pins(behavioural, declared)
    ]
    below = [
        {"concept": c, "asked": d["count"], "probes": list(d["probes"])}
        for c, d in sorted(asked.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
        if 0 < d["count"] < floor
    ]
    return {"concepts": concepts, "active_projects": projects, "asked_below_floor": below}
