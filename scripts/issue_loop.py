#!/usr/bin/env python3
"""Thin shim over the installed ``devloop`` package (issue #151).

The deterministic rail is not in this tree — it is a pinned dev-dependency from
funloops (see the pin in ``pyproject.toml``). This file exists only so every
existing caller — crons, the /issue-loop command doc, muscle memory — keeps
working unchanged: ``python scripts/issue_loop.py <subcommand>`` is
byte-compatible with what it was before the carve-out, which
``tests/test_devloop_boundaries.py`` pins against a pre-carve capture of
``config``'s stdout.

No ``sys.path`` juggling: the package is installed, and prepending the repo
root here is exactly the shadowing vector that file's first seam guards.
"""

import sys

from devloop.cli import main

if __name__ == "__main__":
    sys.exit(main())
