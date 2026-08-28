"""``weave learn`` — the agent-Bash rail under ``/learn`` (#171).

``check`` (learn-note contract), ``probe`` (unanswered question → probe
row, ``operations/prompts.py::record_probe``). Retrieval and the
trajectory partition are the skill's job over ``weave_search``/
``weave_concepts``, and the session's own MCP retrieval calls already
land in ``context_served`` — no mark step (dec-696bacfb). Never prompts;
``probe`` exits 0 with a note when the session is unresolvable — it
never fabricates a key.
"""

from __future__ import annotations

import argparse
import sys

from thinkweave.core.config import load_config
from thinkweave.operations.prompts import record_probe


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


def cmd_learn(args: argparse.Namespace) -> None:
    cfg = load_config()
    action = args.learn_action
    if action == "check":
        from thinkweave.operations.notes import read_note

        meta, _ = read_note(cfg, args.note)
        if meta is None:
            print(f"note {args.note} not in the index — run `weave index` first")
            sys.exit(2)
        problems = validate_learn_note(meta.frontmatter)
        for p in problems:
            print(f"  - {p}")
        print(f"{args.note}: {'ok' if not problems else f'{len(problems)} problem(s)'}")
        sys.exit(1 if problems else 0)
    elif action == "probe":
        ok = record_probe(cfg, args.session, args.text)
        print("probe recorded" if ok else f"session {args.session!r} unresolvable; wrote nothing")
