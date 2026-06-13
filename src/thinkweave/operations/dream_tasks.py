"""Declarative registry of ``/dream``'s subagent tasks — the extensibility seam.

``/dream`` was a single LLM turn that handled five judgment domains serially.
The 2026-06-06 refactor splits it into a two-phase subagent orchestrator:
phase-1 synthesis workers feed an apply step; phase-2 composition/consumption
workers run after the apply on the freshly-mutated state. The registry below
is the seam any new judgment, composition, or consumption domain plugs into.

This module is the dream-side analog of
:mod:`thinkweave.acquisition.sources.registry` — a frozen :class:`DreamTaskSpec`
dataclass per task, a tuple :data:`REGISTRY` declaring all of them, and a
single :func:`enabled_tasks` selector the CLI / orchestrator skill consume.
Anyone familiar with how ``SourceTypeSpec`` extends ``/drain`` already knows
how to add a dream task.

The rationale for keeping this Python (vs YAML) is identical to
``SourceTypeSpec``'s: task surfaces are tied to :class:`DreamCycleScan` /
:class:`DreamCycleResult` fields (a Python contract), ``has_signal`` is a
predicate function, and adding a task is a code change, not a vault-config
change.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class DreamTaskSpec:
    """Declarative spec for one dream-cycle subagent task.

    Attributes:
        surface_key: field name on :class:`DreamCycleScan` (phase 1) or the
            combined ``DreamCycleScan`` + ``DreamCycleResult`` view (phase 2)
            that this worker reads as its primary input.
        worker_name: filename (without ``.md``) under ``agents/``.
            The orchestrator constructs the Task subagent prompt around this
            name.
        plan_keys: keys this worker's ``plan_fragment`` writes. Empty for
            phase-2 workers (they mutate the vault directly via MCP/Bash
            instead of going through ``weave dream apply``).
        has_signal: predicate over a scan-like object — returns ``True`` if
            this worker would have substantive work to do. The orchestrator
            uses this to skip cold-cycle spawns; the CLI selector uses it to
            keep the JSON output honest.
        phase: ``1`` (synthesis, fed into apply) or ``2`` (composition /
            consumption, runs after apply on the freshly-mutated state).
        depends_on: worker names that must complete before this one is
            spawned (intra-phase). Used by the phase-2 orchestrator to fan
            out in waves — Wave A is the set with no deps, Wave B unblocks
            once Wave A finishes.
        enabled: per-spec gate. Flipping to ``False`` disables the task
            without removing it from the registry — useful for shipping a
            worker before its consumer is ready.
    """

    surface_key: str
    worker_name: str
    plan_keys: tuple[str, ...] = ()
    has_signal: Callable[[Any], bool] = field(default=lambda _: True)
    phase: Literal[1, 2] = 1
    depends_on: tuple[str, ...] = ()
    enabled: bool = True


# Predicates for phase-2 surfaces use ``getattr`` defensively so the
# registry stays loadable while the phase-2 fields land on ``DreamCycleScan``
# in a concurrent change. Once those fields are committed, the lambdas keep
# working unchanged.


def _has_unwrapped_sessions(scan: Any) -> bool:
    return bool(getattr(scan, "unwrapped_sessions", None))


def _has_rejudge_queue(scan: Any) -> bool:
    return bool(getattr(scan, "rejudge_queue", None))


def _has_seam_link_queue(scan: Any) -> bool:
    return bool(getattr(scan, "seam_link_queue", None))


def _has_memory_seam(scan: Any) -> bool:
    """Fire the seam worker when any CC fact is dirty or a fact was removed.

    ``dirty`` = new / edited / unresolved / recheck-due facts needing
    (re)judgment; ``removed`` = facts whose CC file is gone (the report
    must drop them). A fully-clean cycle (the steady state) spawns nothing.
    """
    surface = getattr(scan, "memory_seam", None) or {}
    return bool(surface.get("dirty") or surface.get("removed"))


def _has_knowledge_delta(scan: Any) -> bool:
    """Only fire the digest worker when SOMETHING substantive landed in 24h.

    Post-2026-06-07 grain split: ``knowledge_delta`` carries two slices
    (``concept`` and ``event``), each with the same set of substantive
    buckets. The digest worker writes one digest per non-empty slice, so
    we fire as long as ANY substantive bucket on EITHER slice has content.
    """
    delta = getattr(scan, "knowledge_delta", None) or {}
    buckets = (
        "landings_24h",
        "catalyst_additions_24h",
        "verdict_flips_24h",
        "predictions_landed_24h",
    )
    for slice_key in ("concept", "event"):
        slice_data = delta.get(slice_key) or {}
        if any(slice_data.get(b) for b in buckets):
            return True
    return False


REGISTRY: tuple[DreamTaskSpec, ...] = (
    # ----- Phase 1 — synthesis workers (emit plan fragments → apply) -----
    DreamTaskSpec(
        surface_key="promotion_candidates",
        worker_name="dream-promotion-worker",
        plan_keys=("promotions",),
        has_signal=lambda s: bool(getattr(s, "promotion_candidates", None)),
        phase=1,
    ),
    DreamTaskSpec(
        # Reads four surfaces — concept drift pairs, theme dup pairs, and
        # the N-ary grain-coarsening clusters for both families — and rules
        # merge / collapse / distinct on each. Spawns when ANY is non-empty.
        surface_key="drift_pairs",
        worker_name="dream-merge-worker",
        plan_keys=(
            "merges",
            "theme_merges",
            "distinct_pairs",
            "coarsenings",
            "theme_coarsenings",
            "distinct_clusters",
        ),
        has_signal=lambda s: bool(
            getattr(s, "drift_pairs", None)
            or getattr(s, "theme_dup_candidates", None)
            or getattr(s, "coarsen_clusters", None)
            or getattr(s, "theme_coarsen_clusters", None)
        ),
        phase=1,
    ),
    DreamTaskSpec(
        # The theme worker reads two scan surfaces: cluster signals
        # (mint-vs-extend judgment) and theme_log_gaps (directly-filed
        # sources needing catalyst distillation — extensions only, no
        # judgment about which theme). surface_key names the primary.
        surface_key="theme_cluster_signals",
        worker_name="dream-theme-worker",
        plan_keys=("theme_mints", "theme_extensions"),
        has_signal=lambda s: bool(
            getattr(s, "theme_cluster_signals", None)
            or getattr(s, "theme_log_gaps", None)
        ),
        phase=1,
    ),
    DreamTaskSpec(
        surface_key="essence_candidates",
        worker_name="dream-essence-worker",
        plan_keys=("essence_rewrites",),
        has_signal=lambda s: bool(getattr(s, "essence_candidates", None)),
        phase=1,
    ),
    DreamTaskSpec(
        surface_key="recent_probes",
        worker_name="dream-priority-worker",
        plan_keys=("priority_signals",),
        has_signal=lambda s: bool(getattr(s, "recent_probes", None)),
        phase=1,
    ),
    # ----- Phase 2 — composition + consumption workers (write directly) ----
    DreamTaskSpec(
        surface_key="unwrapped_sessions",
        worker_name="dream-wrap-worker",
        has_signal=_has_unwrapped_sessions,
        phase=2,
    ),
    DreamTaskSpec(
        surface_key="rejudge_queue",
        worker_name="dream-judge-worker",
        has_signal=_has_rejudge_queue,
        phase=2,
    ),
    DreamTaskSpec(
        # Hubs whose logs absorbed another hub's entries this (or an
        # earlier) cycle — phase 1's apply enqueues on every merge, so the
        # same cycle's phase 2 stitches the seam. Cross-parent entry pairs
        # only; writes via `weave hubs apply-linkage` (Bash).
        surface_key="seam_link_queue",
        worker_name="dream-seam-link-worker",
        has_signal=_has_seam_link_queue,
        phase=2,
    ),
    DreamTaskSpec(
        # CC auto-memory ↔ vault reconciliation. Reads the cheap dirty-diff
        # surface, resolves each dirty fact's vault twin via
        # weave_search(mode='similar'), judges confirmed-fresh / stale /
        # diverged / durable-unique, and writes the durable map +
        # report through `weave seam commit`. No deps — Wave A.
        surface_key="memory_seam",
        worker_name="dream-seam-worker",
        has_signal=_has_memory_seam,
        phase=2,
    ),
    DreamTaskSpec(
        surface_key="knowledge_delta",
        worker_name="dream-digest-worker",
        has_signal=_has_knowledge_delta,
        phase=2,
        depends_on=("dream-judge-worker",),
    ),
)


def enabled_tasks(scan: Any, *, phase: int) -> list[dict]:
    """Return serializable entries for tasks the orchestrator should spawn.

    A task is included iff ``spec.enabled`` is True, ``spec.phase`` matches
    the requested ``phase``, and ``spec.has_signal(scan)`` returns True
    against the provided scan-like object.

    The returned list mirrors what the CLI emits as JSON for the dream
    orchestrator skill — one dict per task, carrying only the fields the
    skill needs to build subagent prompts and respect intra-phase
    dependency edges. Predicates are not serialized (the skill doesn't
    re-evaluate them).
    """
    return [
        {
            "surface_key": t.surface_key,
            "worker_name": t.worker_name,
            "plan_keys": list(t.plan_keys),
            "depends_on": list(t.depends_on),
        }
        for t in REGISTRY
        if t.enabled and t.phase == phase and t.has_signal(scan)
    ]
