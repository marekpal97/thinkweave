"""Prompt-time retrieval enrichment (R2).

The ``UserPromptSubmit`` hook calls :func:`build_enrichment` on each user
prompt. It surfaces a small, deduped, hard-capped block of vault notes relevant
to the prompt — *delta over startup* — to prepend to what the model sees.

- **Two gates, chosen by the ``prompt_gate`` decision site.** With the site
  off, the similarity arm alone runs and its embedding cosine floor is the
  relevance gate. With it on, an any-term BM25 arm and the unfloored
  similarity arm are fused by RRF into a pool of ``pool_size`` candidates,
  and one decision call asks a ``noul`` per candidate: would this note help
  with the prompt. Candidates at or above ``accept`` are served in RRF order
  within the caps. FTS5 phrase-matches by default, which a prose prompt never
  satisfies, so the keyword arm matches any of the prompt's words instead.

- **Every gated candidate leaves an ex-ante verdict.** The call is recorded as
  one ``decision`` event in the live buffer, one answer per candidate with its
  action (``served``, ``capped`` by the caps, or ``rejected``); the indexer
  projects it into ``verdicts``. On an error, a 429 or a missed deadline the
  gate writes nothing, the hook serves the RRF top ``max_pieces_per_turn``
  and writes a ``decision_miss`` event.

- **Latency is bounded, not eliminated.** The embedding call runs in a daemon
  thread with a wall-clock deadline, and the decision call in another; on
  overrun each is abandoned (Python has no clean thread-kill). Both deadlines
  are clamped to what the firing harness still waits for.

- **Misses are tracked, and adaptive-skip kicks in.** The handler records an
  embedding deadline miss as a ``prompt_time_miss`` event and a gate miss as
  a ``decision_miss`` event. Once ``deadline_miss_limit`` consecutive misses
  accumulate for a session, :func:`build_enrichment` stops attempting for the
  rest of that session rather than re-paying a doomed deadline every turn.

- **Dedup is ledger-based.** The session's :class:`Ledger` carries the
  ``startup`` event's ids, every ``retrieval`` event's, and our own prior
  write-backs. It is read once per prompt: the live
  ``buffer/<session_id>.jsonl`` alone under the floor, the archive too under
  the gate. ``context_served`` is a Stop-time projection (stale mid-session)
  and is NOT consulted here.

- **RLVR meshing.** The hook writes served ids back as a ``retrieval`` event
  tagged ``tool == PROMPT_TIME_TOOL``; the indexer projects those to
  ``context_served`` with ``source='prompttime'`` (distinct from agent-pulled
  ``onthefly``). The miss events are other ``type``s and carry no ``tool``
  field, so they are invisible to both the firings ledger and the
  ``context_served`` projection.

:func:`build_enrichment` writes only the gate's verdicts; the hook handler
owns every other buffer write-back and the stdout emit.
"""

from __future__ import annotations

import threading
import time
from typing import NamedTuple

from thinkweave.core.config import Config
from thinkweave.operations.verdicts import (
    DECISION_MISS,
    PROMPT_GATE,
    Ledger,
    ask_site,
    record_verdict,
)

# Sentinel tool name stamped on the buffer write-back event. The indexer's
# context_served projection keys off this to assign source='prompttime'.
PROMPT_TIME_TOOL = "prompt_time_retrieval"

# Buffer event type for an embedding deadline miss — distinct from both
# "retrieval" (which the indexer projects to context_served) and a
# PROMPT_TIME_TOOL-tagged event (which counts as a firing). Pure telemetry.
PROMPT_TIME_MISS = "prompt_time_miss"

# Render header for the injected block.
_HEADER = "📎 Possibly relevant from your vault (optional — weave_read to expand):"
# Never emit if the remaining char budget can't fit a single useful line.
_MIN_PIECE_CHARS = 80

#: The least remaining harness budget worth starting the similarity arm
#: with. Below it the arm is not attempted and the turn counts as a miss, so
#: a harness whose hook budget is already spent still feeds the adaptive skip.
_MIN_DEADLINE_SECONDS = 0.25


class Enrichment(NamedTuple):
    """One prompt's enrichment outcome, for the hook to emit and record.

    ``missed`` is ``True`` iff the similarity arm blew its deadline;
    ``decision_miss`` names why the prompt gate gave no verdict, and then
    ``block`` holds the ungated RRF top.
    """

    block: str | None
    served_ids: list[str]
    missed: bool
    decision_miss: str = ""


_NOTHING = Enrichment(None, [], False)


class Pool(NamedTuple):
    """The gate's candidates in RRF order; ``missed`` as in :func:`_retrieve`."""

    candidates: list
    missed: bool


