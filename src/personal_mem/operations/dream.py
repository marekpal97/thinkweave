"""Periodic vault-hygiene cycle — the deterministic backbone of ``/dream``.

``/dream`` is the cron-friendly successor to ``/mem-resolve-concepts`` and
``/themes-resolve``. It runs in three phases:

1. **scan** — read-only sweep. Composes drift candidates, promotion-eligible
   proposed concepts, theme candidate stubs, and dormant/resolved themes
   into a structured action plan. Cheap; ~1s on a 6500-note vault.
2. **LLM judgment** — the ``/dream`` skill applies semantic judgment to the
   scan output (which drift pairs are real, which candidates deserve
   canonicalisation, which themes have stale essences) and emits a plan
   dict. This phase lives in the skill, not here.
3. **apply** — execute the structural changes the LLM decided on with
   **one** index rebuild at the end and append a single line to
   ``vault/.mem/maintenance.jsonl``. This is the speed win — rebuilding the
   index once per mutation would be 20× full rebuilds at the per-cycle cap,
   so apply defers every index touch to a single rebuild at the tail.

The architecture mirrors ``operations/wrap.py`` (the ``mem wrap-finalize``
backbone): structured-result dataclasses, per-step timings, errors recorded
but never cascade. Pure orchestration over existing ``synthesis/`` helpers;
imports ``core/`` / ``operations/`` / ``synthesis/`` only, never
``surfaces/``.

The maintenance.jsonl entry is the operations-log artifact that makes
autonomy trustable — every cron-fired cycle leaves one line saying what it
did, so a human can grep the last 30 days and verify the cycle hasn't gone
sideways.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from personal_mem.core.config import Config


# ---------------------------------------------------------------------------
# Maintenance log
# ---------------------------------------------------------------------------

MAINTENANCE_LOG_RELPATH = Path(".mem") / "maintenance.jsonl"
DREAM_REPORTS_RELDIR = Path(".mem") / "dream_reports"


def maintenance_log_path(cfg: Config) -> Path:
    """Append-only operations log for the dream cycle.

    One JSON line per cycle invocation. Stable schema is the
    :meth:`DreamCycleResult.log_entry` output. Cron jobs reading this file
    should rely only on documented keys.
    """
    return cfg.vault_root / MAINTENANCE_LOG_RELPATH


def append_maintenance_log(cfg: Config, entry: dict) -> Path:
    """Append one JSON line to ``vault/.mem/maintenance.jsonl``.

    Creates the parent directory and file if missing. Returns the path of
    the log file (which the caller may surface to the user).
    """
    path = maintenance_log_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return path


def dream_reports_dir(cfg: Config) -> Path:
    """Directory holding per-cycle human-readable dream reports."""
    return cfg.vault_root / DREAM_REPORTS_RELDIR


def dream_report_path(cfg: Config, cycle_id: str) -> Path:
    """Path of the markdown report for ``cycle_id``."""
    return dream_reports_dir(cfg) / f"{cycle_id}.md"


def _new_cycle_id() -> str:
    """Generate a cycle ID of the form ``dream-YYYYMMDD-HHMMSS-<hex6>``.

    Stable enough to grep the log by date; unique enough to disambiguate
    multiple cycles in the same second (rare, but plausible under
    test-suite parallelism).
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"dream-{ts}-{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------------------
# Scan phase — read-only action plan
# ---------------------------------------------------------------------------


