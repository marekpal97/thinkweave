"""Surfaces layer — the three invocation surfaces over operations.

- ``cli``   — the ``weave`` command (argparse scaffold + dispatch table)
- ``hooks`` — Claude Code hook handler + installer (``weave-hook``)
- ``mcp``   — the ``weave_*`` MCP tool server (``weave-mcp``)

Operations: CLI entry (``main``/``build_parser``), hook installation
(``install_hooks``/``uninstall_hooks``).

Invariants: surface handlers are thin wrappers over ``operations`` — no
knowledge-layer logic lives here. Nothing below this layer may import it.

Storage: none — surfaces translate I/O shapes only.

Extension points: new CLI subcommands register in ``cli._DISPATCH``; new
MCP tools under ``mcp/tools``; both sides of an operation stay in parity
(tests/test_surface_contract.py).

``mcp`` is deliberately NOT re-exported here: its server needs the
optional ``mcp`` extra, and the door must stay importable in base
installs. Import ``thinkweave.surfaces.mcp.server`` directly.
"""

from thinkweave.surfaces.cli import build_parser, main
from thinkweave.surfaces.hooks.install import install_hooks, uninstall_hooks

__all__ = [
    "build_parser",
    "install_hooks",
    "main",
    "uninstall_hooks",
]
