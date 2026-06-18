"""Prompt-time retrieval enrichment (R2).

The ``UserPromptSubmit`` hook calls :func:`build_enrichment` on each user
prompt. It surfaces a small, deduped, hard-capped block of vault notes relevant
to the prompt — *delta over startup* — to prepend to what the model sees.

Design (settled by live measurement, 2026-06-07):

- **Relevance is the gate, and it's the embedding cosine — no wordlists.** A
  prompt's hybrid hits are admitted only above a cosine floor. Domain prompts
  score ~0.40+; generic/meta prompts ("can we verify this works?", "what do you
  think?") score ~0.22–0.36 and fall below the floor → no injection. The
  semantic signal discriminates cleanly on its own; there is deliberately no
  stopword/keyword heuristic layer (it was tried and removed — brittle, and the
  floor already does the job).

- **Hybrid = FTS + similarity, RRF-fused.** FTS (phrase) is synchronous and
  near-free; it contributes on exact-keyword prompts. The similarity arm carries
  natural prose. The cosine floor applies to the similarity arm.

- **Latency is bounded, not eliminated.** The embedding call is the cost (a few
  hundred ms typically; slower on some networks). It runs in a daemon thread
  with a wall-clock deadline; on overrun the arm is abandoned and we fall back
  to FTS (→ usually a silent no-op for prose). The hook timeout is set above the
  deadline in install.py.

- **Dedup is buffer-based.** The live ``buffer/<session_id>.jsonl`` carries the
  ``startup`` event's ids, every ``retrieval`` event's, and our own prior
  write-backs. ``context_served`` is a Stop-time projection (stale mid-session)
  and is NOT consulted here.

- **RLVR meshing.** The hook writes served ids back as a ``retrieval`` event
  tagged ``tool == PROMPT_TIME_TOOL``; the indexer projects those to
  ``context_served`` with ``source='prompttime'`` (distinct from agent-pulled
  ``onthefly``), keeping the agent-judgment signal clean and making push
  efficacy measurable.

This module never writes — :func:`build_enrichment` is pure orchestration over
read-only helpers, unit-testable without a live hook. The hook handler owns the
buffer write-back and the stdout emit.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from thinkweave.core.config import Config

# Sentinel tool name stamped on the buffer write-back event. The indexer's
# context_served projection keys off this to assign source='prompttime'.
PROMPT_TIME_TOOL = "prompt_time_retrieval"

# Render header for the injected block.
_HEADER = "📎 Possibly relevant from your vault (optional — weave_read to expand):"
# Never emit if the remaining char budget can't fit a single useful line.
_MIN_PIECE_CHARS = 80


def build_enrichment(
    cfg: Config, session_id: str, prompt_text: str
) -> tuple[str | None, list[str]]:
    """Build the prompt-time enrichment block for one prompt.

    Returns ``(block, served_ids)`` — ``block`` is the text to inject (or
    ``None`` to no-op), ``served_ids`` the note ids it surfaced (for the hook's
    buffer write-back). Pure: reads the live buffer + index, writes nothing.
    """
    rpt = cfg.retrieval_prompt_time
    if not rpt.enabled:
        return None, []

    # Triviality gate only — skip trivially short inputs and slash-commands so
    # we don't pay an embedding on "ok"/"yes"/"/clear". This is NOT a semantic
    # filter; relevance is decided by the cosine floor below.
    t = (prompt_text or "").strip()
    if len(t) < rpt.min_prompt_chars or t.startswith("/"):
        return None, []

    served, firings, injected_chars = _served_ids_and_stats(cfg, session_id)
    if firings >= rpt.max_firings_per_session:
        return None, []
    remaining_session = rpt.max_injected_chars_per_session - injected_chars
    if remaining_session < _MIN_PIECE_CHARS:
        return None, []

    limit = max(rpt.max_pieces_per_turn * 3, 10)
    results = _retrieve(
        cfg,
        prompt_text,
        list(rpt.bias_types),
        limit=limit,
        deadline=rpt.embed_deadline_seconds,
        min_similarity=rpt.min_similarity,
    )
    fresh = [r for r in results if r.id not in served]
    if not fresh:
        return None, []

    char_cap = min(rpt.max_injected_chars_per_turn, remaining_session)
    return _render(fresh[: rpt.max_pieces_per_turn], char_cap)


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


def _retrieve(
    cfg: Config,
    query: str,
    note_type: list[str],
    *,
    limit: int,
    deadline: float,
    min_similarity: float,
):
    """FTS (sync) + similarity (deadlined daemon thread), RRF-fused.

    Mirrors ``Search.hybrid_search`` but (1) bounds the embedding arm to a
    wall-clock deadline so the hook never blocks past its budget and (2) applies
    a cosine floor to the similarity arm — the floor is what keeps generic/meta
    prompts from injecting low-relevance nearest-neighbours. FTS hits (concrete
    keyword matches) are admitted regardless of the floor.

    SQLite connections are thread-affine, so the similarity arm gets its OWN
    ``Search`` inside the daemon thread — sharing the main-thread connection
    raises a ProgrammingError mid-query. Any failure degrades to FTS-only/empty.
    """
    from thinkweave.retrieval.search import Search

    try:
        s = Search(config=cfg)
    except Exception:
        return []
    try:
        wide = max(limit * 2, 20)
        try:
            fts = s.search(query, note_type=note_type, limit=wide) or []
        except Exception:
            fts = []

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
        sem = holder.get("r", []) if not th.is_alive() else []

        # Cosine floor on the similarity arm. ``.rank`` carries the cosine score
        # on results from .similar(); FTS hits keep their place regardless.
        if min_similarity > 0.0:
            sem = [r for r in sem if getattr(r, "rank", 0.0) >= min_similarity]

        return _rrf_fuse(fts, sem, limit)
    finally:
        try:
            s.close()
        except Exception:
            pass


def _rrf_fuse(fts: list, sem: list, limit: int, rrf_k: int = 60) -> list:
    """Reciprocal-rank fusion of two ranked lists (k=60), per Search.hybrid_search."""
    if not sem:
        return fts[:limit]
    if not fts:
        return sem[:limit]
    scores: dict[str, float] = {}
    rows: dict = {}
    for rank, r in enumerate(fts, start=1):
        scores[r.id] = scores.get(r.id, 0.0) + 1.0 / (rrf_k + rank)
        rows[r.id] = r
    for rank, r in enumerate(sem, start=1):
        scores[r.id] = scores.get(r.id, 0.0) + 1.0 / (rrf_k + rank)
        rows.setdefault(r.id, r)
    ranked = sorted(scores, key=lambda i: scores[i], reverse=True)
    out = []
    for nid in ranked[:limit]:
        r = rows[nid]
        r.rank = scores[nid]
        out.append(r)
    return out


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
