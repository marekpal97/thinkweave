"""``weave learn`` — the agent-Bash rail under ``/learn`` (#171).

``check`` (learn-note contract), ``mark`` (``context_served(source='learn')``),
``probe`` (unanswered question → probe row). Retrieval + the trajectory
partition live in the skill over ``weave_search``/``weave_concepts``
(dec-696bacfb). Never prompts; ``mark``/``probe`` exit 0 with a note
when the session is unresolvable — they never fabricate a key.
"""

from __future__ import annotations

import argparse
import sys

from thinkweave.core.config import load_config
from thinkweave.operations import learn, served


def cmd_learn(args: argparse.Namespace) -> None:
    cfg = load_config()
    action = args.learn_action
    if action == "check":
        from thinkweave.operations.notes import read_note

        meta, _ = read_note(cfg, args.note)
        if meta is None:
            print(f"note {args.note} not in the index — run `weave index` first")
            sys.exit(2)
        fm = meta.frontmatter
        problems = learn.validate_learn_note(fm)
        for p in problems:
            print(f"  - {p}")
        print(f"{args.note}: {'ok' if not problems else f'{len(problems)} problem(s)'}")
        sys.exit(1 if problems else 0)
    elif action == "mark":
        n = served.mark(cfg, "learn", args.session, args.note, args.served)
        print(f"context_served(source='learn'): {n} row(s)"
              + ("" if n else f" — session {args.session!r} unresolvable or nothing to mark; wrote nothing"))
    elif action == "probe":
        ok = learn.probe(cfg, args.session, args.text)
        print("probe recorded" if ok else f"session {args.session!r} unresolvable; wrote nothing")
