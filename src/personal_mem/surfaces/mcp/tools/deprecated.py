"""Deprecation aliases for tools folded into ``mem_concepts(action=…)`` and
``mem_graph(filter=…)`` during Phase 4 C consolidation.

Kept isolated so they can be deleted in one move once the deprecation
window closes (one release).
"""

from __future__ import annotations

import sys

# old_name → (new_name, default args to pre-fill)
DEPRECATION_ALIASES: dict[str, tuple[str, dict]] = {
    "mem_concepts_tighten": ("mem_concepts", {"action": "tighten"}),
    "mem_concepts_merge": ("mem_concepts", {"action": "merge"}),
    "mem_concept_search": ("mem_concepts", {"action": "search"}),
    "mem_concept_source_counts": ("mem_concepts", {"action": "source_counts"}),
    "mem_concepts_drift": ("mem_concepts", {"action": "drift"}),
    "mem_source_lens": ("mem_graph", {"filter": "source_lens"}),
    "mem_decisions_for_file": ("mem_graph", {"filter": "decisions_for_file"}),
}


def fold(name: str, arguments: dict) -> tuple[str, dict]:
    """If ``name`` is a deprecated alias, fold to ``(new_name, merged_args)``.

    Otherwise returns the inputs unchanged. Prints a deprecation warning
    to stderr on the first fold of each name.
    """
    if name not in DEPRECATION_ALIASES:
        return name, arguments

    new_name, defaults = DEPRECATION_ALIASES[name]
    key, val = next(iter(defaults.items()))
    print(
        f"deprecated: {name} → {new_name}({key}={val!r}); alias kept "
        f"for one release.",
        file=sys.stderr,
    )
    return new_name, {**defaults, **arguments}