def build_enrichment(
    cfg: Config,
    session_id: str,
    prompt_text: str,
    *,
    deadline: float | None = None,
    task: str = "",
) -> Enrichment:
    """Build the prompt-time enrichment block for one prompt.

    ``deadline`` is the caller's remaining wall-clock budget in seconds (the
    firing harness's hook budget less what the handler already spent). The
    similarity arm runs for the smaller of it and the configured
    ``embed_deadline_seconds``; the prompt gate gets the smaller of what is
    left and its ``deadline_ms``. A remaining budget under
    ``_MIN_DEADLINE_SECONDS`` means nothing is attempted and the turn is a
    miss; the configured deadline alone is never floored.

    With the ``prompt_gate`` decision site off, the cosine floor gates the
    similarity arm. With it on, the gate scores an RRF pool of the keyword
    and similarity arms and writes one ex-ante verdict per candidate to the
    live buffer; ``task`` is the session's open task title. The hook handler
    owns every other buffer write.
    """
    started = time.monotonic()
    rpt = cfg.retrieval_prompt_time
    if not rpt.enabled:
        return _NOTHING

    # Triviality gate only — skip trivially short inputs and slash-commands so
    # we don't pay an embedding on "ok"/"yes"/"/clear".
    t = (prompt_text or "").strip()
    if len(t) < rpt.min_prompt_chars or t.startswith("/"):
        return _NOTHING

    # The gate numbers its verdicts by turn, so it reads the archive too; the
    # floor needs only what the live buffer holds, at no session-note lookup.
    gated = cfg.decision_site(PROMPT_GATE).enabled
    ledger = Ledger.read(cfg, session_id) if gated else Ledger.live(cfg, session_id)
    fired = _firings(ledger)
    if len(fired) >= rpt.max_firings_per_session:
        return _NOTHING
    remaining_session = rpt.max_injected_chars_per_session - sum(
        int(ev.get("chars", 0) or 0) for ev in fired
    )
    if remaining_session < _MIN_PIECE_CHARS:
        return _NOTHING

    # Adaptive skip — a run of consecutive misses (an unreachable embedding
    # endpoint or decision backend) means the next attempt is a sunk cost.
    if _trailing_misses(ledger) >= rpt.deadline_miss_limit:
        return _NOTHING

    deadline_s = rpt.embed_deadline_seconds
    if deadline is not None:
        if deadline < _MIN_DEADLINE_SECONDS:
            return Enrichment(None, [], True)
        deadline_s = min(deadline, deadline_s)

    char_cap = min(rpt.max_injected_chars_per_turn, remaining_session)
    if gated:
        return _site_gated(
            cfg, ledger, prompt_text, task=task, char_cap=char_cap,
            embed_deadline=deadline_s,
            until=None if deadline is None else started + deadline,
        )
    return _floor_gated(cfg, ledger, prompt_text, char_cap=char_cap, embed_deadline=deadline_s)


def _floor_gated(
    cfg: Config, ledger: Ledger, prompt_text: str, *, char_cap: int, embed_deadline: float
) -> Enrichment:
    """The similarity arm alone, gated by its cosine floor."""
    rpt = cfg.retrieval_prompt_time
    results, missed = _retrieve(
        cfg,
        prompt_text,
        list(rpt.bias_types),
        limit=max(rpt.max_pieces_per_turn * 3, 10),
        deadline=embed_deadline,
        min_similarity=rpt.min_similarity,
    )
    if missed:
        return Enrichment(None, [], True)
    served = ledger.served_ids()
    fresh = [r for r in results if r.id not in served]
    return Enrichment(*_render(fresh[: rpt.max_pieces_per_turn], char_cap), False)


def _site_gated(
    cfg: Config,
    ledger: Ledger,
    prompt_text: str,
    *,
    task: str,
    char_cap: int,
    embed_deadline: float,
    until: float | None,
) -> Enrichment:
    """The RRF pool, gated by one ``prompt_gate`` decision call.

    Serves the candidates the site accepts and records one ex-ante verdict
    per candidate. When the site gives no verdict by ``until`` (a
    ``time.monotonic`` instant) or its own deadline, serves the ungated RRF
    top and names why in ``decision_miss``.
    """
    rpt = cfg.retrieval_prompt_time
    gate = cfg.decision_site(PROMPT_GATE)
    served = ledger.served_ids()
    pool = _pool(
        cfg, prompt_text, list(rpt.bias_types),
        size=gate.pool_size + len(served), deadline=embed_deadline,
    )
    fresh = [r for r in pool.candidates if r.id not in served][: gate.pool_size]
    if not fresh:
        return Enrichment(None, [], pool.missed)
    budget = gate.deadline_ms / 1000
    if until is not None:
        budget = min(budget, until - time.monotonic())

    prompts = ledger.prompts()
    slots = {
        "prompt": prompt_text,
        "previous_prompt": prompts[-2] if len(prompts) > 1 else "(none)",
        "task": task or "(none)",
    }
    try:
        if budget < _MIN_DEADLINE_SECONDS:
            raise TimeoutError("the hook budget left no time for the gate")
        decision = ask_site(
            cfg, PROMPT_GATE, slots,
            subjects={r.id: {"candidate": _describe(r)} for r in fresh},
            deadline=budget,
        )
    except Exception as exc:
        block, ids = _render(fresh[: rpt.max_pieces_per_turn], char_cap)
        return Enrichment(block, ids, pool.missed, f"{type(exc).__name__}: {exc}")

    score = {a.subject: a.value for a in decision.answers}
    accepted = [r for r in fresh if score.get(r.id, 0.0) >= gate.accept]
    block, ids = _render(accepted[: rpt.max_pieces_per_turn], char_cap)
    record_verdict(
        cfg, PROMPT_GATE, decision, session_id=ledger.session_key, turn=len(prompts),
        source="prompttime",
        actions={
            r.id: "served" if r.id in ids else "capped" if r in accepted else "rejected"
            for r in fresh
        },
    )
    return Enrichment(block, ids, pool.missed)


