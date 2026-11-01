"""Argparse scaffold for the ``weave`` CLI.

Building the parser is mechanical and noisy; concentrating it here keeps
the dispatch (``__init__.py``) and per-command handlers tidy. Each
``cmd_*`` handler under ``surfaces/cli/`` reads ``args`` produced here
and delegates into ``operations/`` (or, for legacy commands, into the
knowledge-layer modules directly).
"""

from __future__ import annotations

import argparse

from thinkweave.surfaces.cli._parser_basics import (
    add_admin_subparsers,
    add_index_subparsers,
    add_note_subparsers,
)
from thinkweave.surfaces.cli._parser_concepts_hubs import (
    add_concepts_subparsers,
    add_drain_subparsers,
    add_hubs_subparsers,
    add_themes_subparsers,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weave",
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
    add_policy_subparsers(sub)

    return parser


def add_policy_subparsers(sub) -> None:
    """``weave policy inventory`` — the knob inventory from the Config field
    metadata (dec-db6baaab). Logic in surfaces/cli/policy.py."""
    p_policy = sub.add_parser(
        "policy",
        help=(
            "The knob inventory derived from the Config field metadata: render "
            "the rsi-rail template or check a file against it (dec-db6baaab)."
        ),
    )
    policy_sub = p_policy.add_subparsers(dest="policy_action")
    p_inv = policy_sub.add_parser(
        "inventory",
        help="Render every knob address with its default and the events that read it.",
    )
    mode = p_inv.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--toml",
        action="store_true",
        help="Print the rsi-rail thinkweave/config.toml template (rail-eligible knobs, commented).",
    )
    mode.add_argument(
        "--json",
        action="store_true",
        help="Print every knob (address, read_at, rail, default) as one JSON list.",
    )
    mode.add_argument(
        "--check",
        metavar="PATH",
        help="Exit 0 when PATH equals the --toml rendering apart from its live lines, else 1.",
    )
