"""CLI entry point for personal_mem.

Usage: mem <command> [options]

The argparse scaffold lives in ``parser.py``; per-command handlers live in
sibling modules (``notes.py``, ``concepts.py``, ``hubs.py``, ...). This
file owns only the entry point + dispatch table. Helpers that the test
suite reaches for are re-exported below for back-compat.
"""

from __future__ import annotations

import sys

from personal_mem.surfaces.cli.concepts import cmd_concepts
from personal_mem.surfaces.cli.drain import cmd_discover, cmd_drain
from personal_mem.surfaces.cli.dream import cmd_dream
from personal_mem.surfaces.cli.flows import cmd_flow
from personal_mem.surfaces.cli.graph import cmd_graph
from personal_mem.surfaces.cli.hooks import cmd_hooks
from personal_mem.surfaces.cli.hubs import (
    _build_linkage_user_prompt,
    _parse_linkage_response,
    _validate_linkage_revision,
    cmd_hubs,
)
from personal_mem.surfaces.cli.index import (
    cmd_doctor,
    cmd_enrich,
    cmd_import,
    cmd_index,
    cmd_stats,
)
from personal_mem.surfaces.cli.install import cmd_install
from personal_mem.surfaces.cli.intake import cmd_intake
from personal_mem.surfaces.cli.landing import cmd_landing
from personal_mem.surfaces.cli.news import cmd_news_stats
from personal_mem.surfaces.cli.notes import (
    cmd_add,
    cmd_backlog,
    cmd_context,
    cmd_decisions,
    cmd_link,
    cmd_project,
    cmd_search,
    cmd_show,
    cmd_update,
)
from personal_mem.surfaces.cli.parser import build_parser
from personal_mem.surfaces.cli.queue import cmd_queue
from personal_mem.surfaces.cli.rlvr import cmd_rlvr
from personal_mem.surfaces.cli.skill import cmd_skill
from personal_mem.surfaces.cli.themes import cmd_themes
from personal_mem.surfaces.cli.util import cmd_init, cmd_mcp, cmd_prune_orphans, cmd_sources
from personal_mem.surfaces.cli.wrap import cmd_wrap_finalize


_DISPATCH = {
    "add": cmd_add,
    "backlog": cmd_backlog,
    "concepts": cmd_concepts,
    "decisions": cmd_decisions,
    "hubs": cmd_hubs,
    "landing": cmd_landing,
    "project": cmd_project,
    "prune-orphans": cmd_prune_orphans,
    "enrich": cmd_enrich,
    "search": cmd_search,
    "show": cmd_show,
    "link": cmd_link,
    "graph": cmd_graph,
    "index": cmd_index,
    "import": cmd_import,
    "context": cmd_context,
    "stats": cmd_stats,
    "doctor": cmd_doctor,
    "flow": cmd_flow,
    "hooks": cmd_hooks,
    "init": cmd_init,
    "install": cmd_install,
    "mcp": cmd_mcp,
    "intake": cmd_intake,
    "sources": cmd_sources,
    "skill": cmd_skill,
    "queue": cmd_queue,
    "drain": cmd_drain,
    "discover": cmd_discover,
    "dream": cmd_dream,
    "news-stats": cmd_news_stats,
    "themes": cmd_themes,
    "update": cmd_update,
    "wrap-finalize": cmd_wrap_finalize,
    "rlvr": cmd_rlvr,
}


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    _DISPATCH[args.command](args)


__all__ = [
    "main",
    "build_parser",
    "_DISPATCH",
    # Back-compat re-exports — the test suite reaches for these helpers under
    # the legacy underscore names.
    "_build_linkage_user_prompt",
    "_parse_linkage_response",
    "_validate_linkage_revision",
]
