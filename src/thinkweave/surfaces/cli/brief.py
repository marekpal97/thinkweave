"""``weave brief mark`` — /brief's one deterministic write (#170).

Logs the surfaced ids as ``context_served(source='brief')`` once the brief
note is written. The payload side has no collect: the skill composes the
existing surfaces (``weave health --json`` + the ``weave_*`` retrieval
tools) and narrates by judgment — dec-696bacfb. Headless-safe: no prompts,
no LLM.
"""

from __future__ import annotations

import argparse
import os

from thinkweave.core.config import load_config
from thinkweave.operations import served


def cmd_brief(args: argparse.Namespace) -> None:
    cfg = load_config()
    # Claude Code's Bash env exports CLAUDE_CODE_SESSION_ID; hooks see
    # CLAUDE_SESSION_ID via hook_input — accept both.
    session = (
        args.session
        or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        or os.environ.get("CLAUDE_SESSION_ID", "")
    )
    n = served.mark(cfg, "brief", session, args.note, args.served)
    if n:
        print(f"brief mark · {n} id(s) logged as context_served(source='brief') for {args.note}")
    else:
        print(
            f"brief mark · nothing logged: session {session!r} does not resolve to an "
            "indexed session note (pass --session <ses-id|uuid>; a session note appears "
            "once hooks have created it)"
        )
