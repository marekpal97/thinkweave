"""CLI entry point for thinkweave.

Usage: weave <command> [options]

The argparse scaffold lives in ``parser.py``; per-command handlers live in
sibling modules (``notes.py``, ``concepts.py``, ``hubs.py``, ...). This
file owns only the entry point + dispatch table. Helpers that the test
suite reaches for are re-exported below for back-compat.
"""

from __future__ import annotations

import sys

from thinkweave.surfaces.cli.concepts import cmd_concepts
from thinkweave.surfaces.cli.drain import cmd_discover, cmd_drain
from thinkweave.surfaces.cli.dream import cmd_dream
from thinkweave.surfaces.cli.flows import cmd_flow
from thinkweave.surfaces.cli.graph import cmd_graph
from thinkweave.surfaces.cli.hooks import cmd_hooks
from thinkweave.surfaces.cli.hubs import (
    _build_linkage_user_prompt,
    _parse_linkage_response,
    _validate_linkage_revision,
    cmd_hubs,
)
from thinkweave.surfaces.cli.index import (
    cmd_doctor,
    cmd_import,
    cmd_index,
    cmd_stats,
)
from thinkweave.surfaces.cli.install import (
    cmd_dev_link,
    cmd_dev_unlink,
    cmd_install,
    cmd_uninstall,
)
from thinkweave.surfaces.cli.pause import cmd_pause, cmd_resume
from thinkweave.surfaces.cli.intake import cmd_intake
from thinkweave.surfaces.cli.judge import cmd_judge
from thinkweave.surfaces.cli.landing import cmd_landing
from thinkweave.surfaces.cli.notes import (
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
from thinkweave.surfaces.cli.parity import (
    cmd_project_snapshot,
    cmd_prompts,
    cmd_timeline,
    cmd_unlink,
)
from thinkweave.surfaces.cli.parser import build_parser
from thinkweave.surfaces.cli.queue import cmd_queue
from thinkweave.surfaces.cli.rlvr import cmd_rlvr
from thinkweave.surfaces.cli.schedule import cmd_schedule
from thinkweave.surfaces.cli.seam import cmd_seam
from thinkweave.surfaces.cli.skill import cmd_skill
from thinkweave.surfaces.cli.themes import cmd_themes
from thinkweave.surfaces.cli.util import cmd_init, cmd_mcp, cmd_prune_orphans, cmd_sources
from thinkweave.surfaces.cli.wrap import cmd_wrap_finalize


# Grouped by audience (surface contract — see ARCHITECTURE.md "Invocation
# surface" and tests/test_surface_contract.py). Grouping is documentation
# only; dispatch is by key, order is irrelevant.
_DISPATCH = {
    # ── Agent-Bash entries ────────────────────────────────────────────
    # The four narrow subcommands in-session agents / dream workers call
    # from a Bash tool mid-flow (everything else agents reach via MCP):
    # `weave wrap-finalize`, `weave hubs apply-linkage`, `weave landing --doc`,
    # `weave judge --rejudge/--drain`. hubs / landing / judge double as
    # admin surfaces for their other flags.
    "wrap-finalize": cmd_wrap_finalize,
    "hubs": cmd_hubs,
    "landing": cmd_landing,
    "judge": cmd_judge,
    # ── Admin & setup ─────────────────────────────────────────────────
    # Interactive machine / vault administration. No MCP parity by
    # design; agents shouldn't run these (CLAUDE.md §7).
    "init": cmd_init,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "dev-link": cmd_dev_link,
    "dev-unlink": cmd_dev_unlink,
    "hooks": cmd_hooks,
    "schedule": cmd_schedule,
    "mcp": cmd_mcp,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "doctor": cmd_doctor,
    "stats": cmd_stats,
    "skill": cmd_skill,
    "sources": cmd_sources,
    "project": cmd_project,
    # ── Cron & orchestration ──────────────────────────────────────────
    # Invoked by cron flows and headless skill orchestrators (/dream,
    # /drain, /tighten, …) — pipeline verbs plus the write
    # surface headless flows use (`weave add/update/link/unlink`; live
    # agents use the weave_create/update/link/unlink MCP tools instead).
    "index": cmd_index,
    "import": cmd_import,
    "dream": cmd_dream,
    "discover": cmd_discover,
    "drain": cmd_drain,
    "intake": cmd_intake,
    "seam": cmd_seam,
    "queue": cmd_queue,
    "flow": cmd_flow,
    "concepts": cmd_concepts,
    "themes": cmd_themes,
    "prune-orphans": cmd_prune_orphans,
    "rlvr": cmd_rlvr,
    "add": cmd_add,
    "update": cmd_update,
    "link": cmd_link,
    "unlink": cmd_unlink,
    # ── Retrieval-debug ───────────────────────────────────────────────
    # Shell-side mirrors of the MCP read surface — for humans debugging
    # retrieval; agents use the corresponding weave_* tools.
    "search": cmd_search,
    "context": cmd_context,
    "graph": cmd_graph,
    "show": cmd_show,
    "backlog": cmd_backlog,
    "decisions": cmd_decisions,
    "timeline": cmd_timeline,
    "project-snapshot": cmd_project_snapshot,
    "prompts": cmd_prompts,
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
