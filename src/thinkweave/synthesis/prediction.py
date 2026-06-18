"""Prediction history primitives — pure compose, no I/O.

Decisions optionally carry a ``predicted_outcome:`` prose string (claim plus
manifestation pointer) and an append-only ``prediction_history:`` list of
``{match, judged_at, reason}`` entries. ``prediction_match:`` is the
denormalized shortcut to the tail entry's ``match``.

This module owns the shape: coerce legacy flat frontmatter into a synthetic
history list on read, append new verdicts on write, return frontmatter-update
dicts for the caller to persist via :class:`VaultManager`.
"""

from __future__ import annotations

from datetime import datetime, timezone

VERDICTS = frozenset({"confirmed", "contradicted", "pending", "unevaluable", "stale"})


def read_history(fm: dict) -> list[dict]:
    """Return the prediction history list, coercing legacy flat frontmatter.

    If ``prediction_history`` is present (even if empty), trust it — clamp
    any unknown ``match`` values to ``unevaluable`` and filter non-dict
    entries. Otherwise, if legacy ``prediction_match`` is set, synthesize a
    single-entry history from it. Otherwise return ``[]``.
    """
    raw = fm.get("prediction_history")
    if isinstance(raw, list):
        return [_clamp(entry) for entry in raw if isinstance(entry, dict)]

    legacy_match = fm.get("prediction_match")
    if legacy_match:
        return [
            {
                "match": legacy_match if legacy_match in VERDICTS else "unevaluable",
                "judged_at": fm.get("judged_at", ""),
                "reason": "legacy",
            }
        ]
    return []


def append_verdict(
    fm: dict,
    *,
    match: str,
    reason: str,
    judged_at: str | None = None,
) -> dict:
    """Compose frontmatter updates appending one verdict to the history.

    Returns a delta dict with ``prediction_history`` (full appended list),
    ``prediction_match`` (the new tail's match — denormalized for cheap
    reads), and ``judged_at`` (the new tail's timestamp). Unknown ``match``
    values clamp to ``unevaluable``.
    """
    safe_match = match if match in VERDICTS else "unevaluable"
    ts = judged_at or datetime.now(timezone.utc).isoformat()
    new_entry = {"match": safe_match, "judged_at": ts, "reason": reason}
    history = read_history(fm) + [new_entry]
    return {
        "prediction_history": history,
        "prediction_match": safe_match,
        "judged_at": ts,
    }


def _clamp(entry: dict) -> dict:
    if entry.get("match") in VERDICTS:
        return entry
    return {**entry, "match": "unevaluable"}
