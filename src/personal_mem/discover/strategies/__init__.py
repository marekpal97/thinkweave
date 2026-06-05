"""Strategy registry — built-ins auto-register on import.

The registry is a plain dict keyed by strategy name. Each value is a
strategy instance (any object with a ``name`` attribute and a ``run``
method). Adding a new strategy:

    # strategies/my_strategy.py
    class MyStrategy:
        name = "my_strategy"
        def run(self, vault, project, config): ...

    STRATEGY = MyStrategy()

    # strategies/__init__.py
    from . import my_strategy
    register(my_strategy.STRATEGY)

That's it — no surface change, no plumbing edits.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Strategy(Protocol):
    """Discovery-strategy contract.

    Two flavors implement this in practice: internal-state producers
    (``decision_review``, ``prompt_gap``) emit *gap descriptors* the
    ``/discover`` skill turns into search queries; external-trigger
    producers (``rss_poll``, ``mail_poll``, ``external_tool_runner``)
    emit *queue items* (or, for ``mail_poll``, a plan a skill executes
    against MCP). Both flavors share this ``run`` signature; the return
    shape is a plain list of dicts.
    """

    name: str

    def run(
        self,
        vault: Any,
        project: str | None,
        config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...


REGISTRY: dict[str, Strategy] = {}


def register(strategy: Strategy) -> None:
    """Register a strategy by its ``name`` attribute.

    Re-registering the same name overwrites the previous instance —
    intentional, so tests can swap a stub in without restarting.
    """
    name = getattr(strategy, "name", None)
    if not name:
        raise ValueError("Strategy must have a non-empty `name` attribute.")
    REGISTRY[name] = strategy


def get(name: str) -> Strategy:
    """Look up a strategy by name. Raises ``KeyError`` if missing."""
    if name not in REGISTRY:
        raise KeyError(
            f"Unknown discovery strategy: {name!r}. "
            f"Registered: {sorted(REGISTRY)}"
        )
    return REGISTRY[name]


def names() -> list[str]:
    """Return all registered strategy names in insertion order."""
    return list(REGISTRY)


# Auto-register built-ins. Each module exposes a module-level
# ``STRATEGY`` singleton. Built-ins split into two flavors:
#
#   internal-state producers (observe vault, emit gap descriptors):
#     decision_review, prompt_gap
#
#   external-trigger producers (observe outside world, emit queue items):
#     rss_poll, mail_poll, external_tool_runner
#
# Both flavors implement the same ``run(vault, project, config)`` contract.
from personal_mem.discover.strategies import (  # noqa: E402
    decision_review,
    external_tool_runner,
    mail_poll,
    prompt_gap,
    rss_poll,
)

register(decision_review.STRATEGY)
register(prompt_gap.STRATEGY)
register(external_tool_runner.STRATEGY)
register(rss_poll.STRATEGY)
register(mail_poll.STRATEGY)
