"""Periodic vault synthesis + hygiene cycle — the deterministic backbone of ``/dream``.

``/dream`` is the cron-friendly successor to ``/mem-resolve-concepts`` and
``/themes-resolve``, but its scope has grown beyond hygiene: it now mints
new themes from event-grain source clusters, extends existing themes with
fresh sources, surfaces priority signals from recent probe-classified
prompts, and (when ``dream_compute_pagerank`` is set) regenerates concept
hub centrality scores. Hygiene (drift merges, proposed-concept promotion)
remains the deterministic spine; synthesis (theme mint/extend, priority
signals, essence rewrites) is the load-bearing recent addition.

It runs in three phases:

1. **scan** — read-only sweep. Composes drift candidates, promotion-eligible
   proposed concepts, theme cluster signals (with covering-theme rankings
   for the mint-vs-extend decision), and recent probe pressure into a
   structured action plan. Cheap; ~1s on a 6500-note vault.
2. **LLM judgment** — the ``/dream`` skill applies semantic judgment to the
   scan output (which drift pairs are real, which candidates deserve
   canonicalisation, which clusters are arcs worth minting, which themes
   have stale essences, which probes warrant queueing research) and emits
   a plan dict. This phase lives in the skill, not here.
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
# User-visible home for cron synthesis reports (was hidden under .mem/).
# The reports/ tree is excluded from the index (see Indexer.rebuild), like
# landing docs — materialized narrative, not source material.
DREAM_REPORTS_RELDIR = Path("reports") / "dream"


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
    # Active themes carrying ≥1 catalyst within the last 30 days, pre-loaded
    # with their current ``## Essence`` text + last 10 catalyst entries. The
    # phase-1 essence-worker reads this directly so it never has to crawl
    # theme files — the indexer + ``Hub.parse`` bound the cost to (# themes).
    active_themes: list = field(default_factory=list)
    # Sessions whose ``events.jsonl`` is non-empty AND whose frontmatter
    # lacks ``processed: true`` — the phase-2 ``dream-wrap-worker`` input.
    # Discovered via the SQLite index (``type='session'``) so we never crawl
    # the projects/ tree. Capped at 50 entries; older than 30 days skipped.
    unwrapped_sessions: list = field(default_factory=list)
    # Decisions awaiting re-judgment — the phase-2 ``dream-judge-worker``
    # input. Composed from ``.mem/rejudge_queue.jsonl`` plus any stale
    # ``pending`` verdicts discovered via the index (judged_at missing or
    # older than 7 days). Capped at 20 total.
    rejudge_queue: list = field(default_factory=list)
    # Composite knowledge-delta surface — the phase-2 ``dream-digest-worker``
    # input. Pre-computed over a 24h window: landings_24h /
    # catalyst_additions_24h / probe_matches_24h / verdict_flips_24h /
    # predictions_landed_24h, plus a ``theme_mutations_this_cycle`` slot
    # the orchestrator fills in after apply.
    knowledge_delta: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _collect_active_themes(
    cfg: Config,
    *,
    recent_days: int = 30,
    max_catalysts: int = 10,
) -> list[dict]:
    """Themes with ≥1 catalyst inside the last ``recent_days`` days.

    Returns enriched payloads the phase-1 essence-worker can judge in
    isolation — current ``## Essence`` text, the most recent
    ``max_catalysts`` catalyst entries (newest first), total catalyst
    count, last catalyst date. The worker never needs to ``mem_read`` the
    theme file because the scan already loaded it.

    Theme count on a real vault is bounded (dozens, not thousands), so the
    per-theme ``Hub.parse`` file read is fine. The lookup of *which*
    themes to consider goes through the SQLite index — no vault crawl.

    Vault-wide on purpose: themes live at ``vault/themes/{slug}.md``
    regardless of project (CLAUDE.md §3), mirroring how
    ``theme_cluster_signals`` and drift surfaces walk the whole vault.

    Themes whose frontmatter ``status`` is anything other than ``active``
    (default when missing) are skipped: only active themes can have their
    essence rewritten by the dream cycle.
    """
    from datetime import date, timedelta

    from personal_mem.core.indexer import Indexer
    from personal_mem.synthesis.hub import Hub

    cutoff = (date.today() - timedelta(days=recent_days)).isoformat()

    idx = Indexer(config=cfg)
    try:
        rows = list(
            idx.db.execute(
                "SELECT id, title, path, frontmatter "
                "FROM notes WHERE type = 'theme'"
            )
        )
    finally:
        idx.close()

    active: list[dict] = []
    for row in rows:
        try:
            fm = json.loads(row["frontmatter"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        status = (fm.get("status") or "active").lower()
        if status != "active":
            continue

        theme_path = cfg.vault_root / row["path"]
        if not theme_path.exists():
            continue

        try:
            hub = Hub.parse(theme_path, hub_id=row["id"])
        except Exception:  # noqa: BLE001 — corrupt body shouldn't kill scan
            continue

        recent = [e for e in hub.log if e.date >= cutoff]
        if not recent:
            continue

        all_sorted = sorted(hub.log, key=lambda e: e.date, reverse=True)
        last_n = all_sorted[:max_catalysts]

        active.append({
            "theme_id": row["id"],
            "title": row["title"],
            "path": row["path"],
            "essence": hub.essence,
            "recent_catalysts": [
                {
                    "date": e.date,
                    "flag": e.flag,
                    "text": e.text,
                    "citation": e.citation,
                }
                for e in last_n
            ],
            "total_catalysts": len(hub.log),
            "last_catalyst_date": all_sorted[0].date,
        })

    return active


def _collect_unwrapped_sessions(
    cfg: Config,
    *,
    recent_days: int = 30,
    cap: int = 50,
) -> list[dict]:
    """Sessions whose ``events.jsonl`` is non-empty and lack ``processed: true``.

    Phase-2 ``dream-wrap-worker`` input. Each entry carries
    ``{session_id, project, events_jsonl_path, last_activity_ts}`` so the
    worker can read the events buffer directly without re-discovering it.

    Conservative defaults: missing or empty ``events.jsonl`` means the
    session is effectively already wrapped (nothing to extract), so it's
    skipped. Sessions older than ``recent_days`` are skipped — a session
    that's been sitting unwrapped for a month isn't a candidate for
    automated nightly catch-up.

    All discovery goes through the SQLite index (``type='session'``); we
    never walk the projects/ tree.
    """
    from datetime import date, timedelta

    from personal_mem.core.indexer import Indexer

    cutoff_date = (date.today() - timedelta(days=recent_days)).isoformat()

    idx = Indexer(config=cfg)
    try:
        rows = list(
            idx.db.execute(
                "SELECT id, path, frontmatter, project, date "
                "FROM notes WHERE type = 'session'"
            )
        )
    finally:
        idx.close()

    out: list[dict] = []
    for row in rows:
        try:
            fm = json.loads(row["frontmatter"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue

        # Already processed → skip.
        if fm.get("processed") is True:
            continue

        # Date cutoff (frontmatter date is the session's recorded ts).
        session_date = (row["date"] or "")[:10]
        if session_date and session_date < cutoff_date:
            continue

        session_path = cfg.vault_root / row["path"]
        events_path = session_path.parent / "events.jsonl"

        # Conservative: missing/empty events.jsonl ⇒ nothing to wrap.
        try:
            if not events_path.exists() or events_path.stat().st_size == 0:
                continue
        except OSError:
            continue

        out.append({
            "session_id": row["id"],
            "project": row["project"] or "",
            "events_jsonl_path": str(events_path),
            "last_activity_ts": row["date"] or "",
        })
        if len(out) >= cap:
            break

    return out


def _collect_rejudge_queue(
    cfg: Config,
    *,
    stale_pending_age_days: int = 7,
    cap: int = 20,
) -> list[dict]:
    """Combine drained rejudge-queue entries with stale ``pending`` decisions.

    Phase-2 ``dream-judge-worker`` input. Two contributing streams:

    1. ``vault/.mem/rejudge_queue.jsonl`` — already in the right shape
       (``{decision_id, predecessor_decision_id?, queued_at, reason}``;
        legacy entries may carry ``source``/``enqueued_at`` keys —
        normalized below).
    2. Decisions with ``prediction_match == 'pending'`` whose
       ``judged_at`` is missing or older than ``stale_pending_age_days``.
       Fabricated entries: ``predecessor_decision_id: null``,
       ``queued_at: <judged_at or "">``, ``reason: 'stale_pending'``.

    Capped at ``cap`` total. Discovery uses the SQLite index
    (``json_extract`` on the frontmatter blob) — no vault file crawl.
    """
    from datetime import datetime, timedelta, timezone

    from personal_mem.core.indexer import Indexer

    out: list[dict] = []
    seen_ids: set[str] = set()

    # 1. Drain the on-disk queue first (priority — these are explicit).
    queue_path = cfg.vault_root / ".mem" / "rejudge_queue.jsonl"
    if queue_path.exists():
        for line in queue_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            dec_id = row.get("decision_id") or ""
            if not dec_id or dec_id in seen_ids:
                continue
            out.append({
                "decision_id": dec_id,
                "predecessor_decision_id": row.get("predecessor_decision_id"),
                "queued_at": row.get("queued_at")
                    or row.get("enqueued_at")
                    or "",
                "reason": row.get("reason") or "",
            })
            seen_ids.add(dec_id)
            if len(out) >= cap:
                return out

    # 2. Stale-pending sweep via index.
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=stale_pending_age_days)
    ).isoformat()

    idx = Indexer(config=cfg)
    try:
        rows = idx.db.execute(
            """
            SELECT id,
                   json_extract(frontmatter, '$.judged_at') AS judged_at
              FROM notes
             WHERE type = 'decision'
               AND json_extract(frontmatter, '$.prediction_match') = 'pending'
            """,
        ).fetchall()
    finally:
        idx.close()

    for row in rows:
        try:
            dec_id = row["id"]
            judged = row["judged_at"]
        except (KeyError, IndexError):
            dec_id = row[0]
            judged = row[1]
        if not dec_id or dec_id in seen_ids:
            continue
        if judged and str(judged) >= cutoff:
            continue  # fresh — not stale
        out.append({
            "decision_id": dec_id,
            "predecessor_decision_id": None,
            "queued_at": str(judged) if judged else "",
            "reason": "stale_pending",
        })
        seen_ids.add(dec_id)
        if len(out) >= cap:
            break

    return out


def _collect_knowledge_delta(
    cfg: Config,
    *,
    window_hours: int = 24,
) -> dict:
    """Composite 24h surface for the phase-2 ``dream-digest-worker``.

    Returns a **grain-split** shape — ``{concept: {...}, event: {...}}`` —
    so the digest worker can compose two sibling notes:

    - ``concept`` ("what you learned"): concept-grain source landings
      (paper/repo/article/newsletter-concepts/youtube-concepts/podcast-concepts),
      concept-hub catalyst additions, probe matches, decision verdict flips,
      and confirmed predictions.
    - ``event`` ("what happened"): event-grain source landings
      (substack/news/newsletter-events/youtube-events/podcast-events),
      theme-hub catalyst additions, and the orchestrator-filled
      ``theme_mutations_this_cycle`` slot.

    The grain is read off ``SourceTypeSpec.temporal_grain`` (sources/
    registry.py) — adding a new source type with
    ``temporal_grain='event'`` automatically routes its landings into the
    event slice; ``temporal_grain='none'`` (e.g. ``conversation``) drops
    out of both. Catalyst additions split by ``hub_kind`` (``theme`` →
    event, ``concept`` → concept).

    Top-level keys ``window_start`` / ``window_end`` remain at the root
    of the returned dict so downstream consumers can stamp the digest
    note's ``date:`` from a single field regardless of grain. Hub set
    discovery still goes through the SQLite index, never a filesystem
    crawl.
    """
    from datetime import datetime, timedelta, timezone

    from personal_mem.core.indexer import Indexer
    from personal_mem.operations.prompts import recent_probe_pressure
    from personal_mem.sources import registry as source_registry
    from personal_mem.synthesis.hub import Hub

    now = datetime.now(timezone.utc)
    cutoff_dt = now - timedelta(hours=window_hours)
    cutoff_iso = cutoff_dt.isoformat()
    # For the catalyst log scan we compare against the entry's ``date``
    # field, which is YYYY-MM-DD only — use the day-level cutoff.
    cutoff_day = cutoff_dt.date().isoformat()

    # Pre-compute the source_type → temporal_grain lookup once. User-side
    # overlays at vault/.mem/source_types.yaml are honoured.
    grain_lookup: dict[str, str] = {}
    try:
        for spec in source_registry.all_specs(vault_root=cfg.vault_root):
            grain_lookup[spec.slug] = spec.temporal_grain
            for alias in spec.aliases:
                grain_lookup[alias] = spec.temporal_grain
    except Exception:  # noqa: BLE001 — registry load shouldn't kill scan
        grain_lookup = {}

    def _new_grain_bucket() -> dict:
        return {
            "landings_24h": [],
            "catalyst_additions_24h": [],
            "theme_mutations_this_cycle": {
                "theme_mints": [],
                "theme_extensions": [],
            },
            "probe_matches_24h": [],
            "verdict_flips_24h": [],
            "predictions_landed_24h": [],
        }

    delta: dict = {
        "window_start": cutoff_iso,
        "window_end": now.isoformat(),
        "concept": _new_grain_bucket(),
        "event": _new_grain_bucket(),
    }

    idx = Indexer(config=cfg)
    try:
        # 1. Landings (sources created in window) -----------------------
        source_rows = idx.db.execute(
            "SELECT id, title, type, frontmatter, date "
            "FROM notes WHERE type = 'source' AND date >= ?",
            (cutoff_iso,),
        ).fetchall()
        for row in source_rows:
            try:
                fm = json.loads(row["frontmatter"] or "{}")
            except (json.JSONDecodeError, TypeError):
                fm = {}
            relates = fm.get("relates_to") or []
            if not isinstance(relates, list):
                relates = [relates]
            theme_id = next(
                (str(r) for r in relates if str(r).startswith("thm-")), None
            )
            concepts = fm.get("concepts") or []
            if not isinstance(concepts, list):
                concepts = []
            source_type = fm.get("source_type") or "source"
            landing = {
                "id": row["id"],
                "title": row["title"],
                "type": source_type,
                "theme_id": theme_id,
                "concepts": [str(c) for c in concepts],
            }
            # Route by source-type grain. Unknown source types default to
            # ``concept`` (mirrors SourceTypeSpec's own default — "adding a
            # new source type shouldn't silently start floating themes").
            # ``none`` grain (conversation) drops out of both slices.
            grain = grain_lookup.get(source_type, "concept")
            if grain == "event":
                delta["event"]["landings_24h"].append(landing)
            elif grain == "concept":
                delta["concept"]["landings_24h"].append(landing)
            # grain == "none" → not surfaced in either digest

        # 2. Catalyst additions (concept hubs + themes) -----------------
        # Concept hubs: type='note', path under concepts/topics/.
        hub_rows = idx.db.execute(
            "SELECT id, path FROM notes "
            "WHERE type = 'theme' "
            "   OR (type = 'note' AND path LIKE 'concepts/topics/%')"
        ).fetchall()
        for row in hub_rows:
            rel_path = row["path"] or ""
            hub_path = cfg.vault_root / rel_path
            if not hub_path.exists():
                continue
            kind = "theme" if rel_path.startswith("themes/") else "concept"
            try:
                hub = Hub.parse(hub_path, hub_id=row["id"])
            except Exception:  # noqa: BLE001
                continue
            for entry in hub.log:
                if entry.date < cutoff_day:
                    continue
                addition = {
                    "hub": row["id"],
                    "hub_kind": kind,
                    "line_date": entry.date,
                    "flag": entry.flag,
                    "cited_note_id": entry.citation,
                }
                if kind == "theme":
                    delta["event"]["catalyst_additions_24h"].append(addition)
                else:
                    delta["concept"]["catalyst_additions_24h"].append(addition)

        # 3. Probe matches (recent probe pressure × in-window sources) --
        # Lives on the concept slice — probes are the user's "what am I
        # trying to learn" signal, knowledge-oriented by construction.
        try:
            pressure = recent_probe_pressure(cfg, project="", window_days=14)
        except Exception:  # noqa: BLE001
            pressure = {}
        for concept, probe_count in pressure.items():
            matched = idx.db.execute(
                "SELECT notes.id AS id, notes.title AS title "
                "  FROM notes "
                "  JOIN note_concepts ON note_concepts.note_id = notes.id "
                " WHERE note_concepts.concept = ? "
                "   AND notes.type = 'source' "
                "   AND notes.date >= ?",
                (concept, cutoff_iso),
            ).fetchall()
            for src in matched:
                delta["concept"]["probe_matches_24h"].append({
                    "source_id": src["id"],
                    "source_title": src["title"],
                    "concept": concept,
                    "probe_count": probe_count,
                })

        # 4. Verdict flips (decisions with judged_at in window, history
        #    shows a different previous match) — concept slice (decisions
        #    are about your learning loop, not external events).
        flip_rows = idx.db.execute(
            "SELECT id, frontmatter, "
            "       json_extract(frontmatter, '$.judged_at') AS judged_at, "
            "       json_extract(frontmatter, '$.prediction_match') AS match "
            "  FROM notes "
            " WHERE type = 'decision' "
            "   AND json_extract(frontmatter, '$.judged_at') >= ?",
            (cutoff_iso,),
        ).fetchall()
        for row in flip_rows:
            try:
                fm = json.loads(row["frontmatter"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            current_match = fm.get("prediction_match")
            history = fm.get("prediction_history") or []
            if not isinstance(history, list):
                history = []
            prev_match = None
            if len(history) >= 2:
                # Tail is current; one before tail is previous.
                prev_entry = history[-2]
                if isinstance(prev_entry, dict):
                    prev_match = prev_entry.get("match")
            elif len(history) == 1 and not current_match:
                # Treat as null — only one entry and no current value.
                continue

            # Only count as a flip when there is meaningful change.
            if prev_match is None and current_match:
                # Inaugural verdict — not strictly a flip, but the spec
                # says "treat prev_match as null and only include if
                # current prediction_match is set."
                pass
            elif prev_match == current_match:
                continue

            if not current_match:
                continue

            delta["concept"]["verdict_flips_24h"].append({
                "decision_id": row["id"],
                "prediction_match": current_match,
                "prev_match": prev_match,
                "judged_at": fm.get("judged_at") or "",
                "reason": (
                    (history[-1].get("reason") if history and isinstance(history[-1], dict) else "")
                    or ""
                ),
            })

        # 5. Predictions landed (confirmed in window) — concept slice.
        landed_rows = idx.db.execute(
            "SELECT id, frontmatter "
            "  FROM notes "
            " WHERE type = 'decision' "
            "   AND json_extract(frontmatter, '$.prediction_match') = 'confirmed' "
            "   AND json_extract(frontmatter, '$.judged_at') >= ?",
            (cutoff_iso,),
        ).fetchall()
        for row in landed_rows:
            try:
                fm = json.loads(row["frontmatter"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            delta["concept"]["predictions_landed_24h"].append({
                "decision_id": row["id"],
                "predicted_outcome": fm.get("predicted_outcome") or "",
                "prediction_match": "confirmed",
                "judged_at": fm.get("judged_at") or "",
            })
    finally:
        idx.close()

    return delta


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

    # 5. active themes ----------------------------------------------------
    # Themes carrying recent catalyst activity, pre-loaded with their
    # current essence + last 10 catalysts. Phase-1 ``dream-essence-worker``
    # reads this surface to judge whether any essence needs a rewrite —
    # no per-theme ``mem_read`` round trip needed. Vault-wide (themes are
    # not per-project, matching how cluster signals are scanned).
    _t = time.perf_counter()
    try:
        result.active_themes = _collect_active_themes(cfg)
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"active_themes: {e}")
    finally:
        result.timings["active_themes"] = time.perf_counter() - _t

    # 6. unwrapped sessions (phase-2 dream-wrap-worker input) ------------
    _t = time.perf_counter()
    try:
        result.unwrapped_sessions = _collect_unwrapped_sessions(cfg)
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"unwrapped_sessions: {e}")
    finally:
        result.timings["unwrapped_sessions"] = time.perf_counter() - _t

    # 7. rejudge queue (phase-2 dream-judge-worker input) ----------------
    _t = time.perf_counter()
    try:
        result.rejudge_queue = _collect_rejudge_queue(cfg)
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"rejudge_queue: {e}")
    finally:
        result.timings["rejudge_queue"] = time.perf_counter() - _t

    # 8. knowledge delta (phase-2 dream-digest-worker input) -------------
    _t = time.perf_counter()
    try:
        result.knowledge_delta = _collect_knowledge_delta(cfg)
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"knowledge_delta: {e}")
    finally:
        result.timings["knowledge_delta"] = time.perf_counter() - _t

    # ``knowledge_delta`` stats are sub-keyed by grain (``concept`` /
    # ``event``) so cycle telemetry can see at a glance whether the cycle
    # has work for both digest variants, just one, or neither.
    kd = result.knowledge_delta or {}

    def _grain_stats(slice_key: str) -> dict[str, int]:
        s = kd.get(slice_key) or {}
        return {
            "landings_24h": len(s.get("landings_24h", [])),
            "catalyst_additions_24h": len(s.get("catalyst_additions_24h", [])),
            "probe_matches_24h": len(s.get("probe_matches_24h", [])),
            "verdict_flips_24h": len(s.get("verdict_flips_24h", [])),
            "predictions_landed_24h": len(s.get("predictions_landed_24h", [])),
        }

    result.stats = {
        "drift_pairs": len(result.drift_pairs),
        "promotion_candidates": len(result.promotion_candidates),
        "theme_cluster_signals": len(result.theme_cluster_signals),
        "recent_probes": len(result.recent_probes),
        "active_themes": len(result.active_themes),
        "unwrapped_sessions": len(result.unwrapped_sessions),
        "rejudge_queue": len(result.rejudge_queue),
        "knowledge_delta": {
            "concept": _grain_stats("concept"),
            "event": _grain_stats("event"),
        },
    }

    return result


# ---------------------------------------------------------------------------
# Plan-fragment validation — strict allowlist of plan keys + per-item shapes
# ---------------------------------------------------------------------------

# Top-level allowlist. Anything outside this set raises in strict mode (or is
# appended to ``result.errors`` in non-strict). Keep in sync with the apply
# function's plan-reading loops and the docstring on ``apply``.
#
# ``cycle_id`` is plumbed through by the orchestrator (commands/dream.md
# step 1.5 merges it into the plan before writing to disk) so apply can
# stamp the result with the same cycle id used in the maintenance log;
# it is a structural envelope field, not a per-domain plan key.
_VALID_PLAN_TOP_KEYS: frozenset[str] = frozenset({
    "cycle_id",
    "merges",
    "promotions",
    "theme_mints",
    "theme_extensions",
    "essence_rewrites",
    "priority_signals",
})

# Per-top-key sub-key allowlists. Each item in a list-valued plan key is a
# dict; this map tells us which dict keys are permitted on each.
#
# The drift these guards catch (verbatim from the 2026-06 surface map):
# - ``add_source_ids`` for ``source_ids`` inside ``theme_extensions``
# - ``rationale`` for ``essence`` inside ``theme_mints``
#
# Sub-keys for nested dicts (``priority_signals[*].queue_item``) are validated
# in a dedicated nested map below.
_VALID_PLAN_ITEM_KEYS: dict[str, frozenset[str]] = {
    "merges": frozenset({"from", "to", "reason"}),
    "promotions": frozenset({"concept", "domain", "reason"}),
    "theme_mints": frozenset({
        "slug",
        "essence",
        "source_ids",
        "concepts",
        "candidacy",
        "project",
        "parent",
    }),
    "theme_extensions": frozenset({
        "theme_id",
        "source_ids",
        "reason",
    }),
    "essence_rewrites": frozenset({
        "theme_id",
        "new_essence",
        "reason",
    }),
    "priority_signals": frozenset({
        "concept",
        "probe_count",
        "action",
        "queue_item",
        "reason",
    }),
}

# Sub-key allowlist for the one nested-dict shape we ship today. The
# ``Queue.enqueue`` payload itself is open-shape (per-source-type), but the
# fields ``apply`` reads off the dict before forwarding are bounded — we
# allow-list those plus the common payload extensions writers add today.
_VALID_QUEUE_ITEM_KEYS: frozenset[str] = frozenset({
    "source_type",
    "title",
    "concept",
    "source",
    "url",
})


def validate_plan_fragment(plan: dict) -> list[str]:
    """Return a list of warnings for unknown plan keys / item sub-keys.

    Two layers:
    1. Top-level keys must be in :data:`_VALID_PLAN_TOP_KEYS`.
    2. Each item in a list-valued plan key (``merges``, ``promotions``, …)
       is a dict whose keys must be in the matching
       :data:`_VALID_PLAN_ITEM_KEYS` set.

    Empty list ⇒ the plan is structurally clean. The caller decides whether
    to raise (strict mode) or append to a worker_bug-style report
    (non-strict). The check is deliberately key-shape only — semantic
    validation (e.g. ``slug`` looks like a slug, ``source_ids`` are str)
    belongs in the per-step apply paths, which already raise structured
    errors recorded in ``result.errors``.
    """
    warnings: list[str] = []
    if not isinstance(plan, dict):
        warnings.append(f"plan is not a dict: {type(plan).__name__}")
        return warnings

    # Layer 1: top-level keys
    for key in plan.keys():
        if key not in _VALID_PLAN_TOP_KEYS:
            warnings.append(f"unknown plan key: {key!r}")

    # Layer 2: per-item sub-keys
    for top_key, valid_subs in _VALID_PLAN_ITEM_KEYS.items():
        items = plan.get(top_key)
        if not items:
            continue
        if not isinstance(items, list):
            warnings.append(
                f"plan[{top_key!r}] is not a list: {type(items).__name__}"
            )
            continue
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                warnings.append(
                    f"plan[{top_key!r}][{i}] is not a dict: "
                    f"{type(item).__name__}"
                )
                continue
            for sub_key in item.keys():
                if sub_key not in valid_subs:
                    warnings.append(
                        f"unknown sub-key {sub_key!r} in "
                        f"plan[{top_key!r}][{i}]"
                    )
            # Nested queue_item dict on priority_signals
            if top_key == "priority_signals":
                qi = item.get("queue_item")
                if isinstance(qi, dict):
                    for qk in qi.keys():
                        if qk not in _VALID_QUEUE_ITEM_KEYS:
                            warnings.append(
                                f"unknown sub-key {qk!r} in "
                                f"plan[{top_key!r}][{i}].queue_item"
                            )

    return warnings


class PlanValidationError(ValueError):
    """Raised when ``apply(strict=True)`` finds unknown plan / item keys.

    Carries the full warning list so the orchestrator (and any operator
    running ``mem dream apply`` interactively) can see every drift point
    in a single shot.
    """

    def __init__(self, warnings: list[str]):
        self.warnings = list(warnings)
        super().__init__("plan validation failed:\n  " + "\n  ".join(warnings))


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
    # Counts essence rewrites processed by the apply step. Entries with
    # a ``new_essence`` field trigger an in-place ``## Essence`` rewrite;
    # entries lacking ``new_essence`` are log-only no-ops (back-compat
    # with the pre-refactor shape). Renamed from ``essence_rewrites_logged``
    # 2026-06-06 — joint shape now that apply actually writes.
    essence_rewrites_applied: int = 0
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
                "essence_rewrites": self.essence_rewrites_applied,
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
    strict: bool = True,
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
            # New (post-2026-06-06) shape: ``new_essence`` triggers an
            # in-place ``## Essence`` rewrite of the theme file.
            {"theme_id": "thm-X",
             "new_essence": "The arc tightened around X after Y...",
             "reason": "Recent catalysts contradict the prior framing."},
            # Legacy log-only shape — counted, not mutated (back-compat).
            {"theme_id": "thm-Y", "reason": "noted, no rewrite needed"},
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

    ``strict`` (default ``True``) gates plan-fragment validation. The plan
    is run through :func:`validate_plan_fragment` first; unknown top-level
    keys or unknown per-item sub-keys raise
    :class:`PlanValidationError` (so worker drift like ``add_source_ids``
    for ``source_ids`` or ``rationale`` for ``essence`` fails loudly
    instead of silently no-opping). Pass ``strict=False`` to record the
    warnings on ``result.errors`` and still run the rest of apply — the
    non-strict mode exists for legacy plans on disk and for batch
    backfills where any single drift shouldn't abort the cycle.

    Returns :class:`DreamCycleResult` carrying counts, per-step wall
    times, and the path to the appended maintenance-log line.
    """
    # Validation gate — runs before any cycle-id resolution so a bad plan
    # never produces a half-written maintenance-log line. Strict mode raises
    # so the orchestrator stops, sees the full warning list, and re-prompts
    # the offending worker (see commands/dream.md step 1.5).
    plan_warnings = validate_plan_fragment(plan)
    if plan_warnings:
        if strict:
            raise PlanValidationError(plan_warnings)
        # Non-strict: surface every warning on the result so downstream
        # readers (maintenance.jsonl, dream report) see the drift instead
        # of letting unknown keys disappear into a silent no-op.

    result = DreamCycleResult(
        cycle_id=cycle_id or _new_cycle_id(),
        project=project,
    )

    if plan_warnings and not strict:
        for w in plan_warnings:
            result.errors.append(f"plan_validation: {w}")

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

    # 3c. essence rewrites — joint shape: rewrite-and-log when an entry
    # carries ``new_essence``; log-only for legacy entries that don't.
    # Per-entry errors don't cascade — the rest of the step still runs.
    _t = time.perf_counter()
    try:
        rewrites = plan.get("essence_rewrites") or []
        if rewrites:
            from personal_mem.core.indexer import Indexer

            # One indexer query for all rewrites — bounded by rewrites size.
            idx = Indexer(config=cfg)
            try:
                rows = idx.db.execute(
                    "SELECT id, path FROM notes WHERE type = 'theme'"
                ).fetchall()
            finally:
                idx.close()
            theme_paths: dict[str, str] = {
                row["id"]: row["path"] for row in rows
            }

            for r in rewrites:
                try:
                    theme_id = (r.get("theme_id") or "").strip()
                    if not theme_id:
                        result.errors.append(
                            f"essence_rewrite: missing theme_id in {r}"
                        )
                        continue
                    new_essence = r.get("new_essence")
                    if new_essence is None:
                        # Legacy log-only entry — count and move on.
                        result.essence_rewrites_applied += 1
                        continue
                    rel = theme_paths.get(theme_id)
                    if not rel:
                        result.errors.append(
                            f"essence_rewrite: unknown theme_id {theme_id}"
                        )
                        continue
                    theme_path = cfg.vault_root / rel
                    if not theme_path.exists():
                        result.errors.append(
                            f"essence_rewrite: missing file {rel}"
                        )
                        continue
                    _rewrite_theme_essence(theme_path, str(new_essence))
                    result.essence_rewrites_applied += 1
                except Exception as e:  # noqa: BLE001
                    result.errors.append(
                        f"essence_rewrite {r.get('theme_id', '?')}: {e}"
                    )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"essence_rewrites: {e}")
    finally:
        result.timings["essence_rewrites"] = time.perf_counter() - _t

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
        + result.essence_rewrites_applied
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
                # C19b: per-concept PageRank — gated on config.
                # Runs against the freshly-rebuilt edges + note_concepts
                # tables, so scores reflect this cycle's structural
                # changes (merges, promotions, theme mints).
                if getattr(cfg, "dream_compute_pagerank", False):
                    try:
                        from personal_mem.synthesis.centrality import (
                            compute_all_concept_pageranks,
                        )

                        compute_all_concept_pageranks(idx.db)
                    except Exception as e:  # noqa: BLE001
                        result.errors.append(f"pagerank: {e}")
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
# Theme essence rewrite — splice helper used by apply()
# ---------------------------------------------------------------------------


