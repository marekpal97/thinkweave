"""``weave themes`` — theme registry maintenance CLI.

Subcommands:
    rebuild-registry    Rebuild themes.yaml from canonical theme files.

The candidate-stub lifecycle (scan / archive / promote) was removed in
the 2026-05-30 teardown — theme detection now surfaces enriched cluster
signals to ``/dream``, which mints or extends themes directly. See
``synthesis/theme_candidates.py``.
"""

from __future__ import annotations

import argparse

from thinkweave.core.config import load_config


def cmd_themes(args: argparse.Namespace) -> None:
    action = getattr(args, "themes_action", "") or ""

    if action == "rebuild-registry":
        from thinkweave.synthesis.theme_registry import rebuild

        cfg = load_config()
        n = rebuild(cfg)
        print(f"Rebuilt themes.yaml with {n} canonical theme(s).")
        return

    print("Usage: weave themes {rebuild-registry}")