@dataclass
class DreamCycleScan:
    """Read-only action plan emitted by the scan phase.

    Consumed by the LLM judgment phase of ``/dream``, which constructs an
    ``apply`` plan from the survivors. Stable JSON schema — agents read it.
    """

    cycle_id: str
    project: str = ""
    promotion_cap: int = 20
    stats: dict = field(default_factory=dict)
    drift_pairs: list = field(default_factory=list)
    promotion_candidates: list = field(default_factory=list)
    # Enriched cluster signals — clusters of recent event-grain sources
    # sharing concepts. Each carries the raw per-source `proposed_theme:`
    # stamps (`proposed_names`) plus any active theme whose concepts
    # overlap (`covering_themes`). The dream apply phase reads these and
    # either MINTS a new theme (`theme_mints` plan key) or EXTENDS an
    # existing one (`theme_extensions`). No candidate stubs, no vote, no
    # lifecycle — themes change status only by hand.
    theme_cluster_signals: list = field(default_factory=list)
    # Probe-pressure aggregate (Slice 1.5) — ``{concept: probe_count}``
    # over the lookback window. Seeds the LLM judgment phase's
    # ``priority_signals`` plan key: concepts the user has been
    # probing about that the cycle should surface (enqueue or log).
    recent_probes: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def scan(
    cfg: Config,
    *,
    project: str = "",
    promotion_cap: int = 20,
    promotion_threshold: int = 5,
) -> DreamCycleScan:
    """Compose a read-only action plan from three vault-global scans.

    1. **drift pairs** — ``operations.concepts.drift`` filtered through
       ``filter_drift_candidates`` (drops the substring/short-name noise
       documented in [[feedback_dont_trust_drift_blindly]]).
    2. **promotion candidates** — ``proposed_concepts`` at ``count ≥
       promotion_threshold`` (default 5), filtered through
       ``filter_promotion_candidates`` (drops domain-paths, generic
       process terms, underscore-bearing leakage), sorted by count desc,
       capped at ``promotion_cap``.
    3. **theme cluster signals** — ``detect_signals``: clusters of recent
       event-grain sources sharing concepts, each enriched with raw
       ``proposed_theme:`` stamps and overlapping active themes so the
       LLM turn can mint or extend.

    Theme *lifecycle* is intentionally absent — no dormant/resolved
    detection, no status changes. Themes change status only by hand.

    Steps are wrapped: a failure in one is recorded in ``errors``; the
    rest still run. Returns :class:`DreamCycleScan`.
    """
    result = DreamCycleScan(
        cycle_id=_new_cycle_id(),
        project=project,
        promotion_cap=promotion_cap,
    )

    # 1. drift pairs ------------------------------------------------------
    _t = time.perf_counter()
    try:
        from personal_mem.operations.concepts import drift as concept_drift
        from personal_mem.synthesis.concepts import filter_drift_candidates

        d = concept_drift(cfg, project=project)
        # drift_report's near_duplicates is already the tuple shape
        # filter_drift_candidates wants: [(a, b, reason), ...].
        # Convert defensively in case the producer ever switches to dicts.
        raw_pairs = (d.get("report") or {}).get("near_duplicates", []) or []
        tuples: list[tuple[str, str, str]] = []
        for p in raw_pairs:
            if isinstance(p, dict):
                tuples.append(
                    (p.get("from", ""), p.get("to", ""), p.get("reason", ""))
                )
            elif isinstance(p, (tuple, list)) and len(p) >= 2:
                tuples.append(
                    (p[0], p[1], p[2] if len(p) > 2 else "")
                )
        surviving = filter_drift_candidates(tuples)
        result.drift_pairs = [
            {"from": a, "to": b, "reason": r} for a, b, r in surviving
        ]
    except Exception as e:  # noqa: BLE001 — best-effort scan step
        result.errors.append(f"drift: {e}")
    finally:
        result.timings["drift"] = time.perf_counter() - _t

    # 2. promotion candidates ---------------------------------------------
    _t = time.perf_counter()
    try:
        from personal_mem.core.indexer import Indexer
        from personal_mem.synthesis.concepts import (
            filter_promotion_candidates,
            get_all_proposed_concepts,
        )

        idx = Indexer(config=cfg)
        try:
            proposed = get_all_proposed_concepts(idx.db)
        finally:
            idx.close()

        eligible = [c for c, n in proposed.items() if n >= promotion_threshold]
        survivors = filter_promotion_candidates(eligible)
        ranked = sorted(
            (
                {"concept": c, "count": proposed[c]}
                for c in survivors
            ),
            key=lambda r: (-r["count"], r["concept"]),
        )[:promotion_cap]
        result.promotion_candidates = ranked
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"promotion: {e}")
    finally:
        result.timings["promotion"] = time.perf_counter() - _t

    # 3. theme cluster signals -------------------------------------------
    # Clusters of recent event-grain sources sharing concepts, each
    # enriched with the raw per-source `proposed_theme:` tally and any
    # active theme whose concepts overlap. The LLM turn reads these and
    # decides MINT (new arc) or EXTEND (covering_themes non-empty).
    _t = time.perf_counter()
    try:
        from personal_mem.synthesis.theme_candidates import detect_signals

        for sig in detect_signals(cfg):
            result.theme_cluster_signals.append(
                {
                    "source_type": sig.source_type,
                    "cluster_kind": sig.cluster_kind,
                    "label": sig.label,
                    "shared_concepts": sig.shared_concepts,
                    "source_count": len(sig.sources),
                    "sources": sig.sources,
                    "proposed_names": sig.proposed_names,
                    "related_names": sig.related_names,
                    "covering_themes": sig.covering_themes,
                }
            )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"theme_cluster_signals: {e}")
    finally:
        result.timings["theme_cluster_signals"] = time.perf_counter() - _t

    # 4. probe pressure ---------------------------------------------------
    # Aggregate probe-classified prompts into per-concept pressure over
    # a 14-day window. The LLM judgment phase reads this to compose
    # ``priority_signals`` — concepts the user has been asking about
    # that warrant attention (enqueue or log).
    _t = time.perf_counter()
    try:
        from personal_mem.operations.prompts import recent_probe_pressure

        result.recent_probes = recent_probe_pressure(
            cfg, project=project, window_days=14
        )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"recent_probes: {e}")
    finally:
        result.timings["recent_probes"] = time.perf_counter() - _t

    result.stats = {
        "drift_pairs": len(result.drift_pairs),
        "promotion_candidates": len(result.promotion_candidates),
        "theme_cluster_signals": len(result.theme_cluster_signals),
        "recent_probes": len(result.recent_probes),
    }

    return result