def _rewrite_theme_essence(theme_path: Path, new_essence: str) -> None:
    """Rewrite the ``## Essence`` section of a theme file in place.

    Preserves frontmatter, the ``# Title`` heading, and every other ``##``
    section (notably ``## Catalyst log`` and ``## Open questions``).
    Locates the essence section via the same ``extract_section`` slice the
    shared :class:`Hub` parser uses — find the heading, find the next
    ``##`` heading (or EOF), splice the new body between them.

    Idempotent: writing the same essence again is a byte-for-byte no-op.
    If the heading is missing entirely, appends a new section after the
    frontmatter; that path is for safety and never fires in normal use
    (theme files always carry the canonical skeleton).
    """
    from personal_mem.synthesis.hub import ESSENCE_HEADING

    text = theme_path.read_text(encoding="utf-8")
    body = new_essence.strip()
    # Standard inter-section spacing — blank line above the next heading.
    block = f"{ESSENCE_HEADING}\n\n{body}\n\n"

    if ESSENCE_HEADING not in text:
        # Defensive: append at end, never overwrite other content.
        sep = "" if text.endswith("\n") else "\n"
        theme_path.write_text(text + sep + block, encoding="utf-8")
        return

    start = text.index(ESSENCE_HEADING)
    # Find the next ``##`` heading after the essence section (mirrors
    # ``extract_section`` in synthesis.hub).
    import re as _re

    rest = text[start + len(ESSENCE_HEADING):]
    m = _re.search(r"\n##\s", rest)
    if m:
        end = start + len(ESSENCE_HEADING) + m.start() + 1  # +1 = leading \n
    else:
        end = len(text)

    new_text = text[:start] + block + text[end:]
    theme_path.write_text(new_text, encoding="utf-8")


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
    lines.append(f"| Essence rewrites applied | {result.essence_rewrites_applied} |")
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
            "_Entries with ``new_essence`` were rewritten by apply; "
            "entries without it are log-only (legacy shape). See each "
            "theme file's git history for the diff._"
        )
        lines.append("")
        for r in rewrites:
            tid = r.get("theme_id", "?")
            reason = r.get("reason") or "(no reason given)"
            shape = "rewritten" if r.get("new_essence") else "logged"
            lines.append(f"- `{tid}` — {shape} — {reason}")
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
