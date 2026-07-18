"""Periodic vault synthesis + hygiene cycle — the deterministic backbone of ``/dream``.

``/dream`` is the cron-friendly successor to ``/weave-resolve-concepts`` and
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
   ``vault/.weave/maintenance.jsonl``. This is the speed win — rebuilding the
   index once per mutation would be 20× full rebuilds at the per-cycle cap,
   so apply defers every index touch to a single rebuild at the tail.

The architecture mirrors ``operations/wrap.py`` (the ``weave wrap-finalize``
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
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from thinkweave.core.config import Config


# ---------------------------------------------------------------------------
# Maintenance log
# ---------------------------------------------------------------------------

MAINTENANCE_LOG_RELPATH = Path(".weave") / "maintenance.jsonl"
# User-visible home for cron synthesis reports (was hidden under .weave/).
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
    """Append one JSON line to ``vault/.weave/maintenance.jsonl``.

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
    from thinkweave.operations.reports import reports_dir

    return reports_dir(cfg, "dream")


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
    # Recent probe QUESTIONS (flat, deduped, windowed) — the raw
    # ``[{text, ts, session_id, project}]`` list the probe-distillation
    # worker (``dream-priority-worker``) reasons over. Carrying the actual
    # questions (not a ``{concept: count}`` aggregate) is what lets the
    # worker gate operational/meta probes, tie clean ontology concepts,
    # and restate terse questions into workable research queries whose
    # ``queue_item.probes`` `/drain` uses to tighten its search.
    recent_probes: list = field(default_factory=list)
    # Essence candidates across BOTH hub families (themes + concept hubs),
    # pre-loaded with their current ``## Essence`` text + recent catalyst
    # entries. The phase-1 essence-worker reads this directly so it never
    # has to crawl hub files — the indexer + ``Hub.parse`` bound the cost.
    # Each entry carries ``hub_kind: "theme"|"concept"`` plus the
    # placeholder/growth fields the worker's decision rules key on.
    # Placeholder-first ranking, capped (``--essence-cap`` / config
    # ``dream_essence_cap``; 0 = unlimited for backfill runs).
    essence_candidates: list = field(default_factory=list)
    # Per-active-theme list of sources filed to the theme (``relates_to:
    # thm-X``) but absent from its catalyst log — the directly-filed
    # sources (news triage `keep`) that never went through a cluster
    # signal. The theme worker distills these into ``theme_extensions``
    # catalyst entries. Deterministic diff, no LLM.
    theme_log_gaps: list = field(default_factory=list)
    # Sessions whose ``events.jsonl`` is non-empty AND whose frontmatter
    # lacks ``processed: true`` — the phase-2 ``dream-wrap-worker`` input.
    # Discovered via the SQLite index (``type='session'``) so we never crawl
    # the projects/ tree. Capped at 50 entries; older than 30 days skipped.
    unwrapped_sessions: list = field(default_factory=list)
    # Decisions awaiting re-judgment — the phase-2 ``dream-judge-worker``
    # input. Composed from ``.weave/rejudge_queue.jsonl`` plus any stale
    # ``pending`` verdicts discovered via the index (judged_at missing or
    # older than 7 days). Capped at ``dream.rejudge_cap`` (default 20) total.
    rejudge_queue: list = field(default_factory=list)
    # Near-duplicate THEME pairs (drift v2) — all-pairs cosine over the
    # themes' cached note embeddings, history-excluded via the maintenance
    # log, annotated with slug-token overlap + essence excerpts. The merge
    # worker judges these alongside concept drift pairs; survivors land in
    # the ``theme_merges`` plan key (worker payload only — nothing new on
    # disk).
    theme_dup_candidates: list = field(default_factory=list)
    # Grain-coarsening clusters (drift v2, N-ary) — tight near-cliques of
    # fine concepts / themes that may collapse onto one coarser term. The
    # merge worker rules COLLAPSE (→ ``coarsenings`` / ``theme_coarsenings``)
    # or DISTINCT (→ ``distinct_clusters``). Cosine-cohesion-ranked, capped
    # at ``dream_coarsen_cap`` per family, history-excluded via
    # ``geometry.judged_clusters``. Concept members carry domain hints + a
    # ``canonical_target_hint``; theme members are thm-ids with slug/essence.
    coarsen_clusters: list = field(default_factory=list)
    theme_coarsen_clusters: list = field(default_factory=list)
    # Hubs whose folded logs await cross-parent linkage — the phase-2
    # ``dream-seam-link-worker`` input, peeked (not drained) from
    # ``.weave/seam_link_queue.jsonl``. The worker drains for real; the scan
    # only reports so ``has_signal`` can decide whether to spawn. Capped
    # at ``dream_seam_link_cap``.
    seam_link_queue: list = field(default_factory=list)
    # Composite knowledge-delta surface — the phase-2 ``dream-digest-worker``
    # input. Pre-computed over the configured window
    # (``dream.knowledge_delta_hours``, default 24h): landings_24h /
    # catalyst_additions_24h / probe_matches_24h / verdict_flips_24h /
    # predictions_landed_24h, plus a ``theme_mutations_this_cycle`` slot
    # the orchestrator fills in after apply.
    knowledge_delta: dict = field(default_factory=dict)
    # Memory-seam dirty surface — the phase-2 ``dream-seam-worker`` input.
    # Cheap, embedding-free diff of CC auto-memory facts against the durable
    # state map (``vault/.weave/memory_seam.json``): which facts are new /
    # edited / unresolved / recheck-due. Twin resolution + judgment happen
    # in the worker turn (via ``weave_search(mode='similar')``) — the scan
    # stays API-free. ``has_signal`` fires when ``dirty`` or ``removed`` is
    # non-empty.
    memory_seam: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _essence_is_placeholder(essence: str) -> bool:
    """True when an essence has never been genuinely synthesised.

    Thin delegation to the shared predicate next to the placeholder
    constants in ``synthesis/hub.py`` — the same one
    ``landing.py:_truncate_essence`` uses, so the two surfaces can't
    drift again (the old local copy was a bare starts/ends-with-emphasis
    check that flagged real essences opening with emphasis).
    """
    from thinkweave.synthesis.hub import essence_is_placeholder

    return essence_is_placeholder(essence)


def _hub_essence(body: str) -> str:
    """Extract the ``## Essence`` section from a hub's indexed ``body_text``."""
    from thinkweave.synthesis.hub import ESSENCE_HEADING, extract_section

    return extract_section(body, ESSENCE_HEADING).strip()


def _indexed_hub_logs(
    db, *, hub_kind: str | None = None, min_date: str | None = None
) -> dict:
    """Read hub catalyst logs from the ``hub_log_entries`` SQL projection.

    Returns ``{hub_id: [HubLogEntry, ...]}`` in log order (``seq``);
    ``hub_id`` is the thm-id for themes, the vocabulary term (path stem)
    for concept hubs — the namespaces don't collide, so a flat key is
    safe. Freshness: ``Indexer._index_file`` writes these rows
    (``_sync_hub_log``) in the same pass/commit as ``notes.body_text``,
    so reading them carries exactly the freshness of the per-collector
    body re-parse this replaced — with title-aliased citations already
    resolved to note ids via the indexer's path→id map.
    """
    from thinkweave.synthesis.hub import HubLogEntry

    sql = (
        "SELECT hub_id, entry_date, flag, ref_date, cited_note_id, text "
        "FROM hub_log_entries"
    )
    where: list[str] = []
    params: list[str] = []
    if hub_kind:
        where.append("hub_kind = ?")
        params.append(hub_kind)
    if min_date:
        where.append("entry_date >= ?")
        params.append(min_date)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY hub_id, seq"

    logs: dict[str, list[HubLogEntry]] = {}
    for row in db.execute(sql, params):
        logs.setdefault(row["hub_id"], []).append(
            HubLogEntry(
                date=row["entry_date"],
                flag=row["flag"],
                ref=row["ref_date"] or "",
                text=row["text"] or "",
                citation=row["cited_note_id"] or "",
            )
        )
    return logs


