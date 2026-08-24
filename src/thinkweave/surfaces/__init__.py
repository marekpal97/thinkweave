"""Surfaces layer — the three invocation surfaces over operations.

- ``cli``   — the ``weave`` command (argparse scaffold + dispatch table)
- ``hooks`` — Claude Code hook handler + installer (``weave-hook``)
- ``mcp``   — the ``weave_*`` MCP tool server (``weave-mcp``)

Operations: CLI entry (``main``/``build_parser``), hook installation
(``install_hooks``/``uninstall_hooks``).

Invariants: surface handlers are thin wrappers over ``operations`` — no
knowledge-layer logic lives here. Nothing below this layer may import it.
Door names resolve lazily (PEP 562): the ``weave-hook`` console script
imports this package on every hook fire, and an eager CLI import would
pull 25+ command modules and the whole knowledge layer into that path
(``hooks/handler.py`` keeps its own imports lazy for the same reason).

Storage: none — surfaces translate I/O shapes only.

Extension points: new CLI subcommands register in ``cli._DISPATCH``; new
MCP tools under ``mcp/tools``; both sides of an operation stay in parity
(tests/test_surface_contract.py).

``mcp`` is deliberately NOT re-exported here: its server is one console
script (``weave-mcp``) with no other importers, so putting it on the
door would only add import weight (its third-party imports are already
deferred into function bodies). Import ``thinkweave.surfaces.mcp.server``
directly.
"""

_DOOR = {
    "build_parser": "thinkweave.surfaces.cli",
    "main": "thinkweave.surfaces.cli",
    "install_hooks": "thinkweave.surfaces.hooks.install",
    "uninstall_hooks": "thinkweave.surfaces.hooks.install",
}

__all__ = sorted(_DOOR)  # noqa: F822 — names resolve via __getattr__


def __getattr__(name: str):
    try:
        module = _DOOR[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    import importlib

    obj = getattr(importlib.import_module(module), name)
    globals()[name] = obj  # cache: later accesses skip __getattr__
    return obj
