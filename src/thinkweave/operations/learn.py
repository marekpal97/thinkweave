"""``/learn`` deterministic seams (#171).

The skill is prose; these are the two things it cannot do by judgment:

* :func:`validate_learn_note` — the learn-note frontmatter contract.
* :func:`probe` — an unanswered question as a probe row, the exact event
  pair ``weave wrap-finalize --verdicts`` persists, so ``weave_prompts`` /
  the dream probe-distiller see it with no new table.

Retrieval and the trajectory/material partition live in the skill
(``commands/learn.md`` §1) over ``weave_search`` + ``weave_concepts`` —
per dec-696bacfb the partition is provenance the model reads off each
hit's ``type``/``path``, not a Python wrapper.
"""

from __future__ import annotations

from datetime import datetime, timezone

from thinkweave.core.config import Config
from thinkweave.operations.retrieval_log import append_event
from thinkweave.operations.served import resolve_session, session_log


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
    append_event(log, {"ts": ts, "type": "prompt", "text": text, "session_id": ref.source_session})
    append_event(log, {"ts": ts, "type": "probe", "session_id": ref.source_session,
                       "prompt_ref": text[:120]})
    return True
