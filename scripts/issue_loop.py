#!/usr/bin/env python3
"""Thin shim — the deterministic rail lives in ``devloop/`` (issue #94).

Kept so every existing caller (the /issue-loop command doc, crons, muscle
memory) keeps working unchanged: ``python scripts/issue_loop.py <subcommand>``
is byte-compatible with what it was. Package map and rationale:
docs/agents/devloop-boundaries.md.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from devloop.cli import main  # noqa: E402  — needs the sys.path line above

if __name__ == "__main__":
    sys.exit(main())
