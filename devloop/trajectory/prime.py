"""The trajectory note's read face: claim-time prior-trajectory context.

Two interface-level invariants (boundary spec §4):

- **Prime never crashes the loop.** Any index problem (missing db, corrupt
  file, schema drift) degrades to ``primed=false`` with a ``note``.
- **Prime writes nothing to the index.** Its only side effect is the
  served-event append to the session buffer JSONL.

All SQL lives in ``devloop.index_client`` (#100 completed that seam); this
file is composition, rendering and policy only.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

from devloop.index_client import Connection, Error, note_bodies, trajectory_candidates


def is_holdout(run_id: str, holdout: int) -> bool:
    """Stateless 1-in-N-in-expectation holdout: some runs dispatch unprimed.

    Loop runs are numerous, comparable, and gate-scored (#60's ``outcome``),
    so periodically withholding prime context lets the outcome regression
    separate "context helped" from "easy issue". The decision is
    ``sha1(run_id) mod N == 0`` — sampling, not a counter: it holds out one run
    in N *in expectation*, never literally every Nth run. It is stable across
    processes (no PYTHONHASHSEED dependence, unlike ``hash()``) and
    date/random-free, so it is hand-computable and testable. ``holdout <= 0``
    disables holdout entirely.
    """
    if holdout <= 0:
        return False
    digest = int(hashlib.sha1(run_id.encode("utf-8")).hexdigest(), 16)
    return digest % holdout == 0


def _coerce_builds_on(raw: object) -> list[str]:
    """Normalize a trajectory's ``builds_on`` frontmatter to a list of note ids.

    Accepts the plain ``["n-xxxxxx", …]`` form weave_create writes, and tolerates
    path-based wikilinks (``[[path|n-xxxxxx]]``) by taking the trailing id. Any
    non-list / non-string element is dropped — a bad link never crashes prime.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if s.startswith("[[") and s.endswith("]]"):
            s = s[2:-2]
        if "|" in s:
            s = s.split("|")[-1]
        s = s.strip()
        if s:
            out.append(s)
    return out


def resolve_insights(conn: Connection, ids: list[str]) -> list[dict]:
    """The bodies of the insight notes a trajectory builds on.

    Returns ``[{id, body}]`` in ``builds_on`` order, skipping ids that don't
    resolve to a note or resolve to an empty body. The index already holds these
    notes (they are ordinary notes minted at ship time).
    """
    by_id = note_bodies(conn, ids)
    return [{"id": i, "body": by_id[i]} for i in ids if by_id.get(i)]


# Outcome-weighting rank for prime ordering (issue #85). merged-clean/stable
# float to the top, reworked/closed/reverted sink; unlabeled and unknown stay
# neutral (rank 1) so an all-unlabeled match set keeps pure recency order — the
# byte-stable v1 behavior. Python's stable sort preserves recency within a rank.
_OUTCOME_RANK = {
    "merged-clean": 0, "stable": 0,
    "reworked": 2, "reworked-post-merge": 2,
    "closed-unmerged": 2, "reverted": 2, "routed-to-human": 2,
}


def _outcome_rank(label: object) -> int:
    return _OUTCOME_RANK.get(str(label or ""), 1)


def query_trajectories(
    conn: Connection, concepts: list[str], limit: int, scan_cap: int = 40,
    query: str = "",
) -> list[dict]:
    """``[loop-run]`` notes matching this issue that carry reusable color — a
    linked insight note (``builds_on``).

    Retrieval is the seam's fused concept+FTS candidate list
    (:func:`devloop.index_client.trajectory_candidates`): ``concepts`` are
    ontology terms, ``query`` is the issue's own text. Either may be empty (one
    leg then carries the retrieval); both empty matches nothing.

    Returns ``{id, title, issue, outcome, outcome_label, insights}`` dicts, at
    most ``limit``. ``insights`` is the resolved list of linked insight-note
    bodies (``[{id, body}]``); a trajectory whose links resolve to nothing is
    skipped. Up to ``scan_cap`` candidates per leg are scanned; the survivors
    take the outcome-weighting sort (:data:`_OUTCOME_RANK`) — stable, so the
    fused rank order is preserved within an outcome rank and an all-unlabeled
    set keeps the fused order untouched — before truncating to ``limit``.
    """
    out: list[dict] = []
    for r in trajectory_candidates(conn, concepts, query, scan_cap):
        try:
            fm = json.loads(r["frontmatter"] or "{}")
        except json.JSONDecodeError:
            fm = {}
        insights = resolve_insights(conn, _coerce_builds_on(fm.get("builds_on")))
        if not insights:
            continue  # No reusable color — the builds_on links resolved nothing.
        out.append({
            "id": r["id"],
            "title": r["title"] or "",
            "issue": fm.get("issue"),
            "outcome": fm.get("outcome", ""),
            "outcome_label": fm.get("outcome_label", ""),
            "insights": insights,
        })
    out.sort(key=lambda t: _outcome_rank(t.get("outcome_label")))
    return out[:limit]


def render_prime_block(
    trajectories: list[dict], decisions: list[str] | None = None,
    budget_chars: int = 1200,
) -> tuple[str, list[str]]:
    """Render the primed-context markdown + the flat served-id list.

    Each trajectory renders its reusable color first (each capped-in as a whole
    piece until the char budget is spent — at least one always lands if any
    exist). A trajectory serves the BODIES of the insight notes it builds on,
    and ``served`` records the insight ids — that is what the run received.
    ``decisions`` (the decisions_for_file note ids the orchestrator already
    resolved) are appended as an adjacency line so the served log records both
    kinds. ``served`` carries every id actually rendered. Empty input →
    ``('', [])`` so the caller skips cleanly.
    """
    decisions = decisions or []
    if not trajectories and not decisions:
        return "", []
    pieces = ["## Prior trajectories — reusable lessons from similar prior runs\n"]
    served: list[str] = []
    for t in trajectories:
        insights = t.get("insights") or []
        head = f"### #{t.get('issue')} — {t.get('title', '')} ({t.get('outcome', '')})".rstrip()
        piece = f"{head}\n" + "\n".join(ins["body"] for ins in insights) + "\n"
        if served and sum(len(x) for x in pieces) + len(piece) > budget_chars:
            break
        pieces.append(piece)
        served.extend(ins["id"] for ins in insights)
    if decisions:
        pieces.append("Prior decisions for touched files: " + ", ".join(decisions))
        served.extend(decisions)
    return "\n".join(pieces).strip() + "\n", served


def build_prime_payload(
    issue_number: int, run_id: str, concepts: list[str], *,
    conn: Connection | None = None, holdout: int = 5,
    limit: int = 3, budget_chars: int = 1200, decisions: list[str] | None = None,
    query: str = "",
) -> dict:
    """Assemble the claim-time prime payload the orchestrator splices verbatim.

    ``concepts`` (ontology terms) and ``query`` (the issue's text) are the two
    retrieval legs; ``decisions`` are the file-anchored note ids the
    orchestrator resolved at claim time.

    Output keys: ``primed`` (received prime context this run), ``holdout``
    (deliberately withheld), ``served`` (note ids served — trajectory + decisions,
    capped ``limit`` per kind), ``block`` (markdown to splice; ``''`` when
    unprimed), ``note`` (why unprimed, when it is). A held-out or empty-match
    run returns ``primed=False`` with no served ids and an empty block, so the
    loop runs unchanged.
    """
    payload = {
        "issue": issue_number, "run_id": run_id, "concepts": list(concepts),
        "query": query, "holdout": is_holdout(run_id, holdout), "primed": False,
        "served": [], "block": "", "note": "",
    }
    if payload["holdout"]:
        payload["note"] = (
            f"held out (1 run in {holdout} in expectation runs unprimed for the "
            "outcome regression)"
        )
        return payload
    # The query — not the connect — is where a foreign/corrupt file
    # (DatabaseError) or an older index missing note_tags/note_concepts
    # (OperationalError) raises. Guard here so a bad index degrades to unprimed
    # rather than crashing the loop (this module's never-crash invariant).
    index_error = False
    trajectories: list[dict] = []
    if conn is not None:
        try:
            trajectories = query_trajectories(conn, concepts, limit, query=query)
        except Error:
            index_error = True
    decisions = (decisions or [])[:limit]
    block, served = render_prime_block(trajectories, decisions, budget_chars)
    payload["block"] = block
    payload["served"] = served
    payload["primed"] = bool(served)
    if not served:
        payload["note"] = (
            "index unreadable (corrupt or schema-drift) — ran unprimed"
            if index_error else "no matching prior trajectories"
        )
    return payload


# Sentinel tool name stamped on the served-context buffer event. The indexer's
# context_served projection keys off this to assign source='loop-prime' — the
# exact mechanism prompt-time retrieval uses (its PROMPT_TIME_TOOL sentinel →
# source='prompttime'), so context_served stays a pure projection of the
# per-session retrieval_log.jsonl event log.
LOOP_PRIME_TOOL = "loop_prime"


def append_served_event(
    buffer_path: str, run_id: str, issue_number: int,
    served: list[str], session_id: str = "",
) -> None:
    """Append one loop-prime served-context event to the session buffer JSONL.

    Mirrors the prompt-time serving surface: a ``retrieval``-typed event tagged
    with the ``loop_prime`` sentinel tool. ``archive_buffer`` folds it into the
    session's ``retrieval_log.jsonl`` (append-only) at Stop, and the indexer
    projects it to ``context_served(source='loop-prime')`` — recoverable per run
    from the index, derived and rebuildable from the markdown-adjacent log.
    """
    event = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "type": "retrieval",
        "tool": LOOP_PRIME_TOOL,
        "args": {"run_id": run_id, "issue": issue_number, "session_id": session_id},
        "returned_ids": served,
    }
    p = Path(buffer_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
