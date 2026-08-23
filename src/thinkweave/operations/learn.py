"""``/learn`` deterministic seams (#171).

The skill is prose; these are the three things it cannot do by judgment:

* :func:`coverage` — ONE retrieval (FTS on the topic ∪ the concept walk —
  not ``weave_context``: its recency-supplement layer pads unrelated notes
  in and would fabricate a trajectory; its similarity leg is not live, see
  #145), partitioned *after* by provenance into ``trajectory`` (what the user
  authored: session / decision / note incl. ``kind: learn``, ``til``,
  chatgpt imports) and ``material`` (what the world says: source, theme,
  digest, concept-hub essence). The partition IS the presupposition check.
* :func:`validate_learn_note` — the learn-note frontmatter contract.
* :func:`probe` — an unanswered question as a probe row, the exact event
  pair ``weave wrap-finalize --verdicts`` persists, so ``weave_prompts`` /
  the dream probe-distiller see it with no new table.
"""

from __future__ import annotations

from datetime import datetime, timezone

from thinkweave.core.config import Config
from thinkweave.operations.retrieval_log import append_event
from thinkweave.operations.served import resolve_session, session_log

FIRST_CONTACT_LINE = "First contact in the vault — no prior trajectory for '{topic}'."

# World-authored provenance: sources, themes, digests, and both hub families
# (``concepts/topics/*`` concept hubs, ``concepts/*`` domain hubs). The one
# exception is the ChatGPT import — ``type: source`` but ``source_type:
# conversation`` — which is the user's own history and so trajectory.
_MATERIAL_TYPES = frozenset({"source", "theme", "digest", "concept-hub", "domain-hub"})


def coverage(cfg: Config, topic: str, concepts: list[str] | None = None, limit: int = 40) -> dict:
    """Retrieve once, partition by provenance, pick the session mode.

    Mode rule: ``test-first`` iff the trajectory holds a prior ``kind: learn``
    note — only a learn note carries dated solid/shaky claims to re-probe;
    any other trajectory (sessions, decisions, til) is recapped, then taught
    teach-first. ``first_contact`` is true when the trajectory is empty and
    the skill must print ``first_contact_line`` verbatim.
    """
    from thinkweave.operations.search import query_fts
    from thinkweave.retrieval.search import Search

    concepts = [c for c in (concepts or []) if c]
    hits = {r.id: r for r in query_fts(cfg, topic, limit=limit)}
    s = Search(config=cfg)
    try:
        if concepts:
            for r in s.search_by_concept(concepts, limit=limit):
                hits.setdefault(r.id, r)
        fm: dict[str, tuple[str, str]] = {}  # id -> (kind, source_type)
        if hits:
            ph = ",".join("?" * len(hits))
            fm = {
                row[0]: (row[1] or "", row[2] or "")
                for row in s.db.execute(
                    "SELECT id, json_extract(frontmatter, '$.kind'), "
                    f"json_extract(frontmatter, '$.source_type') FROM notes WHERE id IN ({ph})",
                    list(hits),
                )
            }
    finally:
        s.close()

    trajectory, material = [], []
    for r in hits.values():
        kind, source_type = fm.get(r.id, ("", ""))
        row = {"id": r.id, "type": r.type, "title": r.title, "path": r.path,
               "date": r.date or "", "kind": kind}
        is_material = (
            r.type in _MATERIAL_TYPES or r.path.startswith("concepts/")
        ) and source_type != "conversation"
        (material if is_material else trajectory).append(row)
    trajectory.sort(key=lambda h: h["date"])
    prior_learn = [h["id"] for h in trajectory if h["kind"] == "learn"]
    return {
        "topic": topic,
        "concepts": concepts,
        "trajectory": trajectory,
        "material": material,
        "prior_learn_notes": prior_learn,
        "mode": "test-first" if prior_learn else "teach-first",
        "first_contact": not trajectory,
        "first_contact_line": FIRST_CONTACT_LINE.format(topic=topic) if not trajectory else "",
        "fill_cap": cfg.learn_fill_cap,
    }


def validate_learn_note(fm: dict) -> list[str]:
    """Problems with a learn note's frontmatter; empty list = valid."""
    problems: list[str] = []
    if fm.get("kind") != "learn":
        problems.append("kind must be 'learn'")
    if not str(fm.get("topic") or "").strip():
        problems.append("topic is required")
    if not str(fm.get("explain_back") or "").strip():
        problems.append("explain_back must hold the verbatim final explain-back")
    for key in ("solid", "shaky", "friction", "builds_on", "questions", "concepts"):
        if not isinstance(fm.get(key, []), list):
            problems.append(f"{key} must be a list")
    for key, needed in (("solid", ("concept", "date")), ("shaky", ("concept", "date", "why"))):
        entries = fm.get(key)
        if not isinstance(entries, list):
            continue
        for i, entry in enumerate(entries):
            if not (isinstance(entry, dict) and all(str(entry.get(k) or "").strip() for k in needed)):
                problems.append(f"{key}[{i}] needs {', '.join(needed)}")
    return problems


def probe(cfg: Config, session: str, text: str) -> bool:
    """Record ``text`` as a probe-classified prompt on the session's log.

    Same two-line shape the wrap verdict path writes (``prompt`` + ``probe``
    sharing ``ts``/``prompt_ref``), so ``extract_prompts`` classifies it
    without new schema. False when the session cannot be resolved.
    """
    text = text.strip()
    ref = resolve_session(cfg, session)
    if ref is None or not text:
        return False
    ts = datetime.now(timezone.utc).isoformat()
    log = session_log(cfg, ref, "events.jsonl")
    append_event(log, {"ts": ts, "type": "prompt", "text": text,
                       "session_id": ref.source_session, "cwd": "", "origin": "learn"})
    append_event(log, {"ts": ts, "type": "probe", "session_id": ref.source_session,
                       "prompt_ref": text[:120]})
    return True
