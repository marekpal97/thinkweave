"""Assemble RLVR export rows from the decision corpus + context_served.

This is slice 5 of the RLVR substrate. It joins three sources:

- **Decision frontmatter** (verdict, blame_lines, committed, predicted_outcome,
  prediction_match, judged_at, date)
- **Decision body** wikilinks — citations of the form ``[[n-...]]`` /
  ``[[dec-...]]`` etc. Frontmatter relations (``derived_from``, ``related_to``)
  are *deliberately not counted* — they're structural, not semantic citations.
  This was the user's explicit choice when scoping the slice; widening it
  is a deliberate design change, not a quick edit.
- **``context_served``** table — what notes were served to the session this
  decision came out of (``startup`` vs ``onthefly``).

The row schema is locked by ``project_decision_context_rl``::

    {
      "decision_id", "project", "session_id", "created_at",
      "prediction": {"text", "match", "history"},
      "outcome": {"verdict", "committed", "blame_lines", "days_alive"},
      "context": {
        "n_retrievals_onthefly", "cited_onthefly_ids",
        "cited_prompttime_ids", "cited_startup_only_ids", "startup_token_est",
      }
    }

``prediction.history`` is the append-only list of
``{match, judged_at, reason}`` entries from the decision's
``prediction_history:`` frontmatter; ``prediction.match`` is the denormalized
tail entry's match (derived from history rather than from the legacy
``prediction_match`` field, so legacy decisions still produce a correct
shortcut via :func:`read_history`'s back-compat coercion).

Single-row use: ``assemble_row(cfg, decision_id)``.
Batch export: ``export_rows(cfg, project=..., since=..., until=...,
committed_only=...)`` — yields dicts, opens one ``Indexer`` for the whole
iteration, caches ``retrieval_log.jsonl`` reads by ``session_id``.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.vault import VaultManager, extract_wikilink_ids
from personal_mem.synthesis.prediction import read_history

# Same prefix family as operations/retrieval_log.py:_ID_RE. `fullmatch` here —
# the wikilink target must BE an id, not just contain one. `[[some-title]]`
# isn't a citation; `[[n-abc123ef]]` is.
_ID_RE = re.compile(r"(?:n|ses|dec|thm|src|cand|cncpt)-[a-z0-9]{6,}")


@dataclass
class RLVRRow:
    """One row of the RLVR export — one decision, fully joined.

    Field shapes mirror the locked schema in
    ``project_decision_context_rl``. The dataclass is intentionally flat
    for the top-level fields and nested-dict for the three groups
    (``prediction``, ``outcome``, ``context``) — keeps the JSONL output
    self-documenting without an external schema file.
    """

    decision_id: str
    project: str
    session_id: str
    created_at: str
    prediction: dict = field(default_factory=dict)
    outcome: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Citation extraction
# ---------------------------------------------------------------------------


def extract_cited_ids(body: str) -> list[str]:
    """Return body wikilink targets that match the canonical-id pattern.

    Body-only, no frontmatter relations. Duplicates removed; order preserved
    so the export row is reproducible.

    Out of scope (intentional):

    - ``derived_from``, ``related_to``, ``supersedes`` frontmatter lists
      (structural — every decision auto-links to ``source_session``, so
      these would always show as "citations")
    - Concept overlap or text-similarity matches (not deterministic enough
      for an evaluation substrate)
    """
    seen: set[str] = set()
    out: list[str] = []
    # ``extract_wikilink_ids`` recovers the id from path-based ``[[path|id]]``
    # links too, so citations survive the bare→path body migration.
    for target in extract_wikilink_ids(body):
        target = target.strip()
        if _ID_RE.fullmatch(target) and target not in seen:
            seen.add(target)
            out.append(target)
    return out


# ---------------------------------------------------------------------------
# Per-row assembly (single decision)
# ---------------------------------------------------------------------------


def _days_between(start: str, end: str) -> int:
    """ISO date diff in whole days. Returns 0 on malformed/missing inputs."""
    if not start:
        return 0
    try:
        # Accept bare dates and full ISO timestamps.
        s = datetime.fromisoformat(start.replace("Z", "+00:00")) \
            if "T" in start else datetime.fromisoformat(start)
        e = (
            datetime.fromisoformat(end.replace("Z", "+00:00"))
            if "T" in end else datetime.fromisoformat(end)
        ) if end else datetime.now(timezone.utc)
        # Strip tz for naive diff if needed.
        if s.tzinfo and not e.tzinfo:
            e = e.replace(tzinfo=timezone.utc)
        elif e.tzinfo and not s.tzinfo:
            s = s.replace(tzinfo=timezone.utc)
        return max(0, (e - s).days)
    except (ValueError, TypeError):
        return 0


def _read_startup_token_est(log_path: Path) -> int:
    """Read the first ``type: startup`` event from a retrieval log and pull token_est.

    Returns 0 when the log is missing, malformed, or contains no startup event.
    Single-pass: only the first matching line wins (we only emit one per session).
    """
    if not log_path.exists():
        return 0
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "startup":
                    return int(ev.get("token_est", 0) or 0)
    except OSError:
        pass
    return 0


def _assemble_with_indexer(
    idx: Indexer,
    vm: VaultManager,
    decision_id: str,
    *,
    startup_cache: dict[str, int] | None = None,
) -> RLVRRow | None:
    """Build a row using a pre-opened Indexer + VaultManager.

    Used internally by both ``assemble_row`` (single) and ``export_rows``
    (batch). The ``startup_cache`` lets batch callers avoid re-reading the
    same retrieval_log.jsonl for every decision from the same session.
    """
    row = idx.db.execute(
        "SELECT id, path FROM notes WHERE id = ? AND type = 'decision'",
        (decision_id,),
    ).fetchone()
    if not row:
        return None

    dec = vm.read_note(vm.root / row["path"])
    fm = dec.frontmatter
    session_id = fm.get("source_session", "") or ""

    # Citations — body wikilinks only (see extract_cited_ids docstring).
    cited = set(extract_cited_ids(dec.body))

    # Context-served lookups: bucket cited ids by source, count onthefly events.
    onthefly_ids: set[str] = set()
    prompttime_ids: set[str] = set()
    startup_ids: set[str] = set()
    n_retrievals_onthefly = 0
    if session_id:
        rows = idx.db.execute(
            "SELECT note_id, source FROM context_served WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        for r in rows:
            if r["source"] == "onthefly":
                onthefly_ids.add(r["note_id"])
            elif r["source"] == "prompttime":
                prompttime_ids.add(r["note_id"])
            elif r["source"] == "startup":
                startup_ids.add(r["note_id"])
        # Retrieval-event count = distinct ts among onthefly rows. One MCP
        # call → one ts → multiple note_ids share that ts.
        n_row = idx.db.execute(
            "SELECT COUNT(DISTINCT ts) AS n FROM context_served "
            "WHERE session_id = ? AND source = 'onthefly'",
            (session_id,),
        ).fetchone()
        n_retrievals_onthefly = int(n_row["n"] or 0) if n_row else 0

    # Source precedence for a note served via multiple channels:
    # onthefly (agent pulled) > prompttime (system pushed) > startup (boot).
    cited_onthefly = sorted(cited & onthefly_ids)
    cited_prompttime = sorted((cited & prompttime_ids) - onthefly_ids)
    cited_startup_only = sorted(
        (cited & startup_ids) - onthefly_ids - prompttime_ids
    )

    # startup_token_est: re-read the session's retrieval_log.jsonl (cached
    # across batch calls).
    startup_token_est = 0
    if session_id:
        if startup_cache is not None and session_id in startup_cache:
            startup_token_est = startup_cache[session_id]
        else:
            sess_row = idx.db.execute(
                "SELECT path FROM notes WHERE id = ?", (session_id,)
            ).fetchone()
            if sess_row:
                log_path = (
                    vm.root / sess_row["path"]
                ).parent / "retrieval_log.jsonl"
                startup_token_est = _read_startup_token_est(log_path)
            if startup_cache is not None:
                startup_cache[session_id] = startup_token_est

    # days_alive — judged_at if available, else now.
    created_at = str(fm.get("date", "")) if fm.get("date") else ""
    judged_at = str(fm.get("judged_at", "")) if fm.get("judged_at") else ""
    days_alive = _days_between(created_at, judged_at)

    # blame_lines is set by the judge (-1 = couldn't determine, n = surviving lines).
    blame_raw = fm.get("blame_lines")
    if blame_raw in (None, ""):
        blame_lines = -1
    else:
        try:
            blame_lines = int(blame_raw)
        except (ValueError, TypeError):
            blame_lines = -1

    # Prediction history — full append-only list. Tail entry's match is the
    # denormalized shortcut (derived from history, NOT from the legacy
    # `prediction_match` field — keeps things honest when a decision was
    # judged multiple times and the fm shortcut is stale).
    history = read_history(fm)
    tail_match = history[-1]["match"] if history else ""

    return RLVRRow(
        decision_id=decision_id,
        project=fm.get("project", "") or "",
        session_id=session_id,
        created_at=created_at,
        prediction={
            "text": fm.get("predicted_outcome", "") or "",
            "match": tail_match,
            "history": history,
        },
        outcome={
            "verdict": fm.get("verdict", "") or "",
            "committed": bool(fm.get("committed", False)),
            "blame_lines": blame_lines,
            "days_alive": days_alive,
        },
        context={
            "n_retrievals_onthefly": n_retrievals_onthefly,
            "cited_onthefly_ids": cited_onthefly,
            "cited_prompttime_ids": cited_prompttime,
            "cited_startup_only_ids": cited_startup_only,
            "startup_token_est": startup_token_est,
        },
    )


def assemble_row(cfg: Config, decision_id: str) -> RLVRRow | None:
    """Build a single RLVR row for a decision. Returns None if not found.

    Convenience wrapper around :func:`_assemble_with_indexer`. Opens its own
    ``Indexer`` and closes it after — fine for one-off lookups. For batch
    export, use :func:`export_rows` instead, which shares an Indexer and
    a startup-token cache across the whole iteration.
    """
    idx = Indexer(config=cfg)
    vm = VaultManager(config=cfg)
    try:
        return _assemble_with_indexer(idx, vm, decision_id)
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# Batch export
# ---------------------------------------------------------------------------


def export_rows(
    cfg: Config,
    *,
    project: str = "",
    since: str = "",
    until: str = "",
    committed_only: bool = False,
) -> Iterator[dict]:
    """Yield RLVR row dicts for every decision matching the filters.

    Order: by ``date`` then ``id`` (deterministic across runs). Filters are
    SQL-side where cheap; ``committed_only`` is applied post-assembly so the
    bool comes from the same source the row carries.
    """
    idx = Indexer(config=cfg)
    vm = VaultManager(config=cfg)
    try:
        sql = "SELECT id FROM notes WHERE type = 'decision'"
        params: list[str] = []
        if project:
            sql += " AND project = ?"
            params.append(project)
        if since:
            sql += " AND date >= ?"
            params.append(since)
        if until:
            sql += " AND date <= ?"
            params.append(until)
        sql += " ORDER BY date, id"

        startup_cache: dict[str, int] = {}
        for r in idx.db.execute(sql, params):
            row = _assemble_with_indexer(
                idx, vm, r["id"], startup_cache=startup_cache
            )
            if row is None:
                continue
            if committed_only and not row.outcome.get("committed"):
                continue
            yield row.as_dict()
    finally:
        idx.close()