# ---------------------------------------------------------------------------
# Apply phase — execute LLM-judged plan
# ---------------------------------------------------------------------------


@dataclass
class DreamCycleResult:
    """Structured outcome of :func:`apply`. Mirrors WrapFinalizeResult.

    ``ontology_grew`` is the gating signal for hub regeneration: True iff
    at least one promotion in this cycle added a *new* term to the
    ontology (vs. an idempotent sweep of a term already canonical). The
    apply phase reads this flag to decide whether to run the
    hub-skeleton/domain-hub regen chain — those are O(canonical_concepts)
    and were the dominant cost on the 2026-05-23 first-cycle (455s of
    the 481s total). Steady-state cycles, where every promotion is a
    sweep, skip the chain entirely.
    """

    cycle_id: str
    project: str = ""
    merges_applied: int = 0
    promotions_applied: int = 0
    themes_minted: int = 0
    themes_extended: int = 0
    essence_rewrites_logged: int = 0  # body edits done by the skill, logged here
    # Priority signals (Slice 1.5) — split on action.
    # ``enqueued`` rises when ``dream_enqueue_priority_signals`` is
    # True AND the signal's action is ``enqueue`` AND the queue write
    # succeeded. Everything else counts as ``logged`` (gate disabled,
    # action explicitly ``log``, or a missing/malformed queue_item).
    priority_signals_enqueued: int = 0
    priority_signals_logged: int = 0
    ontology_grew: bool = False
    indexed: int = 0
    removed: int = 0
    edges: int = 0
    timings: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    log_path: str = ""
    report_path: str = ""  # markdown report at vault/.mem/dream_reports/<cycle_id>.md

    def as_dict(self) -> dict:
        return asdict(self)

    def log_entry(self, plan: dict) -> dict:
        """Build the maintenance.jsonl line for this cycle.

        Captures intent (the plan) alongside outcome (counts + errors).
        Lets a human grep the log later and answer "what did the cycle
        do on day X, and was anything left unfinished?".
        """
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "cycle_id": self.cycle_id,
            "project": self.project,
            "summary": {
                "merges": self.merges_applied,
                "promotions": self.promotions_applied,
                "themes_minted": self.themes_minted,
                "themes_extended": self.themes_extended,
                "essence_rewrites": self.essence_rewrites_logged,
                "priority_signals_enqueued": self.priority_signals_enqueued,
                "priority_signals_logged": self.priority_signals_logged,
                "ontology_grew": self.ontology_grew,
            },
            "index": {
                "indexed": self.indexed,
                "removed": self.removed,
                "edges": self.edges,
            },
            "plan": plan,
            "errors": list(self.errors),
            "timings": dict(self.timings),
        }


