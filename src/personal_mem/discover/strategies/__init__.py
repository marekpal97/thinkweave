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

from typing import Any

REGISTRY: dict[str, Any] = {}


def register(strategy: Any) -> None:
    """Register a strategy by its ``name`` attribute.

    Re-registering the same name overwrites the previous instance —
    intentional, so tests can swap a stub in without restarting.
    """
    name = getattr(strategy, "name", None)
    if not name:
        raise ValueError("Strategy must have a non-empty `name` attribute.")
    REGISTRY[name] = strategy


def get(name: str) -> Any:
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
# ``STRATEGY`` singleton.
from personal_mem.discover.strategies import (  # noqa: E402
    concept_coverage,
    decision_review,
    external_tool_runner,
    theme_drift,
)

register(concept_coverage.STRATEGY)
register(decision_review.STRATEGY)
register(theme_drift.STRATEGY)
register(external_tool_runner.STRATEGY)
