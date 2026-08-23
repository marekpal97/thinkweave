"""``weave learn`` — the agent-Bash rail under ``/learn`` (#171).

``coverage`` (retrieve + partition + mode), ``check`` (learn-note contract),
``mark`` (``context_served(source='learn')``), ``probe`` (unanswered
question → probe row). Never prompts; ``mark``/``probe`` exit 0 with a note
when the session is unresolvable — they never fabricate a key.
"""

from __future__ import annotations

import argparse
import json
import sys

from thinkweave.core.config import load_config
from thinkweave.operations import learn, served


def cmd_learn(args: argparse.Namespace) -> None:
    cfg = load_config()
    action = args.learn_action
    if action == "coverage":
        cov = learn.coverage(cfg, args.topic, args.concepts)
        if args.json:
            print(json.dumps(cov, indent=2))
            return
        print(f"Mode: {cov['mode']}  (fill cap {cov['fill_cap']})")
        print(cov["first_contact_line"] or f"Trajectory ({len(cov['trajectory'])}):")
        for h in cov["trajectory"]:
            print(f"  {h['date'][:10]}  [{h['type']}{'/' + h['kind'] if h['kind'] else ''}] {h['title']} ({h['id']})")
        print(f"Material ({len(cov['material'])}):")
        for h in cov["material"]:
            print(f"  [{h['type']}] {h['title']} ({h['id']})")
    elif action == "check":
        from thinkweave.core.vault import VaultManager
        from thinkweave.retrieval.search import Search

        s = Search(config=cfg)
        try:
            row = s.get_note_by_id(args.note)
        finally:
            s.close()
        if not row:
            print(f"note {args.note} not in the index — run `weave index` first")
            sys.exit(2)
        fm = VaultManager(config=cfg).read_note(cfg.vault_root / row["path"]).frontmatter
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
    else:
        print("usage: weave learn {coverage,check,mark,probe}")
        sys.exit(2)