def _firings(ledger: Ledger) -> list[dict]:
    """The session's prompt-time injections so far, one event each."""
    return [ev for ev in ledger.events if ev.get("tool") == PROMPT_TIME_TOOL]


def _trailing_misses(ledger: Ledger) -> int:
    """Count R2's misses since its last successful firing (or session start).

    Walks ``prompt_time_miss`` and ``decision_miss`` events and firings
    (``tool == PROMPT_TIME_TOOL``) backwards from the end of the ledger.
    Plain ``prompt`` events do not interrupt the streak, and neither does a
    firing that served the ungated fallback after a decision miss.
    """
    n = 0
    for ev in reversed(ledger.events):
        if ev.get("type") in (PROMPT_TIME_MISS, DECISION_MISS):
            n += 1
        elif ev.get("tool") == PROMPT_TIME_TOOL and not ev.get("fallback"):
            break
    return n


def _retrieve(
    cfg: Config,
    query: str,
    note_type: list[str],
    *,
    limit: int,
    deadline: float,
    min_similarity: float,
) -> tuple[list, bool]:
    """The similarity arm, deadlined in a daemon thread, floored at
    ``min_similarity`` (``0.0`` disables the floor).

    SQLite connections are thread-affine, so the arm gets its OWN ``Search``
    inside the daemon thread — sharing the main-thread connection raises a
    ProgrammingError mid-query.

    Returns ``(results, missed)``. ``missed`` is ``True`` only when the
    daemon thread is still alive after ``deadline`` seconds — the thread is
    abandoned and the caller records a deadline miss. Any other failure
    (``Search()`` construction, the query itself raising) returns fast as an
    empty result with ``missed=False``.
    """
    from thinkweave.retrieval.search import Search

    try:
        s = Search(config=cfg)
    except Exception:
        return [], False
    try:
        wide = max(limit * 2, 20)
        holder: dict = {}

        def _run() -> None:
            try:
                s2 = Search(config=cfg)
                try:
                    holder["r"] = s2.similar(query, note_type=note_type, limit=wide)
                finally:
                    s2.close()
            except Exception:
                holder["r"] = []

        th = threading.Thread(target=_run, daemon=True)
        th.start()
        th.join(timeout=max(0.0, deadline))
        if th.is_alive():
            return [], True

        sem = holder.get("r", []) or []
        # ``.rank`` carries the cosine score on results from .similar().
        if min_similarity > 0.0:
            sem = [r for r in sem if getattr(r, "rank", 0.0) >= min_similarity]
        return sem[:limit], False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _pool(
    cfg: Config, query: str, note_type: list[str], *, size: int, deadline: float
) -> Pool:
    """The top ``size`` of the any-term BM25 arm and the unfloored similarity
    arm, fused by RRF."""
    from thinkweave.retrieval.search import Search, rrf_fuse

    s = Search(config=cfg)
    try:
        keyword = s.search(query, note_type=note_type, limit=size, any_term=True)
    finally:
        s.close()
    similar, missed = _retrieve(
        cfg, query, note_type, limit=size, deadline=deadline, min_similarity=0.0
    )
    return Pool(rrf_fuse([keyword, similar], cfg.retrieval_rrf_k, size), missed)


def _describe(r) -> str:
    """A candidate as the gate reads it: title, type and any matched snippet."""
    title = (getattr(r, "title", "") or r.id).strip()
    snippet = (getattr(r, "snippet", "") or "").replace(">>>", "").replace("<<<", "")
    text = f"{title} ({getattr(r, 'type', 'note')})"
    return f"{text}: {snippet.strip()}" if snippet.strip() else text


def _render(results: list, char_cap: int) -> tuple[str | None, list[str]]:
    """Render the injected block under a char budget. First line always fits."""
    lines = [_HEADER]
    total = len(_HEADER) + 1
    used: list[str] = []
    for r in results:
        title = (getattr(r, "title", "") or r.id).strip()
        line = f"- [[{r.id}]] ({getattr(r, 'type', 'note')}) — {title}"
        if used and total + len(line) + 1 > char_cap:
            break
        lines.append(line)
        total += len(line) + 1
        used.append(r.id)
    if not used:
        return None, []
    return "\n".join(lines), used
