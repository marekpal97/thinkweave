"""``weave brief`` — the deterministic halves of ``/brief`` (#170).

``collect --json`` emits the payload documented in
:mod:`thinkweave.operations.brief` (the skill narrates from it); ``mark``
logs the surfaced ids as ``context_served(source='brief')`` once the brief
note is written. Headless-safe: no prompts, no LLM.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from thinkweave.core.config import load_config
from thinkweave.operations import brief


def cmd_brief(args: argparse.Namespace) -> None:
    cfg = load_config()
    if args.brief_action == "mark":
        # Claude Code's Bash env exports CLAUDE_CODE_SESSION_ID; hooks see
        # CLAUDE_SESSION_ID via hook_input — accept both.
        session = (
            args.session
            or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
            or os.environ.get("CLAUDE_SESSION_ID", "")
        )
        n = brief.mark(cfg, args.note, args.served, session_id=session)
        if n:
            print(f"brief mark · {n} id(s) logged as context_served(source='brief') for {args.note}")
        else:
            print(
                f"brief mark · nothing logged: session {session!r} does not resolve to an "
                "indexed session note (pass --session <ses-id|uuid>; a session note appears "
                "once hooks have created it)"
            )
        return
    payload = brief.collect(cfg)
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    print(f"brief since {payload['since']} ({payload['since_reason']})")
    if payload["banner"]:
        print(f"!! {payload['banner']}")
    print("render_plan: " + ", ".join(payload["render_plan"]))
    for lane in payload["lanes"]:
        print(f"  {lane['source_type']:<22} landed={lane['landed']:<3} {lane['state']}")
    print(f"served: {len(payload['served_ids'])} id(s)")
    sys.exit(0)
