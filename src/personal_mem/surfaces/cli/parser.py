"""Argparse scaffold for the ``mem`` CLI.

Building the parser is mechanical and noisy; concentrating it here keeps
the dispatch (``__init__.py``) and per-command handlers tidy. Each
``cmd_*`` handler under ``surfaces/cli/`` reads ``args`` produced here
and delegates into ``operations/`` (or, for legacy commands, into the
knowledge-layer modules directly).
"""

from __future__ import annotations

import argparse

from personal_mem.surfaces.cli._parser_basics import (
    add_admin_subparsers,
    add_index_subparsers,
    add_note_subparsers,
)
from personal_mem.surfaces.cli._parser_concepts_hubs import (
    add_concepts_subparsers,
    add_drain_subparsers,
    add_hubs_subparsers,
    add_themes_subparsers,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mem",
        description="Obsidian-native universal memory layer",
    )
    sub = parser.add_subparsers(dest="command")

    add_note_subparsers(sub)
    add_index_subparsers(sub)
    add_admin_subparsers(sub)
    add_concepts_subparsers(sub)
    add_hubs_subparsers(sub)
    add_drain_subparsers(sub)
    add_themes_subparsers(sub)

    return parser
