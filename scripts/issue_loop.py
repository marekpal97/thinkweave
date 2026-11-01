#!/usr/bin/env python3
"""Thin shim over the installed ``devloop`` package (issue #151).

The deterministic rail is not in this tree — it is a pinned dev-dependency from
funloops (see the pin in ``pyproject.toml``). This file exists only so every
existing caller — crons, the /issue-loop command doc, muscle memory — keeps
working unchanged: ``python scripts/issue_loop.py <subcommand>`` is
byte-compatible with what it was before the carve-out, which
``tests/test_devloop_boundaries.py`` pins against a pre-carve capture of
``config``'s stdout.

Two host seams ride on it (issue #346):

- **Policy directory.** When thinkweave's config names the rsi-rail repo
  (``THINKWEAVE_RSI_RAIL`` or the user-scope ``rsi_rail`` key), the shim
  exports ``DEVLOOP_POLICY_DIR=<rsi_rail>/devloop`` before devloop runs, so
  the pack texts there replace devloop's packaged defaults. The env var, not
  the ``--policy-dir`` flag, so argv reaches devloop untouched. A
  ``DEVLOOP_POLICY_DIR`` already in the environment wins, even empty (devloop
  reads empty as unset: the way to run without the rail's texts). No rail:
  nothing is exported and devloop behaves as before.
- **Run manifest.** ``trajectory`` gets ``--manifest-json`` with
  :func:`thinkweave.core.manifest.collect`'s manifest (the rail commit, the
  harness, and the readers: thinkweave and the funloops pin), unless the
  caller passed one. ``weave task record-run`` lands it on the round.

No ``sys.path`` juggling: the package is installed, and prepending the repo
root here is exactly the shadowing vector that file's first seam guards.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

from devloop.cli import main

POLICY_ENV = "DEVLOOP_POLICY_DIR"


def _policy_dir(cfg) -> None:
    if POLICY_ENV not in os.environ and cfg.rsi_rail:
        os.environ[POLICY_ENV] = str(Path(cfg.rsi_rail) / "devloop")


def _with_manifest(cfg, argv: list[str], scratch: Path) -> list[str]:
    """``argv`` plus ``--manifest-json`` for a ``trajectory`` run that has none."""
    if not argv or argv[0] != "trajectory" or {"-h", "--help"} & set(argv):
        return argv
    if any(a == "--manifest-json" or a.startswith("--manifest-json=") for a in argv):
        return argv
    from thinkweave.core import manifest
    from thinkweave.core.harness import active

    path = scratch / "manifest.json"
    path.write_text(json.dumps(manifest.collect(cfg, active().id)), encoding="utf-8")
    return [*argv, "--manifest-json", str(path)]


def run(argv: list[str]) -> int:
    from thinkweave.core.config import load_config

    with tempfile.TemporaryDirectory(prefix="issue-loop-") as scratch:
        # Only the host seams are guarded: devloop's own errors stay its own.
        try:
            cfg = load_config()
            _policy_dir(cfg)
            argv = _with_manifest(cfg, argv, Path(scratch))
        except (OSError, ValueError, RuntimeError) as exc:
            print(json.dumps({"error": f"thinkweave host seam: {exc}"}))
            return 2
        return main(argv)


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
