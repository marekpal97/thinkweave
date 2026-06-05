"""Discovery strategies — pluggable gap-analysis layer.

A *discovery strategy* walks the vault looking for missing or stalled
work and emits a list of queue-item dicts. The CLI ``mem discover`` is a
thin shell over this registry: it loads the strategies named in
``sources.yaml: projects.<project>.discover_strategies`` (or the explicit
``--strategy`` flag) and calls each one's ``run`` method.

Built-in strategies:

- ``decision_review`` — surface stalled ``proposed``/``accepted``
  decisions that haven't seen activity in N days.
- ``prompt_gap`` — surface hyphenated-compound terms the user has
  probed about that aren't in the ontology (canonical or proposed).
- ``rss_poll`` / ``mail_poll`` — external-trigger producers that
  enqueue queue items (or emit a fetch plan) from RSS feeds and Gmail.
- ``external_tool_runner`` — shell out to user-provided scripts and
  parse their JSONL stdout into queue items.

Adding a new strategy = drop a file under ``strategies/`` exposing a
module-level ``STRATEGY`` instance and add one ``register()`` line in
``strategies/__init__.py``. Nothing else in the framework needs to know.
"""

from __future__ import annotations

from typing import Any, Protocol

from personal_mem.discover.strategies import REGISTRY, get, names, register


class DiscoveryStrategy(Protocol):
    """Protocol every strategy implements.

    ``name`` is the lookup key used in ``sources.yaml`` and on the CLI
    ``--strategy`` flag. ``run`` returns a list of queue-item dicts —
    the caller (``mem discover``) is responsible for actually enqueuing
    them. Strategies don't write to the vault directly.
    """

    name: str

    def run(
        self,
        vault: Any,
        project: str | None,
        config: dict[str, Any],
    ) -> list[dict[str, Any]]:
        ...


__all__ = [
    "DiscoveryStrategy",
    "REGISTRY",
    "get",
    "names",
    "register",
]
