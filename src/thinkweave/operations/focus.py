"""Behavioral project focus — the one deterministic focus signal (#170).

:func:`active_projects` is shared by ``/dream`` (digest active_focus) and
``/brief`` so both surfaces agree on one definition. It is deliberately the
*only* computed focus signal: the concept-level merge (asked ▸ done ▸
declared) is the narrating model's judgment over raw substrates it can
already reach — ``weave_prompts`` (asked), the window's session/decision
concepts (done), ``PRIORITIES.yaml`` ``focus.*`` + ``RESEARCH_FOCUS.md``
(declared) — per dec-696bacfb: no scoring model in Python for what is
editorial judgment.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from thinkweave.acquisition.sources.priorities import apply_pins


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