def _collect_essence_candidates(
    cfg: Config,
    *,
    recent_days: int = 30,
    max_catalysts: int | None = None,
    placeholder_max_catalysts: int | None = None,
    cap: int = 12,
) -> list[dict]:
    """Hubs (themes AND concept hubs) whose essence deserves worker attention.

    Returns enriched payloads the phase-1 essence-worker can judge in
    isolation — current ``## Essence`` text, recent catalyst entries
    (newest first; ``placeholder_max_catalysts`` for placeholder essences,
    which need more material to compose fresh — both default from config
    ``dream.essence_max_catalysts`` / ``dream.essence_placeholder_max_catalysts``),
    total catalyst count, and the growth fields the worker's decision
    rules key on.

    Inclusion (deterministic prefilter — semantic judgment stays in the
    worker):

    - **theme** (``status: active`` only, ≥1 catalyst):
      placeholder essence, OR a catalyst in the last ``recent_days``
      days, OR ≥5 catalysts since the ``essence_updated`` stamp.
    - **concept hub**: placeholder essence with ≥5 catalysts, OR ≥10
      catalysts since the stamp, OR ≥1 ``contradicts`` in the window.

    ``catalysts_since_essence`` counts entries dated after the
    ``essence_updated`` frontmatter stamp; hubs without the stamp count
    *all* entries (an unstamped essence has never been synthesised by the
    cycle, so everything in the log post-dates it).

    Ranking is placeholder-first, then total catalysts desc, capped at
    ``cap`` (config ``dream_essence_cap`` / ``--essence-cap``; 0 =
    unlimited — the backfill lever). Discovery AND content both go through
    the SQLite index — catalyst logs come from the ``hub_log_entries``
    projection (one indexed query for all hubs) and the essence from the
    indexed ``body_text``, never a per-hub file read. On a
    Windows-mounted WSL vault (~250ms/file over /mnt/c) the file-read
    variant cost minutes per scan across ~1000+ hubs; the index is kept
    warm by ``VaultManager.create_note`` and every rebuild, so it carries
    the same freshness the rest of the scan already trusts.
    """
    from datetime import date, timedelta

    from thinkweave.core.indexer import Indexer
    from thinkweave.synthesis.hub import FLAG_CONTRADICTS

    if max_catalysts is None:
        max_catalysts = int(
            getattr(cfg, "dream_essence_max_catalysts", 10) or 10
        )
    if placeholder_max_catalysts is None:
        placeholder_max_catalysts = int(
            getattr(cfg, "dream_essence_placeholder_max_catalysts", 25) or 25
        )

    cutoff = (date.today() - timedelta(days=recent_days)).isoformat()

    idx = Indexer(config=cfg)
    try:
        rows = list(
            idx.db.execute(
                # Hubs index as type='concept-hub' (their frontmatter
                # type); 'note' is kept for legacy skeletons without it.
                "SELECT id, title, path, frontmatter, body_text FROM notes "
                "WHERE type = 'theme' "
                "   OR (type IN ('concept-hub', 'note') "
                "       AND path LIKE 'concepts/topics/%')"
            )
        )
        hub_logs = _indexed_hub_logs(idx.db)
    finally:
        idx.close()

    candidates: list[dict] = []
    for row in rows:
        try:
            fm = json.loads(row["frontmatter"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue

        rel_path = row["path"] or ""
        hub_kind = "concept" if rel_path.startswith("concepts/topics/") else "theme"

        if hub_kind == "theme":
            status = (fm.get("status") or "active").lower()
            if status != "active":
                continue

        try:
            essence = _hub_essence(row["body_text"] or "")
        except Exception:  # noqa: BLE001 — corrupt body shouldn't kill scan
            continue
        # Catalyst log from the SQL projection — keyed by thm-id for
        # themes, vocabulary term (path stem) for concept hubs.
        hub_ident = (
            Path(rel_path).stem if hub_kind == "concept" else row["id"]
        )
        log = hub_logs.get(hub_ident, [])

        total = len(log)
        if total == 0:
            continue  # nothing to synthesise from, either kind

        placeholder = _essence_is_placeholder(essence)
        stamp = str(fm.get("essence_updated") or "")[:10]
        since_essence = (
            sum(1 for e in log if e.date > stamp) if stamp else total
        )
        recent = [e for e in log if e.date >= cutoff]
        recent_contradicts = sum(
            1 for e in recent if e.flag == FLAG_CONTRADICTS
        )

        if hub_kind == "theme":
            include = placeholder or bool(recent) or since_essence >= 5
        else:
            include = (
                (placeholder and total >= 5)
                or since_essence >= 10
                or recent_contradicts >= 1
            )
        if not include:
            continue

        all_sorted = sorted(log, key=lambda e: e.date, reverse=True)
        n_catalysts = placeholder_max_catalysts if placeholder else max_catalysts
        last_n = all_sorted[:n_catalysts]

        entry = {
            "hub_kind": hub_kind,
            "title": row["title"],
            "path": rel_path,
            "essence": essence,
            "essence_is_placeholder": placeholder,
            "essence_word_count": len((essence or "").split()),
            "essence_updated": stamp,
            "catalysts_since_essence": since_essence,
            "recent_contradicts": recent_contradicts,
            "recent_catalysts": [
                {
                    "date": e.date,
                    "flag": e.flag,
                    "text": e.text,
                    "citation": e.citation,
                }
                for e in last_n
            ],
            "total_catalysts": total,
            "last_catalyst_date": all_sorted[0].date,
        }
        if hub_kind == "theme":
            entry["theme_id"] = row["id"]
        else:
            # Concept hubs are identified by the vocabulary term — the
            # filename stem, which concept_hub_path() round-trips.
            entry["concept"] = Path(rel_path).stem
        candidates.append(entry)

    candidates.sort(
        key=lambda c: (
            not c["essence_is_placeholder"],
            -c["total_catalysts"],
            c.get("theme_id") or c.get("concept") or "",
        )
    )
    if cap and cap > 0:
        candidates = candidates[:cap]
    return candidates


def _collect_theme_log_gaps(
    cfg: Config,
    *,
    cap_per_theme: int = 10,
) -> list[dict]:
    """Sources filed to an active theme but missing from its catalyst log.

    The directly-filed path (news triage ``keep`` → worker stamps
    ``relates_to: thm-X`` at create time) bypasses cluster detection
    entirely — ``detect_signals`` excludes already-filed sources as
    settled, and ``extend_theme_with_sources`` only ever ran for cluster
    members. This diff closes that gap: every active theme is checked for
    cited-in-frontmatter-but-absent-from-log sources, newest first,
    capped at ``cap_per_theme``. The theme worker turns each entry into a
    normal ``theme_extensions`` item with distilled ``catalysts``.

    Membership test is against the union of the theme's catalyst-log
    citations and its ``cites:`` frontmatter — a source in ``cites:`` but
    not the log was linked by a legacy extend that predates log entries,
    and re-adding it would duplicate the frontmatter entry.
    """
    from thinkweave.core._utils import as_list
    from thinkweave.core.indexer import Indexer
    from thinkweave.synthesis.theme_candidates import _excerpt

    idx = Indexer(config=cfg)
    try:
        theme_rows = list(
            idx.db.execute(
                "SELECT id, title, path, frontmatter "
                "FROM notes WHERE type = 'theme' AND id LIKE 'thm-%'"
            )
        )
        source_rows = list(
            idx.db.execute(
                "SELECT id, title, date, frontmatter, body_text "
                "FROM notes WHERE type = 'source'"
            )
        )
        # Cited note ids per theme, from the hub_log_entries projection
        # (same freshness as the body_text these used to be parsed from).
        theme_logs = _indexed_hub_logs(idx.db, hub_kind="theme")
    finally:
        idx.close()

    # theme_id → filed sources (from each source's relates_to).
    filed: dict[str, list[dict]] = {}
    for row in source_rows:
        try:
            fm = json.loads(row["frontmatter"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for rel in as_list(fm.get("relates_to")):
            rel = str(rel)
            if rel.startswith("thm-"):
                filed.setdefault(rel, []).append(row)

    gaps: list[dict] = []
    for trow in theme_rows:
        try:
            tfm = json.loads(trow["frontmatter"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if (tfm.get("status") or "active").lower() != "active":
            continue
        theme_id = trow["id"]
        filed_here = filed.get(theme_id)
        if not filed_here:
            continue

        logged = {
            e.citation for e in theme_logs.get(theme_id, []) if e.citation
        }
        logged |= {str(c) for c in as_list(tfm.get("cites"))}

        missing = [s for s in filed_here if s["id"] not in logged]
        if not missing:
            continue
        missing.sort(key=lambda r: r["date"] or "", reverse=True)
        gaps.append({
            "theme_id": theme_id,
            "title": trow["title"],
            "sources": [
                {
                    "id": s["id"],
                    "title": s["title"] or "",
                    "date": s["date"] or "",
                    "excerpt": _excerpt(s["body_text"] or ""),
                }
                for s in missing[:cap_per_theme]
            ],
        })
    return gaps


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

    from thinkweave.core.indexer import Indexer

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


#: Default for how many rejudge entries one cycle hands to the phase-2
#: judge worker (config ``dream.rejudge_cap``). Shared by the scan
#: collector below AND apply's consumption step — apply removes exactly
#: this prefix of the on-disk queue (the handed-off entries), so anything
#: beyond the cap survives for the next cycle. Both sites resolve through
#: :func:`_rejudge_cap` so they can never disagree.
_REJUDGE_CAP = 20


def _rejudge_cap(cfg: Config) -> int:
    """Per-cycle rejudge hand-off cap (config ``dream.rejudge_cap``)."""
    return int(getattr(cfg, "dream_rejudge_cap", _REJUDGE_CAP) or 0)


def _probe_window_days(cfg: Config) -> int:
    """Probe-pressure lookback window (config ``dream.probe_window_days``).

    Read by BOTH probe surfaces — the scan's ``recent_probes`` payload and
    the knowledge-delta probe-match slice — so the two views always cover
    the same window.
    """
    return int(getattr(cfg, "dream_probe_window_days", 14) or 14)


def _collect_rejudge_queue(
    cfg: Config,
    *,
    stale_pending_age_days: int = 7,
    cap: int | None = None,
) -> list[dict]:
    """Combine queued rejudge entries with stale ``pending`` decisions.

    Phase-2 ``dream-judge-worker`` input. Two contributing streams:

    1. ``vault/.weave/rejudge_queue.jsonl`` — already in the right shape
       (``{decision_id, predecessor_decision_id?, queued_at, reason}``;
        legacy entries may carry ``source``/``enqueued_at`` keys —
        normalized below). Read-only here — the queue file is consumed
        by :func:`apply` (the hand-off point), not by the scan, so a
        scan-only run never loses entries.
    2. Decisions with ``prediction_match == 'pending'`` whose
       ``judged_at`` is missing or older than ``stale_pending_age_days``.
       Fabricated entries: ``predecessor_decision_id: null``,
       ``queued_at: <judged_at or "">``, ``reason: 'stale_pending'``.

    Capped at ``cap`` total (default: config ``dream.rejudge_cap``).
    Discovery uses the SQLite index (``json_extract`` on the frontmatter
    blob) — no vault file crawl.
    """
    from datetime import datetime, timedelta, timezone

    from thinkweave.core.indexer import Indexer

    if cap is None:
        cap = _rejudge_cap(cfg)
    if cap <= 0:
        # 0 disables the hand-off entirely — and must match apply's
        # consumption slice ([:0] removes nothing) so no entry is ever
        # consumed without being handed to the worker.
        return []

    out: list[dict] = []
    seen_ids: set[str] = set()

    # 1. Read the on-disk queue first (priority — these are explicit).
    queue_path = cfg.vault_root / ".weave" / "rejudge_queue.jsonl"
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
    window_hours: int | None = None,
) -> dict:
    """Composite knowledge-delta surface for the phase-2 ``dream-digest-worker``.

    Window defaults to config ``dream.knowledge_delta_hours`` (24).

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

    from thinkweave.core.indexer import Indexer
    from thinkweave.operations.prompts import recent_probe_pressure
    from thinkweave.acquisition.sources import registry as source_registry

    if window_hours is None:
        window_hours = int(getattr(cfg, "dream_knowledge_delta_hours", 24) or 24)

    now = datetime.now(timezone.utc)
    cutoff_dt = now - timedelta(hours=window_hours)
    cutoff_iso = cutoff_dt.isoformat()
    # For the catalyst log scan we compare against the entry's ``date``
    # field, which is YYYY-MM-DD only — use the day-level cutoff.
    cutoff_day = cutoff_dt.date().isoformat()

    # Pre-compute the source_type → temporal_grain lookup once. User-side
    # overlays at vault/.weave/source_types.yaml are honoured.
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

    # Behavioral current-focus — what the user is ACTUALLY working on, derived
    # from observed activity rather than a hand-maintained list (which rots —
    # PRIORITIES.yaml's focus.* drifted to a renamed-away project). Drives the
    # digest's "## Most actionable" ranking. Cross-slice → delta root.
    #   active_projects: projects with sessions in the last 14d (filled below,
    #                    needs the index) — what you're hands-on with now.
    #   probed_concepts: concepts under recent probe pressure — what you keep
    #                    asking about.
    # Both self-heal each cycle; a quiet period just yields empty lists, and
    # the narrative honestly reports "nothing intersects your recent focus".
    # Optional pin layer over the behavioral signal: PRIORITIES.yaml ``focus.*``
    # lists. Behavioral activity is the default ranking; pins are *appended*
    # (not prepended) so the automatic signal leads, but a deliberately-watched
    # yet currently-quiet project/concept still surfaces. Empty/missing
    # PRIORITIES → pure behavioral, exactly as before.
    from thinkweave.acquisition.sources.priorities import (
        apply_pins,
        focus_active_projects,
        focus_concepts,
        load_priorities,
    )

    priorities = load_priorities(getattr(cfg, "vault_root", None))
    pinned_projects = focus_active_projects(priorities)
    pinned_concepts = focus_concepts(priorities)
    window_days = cfg.salience_activity_window_days

    probed_concepts: list[str] = []
    try:
        pressure = recent_probe_pressure(cfg, window_days=window_days)  # {concept: count}
        probed_concepts = [c for c, _ in sorted(pressure.items(), key=lambda kv: -kv[1])[:10]]
    except Exception:  # noqa: BLE001 — probe load shouldn't kill scan
        probed_concepts = []
    # Pins are a floor: behavioural probe ranking leads, declared focus
    # concepts are appended so a watched-but-quiet concept still surfaces.
    probed_concepts = apply_pins(probed_concepts, pinned_concepts)
    active_focus = {"active_projects": [], "probed_concepts": probed_concepts}

    delta: dict = {
        "window_start": cutoff_iso,
        "window_end": now.isoformat(),
        "active_focus": active_focus,
        "concept": _new_grain_bucket(),
        "event": _new_grain_bucket(),
    }

    idx = Indexer(config=cfg)
    try:
        # 0. active_projects — behavioral focus: which projects saw sessions
        #    in the last 14d (mutates the active_focus dict already on delta).
        #    Excludes meta buckets (_unscoped/_personal/…). Self-heals: a
        #    renamed or abandoned project simply stops appearing.
        try:
            cutoff_window = (now - timedelta(days=window_days)).date().isoformat()
            proj_rows = idx.db.execute(
                "SELECT project, COUNT(*) AS c FROM notes "
                "WHERE type = 'session' AND date >= ? "
                "GROUP BY project ORDER BY c DESC",
                (cutoff_window,),
            ).fetchall()
            behavioral_projects = [
                row["project"] for row in proj_rows
                if row["project"] and not row["project"].startswith("_")
            ][:8]
            # Pins are a floor (see apply_pins): behavioural activity leads.
            behavioral_projects = apply_pins(behavioral_projects, pinned_projects)
            active_focus["active_projects"] = behavioral_projects
        except Exception:  # noqa: BLE001 — focus is best-effort
            pass

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
        # Concept hubs index as type='concept-hub' (their frontmatter
        # type) — the old type='note' filter matched zero hubs on real
        # vaults, silently emptying the concept digest's catalyst slice.
        # 'note' is kept for legacy skeletons without frontmatter type.
        # In-window entries come from the hub_log_entries projection — a
        # single indexed ``entry_date >= ?`` query (same freshness as the
        # body_text these were previously parsed out of); the notes query
        # only maps hub identity (thm-id / path stem) back to the note id
        # and path-derived kind the digest payload has always carried.
        hub_rows = idx.db.execute(
            "SELECT id, path FROM notes "
            "WHERE type = 'theme' "
            "   OR (type IN ('concept-hub', 'note') "
            "       AND path LIKE 'concepts/topics/%')"
        ).fetchall()
        recent_logs = _indexed_hub_logs(idx.db, min_date=cutoff_day)
        for row in hub_rows:
            rel_path = row["path"] or ""
            kind = "theme" if rel_path.startswith("themes/") else "concept"
            hub_ident = (
                Path(rel_path).stem
                if rel_path.startswith("concepts/topics/")
                else row["id"]
            )
            for entry in recent_logs.get(hub_ident, []):
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
            pressure = recent_probe_pressure(
                cfg, project="", window_days=_probe_window_days(cfg)
            )
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


def _collect_memory_seam(cfg: Config) -> dict:
    """Cheap dirty-diff of CC auto-memory against the durable seam map.

    Phase-2 ``dream-seam-worker`` input. Delegates to
    :mod:`thinkweave.synthesis.memory_seam` — walks the CC memory dirs,
    diffs each fact's content hash against ``vault/.weave/memory_seam.json``,
    and returns which facts need (re)judgment this cycle (``new`` /
    ``content_changed`` / ``prior_unresolved`` / ``recheck_due``), capped at
    ``seam.cap``. Embedding-free: twin resolution + verdicts live in the
    worker's turn, keeping the scan phase's API-free contract intact.

    The surface carries the cosine ``thresholds`` and file paths the worker
    needs so it can resolve twins (``weave_search(mode='similar')``), judge,
    and write back via ``weave seam commit`` without re-deriving config.
    """
    from thinkweave.synthesis import memory_seam

    facts = memory_seam.collect_cc_facts()
    state = memory_seam.load_state(cfg)
    surface = memory_seam.detect_dirty(
        facts,
        state,
        stale_age_days=int(getattr(cfg, "seam_stale_age_days", 30) or 30),
        recheck_days=int(getattr(cfg, "seam_recheck_days", 14) or 14),
        cap=int(getattr(cfg, "seam_cap", 20) or 0),
    )
    surface["thresholds"] = {
        "twin": float(getattr(cfg, "seam_cosine_twin", 0.70) or 0.70),
        "none": float(getattr(cfg, "seam_cosine_none", 0.55) or 0.55),
    }
    surface["report_path"] = str(memory_seam.report_path(cfg))
    surface["state_path"] = str(memory_seam.state_path(cfg))
    return surface


def _active_theme_meta(cfg: Config) -> dict[str, dict]:
    """thm-id → {slug, title, essence_excerpt, concepts} for ACTIVE themes.

    Shared by the theme dedup-pair and coarsen-cluster collectors so both
    judge from the same content slice. Non-active (resolved / merged)
    themes are excluded — they're frozen and never re-litigated.
    """
    from thinkweave.core.indexer import Indexer

    idx = Indexer(config=cfg)
    try:
        rows = idx.db.execute(
            "SELECT id, title, path, frontmatter, body_text FROM notes "
            "WHERE type = 'theme'"
        ).fetchall()
    finally:
        idx.close()

    meta: dict[str, dict] = {}
    for row in rows:
        try:
            fm = json.loads(row["frontmatter"] or "{}")
        except (json.JSONDecodeError, TypeError):
            fm = {}
        status = str(fm.get("status") or "active").split(":")[0]
        if status != "active":
            continue
        essence = _hub_essence(row["body_text"] or "")
        slug = Path(str(row["path"] or "")).stem
        concepts = fm.get("concepts") or []
        if not isinstance(concepts, list):
            concepts = [concepts]
        meta[row["id"]] = {
            "slug": slug,
            "title": row["title"] or slug,
            "essence_excerpt": essence[:300],
            "concepts": [str(c).lower() for c in concepts],
        }
    return meta


def _collect_theme_dup_candidates(
    cfg: Config, *, rejudge_pairs: bool = False
) -> list[dict]:
    """Near-duplicate ACTIVE theme pairs by embedding cosine (drift v2).

    Themes are embedded whole (essence + catalyst log ride in
    ``body_text``), so their cached vectors already encode content.
    All-pairs is fine at theme scale. Each candidate carries slugs,
    titles, essence excerpts, shared concepts, and a slug-token Jaccard —
    enough for the merge worker to elect a survivor without ``weave_read``
    round-trips. Pairs a past cycle ruled on are excluded via the
    maintenance-log history unless ``rejudge_pairs``.
    """
    from thinkweave.synthesis import geometry

    threshold = float(getattr(cfg, "dream_cosine_threshold", 0.8) or 0.8)

    meta = _active_theme_meta(cfg)

    vectors = {
        tid: vec
        for tid, vec in geometry.theme_vectors(cfg).items()
        if tid in meta
    }
    if len(vectors) < 2:
        return []

    judged = set() if rejudge_pairs else geometry.judged_pairs(cfg)

    def _tokens(slug: str) -> set[str]:
        return {t for t in re.split(r"[^a-z0-9]+", slug.lower()) if t}

    out: list[dict] = []
    for a, b, cos in geometry.cosine_pairs(vectors, threshold=threshold):
        if geometry.pair_key("theme", a, b) in judged:
            continue
        ma, mb = meta[a], meta[b]
        ta, tb = _tokens(ma["slug"]), _tokens(mb["slug"])
        union = ta | tb
        out.append(
            {
                "from_id": a,
                "to_id": b,
                "cosine": cos,
                "slugs": {a: ma["slug"], b: mb["slug"]},
                "titles": {a: ma["title"], b: mb["title"]},
                "essence_excerpts": {
                    a: ma["essence_excerpt"],
                    b: mb["essence_excerpt"],
                },
                "shared_concepts": sorted(
                    set(ma["concepts"]) & set(mb["concepts"])
                ),
                "slug_token_overlap": round(
                    len(ta & tb) / len(union), 3
                ) if union else 0.0,
            }
        )
    return out


def _collect_theme_coarsen_clusters(
    cfg: Config, *, rejudge_pairs: bool = False
) -> list[dict]:
    """Grain-coarsening clusters over ACTIVE themes (drift v2, N-ary).

    Tight near-cliques of themes that may track one over-split arc and
    collapse onto a single survivor. Mirrors the concept coarsen surface;
    members are thm-ids carrying slug / title / essence excerpt. Excludes
    clusters that touch a folded-away theme (overlap) or that were ruled
    DISTINCT (exact key) via ``geometry.judged_clusters``.
    """
    from thinkweave.synthesis import geometry

    coarsen_threshold = float(
        getattr(cfg, "dream_coarsen_threshold", 0.85) or 0.85
    )
    coarsen_cap = int(getattr(cfg, "dream_coarsen_cap", 3) or 0)
    coarsen_max_size = int(getattr(cfg, "dream_coarsen_max_size", 6) or 6)

    meta = _active_theme_meta(cfg)
    vectors = {
        tid: vec
        for tid, vec in geometry.theme_vectors(cfg).items()
        if tid in meta
    }
    if len(vectors) < 2:
        return []

    coarsened_members, distinct_keys = (
        (set(), set()) if rejudge_pairs else geometry.judged_clusters(cfg)
    )
    clusters = geometry.theme_clusters(
        vectors, threshold=coarsen_threshold, max_size=coarsen_max_size
    )
    clusters = [
        c
        for c in clusters
        if geometry.cluster_key("theme", c[0]) not in distinct_keys
        and not any(("theme", m) in coarsened_members for m in c[0])
    ]
    if coarsen_cap:
        clusters = clusters[:coarsen_cap]

    out: list[dict] = []
    for members, avg, mn in clusters:
        shared = (
            set.intersection(*[set(meta[t]["concepts"]) for t in members])
            if members
            else set()
        )
        out.append(
            {
                "members": members,
                "avg_cosine": avg,
                "min_cosine": mn,
                "slugs": {t: meta[t]["slug"] for t in members},
                "titles": {t: meta[t]["title"] for t in members},
                "essence_excerpts": {
                    t: meta[t]["essence_excerpt"] for t in members
                },
                "shared_concepts": sorted(shared),
            }
        )
    return out


def scan(
    cfg: Config,
    *,
    project: str = "",
    promotion_cap: int | None = None,
    promotion_threshold: int | None = None,
    essence_cap: int | None = None,
    rejudge_pairs: bool = False,
) -> DreamCycleScan:
    """Compose a read-only action plan from three vault-global scans.

    1. **drift pairs (v2)** — union of string near-dupes
       (``find_near_duplicates`` → ``filter_drift_candidates``) and
       centroid-cosine pairs ≥ ``dream_cosine_threshold`` (synonyms with
       zero string overlap). Already-judged pairs are excluded via the
       maintenance-log history (pass ``rejudge_pairs=True`` to re-surface
       them); survivors are ranked by cosine descending, capped at
       ``dream_drift_cap``, and shipped with evidence packets.
    2. **promotion candidates** — ``proposed_concepts`` at ``count ≥
       promotion_threshold`` (default: config ``dream.promotion_threshold``,
       5), filtered through ``filter_promotion_candidates`` (drops
       domain-paths, generic process terms, underscore-bearing leakage),
       sorted by count desc, capped at ``promotion_cap`` (default: config
       ``dream.promotion_cap``, 20).
    3. **theme cluster signals** — ``detect_signals``: clusters of recent
       event-grain sources sharing concepts, each enriched with raw
       ``proposed_theme:`` stamps and overlapping active themes so the
       LLM turn can mint or extend.

    Theme *lifecycle* is intentionally absent — no dormant/resolved
    detection, no status changes. Themes change status only by hand.

    Steps are wrapped: a failure in one is recorded in ``errors``; the
    rest still run. Returns :class:`DreamCycleScan`.
    """
    if promotion_cap is None:
        promotion_cap = int(getattr(cfg, "dream_promotion_cap", 20) or 0)
    if promotion_threshold is None:
        promotion_threshold = int(
            getattr(cfg, "dream_promotion_threshold", 5) or 5
        )

    result = DreamCycleScan(
        cycle_id=_new_cycle_id(),
        project=project,
        promotion_cap=promotion_cap,
    )

    # 1. drift pairs (v2) ---------------------------------------------------
    # Pair pool = string near-dupes (typo catcher — typo'd concepts tag too
    # few notes for a stable centroid) ∪ centroid-cosine pairs ≥ threshold
    # (synonym catcher — finds pairs with zero string overlap). Pairs a past
    # cycle already ruled on (merged OR distinct, read back from
    # maintenance.jsonl) are excluded so the pool drains instead of
    # re-litigating the lexical head every night. Ranked by cosine
    # descending, capped by ``dream_drift_cap``. Each survivor carries an
    # evidence packet (domains, same_domain, counts, co-occurrence, sample
    # titles) so the merge worker judges from contents without extra
    # ``weave_read`` round-trips.
    _t = time.perf_counter()
    try:
        from thinkweave.synthesis import geometry
        from thinkweave.synthesis.concepts import (
            filter_drift_candidates,
            find_near_duplicates,
        )

        judged = set() if rejudge_pairs else geometry.judged_pairs(cfg)
        threshold = float(getattr(cfg, "dream_cosine_threshold", 0.8) or 0.8)
        drift_cap = int(getattr(cfg, "dream_drift_cap", 15) or 0)

        # String generator — full pair list (the old [:5] advisory slice
        # starved everything past the lexical head; see 2026-06-10 audit).
        from thinkweave.core.indexer import Indexer

        idx = Indexer(config=cfg)
        try:
            concept_rows = idx.db.execute(
                """
                SELECT DISTINCT concept FROM note_concepts
                WHERE (? = '' OR note_id IN (
                    SELECT id FROM notes WHERE project = ?
                ))
                """,
                (project, project),
            ).fetchall()
        finally:
            idx.close()
        all_concepts = [r["concept"] for r in concept_rows]
        string_pairs = filter_drift_candidates(
            find_near_duplicates(all_concepts)
        )

        # Cosine generator — usage centroids over the embedding cache.
        centroids = geometry.concept_centroids(cfg)
        cos_pairs = geometry.cosine_pairs(centroids, threshold=threshold)

        pool: dict[frozenset, tuple[str, str, float | None, str]] = {}
        for a, b, cos in cos_pairs:
            pool[frozenset((a, b))] = (a, b, cos, f"cosine {cos}")
        for a, b, reason in string_pairs:
            key = frozenset((a, b))
            if key in pool:
                pa, pb, pcos, preason = pool[key]
                pool[key] = (pa, pb, pcos, f"{preason}; {reason}")
            else:
                # Attach the centroid cosine as evidence even below
                # threshold — the worker should see "0.31" on a string
                # pair and smell a homonym.
                cos = None
                va, vb = centroids.get(a), centroids.get(b)
                if va is not None and vb is not None and len(va) == len(vb):
                    cos = round(geometry._dot(va, vb), 4)
                pool[key] = (a, b, cos, reason)

        surviving = [
            row
            for key, row in pool.items()
            if geometry.pair_key("concept", row[0], row[1]) not in judged
        ]
        surviving.sort(
            key=lambda r: (r[2] is None, -(r[2] or 0.0), r[0])
        )
        if drift_cap:
            surviving = surviving[:drift_cap]
        result.drift_pairs = geometry.build_concept_evidence(cfg, surviving)

        # Grain-coarsening clusters — N-ary near-cliques over the SAME
        # centroids (no recompute). Stricter threshold than synonym merge.
        # Exclude clusters that touch a folded-away term (overlap) or that
        # were already ruled DISTINCT (exact key) — the anti-oscillation
        # guard from geometry.judged_clusters.
        coarsen_threshold = float(
            getattr(cfg, "dream_coarsen_threshold", 0.85) or 0.85
        )
        coarsen_cap = int(getattr(cfg, "dream_coarsen_cap", 3) or 0)
        coarsen_max_size = int(getattr(cfg, "dream_coarsen_max_size", 6) or 6)
        coarsened_members, distinct_keys = (
            (set(), set()) if rejudge_pairs else geometry.judged_clusters(cfg)
        )
        clusters = geometry.concept_clusters(
            centroids, threshold=coarsen_threshold, max_size=coarsen_max_size
        )
        clusters = [
            c
            for c in clusters
            if geometry.cluster_key("concept", c[0]) not in distinct_keys
            and not any(("concept", m) in coarsened_members for m in c[0])
        ]
        if coarsen_cap:
            clusters = clusters[:coarsen_cap]
        result.coarsen_clusters = geometry.build_concept_cluster_evidence(
            cfg, clusters
        )
    except Exception as e:  # noqa: BLE001 — best-effort scan step
        result.errors.append(f"drift: {e}")
    finally:
        result.timings["drift"] = time.perf_counter() - _t

    # 2. promotion candidates ---------------------------------------------
    _t = time.perf_counter()
    try:
        from thinkweave.core.indexer import Indexer
        from thinkweave.synthesis.concepts import (
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
        from thinkweave.synthesis.theme_candidates import detect_signals

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

    # 3b. theme dup candidates (drift v2, theme family) -------------------
    # All-pairs cosine over the active themes' cached note embeddings —
    # n(themes) is small, so this is cheap. History-excluded like concept
    # pairs. The merge worker elects the survivor; apply's ``theme_merges``
    # step runs ``merge_theme_into`` (fold + repoint + tombstone + seam).
    _t = time.perf_counter()
    try:
        result.theme_dup_candidates = _collect_theme_dup_candidates(
            cfg, rejudge_pairs=rejudge_pairs
        )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"theme_dup_candidates: {e}")
    finally:
        result.timings["theme_dup_candidates"] = time.perf_counter() - _t

    # 3c. theme coarsen clusters (drift v2, N-ary, theme family) ----------
    _t = time.perf_counter()
    try:
        result.theme_coarsen_clusters = _collect_theme_coarsen_clusters(
            cfg, rejudge_pairs=rejudge_pairs
        )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"theme_coarsen_clusters: {e}")
    finally:
        result.timings["theme_coarsen_clusters"] = time.perf_counter() - _t

    # 4. probe questions --------------------------------------------------
    # Collect recent probe-classified prompt QUESTIONS (flat, deduped)
    # over the configured window (``dream.probe_window_days``, default 14
    # days). The probe-distillation worker reasons over the raw questions
    # — gating operational/meta ones, tying clean ontology concepts, and
    # restating them into workable research queries — then emits
    # ``priority_signals`` whose queue items thread the verbatim probes.
    _t = time.perf_counter()
    try:
        from thinkweave.operations.prompts import recent_probe_questions

        result.recent_probes = recent_probe_questions(
            cfg, project=project, window_days=_probe_window_days(cfg)
        )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"recent_probes: {e}")
    finally:
        result.timings["recent_probes"] = time.perf_counter() - _t

    # 5. essence candidates ------------------------------------------------
    # Hubs (themes + concept hubs) whose essence deserves worker attention,
    # pre-loaded with essence + recent catalysts. Phase-1
    # ``dream-essence-worker`` reads this surface directly — no per-hub
    # ``weave_read`` round trip needed. Vault-wide (hubs are not
    # per-project, matching how cluster signals are scanned).
    _t = time.perf_counter()
    try:
        if essence_cap is None:
            essence_cap = int(getattr(cfg, "dream_essence_cap", 12) or 0)
        result.essence_candidates = _collect_essence_candidates(
            cfg, cap=essence_cap
        )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"essence_candidates: {e}")
    finally:
        result.timings["essence_candidates"] = time.perf_counter() - _t

    # 5b. theme log gaps ----------------------------------------------------
    # Directly-filed sources (relates_to: thm-X stamped at create time)
    # that never produced a catalyst-log entry. The theme worker distills
    # them into normal ``theme_extensions`` items.
    _t = time.perf_counter()
    try:
        result.theme_log_gaps = _collect_theme_log_gaps(cfg)
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"theme_log_gaps: {e}")
    finally:
        result.timings["theme_log_gaps"] = time.perf_counter() - _t

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

    # 7b. seam-link queue (phase-2 dream-seam-link-worker input) ----------
    # Peek only — the worker drains for real via `weave hubs apply-linkage`.
    # Items enqueued by THIS cycle's apply (merges land after the scan)
    # are picked up by the orchestrator re-checking the queue post-apply
    # (commands/dream.md step 2.1) or, at worst, by the next cycle.
    _t = time.perf_counter()
    try:
        from thinkweave.operations import seam_link_queue as _slq

        seam_cap = int(getattr(cfg, "dream_seam_link_cap", 10) or 0)
        items = _slq.peek(cfg)
        result.seam_link_queue = items[:seam_cap] if seam_cap else items
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"seam_link_queue: {e}")
    finally:
        result.timings["seam_link_queue"] = time.perf_counter() - _t

    # 8. knowledge delta (phase-2 dream-digest-worker input) -------------
    _t = time.perf_counter()
    try:
        result.knowledge_delta = _collect_knowledge_delta(cfg)
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"knowledge_delta: {e}")
    finally:
        result.timings["knowledge_delta"] = time.perf_counter() - _t

    # 9. memory seam (phase-2 dream-seam-worker input) -------------------
    # Cheap, embedding-free: diff CC auto-memory against the durable map.
    _t = time.perf_counter()
    try:
        result.memory_seam = _collect_memory_seam(cfg)
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"memory_seam: {e}")
    finally:
        result.timings["memory_seam"] = time.perf_counter() - _t

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
        "coarsen_clusters": len(result.coarsen_clusters),
        "theme_coarsen_clusters": len(result.theme_coarsen_clusters),
        "promotion_candidates": len(result.promotion_candidates),
        "theme_cluster_signals": len(result.theme_cluster_signals),
        "theme_dup_candidates": len(result.theme_dup_candidates),
        "recent_probes": len(result.recent_probes),
        "essence_candidates": len(result.essence_candidates),
        "theme_log_gaps": len(result.theme_log_gaps),
        "unwrapped_sessions": len(result.unwrapped_sessions),
        "rejudge_queue": len(result.rejudge_queue),
        "seam_link_queue": len(result.seam_link_queue),
        "memory_seam": {
            "dirty": len((result.memory_seam or {}).get("dirty", [])),
            "removed": len((result.memory_seam or {}).get("removed", [])),
            "carried": (result.memory_seam or {}).get("carried_count", 0),
        },
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
    "coarsenings",
    "theme_coarsenings",
    "distinct_clusters",
    "promotions",
    "theme_mints",
    "theme_extensions",
    "theme_merges",
    "distinct_pairs",
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
    # Grain coarsening (drift v2, N-ary). COLLAPSE a tight near-clique of
    # fine concepts onto ``target`` (a member, an existing canonical term
    # in another domain, or a NEW term + ``target_domain`` when
    # ``target_is_new``). Apply folds each member hub into the target,
    # writes the ontology when new, and snapshots ``member`` provenance
    # for reversible re-split.
    "coarsenings": frozenset({
        "members",
        "target",
        "target_domain",
        "target_is_new",
        "reason",
        "min_cosine",
    }),
    # Theme arc coarsening: collapse N over-split themes into one
    # ``survivor_id`` via ``merge_theme_into`` (fold + tombstone). No
    # ontology write — themes have no ontology.
    "theme_coarsenings": frozenset({"members", "survivor_id", "reason"}),
    # N-ary leave-with-memory verdict: a cluster the worker judged to be
    # genuinely-distinct grains. Recorded (exact member set) so it never
    # re-surfaces; reopen via ``weave dream scan --rejudge``.
    "distinct_clusters": frozenset({"kind", "members", "reason", "min_cosine"}),
    "promotions": frozenset({"concept", "domain", "reason"}),
    "theme_mints": frozenset({
        "slug",
        "title",
        "essence",
        "source_ids",
        "concepts",
        "candidacy",
        "project",
        "parent",
        "catalysts",
    }),
    "theme_extensions": frozenset({
        "theme_id",
        "source_ids",
        "catalysts",
        "reason",
    }),
    # Theme dedup (drift v2): survivor = ``to_id``, elected by the merge
    # worker from the scan's ``theme_dup_candidates``. Apply runs
    # ``merge_theme_into`` (fold + repoint + tombstone + seam enqueue).
    "theme_merges": frozenset({"from_id", "to_id", "reason"}),
    # Leave-with-memory verdicts: pairs the merge worker judged NOT
    # duplicates. No mutation — recorded in the maintenance.jsonl
    # ``verdicts`` block (+ dream report) so future scans stop
    # re-surfacing them. ``kind`` ∈ {concept, theme}; ``pair`` is the
    # two names/ids.
    "distinct_pairs": frozenset({"kind", "pair", "reason", "cosine"}),
    "essence_rewrites": frozenset({
        "hub_kind",
        "theme_id",
        "concept",
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
# ``probes`` carries the probe texts that drove the signal (≤3, copied by
# the priority worker from the scan's ``recent_probes``) so `/drain` can
# tighten its search queries to the user's actual questions.
_VALID_QUEUE_ITEM_KEYS: frozenset[str] = frozenset({
    "source_type",
    "title",
    "concept",
    "source",
    "url",
    "probes",
})

# Sub-key allowlist for per-source catalyst distillations carried by
# ``theme_mints`` / ``theme_extensions`` items. ``text`` is the 1-2
# sentence artifact the dream theme worker composed for that source;
# ``flag`` is the catalyst-log flag (new/agrees/contradicts/extends).
_VALID_CATALYST_ITEM_KEYS: frozenset[str] = frozenset({
    "source_id",
    "text",
    "flag",
    "ref",
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
            # Nested catalysts list on theme mints/extensions
            if top_key in ("theme_mints", "theme_extensions"):
                cats = item.get("catalysts")
                if cats is not None and not isinstance(cats, list):
                    warnings.append(
                        f"plan[{top_key!r}][{i}].catalysts is not a "
                        f"list: {type(cats).__name__}"
                    )
                elif isinstance(cats, list):
                    for j, cat in enumerate(cats):
                        if not isinstance(cat, dict):
                            warnings.append(
                                f"plan[{top_key!r}][{i}].catalysts[{j}] "
                                f"is not a dict: {type(cat).__name__}"
                            )
                            continue
                        for ck in cat.keys():
                            if ck not in _VALID_CATALYST_ITEM_KEYS:
                                warnings.append(
                                    f"unknown sub-key {ck!r} in "
                                    f"plan[{top_key!r}][{i}].catalysts[{j}]"
                                )

    return warnings


class PlanValidationError(ValueError):
    """Raised when ``apply(strict=True)`` finds unknown plan / item keys.

    Carries the full warning list so the orchestrator (and any operator
    running ``weave dream apply`` interactively) can see every drift point
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
    theme_merges_applied: int = 0
    distinct_pairs_recorded: int = 0
    # Grain coarsening (drift v2, N-ary) — concept cluster collapses, theme
    # cluster collapses, and N-ary distinct rulings recorded this cycle.
    coarsenings_applied: int = 0
    theme_coarsenings_applied: int = 0
    distinct_clusters_recorded: int = 0
    # Deterministic staleness auto-resolve (C2) — active themes flipped to
    # ``resolved`` because their newest catalyst entry aged past
    # ``theme_resolve_after_days``.
    themes_resolved: int = 0
    seams_enqueued: int = 0
    # Rejudge-queue hand-off consumption — entries removed from
    # ``.weave/rejudge_queue.jsonl`` because the scan surfaced them into the
    # phase-2 ``dream-judge-worker``'s prompt (apply is the hand-off
    # point; entries beyond the scan cap survive for the next cycle).
    rejudge_consumed: int = 0
    # Evidence-gated supersession flips this cycle — predecessors a
    # ``supersedes:`` declaration enqueued, re-judged by the structural
    # blame-survival judge and flipped to ``superseded`` (the headless/
    # deferred counterpart of wrap-finalize's flip).
    supersession_flips: int = 0
    ontology_grew: bool = False
    indexed: int = 0
    removed: int = 0
    edges: int = 0
    # Outcome id-lists — the apply-result contract the phase-2 digest
    # worker reads (commands/dream.md step 2.2 fills
    # ``theme_mutations_this_cycle`` from these): ``theme_mints`` carries
    # ``{theme_id, slug, essence}`` per minted theme; ``theme_extensions``
    # carries ``{theme_id, added_source_ids, added_concept}``. Counter
    # twins (``themes_minted`` / ``themes_extended``) stay for the
    # maintenance-log summary.
    theme_mints: list = field(default_factory=list)
    theme_extensions: list = field(default_factory=list)
    # Verdict memory (drift v2) — the *applied* ontology rulings of this
    # cycle, written into the maintenance.jsonl ``verdicts`` block that
    # ``geometry.judged_pairs`` reads back to keep judged pairs from
    # re-surfacing. Applied-only on purpose: an errored merge stays
    # eligible for retry next cycle.
    applied_merges: list = field(default_factory=list)
    applied_theme_merges: list = field(default_factory=list)
    recorded_distinct_pairs: list = field(default_factory=list)
    # N-ary coarsening verdicts (drift v2). ``recorded_coarsenings`` items
    # carry the reversibility snapshot — ``{members, target, target_was_new,
    # member_note_ids, fold_dates, winner_citations_pre_fold, reason,
    # min_cosine}`` — so ``weave dream revert-coarsen`` can re-split exactly.
    recorded_coarsenings: list = field(default_factory=list)
    recorded_theme_coarsenings: list = field(default_factory=list)
    recorded_distinct_clusters: list = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    log_path: str = ""
    report_path: str = ""  # markdown report at vault/.weave/dream_reports/<cycle_id>.md

    def as_dict(self) -> dict:
        return asdict(self)

    def log_entry(self, plan: dict) -> dict:
        """Build the maintenance.jsonl line for this cycle.

        Captures intent (the plan) alongside outcome (counts + errors).
        Lets a human grep the log later and answer "what did the cycle
        do on day X, and was anything left unfinished?". The ``verdicts``
        block doubles as the ontology judgment memory — the scan's
        ``geometry.judged_pairs`` reads merged/distinct rulings back from
        here, which is what makes the drift pool drain (no separate
        ledger file, by design).
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
                "theme_merges": self.theme_merges_applied,
                "distinct_pairs": self.distinct_pairs_recorded,
                "coarsenings": self.coarsenings_applied,
                "theme_coarsenings": self.theme_coarsenings_applied,
                "distinct_clusters": self.distinct_clusters_recorded,
                "themes_resolved": self.themes_resolved,
                "seams_enqueued": self.seams_enqueued,
                "rejudge_consumed": self.rejudge_consumed,
                "supersession_flips": self.supersession_flips,
                "essence_rewrites": self.essence_rewrites_applied,
                "priority_signals_enqueued": self.priority_signals_enqueued,
                "priority_signals_logged": self.priority_signals_logged,
                "ontology_grew": self.ontology_grew,
            },
            "verdicts": {
                "merges": list(self.applied_merges),
                "theme_merges": list(self.applied_theme_merges),
                "distinct_pairs": list(self.recorded_distinct_pairs),
                "coarsenings": list(self.recorded_coarsenings),
                "theme_coarsenings": list(self.recorded_theme_coarsenings),
                "distinct_clusters": list(self.recorded_distinct_clusters),
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


def _auto_resolve_stale_themes(cfg: Config, result: "DreamCycleResult") -> None:
    """Flip ``active`` themes with no recent catalyst entry to ``resolved``.

    Deterministic (no LLM). The newest ``hub_log_entries.entry_date`` for a
    theme is its activity clock; an empty stub falls back to its created
    ``date``. Past ``theme_resolve_after_days`` (config, default 60; ``0``
    disables) the theme is frozen. Mutates the file frontmatter + registry;
    the caller's tail rebuild reindexes. Reversible by hand (flip back to
    active) and self-correcting (any new entry resets the clock).
    """
    resolve_after = int(getattr(cfg, "theme_resolve_after_days", 60) or 0)
    if resolve_after <= 0:
        return

    from thinkweave.core.indexer import Indexer
    from thinkweave.core.vault import parse_frontmatter
    from thinkweave.synthesis import theme_registry
    from thinkweave.synthesis.hub import set_frontmatter_keys

    today = datetime.now(timezone.utc).date()

    idx = Indexer(config=cfg)
    try:
        theme_rows = idx.db.execute(
            "SELECT id, path, frontmatter FROM notes WHERE type = 'theme'"
        ).fetchall()
        for row in theme_rows:
            try:
                fm = json.loads(row["frontmatter"] or "{}")
            except (json.JSONDecodeError, TypeError):
                fm = {}
            if str(fm.get("status") or "active").split(":")[0] != "active":
                continue
            last = idx.db.execute(
                "SELECT MAX(entry_date) AS d FROM hub_log_entries "
                "WHERE hub_id = ?",
                (row["id"],),
            ).fetchone()
            ref = (last["d"] if last else None) or str(fm.get("date") or "")
            ref_date = _parse_iso_date(ref)
            if ref_date is None:
                continue  # unparseable → leave it alone
            if (today - ref_date).days <= resolve_after:
                continue
            theme_path = cfg.vault_root / str(row["path"])
            if not theme_path.exists():
                continue
            # The index status (``fm`` above) can be stale within a single
            # apply(): theme merges run earlier in the same cycle with
            # ``rebuild_index=False``, so a just-tombstoned loser still shows
            # ``active`` here. Re-read the authoritative on-disk status before
            # flipping — never clobber a fresh ``merged-into:`` (or any
            # non-active) stamp written this cycle.
            disk_fm, _ = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
            if str(disk_fm.get("status") or "active").split(":")[0] != "active":
                continue
            set_frontmatter_keys(theme_path, {"status": "resolved"})
            try:
                theme_registry.upsert(cfg, row["id"], {"status": "resolved"})
            except Exception as e:  # noqa: BLE001 — registry drift is non-fatal
                result.errors.append(
                    f"auto_resolve {row['id']}: registry upsert: {e}"
                )
            result.themes_resolved += 1
    finally:
        idx.close()


def _parse_iso_date(value: str):
    """Best-effort ``YYYY-MM-DD`` / ISO-datetime → ``date`` (None on failure)."""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def apply(
    cfg: Config,
    *,
    plan: dict,
    project: str = "",
    cycle_id: str | None = None,
    strict: bool = True,
    force_coarsen: bool = False,
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
                            "source": "dream-priority-signal",
                            "probes": ["How does vLLM decide batch size "
                                       "under mixed sequence lengths?"]},
             "reason": "User asked 4× in 14d; vault has no source coverage."},
            {"concept": "embeddings", "probe_count": 6,
             "action": "log",
             "reason": "Asked repeatedly but already well-sourced — note for the user."},
            ...
          ],
        }

    Order matters: merges → promotions → theme mints → theme extensions →
    theme merges → distinct-pair recording → rejudge-queue hand-off
    consumption → ONE index rebuild → maintenance.jsonl append. Each step
    is wrapped; failure in one is recorded in ``errors`` and the rest
    still run.

    Two drift-v2 keys ride alongside (2026-06-11): ``theme_merges``
    (``{from_id, to_id, reason}`` — ``merge_theme_into`` fold + repoint +
    tombstone + seam enqueue) and ``distinct_pairs`` (``{kind, pair,
    reason, cosine}`` — no mutation; recorded into the maintenance-log
    ``verdicts`` block that the next scan reads back as judgment memory).
    Concept merges fold the losing hub into the winner and archive the
    husk with a ``merged-into:`` stamp — never delete.

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
    # The losing hub is FOLDED into the winner and archived with a
    # ``merged-into:`` tombstone (fold_concept_hub_on_merge) — never
    # deleted; its catalyst log is knowledge, not residue.
    _t = time.perf_counter()
    try:
        from thinkweave.synthesis.concepts import (
            fold_concept_hub_on_merge,
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
                    fold_stats = fold_concept_hub_on_merge(cfg, from_c, to_c)
                    if fold_stats.get("error"):
                        result.errors.append(
                            f"merge {from_c}→{to_c} hub fold: "
                            f"{fold_stats['error']}"
                        )
                    if fold_stats.get("fold_dates"):
                        result.seams_enqueued += 1
                    result.merges_applied += 1
                    result.applied_merges.append(
                        {
                            "from": from_c,
                            "to": to_c,
                            "reason": str(m.get("reason") or ""),
                        }
                    )
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
        from thinkweave.synthesis.concepts import promote_proposed_concept

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
        from thinkweave.synthesis.theme_candidates import mint_theme_from_signal

        for tm in plan.get("theme_mints") or []:
            try:
                slug = tm.get("slug") or ""
                source_ids = tm.get("source_ids") or []
                if not slug or not source_ids:
                    result.errors.append(
                        f"theme_mint: missing slug/source_ids in {tm}"
                    )
                    continue
                # Cheap mint (2026-06-13 symmetry closure): essence is
                # optional. An empty/short essence is NOT a rejection any
                # more — the stub gets the placeholder and the dual-family
                # essence worker composes it on a later cycle (same as a
                # freshly-promoted concept hub). The old ``<5 words`` guard
                # that re-queued the whole cluster is gone.
                essence = (tm.get("essence") or "").strip()
                mint_path = mint_theme_from_signal(
                    cfg,
                    slug=slug,
                    essence=essence,
                    cluster_source_ids=list(source_ids),
                    cluster_concepts=list(tm.get("concepts") or []),
                    candidacy=tm.get("candidacy") or "inferred-from-signal",
                    project=tm.get("project") or "",
                    parent=tm.get("parent") or "",
                    title=tm.get("title") or "",
                    catalysts=tm.get("catalysts") or None,
                    rebuild_index=False,
                )
                result.themes_minted += 1
                # Apply-result contract (commands/dream.md step 2.2 /
                # dream-digest-worker): surface the minted id, not just
                # the counter. The helper returns the file path; the
                # thm-id lives in its frontmatter.
                theme_id = ""
                try:
                    from thinkweave.core.vault import parse_frontmatter

                    fm_mint, _ = parse_frontmatter(
                        mint_path.read_text(encoding="utf-8")
                    )
                    theme_id = str(fm_mint.get("id") or "")
                except Exception as e:  # noqa: BLE001
                    result.errors.append(
                        f"theme_mint {slug}: minted but id unread ({e})"
                    )
                result.theme_mints.append(
                    {"theme_id": theme_id, "slug": slug, "essence": essence}
                )
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
        from thinkweave.synthesis.theme_candidates import (
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
                    catalysts=tx.get("catalysts") or None,
                    rebuild_index=False,
                )
                if n:
                    result.themes_extended += 1
                    # Same id-list contract as theme_mints. The plan's
                    # source_ids are the hand-off list; already-cited
                    # sources were skipped by the helper (n ≤ len).
                    result.theme_extensions.append(
                        {
                            "theme_id": theme_id,
                            "added_source_ids": list(source_ids),
                            "added_concept": "",
                        }
                    )
            except Exception as e:  # noqa: BLE001
                result.errors.append(
                    f"theme_extend {tx.get('theme_id', '?')}: {e}"
                )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"theme_extensions: {e}")
    finally:
        result.timings["theme_extensions"] = time.perf_counter() - _t

    # 3b2. theme merges (drift v2) ----------------------------------------
    # Duplicate-theme folds elected by the merge worker from the scan's
    # ``theme_dup_candidates``. Each item is {from_id, to_id, [reason]};
    # ``merge_theme_into`` folds the catalyst log + cites, repoints
    # relates_to, tombstones the loser (``merged-into:`` — file stays on
    # disk, reversible), updates the registry, and enqueues the survivor
    # for the phase-2 seam-link pass. Runs after extensions so a theme
    # extended this cycle still merges cleanly.
    _t = time.perf_counter()
    try:
        from thinkweave.synthesis.theme_candidates import merge_theme_into

        for tm in plan.get("theme_merges") or []:
            try:
                from_id = (tm.get("from_id") or "").strip()
                to_id = (tm.get("to_id") or "").strip()
                if not from_id or not to_id or from_id == to_id:
                    result.errors.append(
                        f"theme_merge: bad from_id/to_id in {tm}"
                    )
                    continue
                m_stats = merge_theme_into(
                    cfg,
                    from_id=from_id,
                    to_id=to_id,
                    rebuild_index=False,
                )
                if m_stats.get("fold_dates"):
                    result.seams_enqueued += 1
                result.theme_merges_applied += 1
                result.applied_theme_merges.append(
                    {
                        "from_id": from_id,
                        "to_id": to_id,
                        "reason": str(tm.get("reason") or ""),
                    }
                )
            except Exception as e:  # noqa: BLE001
                result.errors.append(
                    f"theme_merge {tm.get('from_id', '?')}"
                    f"→{tm.get('to_id', '?')}: {e}"
                )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"theme_merges: {e}")
    finally:
        result.timings["theme_merges"] = time.perf_counter() - _t

    # 3b3. distinct pairs — verdict memory, no mutation -------------------
    # Pairs the merge worker judged NOT duplicates. Recording them in the
    # result (→ maintenance.jsonl ``verdicts`` block) is the whole action:
    # the next scan's ``geometry.judged_pairs`` reads them back and stops
    # re-surfacing the pair. This is what makes the drift pool drain.
    try:
        for dp in plan.get("distinct_pairs") or []:
            pair = dp.get("pair") or []
            if not isinstance(pair, list) or len(pair) != 2:
                result.errors.append(f"distinct_pair: bad pair in {dp}")
                continue
            result.recorded_distinct_pairs.append(
                {
                    "kind": str(dp.get("kind") or "concept"),
                    "pair": [str(pair[0]), str(pair[1])],
                    "reason": str(dp.get("reason") or ""),
                    "cosine": dp.get("cosine"),
                }
            )
            result.distinct_pairs_recorded += 1
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"distinct_pairs: {e}")

    # 3b4. concept coarsenings (drift v2, N-ary) --------------------------
    # COLLAPSE a tight near-clique of fine concepts onto one coarser
    # ``target``. Runs AFTER pairwise merges (shares the alias map + note
    # frontmatter). Each member folds into the target via the same
    # merge_concept_in_notes + alias + fold_concept_hub_on_merge path as a
    # pairwise merge; a NEW target is written to the ontology first. Before
    # any mutation we snapshot ``member_note_ids`` + the winner's pre-fold
    # citations so ``weave dream revert-coarsen`` can re-split exactly even
    # after the seam-link worker clears the transient fold stamps. The
    # ``dream_coarsen_apply`` gate (or ``force_coarsen`` from /tighten)
    # decides whether the fold runs; with the gate off, nothing is recorded
    # so the cluster re-surfaces for the on-demand front door.
    _t = time.perf_counter()
    do_coarsen = force_coarsen or bool(getattr(cfg, "dream_coarsen_apply", True))
    try:
        coarsenings = plan.get("coarsenings") or []
        if coarsenings and not do_coarsen:
            result.errors.append(
                f"coarsenings: surfaced {len(coarsenings)} cluster(s) but "
                "dream_coarsen_apply=false — apply via /tighten"
            )
        elif coarsenings:
            from thinkweave.core.indexer import Indexer
            from thinkweave.synthesis.concept_hub import (
                _safe_hub_maps,
                concept_hub_path,
                parse_concept_hub,
            )
            from thinkweave.synthesis.concepts import (
                fold_concept_hub_on_merge,
                load_aliases,
                merge_concept_in_notes,
                promote_proposed_concept,
                save_aliases,
            )

            _, _, path_to_id = _safe_hub_maps(cfg)
            idx = Indexer(config=cfg)
            aliases = load_aliases(cfg)
            did_fold = False
            try:
                for c in coarsenings:
                    try:
                        target = (c.get("target") or "").lower().strip()
                        members = [
                            str(m).lower().strip()
                            for m in (c.get("members") or [])
                            if str(m).strip()
                        ]
                        losers = [m for m in members if m != target]
                        if not target or not losers:
                            result.errors.append(
                                f"coarsen: empty target/members in {c}"
                            )
                            continue
                        target_is_new = bool(c.get("target_is_new"))
                        target_domain = (
                            c.get("target_domain") or ""
                        ).lower().strip()

                        # Reversibility snapshot — BEFORE any mutation.
                        member_note_ids = {
                            m: [
                                r["note_id"]
                                for r in idx.db.execute(
                                    "SELECT note_id FROM note_concepts "
                                    "WHERE concept = ?",
                                    (m,),
                                )
                            ]
                            for m in losers
                        }
                        hp = concept_hub_path(cfg, target)
                        winner_citations = sorted(
                            parse_concept_hub(
                                hp, path_to_id=path_to_id
                            ).cited_ids
                        )

                        if target_is_new and target_domain:
                            stats = promote_proposed_concept(
                                cfg,
                                target,
                                domain=target_domain,
                                rebuild_index=False,
                            )
                            if stats.get("ontology_updated"):
                                result.ontology_grew = True

                        fold_dates_all: list[str] = []
                        for m in losers:
                            merge_concept_in_notes(cfg.vault_root, m, target)
                            existing = aliases.get(target, [])
                            if m not in existing:
                                existing.append(m)
                            if m in aliases:
                                for old in aliases.pop(m):
                                    if old != target and old not in existing:
                                        existing.append(old)
                            aliases[target] = existing
                            fold_stats = fold_concept_hub_on_merge(cfg, m, target)
                            if fold_stats.get("error"):
                                result.errors.append(
                                    f"coarsen {m}→{target} fold: "
                                    f"{fold_stats['error']}"
                                )
                            if fold_stats.get("fold_dates"):
                                fold_dates_all.extend(fold_stats["fold_dates"])
                                result.seams_enqueued += 1
                            did_fold = True

                        result.recorded_coarsenings.append(
                            {
                                "members": members,
                                "target": target,
                                "target_was_new": target_is_new,
                                "member_note_ids": member_note_ids,
                                "winner_citations_pre_fold": winner_citations,
                                "fold_dates": sorted(set(fold_dates_all)),
                                "reason": str(c.get("reason") or ""),
                                "min_cosine": c.get("min_cosine"),
                            }
                        )
                        result.coarsenings_applied += 1
                    except Exception as e:  # noqa: BLE001
                        result.errors.append(
                            f"coarsen {c.get('target', '?')}: {e}"
                        )
                if did_fold:
                    save_aliases(cfg, aliases)
            finally:
                idx.close()
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"coarsenings: {e}")
    finally:
        result.timings["coarsenings"] = time.perf_counter() - _t

    # 3b5. theme coarsenings (drift v2, N-ary) ----------------------------
    # Collapse N over-split themes into one ``survivor_id`` — N-1 folds via
    # merge_theme_into (tombstone, reversible). No ontology write.
    _t = time.perf_counter()
    try:
        theme_coarsenings = plan.get("theme_coarsenings") or []
        if theme_coarsenings and not do_coarsen:
            result.errors.append(
                f"theme_coarsenings: surfaced {len(theme_coarsenings)} "
                "cluster(s) but dream_coarsen_apply=false — apply via /tighten"
            )
        elif theme_coarsenings:
            from thinkweave.synthesis.theme_candidates import merge_theme_into

            for c in theme_coarsenings:
                try:
                    survivor = (c.get("survivor_id") or "").strip()
                    members = [
                        str(m).strip()
                        for m in (c.get("members") or [])
                        if str(m).strip()
                    ]
                    losers = [m for m in members if m != survivor]
                    if not survivor or not losers:
                        result.errors.append(
                            f"theme_coarsen: empty survivor/members in {c}"
                        )
                        continue
                    fold_dates_all: list[str] = []
                    for m in losers:
                        m_stats = merge_theme_into(
                            cfg, from_id=m, to_id=survivor, rebuild_index=False
                        )
                        if m_stats.get("fold_dates"):
                            fold_dates_all.extend(m_stats["fold_dates"])
                            result.seams_enqueued += 1
                    result.recorded_theme_coarsenings.append(
                        {
                            "members": members,
                            "survivor_id": survivor,
                            "fold_dates": sorted(set(fold_dates_all)),
                            "reason": str(c.get("reason") or ""),
                        }
                    )
                    result.theme_coarsenings_applied += 1
                except Exception as e:  # noqa: BLE001
                    result.errors.append(
                        f"theme_coarsen {c.get('survivor_id', '?')}: {e}"
                    )
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"theme_coarsenings: {e}")
    finally:
        result.timings["theme_coarsenings"] = time.perf_counter() - _t

    # 3b6. distinct clusters — N-ary verdict memory, no mutation ----------
    # Clusters the worker judged genuinely-distinct grains. Recorded (exact
    # member set) so geometry.judged_clusters stops re-surfacing them.
    try:
        for dc in plan.get("distinct_clusters") or []:
            members = dc.get("members") or []
            if not isinstance(members, list) or len(members) < 2:
                result.errors.append(f"distinct_cluster: bad members in {dc}")
                continue
            result.recorded_distinct_clusters.append(
                {
                    "kind": str(dc.get("kind") or "concept"),
                    "members": [str(m) for m in members],
                    "reason": str(dc.get("reason") or ""),
                    "min_cosine": dc.get("min_cosine"),
                }
            )
            result.distinct_clusters_recorded += 1
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"distinct_clusters: {e}")

    # 3c. essence rewrites — joint shape: rewrite-and-log when an entry
    # carries ``new_essence``; log-only for legacy entries that don't.
    # Per-entry errors don't cascade — the rest of the step still runs.
    _t = time.perf_counter()
    try:
        rewrites = plan.get("essence_rewrites") or []
        if rewrites:
            from thinkweave.core.indexer import Indexer
            from thinkweave.synthesis.concept_hub import concept_hub_path

            # One indexer query for all theme rewrites — bounded by size.
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
                hub_label = r.get("theme_id") or r.get("concept") or "?"
                try:
                    hub_kind = (r.get("hub_kind") or "theme").strip().lower()
                    if hub_kind == "concept":
                        ident = (r.get("concept") or "").strip()
                    else:
                        ident = (r.get("theme_id") or "").strip()
                    if not ident:
                        result.errors.append(
                            "essence_rewrite: missing "
                            f"{'concept' if hub_kind == 'concept' else 'theme_id'}"
                            f" in {r}"
                        )
                        continue
                    new_essence = r.get("new_essence")
                    if new_essence is None:
                        # Legacy log-only entry — count and move on.
                        result.essence_rewrites_applied += 1
                        continue
                    if hub_kind == "concept":
                        hub_path = concept_hub_path(cfg, ident)
                        if not hub_path.exists():
                            result.errors.append(
                                f"essence_rewrite: unknown concept hub {ident}"
                            )
                            continue
                    else:
                        rel = theme_paths.get(ident)
                        if not rel:
                            result.errors.append(
                                f"essence_rewrite: unknown theme_id {ident}"
                            )
                            continue
                        hub_path = cfg.vault_root / rel
                        if not hub_path.exists():
                            result.errors.append(
                                f"essence_rewrite: missing file {rel}"
                            )
                            continue
                    _rewrite_hub_essence(hub_path, str(new_essence))
                    result.essence_rewrites_applied += 1
                except Exception as e:  # noqa: BLE001
                    result.errors.append(
                        f"essence_rewrite {hub_label}: {e}"
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
        from thinkweave.acquisition.sources.queue import Queue

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

    # 3e. rejudge-queue hand-off consumption -------------------------------
    # The scan surfaced (capped at ``dream.rejudge_cap``) queue entries into the
    # phase-2 ``dream-judge-worker``'s prompt; apply is the cycle's point of
    # no return, so the handed-off prefix leaves the on-disk queue here —
    # mirroring ``weave judge --drain`` (consume at hand-off; the worker
    # records verdicts via ``weave_update``, never the file). Entries beyond
    # the cap were never handed off and survive for the next cycle. A
    # verdict lost to a downstream worker error self-heals: the decision's
    # ``prediction_match`` stays ``pending`` and the next scan's
    # stale-pending index sweep re-surfaces it.
    _t = time.perf_counter()
    try:
        from thinkweave.operations import rejudge_queue as _rq

        handed_entries = _rq.peek(cfg)[: _rejudge_cap(cfg)]
        handed = [e.get("decision_id") for e in handed_entries]

        # Evidence-gated supersession flip (headless/deferred recovery).
        # weave_extract / weave_create only *enqueue* a predecessor on a
        # ``supersedes:`` declaration; the dream cycle is where those flip,
        # now that a commit may have landed. Re-judge each supersession
        # predecessor structurally — blame survival decides ``superseded``
        # (lines replaced) vs ``kept`` (still co-contributing). Runs before
        # the hand-off removal below so we judge the same entries the
        # phase-2 prediction worker will see (the two consumers write
        # different fields — status here, prediction_match there).
        super_ids = [
            e.get("decision_id")
            for e in handed_entries
            if e.get("source") == "supersession" and e.get("decision_id")
        ]
        if super_ids:
            try:
                from thinkweave.operations.decisions import (
                    rejudge_supersession_predecessors,
                )

                flipped = rejudge_supersession_predecessors(cfg, super_ids)
                result.supersession_flips = sum(
                    1 for _d, r in flipped if r.get("verdict") == "superseded"
                )
            except Exception as e:  # noqa: BLE001
                result.errors.append(f"supersession_flip: {e}")

        if handed:
            result.rejudge_consumed = _rq.remove(cfg, handed)
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"rejudge_consume: {e}")
    finally:
        result.timings["rejudge_consume"] = time.perf_counter() - _t

    # 3f. deterministic staleness auto-resolve (C2) -----------------------
    # An ``active`` theme whose newest catalyst-log entry (or, for an empty
    # stub, its created date) has aged past ``theme_resolve_after_days`` is
    # flipped to ``resolved`` — the one automatic theme-lifecycle trigger,
    # and mechanically observable (no semantic inference). Runs after all
    # the structural theme steps so a theme touched this cycle (its entry
    # date refreshed) is correctly seen as fresh. ``0`` disables.
    _t = time.perf_counter()
    try:
        _auto_resolve_stale_themes(cfg, result)
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"theme_auto_resolve: {e}")
    finally:
        result.timings["theme_auto_resolve"] = time.perf_counter() - _t

    # 4. one index rebuild + concept-hub maintenance ----------------------
    _t = time.perf_counter()
    structural_changes = (
        result.merges_applied
        + result.promotions_applied
        + result.themes_minted
        + result.themes_extended
        + result.theme_merges_applied
        + result.coarsenings_applied
        + result.theme_coarsenings_applied
        + result.themes_resolved
        + result.essence_rewrites_applied
        + result.priority_signals_enqueued
    )
    if structural_changes:
        try:
            from thinkweave.core.indexer import Indexer

            # Gate hub regen on *actual* ontology growth — not just
            # "promotions ran." Idempotent sweeps (where the concept was
            # already canonical) don't need new hub skeletons or domain
            # hubs. On the 2026-05-23 first cycle this chain was 95%+ of
            # apply wall-time on a 6500-note WSL vault; skipping it on
            # sweep-only cycles is the routine speed win.
            #
            # Wikilink materialization (add_hub_wikilinks) is *not* in
            # this chain by design — it's quadratic over notes × concepts
            # and belongs on the dedicated `weave index --materialize-links`
            # path that /update-hubs owns. Dream stays in its lane.
            if result.ontology_grew:
                from thinkweave.synthesis.concepts import (
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
                        from thinkweave.synthesis.centrality import (
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
# Hub essence rewrite — splice helper used by apply()
# ---------------------------------------------------------------------------


def _rewrite_hub_essence(hub_path: Path, new_essence: str) -> None:
    """Rewrite the ``## Essence`` section of a hub file in place.

    Surface-agnostic — themes and concept hubs share the section grammar.
    Preserves frontmatter (plus stamps ``essence_updated: <today>`` so the
    essence-candidate scan can count catalysts-since-essence), the
    ``# Title`` heading, and every other ``##`` section (notably
    ``## Catalyst log`` and ``## Open questions``). Locates the essence
    section via the same ``extract_section`` slice the shared
    :class:`Hub` parser uses — find the heading, find the next ``##``
    heading (or EOF), splice the new body between them.

    Near-idempotent: writing the same essence again only refreshes the
    ``essence_updated`` stamp. If the heading is missing entirely,
    appends a new section at the end; that path is for safety and never
    fires in normal use (hub files always carry the canonical skeleton).
    """
    from thinkweave.synthesis.hub import ESSENCE_HEADING

    text = hub_path.read_text(encoding="utf-8")
    body = new_essence.strip()
    # Standard inter-section spacing — blank line above the next heading.
    block = f"{ESSENCE_HEADING}\n\n{body}\n\n"

    if ESSENCE_HEADING not in text:
        # Defensive: append at end, never overwrite other content.
        sep = "" if text.endswith("\n") else "\n"
        text = text + sep + block
    else:
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
        text = text[:start] + block + text[end:]

    # Stamp essence_updated in frontmatter — the growth-trigger baseline.
    # Targeted line edit, NOT a parse → render_frontmatter round-trip:
    # re-rendering the whole block drops YAML comments / empty keys and
    # re-serializes every value, so user-authored frontmatter bytes
    # outside the one stamped key must stay untouched.
    try:
        text = _set_frontmatter_line(
            text,
            "essence_updated",
            datetime.now(timezone.utc).date().isoformat(),
        )
    except Exception:  # noqa: BLE001 — stamp is advisory, never block the rewrite
        pass

    hub_path.write_text(text, encoding="utf-8")


def _set_frontmatter_line(text: str, key: str, value: str) -> str:
    """Set a top-level ``key: value`` in frontmatter via a targeted line edit.

    Replaces the existing ``key:`` line in place (column-0 keys only —
    nested same-named keys are never clobbered), or inserts one before
    the closing ``---``; every other frontmatter byte stays exactly as
    authored. No-op when there is no frontmatter block. Deliberately NOT
    ``synthesis.hub.set_frontmatter_keys``, which round-trips through
    ``render_frontmatter`` and re-serializes the whole block.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text
    close = next(
        (i for i in range(1, len(lines)) if lines[i].strip() == "---"), None
    )
    if close is None:
        return text

    new_line = f"{key}: {value}"
    key_re = re.compile(rf"^{re.escape(key)}\s*:")
    for i in range(1, close):
        if key_re.match(lines[i]):
            lines[i] = new_line
            break
    else:
        lines.insert(close, new_line)
    return "\n".join(lines)


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
    lines.append("- **Maintenance log**: `vault/.weave/maintenance.jsonl`")
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
    lines.append(f"| Theme merges | {result.theme_merges_applied} |")
    lines.append(f"| Distinct rulings recorded | {result.distinct_pairs_recorded} |")
    lines.append(f"| Hub seams enqueued | {result.seams_enqueued} |")
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

    theme_merges = plan.get("theme_merges") or []
    if theme_merges:
        lines.append(f"## Theme merges ({len(theme_merges)})")
        lines.append("")
        lines.append(
            "_The loser keeps its file with `merged-into:` status — "
            "reversible by hand. Catalyst logs were folded into the "
            "survivor; the seam-link worker stitches cross-parent "
            "linkage on the next phase-2 pass._"
        )
        lines.append("")
        for tm in theme_merges:
            reason = tm.get("reason") or "(no reason given)"
            lines.append(
                f"- **{tm.get('from_id', '?')} → {tm.get('to_id', '?')}**"
                f" — {reason}"
            )
        lines.append("")

    distinct = plan.get("distinct_pairs") or []
    if distinct:
        lines.append(f"## Distinct rulings ({len(distinct)})")
        lines.append("")
        lines.append(
            "_Pairs judged NOT duplicates. Recorded in the maintenance "
            "log so future scans stop re-surfacing them; re-open with "
            "`weave dream scan --rejudge-pairs`._"
        )
        lines.append("")
        for dp in distinct:
            pair = dp.get("pair") or ["?", "?"]
            kind = dp.get("kind") or "concept"
            cos = dp.get("cosine")
            cos_str = f" (cosine {cos})" if cos is not None else ""
            reason = dp.get("reason") or "(no reason given)"
            lines.append(
                f"- [{kind}] **{pair[0]} ≠ {pair[1]}**{cos_str} — {reason}"
            )
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
    from thinkweave.operations.reports import recent_reports

    return [
        {"cycle_id": r["run_id"], "path": r["path"], "mtime": r["mtime"]}
        for r in recent_reports(cfg, "dream", n=n)
    ]
