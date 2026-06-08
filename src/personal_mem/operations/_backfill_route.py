"""Centralised route selector for backfill subcommands.

Three backfill paths take ``--via {inline,batch}``:
``mem import chatgpt``, ``mem enrich``, ``mem hubs link``, plus the
established ``mem import claude-code --enrich``. Each callsite used to
re-derive the same "explicit flag > size threshold > key presence"
decision inline. Centralising here keeps the rule one place.

Rule (in order):

  1. Explicit ``--via <inline|batch>`` from the CLI wins.
  2. Otherwise, ``batch`` if the work-list is larger than
     ``default_threshold_n`` AND the configured provider's API key is
     available — large workloads benefit from the wrapper's async
     fan-out, but only if there's a key to use it with.
  3. Otherwise ``inline`` — the CC skill loops items through the
     running model, no provider key required.

Inline path semantics (post-2026-06-06): the CC subagent / skill does
the work via the running session's model. Batch path: the wrapper
(``core/agent_client.batch_completions_sync``) fires N async
completions in parallel against the configured provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from personal_mem.core.api_keys import get_provider_key


Route = Literal["inline", "batch"]


@dataclass(frozen=True)
class RouteDecision:
    """Result of :func:`choose_route`.

    ``route`` is the picked path. ``reason`` is a short string the
    caller can surface to the user explaining *why* (especially useful
    when the user asked for ``batch`` and the function downgraded to
    ``inline`` because no key was found).
    """
    route: Route
    reason: str


def choose_route(
    *,
    via: str | None,
    n_items: int,
    default_threshold_n: int = 200,
    provider: str = "openai",
) -> RouteDecision:
    """Pick the route for a backfill subcommand.

    Args:
        via: The user's explicit ``--via`` value, or ``None`` when the
            flag was omitted.
        n_items: The work-list size (after dedup / filtering).
        default_threshold_n: Above this size, prefer batch when a key
            is available. Default 200 (mirrors the
            ``mem import claude-code`` precedent).
        provider: Which provider's key to check for the batch
            availability heuristic. Defaults to ``openai``.

    Returns:
        :class:`RouteDecision` with the picked route and a short
        rationale string.
    """
    via_norm = (via or "").strip().lower()
    has_key = bool(get_provider_key(provider))

    # Rule 1: explicit flag wins, but downgrade batch→inline when no key.
    if via_norm == "batch":
        if has_key:
            return RouteDecision("batch", "user requested --via batch")
        return RouteDecision(
            "inline",
            f"user requested --via batch but no {provider.upper()}_API_KEY found; "
            f"falling back to inline",
        )
    if via_norm == "inline":
        return RouteDecision("inline", "user requested --via inline")

    # Rule 2: size threshold + key.
    if n_items > default_threshold_n and has_key:
        return RouteDecision(
            "batch",
            f"{n_items} items > threshold {default_threshold_n} and "
            f"{provider} key present → batch",
        )

    # Rule 3: fallback.
    if n_items > default_threshold_n and not has_key:
        return RouteDecision(
            "inline",
            f"{n_items} items > threshold {default_threshold_n} but no "
            f"{provider.upper()}_API_KEY → inline",
        )
    return RouteDecision(
        "inline",
        f"{n_items} items ≤ threshold {default_threshold_n} → inline",
    )
