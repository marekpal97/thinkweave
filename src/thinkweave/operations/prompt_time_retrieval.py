"""Prompt-time retrieval enrichment (R2).

The ``UserPromptSubmit`` hook calls :func:`build_enrichment` on each user
prompt. It surfaces a small, deduped, hard-capped block of vault notes relevant
to the prompt — *delta over startup* — to prepend to what the model sees.

Design (rewritten 2026-07-05 — the FTS arm never fired in production; see
below):

- **Relevance is the gate, and it's the embedding cosine — no wordlists.** A
  prompt's hits are admitted only above a cosine floor. Domain prompts score
  ~0.40+; generic/meta prompts ("can we verify this works?", "what do you
  think?") score ~0.22–0.36 and fall below the floor → no injection. The
  semantic signal discriminates cleanly on its own; there is deliberately no
  stopword/keyword heuristic layer (it was tried and removed — brittle, and the
  floor already does the job).

- **Similarity-only — the FTS arm was removed.** This hook used to also run a
  synchronous FTS5 query, RRF-fused with the similarity arm. FTS5 AND-matches
  every token in the query by default, and a natural-language prompt is a
  dozen-plus tokens of prose, not a keyword phrase — so the FTS arm was a
  structural no-op against real prompts (measured: zero ``source='prompttime'``
  rows in ``context_served`` since R2 shipped 2026-06-13, across every session
  since). Rather than carry dead machinery, the FTS arm and its RRF fusion are
  gone; only the deadlined similarity search remains, cosine-floored as before.

- **Latency is bounded, not eliminated.** The embedding call is the cost (a few
  hundred ms typically; slower on some networks). It runs in a daemon thread
  with a wall-clock deadline; on overrun the arm is abandoned and the hook
  returns no injection for that turn — a graceful no-op, never a raise. The
  hook's own timeout (configured in ``hooks/hooks.json`` for the plugin route,
  or per-phase by ``weave hooks install`` for the CLI route) is set generously
  above this deadline.

- **Deadline misses are tracked, and adaptive-skip kicks in.** A deadline miss
  means the embedding call is still running when the hook must return —
  the thread is abandoned (Python has no clean thread-kill), so the ~4s cost
  is sunk without a payoff. The handler records each miss as a distinct
  ``prompt_time_miss`` buffer event (never tagged as a firing, never typed
  ``retrieval`` — see the RLVR meshing note below). Once
  ``deadline_miss_limit`` consecutive misses accumulate for a session (a slow
  or unreachable embedding endpoint), :func:`build_enrichment` skips the
  similarity arm for the rest of that session rather than re-paying a doomed
  ~4s tax on every remaining turn.

- **Dedup is buffer-based.** The live ``buffer/<session_id>.jsonl`` carries the
  ``startup`` event's ids, every ``retrieval`` event's, and our own prior
  write-backs. ``context_served`` is a Stop-time projection (stale mid-session)
  and is NOT consulted here.

- **RLVR meshing.** The hook writes served ids back as a ``retrieval`` event
  tagged ``tool == PROMPT_TIME_TOOL``; the indexer projects those to
  ``context_served`` with ``source='prompttime'`` (distinct from agent-pulled
  ``onthefly``), keeping the agent-judgment signal clean and making push
  efficacy measurable. The ``prompt_time_miss`` telemetry event is a different
  ``type`` entirely and carries no ``tool`` field, so it is invisible to both
  the firings ledger (:func:`_served_ids_and_stats`, keyed on
  ``tool == PROMPT_TIME_TOOL``) and the indexer's ``context_served``
  projection (keyed on ``type in ("startup", "retrieval")``) — it is pure
  telemetry, never mistaken for a served note or a firing.

This module never writes — :func:`build_enrichment` is pure orchestration over
read-only helpers, unit-testable without a live hook. The hook handler owns the
buffer write-back (both the served-ids write-back and the miss telemetry) and
the stdout emit.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from thinkweave.core.config import Config

# Sentinel tool name stamped on the buffer write-back event. The indexer's
# context_served projection keys off this to assign source='prompttime'.
PROMPT_TIME_TOOL = "prompt_time_retrieval"

# Buffer event type for a deadline miss — distinct from both "retrieval"
# (which the indexer projects to context_served) and a PROMPT_TIME_TOOL-tagged
# event (which _served_ids_and_stats counts as a firing). Pure telemetry.
PROMPT_TIME_MISS = "prompt_time_miss"

# Render header for the injected block.
_HEADER = "📎 Possibly relevant from your vault (optional — weave_read to expand):"
# Never emit if the remaining char budget can't fit a single useful line.
_MIN_PIECE_CHARS = 80


def build_enrichment(
    cfg: Config, session_id: str, prompt_text: str
) -> tuple[str | None, list[str], bool]:
    """Build the prompt-time enrichment block for one prompt.

    Returns ``(block, served_ids, missed)``:

    - ``block`` — the text to inject, or ``None`` to no-op.
    - ``served_ids`` — the note ids the block surfaced (for the hook's
      served-ids buffer write-back).
    - ``missed`` — ``True`` iff the similarity arm blew its wall-clock
      deadline this turn (the hook should record a ``prompt_time_miss``
      telemetry event). ``False`` for every other outcome, including a
      clean empty result, an early gate, or the adaptive skip itself (the
      skip prevents a *new* attempt — it isn't itself a miss).

    Pure: reads the live buffer + index, writes nothing. The hook handler
    owns every buffer write-back (both served ids and miss telemetry).
    """
    rpt = cfg.retrieval_prompt_time
    if not rpt.enabled:
        return None, [], False

    # Triviality gate only — skip trivially short inputs and slash-commands so
    # we don't pay an embedding on "ok"/"yes"/"/clear". This is NOT a semantic
    # filter; relevance is decided by the cosine floor below.
    t = (prompt_text or "").strip()
    if len(t) < rpt.min_prompt_chars or t.startswith("/"):
        return None, [], False

    served, firings, injected_chars = _served_ids_and_stats(cfg, session_id)
    if firings >= rpt.max_firings_per_session:
        return None, [], False
    remaining_session = rpt.max_injected_chars_per_session - injected_chars
    if remaining_session < _MIN_PIECE_CHARS:
        return None, [], False

    # Adaptive skip — a run of consecutive deadline misses (e.g. an
    # unreachable or saturated embedding endpoint) means the similarity arm
    # is currently a sunk ~deadline-seconds cost with no payoff. Stop paying
    # it for the rest of this session rather than re-trying every turn.
    if _consecutive_trailing_misses(cfg, session_id) >= rpt.deadline_miss_limit:
        return None, [], False

    limit = max(rpt.max_pieces_per_turn * 3, 10)
    results, missed = _retrieve(
        cfg,
        prompt_text,
        list(rpt.bias_types),
        limit=limit,
        deadline=rpt.embed_deadline_seconds,
        min_similarity=rpt.min_similarity,
    )
    if missed:
        return None, [], True

    fresh = [r for r in results if r.id not in served]
    if not fresh:
        return None, [], False

    char_cap = min(rpt.max_injected_chars_per_turn, remaining_session)
    block, ids = _render(fresh[: rpt.max_pieces_per_turn], char_cap)
    return block, ids, False


def _read_buffer_events(cfg: Config, session_id: str) -> list[dict]:
    """Read the live per-session event buffer (tolerates malformed lines)."""
    buf: Path = cfg.weave_dir / "buffer" / f"{session_id}.jsonl"
    if not buf.exists():
        return []
    out: list[dict] = []
    for line in buf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _served_ids_and_stats(
    cfg: Config, session_id: str
) -> tuple[set[str], int, int]:
    """Cumulative session ledger from the live buffer.

    Returns ``(served_ids, firings, injected_chars)``:
    - served_ids = ids served via startup + on-the-fly + prior prompt-time
      injections (the dedup exclude set),
    - firings = how many times R2 has already injected this session,
    - injected_chars = cumulative size of those injections (session ceiling).
    """
    served: set[str] = set()
    firings = 0
    injected_chars = 0
    for ev in _read_buffer_events(cfg, session_id):
        if ev.get("type") in ("startup", "retrieval"):
            for nid in ev.get("returned_ids", []) or []:
                if isinstance(nid, str) and nid:
                    served.add(nid)
        if ev.get("tool") == PROMPT_TIME_TOOL:
            firings += 1
            injected_chars += int(ev.get("chars", 0) or 0)
    return served, firings, injected_chars


def _consecutive_trailing_misses(cfg: Config, session_id: str) -> int:
    """Count trailing consecutive R2 deadline misses in the live buffer.

    Walks R2's own outcome events — ``prompt_time_miss`` telemetry and
    successful firings (``tool == PROMPT_TIME_TOOL``) — from the end of the
    buffer, backwards. Every plain ``prompt`` event (written once per user
    turn, miss or not) is not part of this ledger and doesn't interrupt the
    streak; only a successful firing resets it to zero. This makes the count
    "misses since the last successful injection (or since session start)",
    which is what the adaptive-skip gate wants: a slow/unreachable embedding
    endpoint keeps missing turn after turn with ordinary prompts interleaved.
    """
    n = 0
    for ev in reversed(_read_buffer_events(cfg, session_id)):
        if ev.get("type") == PROMPT_TIME_MISS:
            n += 1
        elif ev.get("tool") == PROMPT_TIME_TOOL:
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
    """Similarity-only retrieval, deadlined in a daemon thread.

    FTS is not attempted here — see the module docstring: FTS5 AND-matches
    every token, so a full natural-language prompt almost never matches it.
    The cosine floor on the similarity arm is what keeps generic/meta prompts
    from injecting low-relevance nearest-neighbours.

    SQLite connections are thread-affine, so the similarity arm gets its OWN
    ``Search`` inside the daemon thread — sharing the main-thread connection
    raises a ProgrammingError mid-query.

    Returns ``(results, missed)``. ``missed`` is ``True`` only when the
    daemon thread is still alive after ``deadline`` seconds — the thread is
    abandoned (not killed; Python has no clean thread-kill) and the caller
    should record this as a deadline miss. Any other failure (``Search()``
    construction, the query itself raising) returns fast and is reported as
    an ordinary empty result — ``missed=False`` — since there's no wasted
    wall-clock budget to track in that case.
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
        # Cosine floor. ``.rank`` carries the cosine score on results from
        # .similar() — this is THE relevance gate for this hook.
        if min_similarity > 0.0:
            sem = [r for r in sem if getattr(r, "rank", 0.0) >= min_similarity]
        return sem[:limit], False
    finally:
        try:
            s.close()
        except Exception:
            pass


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
