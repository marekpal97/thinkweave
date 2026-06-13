"""End-of-session wrap finalization — the deterministic tail of ``/mem-wrap``.

``/mem-wrap`` has two phases. The first is an LLM phase: distil a session
digest from the conversation, then write the session's insights and decisions
via ``mem_extract``. The second is *this* — purely deterministic plumbing:
prune stub session folders, (re)index, judge the freshly written decisions
against git evidence, and refresh the DECISIONS / BACKLOG landing docs, plus a
read-only concept-drift advisory.

Bundling that chain into one in-process call is the whole point: it used to be
~5 separate MCP round-trips, each costing a full model turn on whatever model
the wrap session was running. Here it's one ``mem wrap-finalize`` Bash call
with **zero** model turns.

Pure orchestration over existing operations — returns a structured result; the
CLI surface (``surfaces/cli/wrap.py``) formats the human-readable report.
Imports ``core/`` / ``operations/`` / ``synthesis/`` only — never ``surfaces/``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from personal_mem.core.config import Config


@dataclass
class WrapFinalizeResult:
    """Structured outcome of :func:`finalize_wrap`."""

    session_id: str
    project: str = ""
    orphans_pruned: int = 0
    orphans_freed_bytes: int = 0
    indexed: int = 0
    removed: int = 0
    edges: int = 0
    decisions_judged: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)  # verdict -> count
    landing_written: list[str] = field(default_factory=list)
    drift_text: str = ""
    errors: list[str] = field(default_factory=list)
    # Per-step wall time (seconds) — keys: prune, index, judge, landing, drift.
    # Populated even when a step errors, so a slow failure is visible.
    timings: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "project": self.project,
            "orphans_pruned": self.orphans_pruned,
            "orphans_freed_bytes": self.orphans_freed_bytes,
            "indexed": self.indexed,
            "removed": self.removed,
            "edges": self.edges,
            "decisions_judged": self.decisions_judged,
            "verdicts": self.verdicts,
            "landing_written": self.landing_written,
            "drift_text": self.drift_text,
            "errors": self.errors,
            "timings": self.timings,
        }


def finalize_wrap(
    cfg: Config,
    *,
    session_id: str,
    project: str = "",
    prune: bool = True,
) -> WrapFinalizeResult:
    """Run the deterministic post-extraction chain in one process.

    Order:

    1. **prune** orphan session folders (conservative GC; ``session_id`` is
       protected). Done first so the reindex in step 2 also drops their rows.
    2. **index** — incremental rebuild. Picks up the notes ``mem_extract`` just
       wrote and removes any pruned folders' rows in the same pass.
    3. **judge** — ``judge_and_writeback(session_id=...)``: verdict + status
       onto each new decision, batched, re-indexing touched files.
    4. **landing** — regenerate DECISIONS.md + BACKLOG.md (cheap; always done).
       STATE.md is *not* touched — refreshing it is an LLM judgment call the
       wrap skill makes, not this deterministic tail.
    5. **drift** — read-only concept-drift advisory; surfaced in the result,
       never acted on here.

    Every step is wrapped: a failure in one is recorded in ``errors`` and the
    rest still run. Returns a :class:`WrapFinalizeResult`.
    """
    result = WrapFinalizeResult(session_id=session_id, project=project)

    # 1. prune orphan session folders -------------------------------------
    if prune:
        _t = time.perf_counter()
        try:
            from personal_mem.operations.prune import find_orphans, prune_orphans

            orphans = find_orphans(
                cfg, project=project, current_session_id=session_id
            )
            if orphans:
                pr = prune_orphans(orphans, dry_run=False)
                result.orphans_pruned = pr.deleted
                result.orphans_freed_bytes = pr.freed_bytes
        except Exception as e:  # noqa: BLE001 — best-effort GC
            result.errors.append(f"prune: {e}")
        finally:
            result.timings["prune"] = time.perf_counter() - _t

    # 2. reindex ----------------------------------------------------------
    _t = time.perf_counter()
    try:
        from personal_mem.core.indexer import Indexer
        from personal_mem.core.vault import VaultManager

        VaultManager(config=cfg).ensure_dirs()
        idx = Indexer(config=cfg)
        try:
            stats = idx.rebuild(full=False)
        finally:
            idx.close()
        result.indexed = stats.get("indexed", 0)
        result.removed = stats.get("removed", 0)
        result.edges = stats.get("edges", 0)
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"index: {e}")
    finally:
        result.timings["index"] = time.perf_counter() - _t

    # 3. judge extracted decisions + write back verdict/status ------------
    _t = time.perf_counter()
    try:
        from personal_mem.operations.decisions import (
            judge_and_writeback,
            rejudge_supersession_predecessors,
        )

        judged = judge_and_writeback(cfg, session_id=session_id)
        result.decisions_judged = len(judged)
        for _dec, res in judged:
            verdict = res.get("verdict", "unknown")
            result.verdicts[verdict] = result.verdicts.get(verdict, 0) + 1

        # Evidence-gated supersession flip. mem_extract only *enqueues* a
        # predecessor when a new decision declares ``supersedes: [dec-X]`` —
        # it never flips status. The wrap worker holds this session's commits,
        # so re-judge every such predecessor now: blame survival decides
        # whether the predecessor's lines were actually replaced (→
        # ``superseded``) or still co-contribute (→ ``kept``). Predecessors
        # whose successor isn't committed yet stay put and wait for a later
        # cycle (dream apply drains the headless/deferred backlog).
        pred_ids: list[str] = []
        for _dec, _res in judged:
            sup = _dec.frontmatter.get("supersedes") or []
            if isinstance(sup, str):
                sup = [sup]
            pred_ids.extend(str(s) for s in sup if s)
        if pred_ids:
            pred_judged = rejudge_supersession_predecessors(cfg, pred_ids)
            result.decisions_judged += len(pred_judged)
            for _dec, res in pred_judged:
                verdict = res.get("verdict", "unknown")
                result.verdicts[verdict] = result.verdicts.get(verdict, 0) + 1
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"judge: {e}")
    finally:
        result.timings["judge"] = time.perf_counter() - _t

    # 4. refresh DECISIONS + BACKLOG landing docs -------------------------
    # Two cheap SQL renders, not collapsible: ``write_landing_docs`` has no
    # project-scoped ``all`` value — ``docs="all"`` would wrongly regenerate
    # STATE.md (LLM-owned) and THEMES.md (global). Timings confirm sub-second.
    _t = time.perf_counter()
    if project:
        try:
            from personal_mem.synthesis.landing import write_landing_docs

            for doc in ("decisions", "backlog"):
                written = write_landing_docs(cfg, project, docs=doc)
                result.landing_written.extend(sorted(written.keys()))
        except Exception as e:  # noqa: BLE001
            result.errors.append(f"landing: {e}")
    else:
        result.errors.append("landing: skipped (no project)")
    result.timings["landing"] = time.perf_counter() - _t

    # 5. concept-drift advisory (read-only) -------------------------------
    _t = time.perf_counter()
    try:
        from personal_mem.operations.concepts import drift as concept_drift

        d = concept_drift(cfg, project=project)
        result.drift_text = (d.get("text") or "").strip()
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"drift: {e}")
    finally:
        result.timings["drift"] = time.perf_counter() - _t

    return result