def apply(
    cfg: Config,
    *,
    plan: dict,
    project: str = "",
    cycle_id: str | None = None,
) -> DreamCycleResult:
    """Execute the LLM-decided structural changes for one dream cycle.

    Plan shape (all keys optional; missing/empty = no-op for that surface)::

        {
          "merges": [
            {"from": "fastapi", "to": "api", "reason": "subset of api"},
            ...
          ],
          "promotions": [
            {"concept": "diagnostics", "domain": "swe", "reason": "..."},
            ...
          ],
          "theme_mints": [
            {"slug": "iran-war",
             "essence": "1-sentence narrative description.",
             "source_ids": ["src-A", "src-B", "src-C"],
             "concepts": ["geopolitics", "oil"],
             "project": "" (optional), "parent": "thm-X" (optional)},
            ...
          ],
          "theme_extensions": [
            {"theme_id": "thm-X", "source_ids": ["src-D", "src-E"],
             "reason": "..."},
            ...
          ],
          "essence_rewrites": [
            {"theme_id": "thm-X", "reason": "..."},  # log-only; the skill
                                                     # already Edit'd the file
            ...
          ],
          "priority_signals": [
            # Composed by the LLM judgment phase from
            # ``scan().recent_probes``. The ``action`` field is the LLM's
            # call; ``enqueue`` only actually writes when
            # ``cfg.dream_enqueue_priority_signals`` is True, otherwise
            # the entry counts as logged.
            {"concept": "dynamic-batching", "probe_count": 4,
             "action": "enqueue",
             "queue_item": {"source_type": "article",
                            "title": "Survey: dynamic-batching",
                            "concept": "dynamic-batching",
                            "source": "dream-priority-signal"},
             "reason": "User asked 4× in 14d; vault has no source coverage."},
            {"concept": "embeddings", "probe_count": 6,
             "action": "log",
             "reason": "Asked repeatedly but already well-sourced — note for the user."},
            ...
          ],
        }

    Order matters: merges → promotions → theme mints → theme extensions →
    ONE index rebuild → maintenance.jsonl append. Each step is wrapped;
    failure in one is recorded in ``errors`` and the rest still run.

    There is deliberately no theme-status / candidate-archival surface:
    theme lifecycle is hand-driven, and the candidate-stub path was
    removed in the 2026-05-30 teardown.

    Returns :class:`DreamCycleResult` carrying counts, per-step wall
    times, and the path to the appended maintenance-log line.
    """
    result = DreamCycleResult(
        cycle_id=cycle_id or _new_cycle_id(),
        project=project,
    )

    # 1. merges -----------------------------------------------------------
    # Bypass operations.concepts.merge (which rebuilds per call). Use the
    # synthesis helpers directly and rebuild once at the end (step 4).
    _t = time.perf_counter()
    try:
        from personal_mem.synthesis.concepts import (
            delete_concept_hub,
            load_aliases,
            merge_concept_in_notes,
            save_aliases,
        )

        merges = plan.get("merges") or []
        if merges:
            aliases = load_aliases(cfg)
            for m in merges:
                try:
                    from_c = (m.get("from") or "").lower().strip()
                    to_c = (m.get("to") or "").lower().strip()
                    if not from_c or not to_c or from_c == to_c:
                        continue
                    merge_concept_in_notes(cfg.vault_root, from_c, to_c)
                    existing = aliases.get(to_c, [])
                    if from_c not in existing:
                        existing.append(from_c)
                    if from_c in aliases:
                        for old in aliases.pop(from_c):
                            if old != to_c and old not in existing:
                                existing.append(old)
                    aliases[to_c] = existing
                    delete_concept_hub(cfg, from_c)
                    result.merges_applied += 1
                except Exception as e:  # noqa: BLE001
                    result.errors.append(
                        f"merge {m.get('from', '?')}→{m.get('to', '?')}: {e}"
                    )
            if result.merges_applied:
                save_aliases(cfg, aliases)
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"merges: {e}")
    finally:
        result.timings["merges"] = time.perf_counter() - _t

    # 2. proposed-concept promotions --------------------------------------
    # Each promotion walks the vault to shift the term proposed → canonical;
    # we suppress its per-call rebuild and do one full rebuild after.
    # The synthesis helper reports whether the ontology actually grew (vs
    # an idempotent sweep of a term already canonical) — we OR those flags
    # so step 4 can skip hub regen on pure-sweep cycles.
    _t = time.perf_counter()
    try:
        from personal_mem.synthesis.concepts import promote_proposed_concept

        for p in plan.get("promotions") or []:
            try:
                concept = (p.get("concept") or "").lower().strip()
                domain = (p.get("domain") or "").lower().strip()
                if not concept or not domain:
                    result.errors.append(
                        f"promote: missing concept/domain in {p}"
                    )
                    continue
                stats = promote_proposed_concept(
                    cfg, concept, domain=domain, rebuild_index=False
                )
                if stats.get("ontology_updated"):
                    result.ontology_grew = True
                result.promotions_applied += 1
            except Exception as e:  # noqa: BLE001
                result.errors.append(
                    f"promote {p.get('concept', '?')}: {e}"
                )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"promotions: {e}")
    finally:
        result.timings["promotions"] = time.perf_counter() - _t

    # 3a. theme mints ----------------------------------------------------
    # Plan items composed by /dream from `theme_cluster_signals` with no
    # on-topic covering theme. Each item is
    # {slug, essence, source_ids, [concepts], [project], [parent]}.
    _t = time.perf_counter()
    try:
        from personal_mem.synthesis.theme_candidates import mint_theme_from_signal

        for tm in plan.get("theme_mints") or []:
            try:
                slug = tm.get("slug") or ""
                source_ids = tm.get("source_ids") or []
                if not slug or not source_ids:
                    result.errors.append(
                        f"theme_mint: missing slug/source_ids in {tm}"
                    )
                    continue
                mint_theme_from_signal(
                    cfg,
                    slug=slug,
                    essence=tm.get("essence") or "",
                    cluster_source_ids=list(source_ids),
                    cluster_concepts=list(tm.get("concepts") or []),
                    candidacy=tm.get("candidacy") or "inferred-from-signal",
                    project=tm.get("project") or "",
                    parent=tm.get("parent") or "",
                    rebuild_index=False,
                )
                result.themes_minted += 1
            except Exception as e:  # noqa: BLE001
                result.errors.append(f"theme_mint {tm.get('slug', '?')}: {e}")
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"theme_mints: {e}")
    finally:
        result.timings["theme_mints"] = time.perf_counter() - _t

    # 3b. theme extensions -----------------------------------------------
    # The steady-state case: new event-grain sources landed on an arc a
    # theme already tracks. Link them and append catalyst lines. Each item
    # is {theme_id, source_ids, [reason]}.
    _t = time.perf_counter()
    try:
        from personal_mem.synthesis.theme_candidates import (
            extend_theme_with_sources,
        )

        for tx in plan.get("theme_extensions") or []:
            try:
                theme_id = tx.get("theme_id") or ""
                source_ids = tx.get("source_ids") or []
                if not theme_id or not source_ids:
                    result.errors.append(
                        f"theme_extend: missing theme_id/source_ids in {tx}"
                    )
                    continue
                n = extend_theme_with_sources(
                    cfg,
                    theme_id=theme_id,
                    source_ids=list(source_ids),
                    rebuild_index=False,
                )
                if n:
                    result.themes_extended += 1
            except Exception as e:  # noqa: BLE001
                result.errors.append(
                    f"theme_extend {tx.get('theme_id', '?')}: {e}"
                )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"theme_extensions: {e}")
    finally:
        result.timings["theme_extensions"] = time.perf_counter() - _t

    # 3c. essence rewrites — log-only (the skill already Edit'd files) ----
    result.essence_rewrites_logged = len(plan.get("essence_rewrites") or [])

    # 3d. priority signals — log-or-enqueue per LLM judgment + cfg gate --
    # Composed from ``scan().recent_probes`` upstream. Each signal either
    # writes a queue_item (when ``action='enqueue'`` AND
    # ``cfg.dream_enqueue_priority_signals`` is True) or just contributes
    # to the cycle's log counter. Errors are per-entry and don't cascade.
    _t = time.perf_counter()
    try:
        from personal_mem.sources.queue import Queue

        signals = plan.get("priority_signals") or []
        enqueue_gate = bool(
            getattr(cfg, "dream_enqueue_priority_signals", False)
        )
        for sig in signals:
            try:
                if not isinstance(sig, dict):
                    result.errors.append(f"priority_signal: not a dict ({sig!r})")
                    continue
                action = (sig.get("action") or "log").lower()
                if action == "enqueue" and enqueue_gate:
                    queue_item = sig.get("queue_item") or {}
                    source_type = (queue_item.get("source_type") or "").strip()
                    if not source_type:
                        result.errors.append(
                            "priority_signal(enqueue): missing "
                            f"queue_item.source_type in {sig}"
                        )
                        result.priority_signals_logged += 1
                        continue
                    Queue.for_source_type(
                        source_type, cfg.vault_root
                    ).enqueue(queue_item)
                    result.priority_signals_enqueued += 1
                else:
                    # Either action='log' OR gate disabled — log only.
                    result.priority_signals_logged += 1
            except Exception as e:  # noqa: BLE001
                result.errors.append(
                    f"priority_signal {sig.get('concept', '?')}: {e}"
                )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"priority_signals: {e}")
    finally:
        result.timings["priority_signals"] = time.perf_counter() - _t

    # 4. one index rebuild + concept-hub maintenance ----------------------
    _t = time.perf_counter()
    structural_changes = (
        result.merges_applied
        + result.promotions_applied
        + result.themes_minted
        + result.themes_extended
        + result.essence_rewrites_logged
        + result.priority_signals_enqueued
    )
    if structural_changes:
        try:
            from personal_mem.core.indexer import Indexer

            # Gate hub regen on *actual* ontology growth — not just
            # "promotions ran." Idempotent sweeps (where the concept was
            # already canonical) don't need new hub skeletons or domain
            # hubs. On the 2026-05-23 first cycle this chain was 95%+ of
            # apply wall-time on a 6500-note WSL vault; skipping it on
            # sweep-only cycles is the routine speed win.
            #
            # Wikilink materialization (add_hub_wikilinks) is *not* in
            # this chain by design — it's quadratic over notes × concepts
            # and belongs on the dedicated `mem index --materialize-links`
            # path that /update-hubs owns. Dream stays in its lane.
            if result.ontology_grew:
                from personal_mem.synthesis.concepts import (
                    generate_concept_hub_skeletons,
                    generate_domain_hubs,
                    hubs_marker_path,
                    load_ontology,
                )

                try:
                    ontology = load_ontology()
                    generate_domain_hubs(cfg, ontology)
                    generate_concept_hub_skeletons(cfg, ontology)
                    hubs_marker_path(cfg).touch()
                except Exception as e:  # noqa: BLE001
                    result.errors.append(f"hub_skeletons: {e}")

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
    result.timings["index"] = time.perf_counter() - _t

    # 5. maintenance.jsonl append ----------------------------------------
    _t = time.perf_counter()
    try:
        log_path = append_maintenance_log(cfg, result.log_entry(plan))
        result.log_path = str(log_path)
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"log: {e}")
    result.timings["log"] = time.perf_counter() - _t

    # 6. human-readable markdown report ----------------------------------
    _t = time.perf_counter()
    try:
        report_path = write_dream_report(cfg, result, plan)
        result.report_path = str(report_path)
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"report: {e}")
    result.timings["report"] = time.perf_counter() - _t

    return result


# ---------------------------------------------------------------------------
# Markdown report — the human-trust artifact
# ---------------------------------------------------------------------------


def write_dream_report(cfg: Config, result: DreamCycleResult, plan: dict) -> Path:
    """Write the per-cycle markdown report and return its path.

    The maintenance.jsonl line is the machine-readable record; this
    markdown file is the human-trust artifact — `state_of_play` links to
    the most recent reports under "Recent Maintenance" so a user can
    open one and see exactly what the cycle did without parsing JSON.

    Empty sections are skipped; the summary table is always rendered.
    Idempotent overwrite — re-running the same cycle_id replaces the file
    (cycle_ids are timestamped, so this only matters in tests).
    """
    path = dream_report_path(cfg, result.cycle_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_dream_report(result, plan), encoding="utf-8")
    return path


def _render_dream_report(result: DreamCycleResult, plan: dict) -> str:
    """Render a DreamCycleResult + plan into a markdown report string."""
    lines: list[str] = []
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    project = result.project or "(global)"

    lines.append(f"# Dream cycle `{result.cycle_id}`")
    lines.append("")
    lines.append(f"- **Timestamp**: {ts}")
    lines.append(f"- **Project**: {project}")
    lines.append(f"- **Maintenance log**: `vault/.mem/maintenance.jsonl`")
    lines.append("")

    # --- Summary table ---------------------------------------------------
    lines.append("## Summary")
    lines.append("")
    lines.append("| Action | Count |")
    lines.append("|---|---|")
    lines.append(f"| Concept merges | {result.merges_applied} |")
    lines.append(f"| Concept promotions | {result.promotions_applied} |")
    lines.append(f"| Themes minted | {result.themes_minted} |")
    lines.append(f"| Themes extended | {result.themes_extended} |")
    lines.append(f"| Essence rewrites logged | {result.essence_rewrites_logged} |")
    lines.append(
        f"| Priority signals enqueued | {result.priority_signals_enqueued} |"
    )
    lines.append(
        f"| Priority signals logged | {result.priority_signals_logged} |"
    )
    lines.append(f"| Ontology grew? | {'yes' if result.ontology_grew else 'no'} |")
    lines.append(f"| Notes indexed | {result.indexed} |")
    lines.append(f"| Edges rebuilt | {result.edges} |")
    lines.append(f"| Errors | {len(result.errors)} |")
    lines.append("")

    # --- Per-action sections (skip empty) -------------------------------
    merges = plan.get("merges") or []
    if merges:
        lines.append(f"## Concept merges ({len(merges)})")
        lines.append("")
        for m in merges:
            reason = m.get("reason") or "(no reason given)"
            lines.append(f"- **{m.get('from', '?')} → {m.get('to', '?')}** — {reason}")
        lines.append("")

    promotions = plan.get("promotions") or []
    if promotions:
        lines.append(f"## Concept promotions ({len(promotions)})")
        lines.append("")
        for p in promotions:
            concept = p.get("concept", "?")
            domain = p.get("domain") or "(no domain)"
            reason = p.get("reason") or "(no reason given)"
            lines.append(f"- **{concept}** (domain: {domain}) — {reason}")
        lines.append("")

    theme_mints = plan.get("theme_mints") or []
    if theme_mints:
        lines.append(f"## Themes minted ({len(theme_mints)})")
        lines.append("")
        for t in theme_mints:
            slug = t.get("slug", "?")
            essence = (t.get("essence") or "").strip()
            srcs = t.get("source_ids") or []
            concepts = t.get("concepts") or []
            lines.append(f"- **{slug}**")
            if essence:
                lines.append(f"  - Essence: {essence}")
            if concepts:
                lines.append(f"  - Concepts: {', '.join(concepts)}")
            if srcs:
                lines.append(f"  - Sources ({len(srcs)}): {', '.join(srcs[:5])}")
        lines.append("")

    theme_exts = plan.get("theme_extensions") or []
    if theme_exts:
        lines.append(f"## Themes extended ({len(theme_exts)})")
        lines.append("")
        for t in theme_exts:
            tid = t.get("theme_id", "?")
            srcs = t.get("source_ids") or []
            reason = (t.get("reason") or "").strip()
            line = f"- `{tid}` — +{len(srcs)} source(s)"
            if reason:
                line += f" — {reason}"
            lines.append(line)
        lines.append("")

    rewrites = plan.get("essence_rewrites") or []
    if rewrites:
        lines.append(f"## Essence rewrites ({len(rewrites)})")
        lines.append("")
        lines.append(
            "_Body edits were made by the skill before apply; see the "
            "theme file's git history for the diff._"
        )
        lines.append("")
        for r in rewrites:
            tid = r.get("theme_id", "?")
            reason = r.get("reason") or "(no reason given)"
            lines.append(f"- `{tid}` — {reason}")
        lines.append("")

    # --- Priority signals (Slice 1.5) ------------------------------------
    # Splits into "What I queued" (action=enqueue + gate hot) and
    # "What I noted" (everything else). Each list shows the concept,
    # the probe count that drove it, and the LLM's reason — so the user
    # can read this section and understand exactly why dream surfaced
    # each signal.
    signals = plan.get("priority_signals") or []
    if signals:
        enqueued = [
            s for s in signals
            if isinstance(s, dict)
            and (s.get("action") or "log").lower() == "enqueue"
            and s.get("queue_item", {}).get("source_type")
        ]
        logged = [s for s in signals if s not in enqueued]

        if enqueued:
            lines.append(f"## What I queued ({len(enqueued)})")
            lines.append("")
            for s in enqueued:
                concept = s.get("concept", "?")
                count = s.get("probe_count", 0)
                qi = s.get("queue_item") or {}
                source_type = qi.get("source_type", "?")
                title = qi.get("title") or concept
                reason = s.get("reason") or "(no reason given)"
                lines.append(
                    f"- **{concept}** ({count} probe{'s' if count != 1 else ''}) "
                    f"→ `{source_type}` queue — {title}"
                )
                lines.append(f"  - Reason: {reason}")
            lines.append("")
        if logged:
            lines.append(f"## What I noted ({len(logged)})")
            lines.append("")
            for s in logged:
                concept = s.get("concept", "?")
                count = s.get("probe_count", 0)
                reason = s.get("reason") or "(no reason given)"
                lines.append(
                    f"- **{concept}** ({count} probe{'s' if count != 1 else ''}) "
                    f"— {reason}"
                )
            lines.append("")

    # --- Errors (if any) ------------------------------------------------
    if result.errors:
        lines.append(f"## Errors ({len(result.errors)})")
        lines.append("")
        for e in result.errors:
            lines.append(f"- {e}")
        lines.append("")

    # --- Timings --------------------------------------------------------
    if result.timings:
        lines.append("## Timings")
        lines.append("")
        lines.append("| Step | Duration (s) |")
        lines.append("|---|---|")
        for step, secs in result.timings.items():
            lines.append(f"| {step} | {secs:.3f} |")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def recent_dream_reports(cfg: Config, n: int = 3) -> list[dict]:
    """Return up to ``n`` most recent dream-report descriptors (newest first).

    Each entry: ``{cycle_id, path, mtime}``. Sorted by file mtime
    descending. Used by ``state_of_play`` to surface "Recent Maintenance"
    links — the user clicks through to read what the cycle did.

    Returns ``[]`` if the reports directory doesn't exist (dream has
    never run on this vault).
    """
    reports_dir = dream_reports_dir(cfg)
    if not reports_dir.exists():
        return []
    rows: list[dict] = []
    for path in reports_dir.glob("dream-*.md"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        rows.append({
            "cycle_id": path.stem,
            "path": str(path),
            "mtime": mtime,
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:n]
