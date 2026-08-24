"""``weave brief`` — the deterministic collect behind ``/brief`` (#170).

``/brief`` is the daily orientation surface: a live meta layer over the
nightly digests that reads the substrate fresh and tells the user where the
edges are. Composition mirrors ``/wrap`` — *deterministic collect → LLM
narration* — and this module is the collect half: pure reads, no LLM, one
JSON payload the skill narrates from.

JSON contract (``weave brief collect --json``) — every key always present;
an empty section is an explicit empty, never missing::

    generated_at        iso-8601
    since               iso-8601 — watermark date, or now-24h on a first run
    since_reason        "watermark" | "first_run_24h"
    watermark           {"id", "date"} | null   — newest digest with kind: brief
    health              the `weave health --json` report (operations.health)
    banner              str | null — loud line when the nightly digest is stale/missing
    timeline            {"sessions": [{id,title,project,date}], "decisions": [{id,title,status,verdict,date}]}
    landings            {source_type: [{id,title,concepts,theme_id}]} — only lanes that landed
    lanes               [{source_type, landed, queue_depth, job, state}]
                        state ∈ kept | ran_nothing_kept | dead | unknown
    queues              health's queue rows (depth + backlog per lane)
    strategies          configured discover strategy names (sources.yaml)
    focus               operations.focus.rank() — ranked concept vector + active_projects
    attention           {"predictions_due": [ids],
                         "proposed_near_threshold": [{concept,count,threshold}] (≤ _NEAR_CAP),
                         "proposed_near_threshold_total": int (before the cap),
                         "pressured_unanswered": [{concept,asked,probes}]}
    catalysts           [{hub,hub_kind,flag,entry_date,ref_date,cited_note_id,text}]
                        agrees/contradicts/extends entries since the watermark
                        or citing a note that landed since it
    contradictions      the CONTRADICTIONS & EXTENSIONS subset of catalysts —
                        contradicts first, then extends (what the skill renders)
    theme_movements     [{hub,flag,entry_date,cited_note_id,text,shown_in_contradictions}]
                        theme-hub log deltas; the flag marks rows the
                        CONTRADICTIONS section already renders (dedupe rule)
    essence_rewrites    [{id,type,title,essence_updated}]
    connections         [{new_id,new_title,old_id,old_title,score}] ≤ 2, strong only
    connections_reason  str | null — why connections is empty (embeddings absent, …)
    render_plan         ordered section keys the narration renders (non-empty only)
    served_ids          every note id the payload surfaces → `weave brief mark`

``render_plan`` is what makes the narration contract testable: the skill
renders exactly these sections, in this order, and nothing else.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from thinkweave.core.config import Config
from thinkweave.operations import focus, health

SECTION_ORDER = (
    "in_brief", "health", "contradictions", "theme_movements", "no_news", "papers",
    "understanding_shifted", "focus", "acquisition_outlook", "attention", "connections",
)
CONTRACT_KEYS = (
    "generated_at", "since", "since_reason", "watermark", "health", "banner",
    "timeline", "landings", "lanes", "queues", "strategies", "focus", "attention",
    "catalysts", "contradictions", "theme_movements", "essence_rewrites", "connections",
    "connections_reason", "render_plan", "served_ids",
)
_CATALYST_FLAGS = ("agrees", "contradicts", "extends")
_PAPER_LANES = ("paper",)
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# ponytail: flat cap on the near-promotion list (120 live entries at count 4);
# upgrade path is a [brief] knob if anyone wants to tune it.
_NEAR_CAP = 20


def note_title(when: datetime) -> str:
    """Title (= filename slug) of the brief note written at ``when``.

    ``brief-`` first, deliberately: ``health._digest`` only counts
    date-prefixed files, so a brief never masks a stale nightly digest.
    """
    return f"brief-{when.strftime('%Y-%m-%d-%H%M')}"


def find_watermark(db: sqlite3.Connection) -> dict | None:
    row = db.execute(
        "SELECT id, date FROM notes WHERE type = 'digest' "
        "AND json_extract(frontmatter, '$.kind') = 'brief' ORDER BY date DESC LIMIT 1"
    ).fetchone()
    return {"id": row["id"], "date": row["date"]} if row else None


def collect(
    cfg: Config, *, now: datetime | None = None, crontab_text: str | None = None
) -> dict:
    """Build the brief payload (schema in the module docstring)."""
    from thinkweave.acquisition.sources.config import load_user_config
    from thinkweave.core.indexer import Indexer
    from thinkweave.synthesis.concepts import get_all_proposed_concepts

    now = now or datetime.now(timezone.utc)
    report = health.collect(cfg, now=now, crontab_text=crontab_text)
    idx = Indexer(config=cfg)
    try:
        db = idx.db
        watermark = find_watermark(db)
        since = watermark["date"] if watermark else (now - timedelta(hours=24)).isoformat()

        timeline = {
            "sessions": [
                dict(r) for r in db.execute(
                    "SELECT id, title, project, date FROM notes "
                    "WHERE type = 'session' AND date >= ? ORDER BY date", (since,)
                )
            ],
            "decisions": [
                {
                    "id": r["id"], "title": r["title"], "date": r["date"],
                    "status": _fm(r).get("status"), "verdict": _fm(r).get("verdict"),
                }
                for r in db.execute(
                    "SELECT id, title, date, frontmatter FROM notes "
                    "WHERE type = 'decision' AND date >= ? ORDER BY date", (since,)
                )
            ],
        }

        landings: dict[str, list[dict]] = {}
        for r in db.execute(
            "SELECT id, title, frontmatter FROM notes WHERE type = 'source' AND date >= ? "
            "ORDER BY date", (since,)
        ):
            fm = _fm(r)
            relates = fm.get("relates_to") or []
            relates = relates if isinstance(relates, list) else [relates]
            landings.setdefault(fm.get("source_type") or "source", []).append({
                "id": r["id"], "title": r["title"],
                "concepts": [str(c) for c in (fm.get("concepts") or [])],
                "theme_id": next((str(x) for x in relates if str(x).startswith("thm-")), None),
            })
        landed_ids = [n["id"] for rows in landings.values() for n in rows]
        landed_concepts = {c for rows in landings.values() for n in rows for c in n["concepts"]}

        lanes = [_lane(q, landings, report["jobs"]) for q in report["queues"]]

        vec = focus.rank(cfg, now=now, db=db)

        since_day = since[:10]
        catalysts = [
            dict(r) for r in db.execute(
                f"SELECT hub_id AS hub, hub_kind, flag, entry_date, ref_date, cited_note_id, text "
                f"FROM hub_log_entries WHERE flag IN ({_ph(_CATALYST_FLAGS)}) "
                f"AND (entry_date >= ? OR cited_note_id IN ({_ph(landed_ids)})) "
                f"ORDER BY entry_date DESC, seq DESC",
                (*_CATALYST_FLAGS, since_day, *landed_ids),
            )
        ]
        theme_movements = [
            {**r, "shown_in_contradictions": r["flag"] in ("contradicts", "extends")}
            for r in map(dict, db.execute(
                "SELECT hub_id AS hub, flag, entry_date, cited_note_id, text FROM hub_log_entries "
                "WHERE hub_kind = 'theme' AND entry_date >= ? ORDER BY entry_date DESC, seq DESC",
                (since_day,),
            ))
        ]
        essence_rewrites = [
            {"id": r["id"], "type": r["type"], "title": r["title"],
             "essence_updated": _fm(r).get("essence_updated")}
            for r in db.execute(
                "SELECT id, type, title, frontmatter FROM notes "
                "WHERE json_extract(frontmatter, '$.essence_updated') >= ?", (since_day,)
            )
        ]

        threshold = cfg.dream_promotion_threshold
        near = [
            {"concept": c, "count": n, "threshold": threshold}
            for c, n in sorted(get_all_proposed_concepts(db).items(), key=lambda kv: -kv[1])
            if threshold - 1 <= n < threshold and _SLUG.match(c)
        ]
        attention = {
            "predictions_due": _predictions_due(db, now),
            "proposed_near_threshold": near[:_NEAR_CAP],
            "proposed_near_threshold_total": len(near),
            "pressured_unanswered": [
                {"concept": c["concept"], "asked": c["asked"], "probes": c["probes"]}
                for c in vec["concepts"]
                if c["asked"] >= cfg.brief_attention_pressure
                and c["concept"] not in landed_concepts
            ],
        }

        connections, reason = _connections(cfg, landings, since, db)
    finally:
        idx.close()

    user_cfg = load_user_config(cfg.vault_root)
    projects_cfg = user_cfg.get("projects", {}) or {}
    scope = projects_cfg.get(cfg.default_project) or projects_cfg.get("default", {}) or {}
    strategies = list(scope.get("discover_strategies", []))

    d = report["digest"]
    banner = None
    if d["stale"]:
        banner = (
            f"nightly digest is stale: latest {d['latest']} is {d['age_days']}d old — "
            "the /dream cron likely did not run; briefing from raw landings instead"
            if d["latest"] else
            "no nightly digest found — /dream has never composed one; briefing from raw landings"
        )

    contradictions = [c for c in catalysts if c["flag"] == "contradicts"] + [
        c for c in catalysts if c["flag"] == "extends"
    ]
    papers = [n for lane in _PAPER_LANES for n in landings.get(lane, [])]
    present = {
        "in_brief": True,
        "health": bool(banner or report["flags"]),
        "contradictions": bool(contradictions),
        "theme_movements": bool(theme_movements),
        "no_news": any(lane["state"] == "dead" for lane in lanes),
        "papers": bool(papers),
        "understanding_shifted": bool(essence_rewrites),
        "focus": bool(vec["concepts"]),
        "acquisition_outlook": bool(landed_ids or report["advisories"]),
        "attention": any(
            attention[k] for k in ("predictions_due", "proposed_near_threshold", "pressured_unanswered")
        ),
        "connections": bool(connections),
    }
    served = set(landed_ids)
    served.update(s["id"] for s in timeline["sessions"])
    served.update(d_["id"] for d_ in timeline["decisions"])
    served.update(c["cited_note_id"] for c in catalysts if c["cited_note_id"])
    served.update(c["hub"] for c in catalysts if c["hub"].startswith("thm-"))
    served.update(t["hub"] for t in theme_movements)
    served.update(t["cited_note_id"] for t in theme_movements if t["cited_note_id"])
    served.update(n["theme_id"] for rows in landings.values() for n in rows if n["theme_id"])
    served.update(e["id"] for e in essence_rewrites)
    served.update(attention["predictions_due"])
    served.update(x["old_id"] for x in connections)

    return {
        "generated_at": now.isoformat(),
        "since": since,
        "since_reason": "watermark" if watermark else "first_run_24h",
        "watermark": watermark,
        "health": report,
        "banner": banner,
        "timeline": timeline,
        "landings": landings,
        "lanes": lanes,
        "queues": report["queues"],
        "strategies": strategies,
        "focus": vec,
        "attention": attention,
        "catalysts": catalysts,
        "contradictions": contradictions,
        "theme_movements": theme_movements,
        "essence_rewrites": essence_rewrites,
        "connections": connections,
        "connections_reason": reason,
        "render_plan": [s for s in SECTION_ORDER if present[s]],
        "served_ids": sorted(served),
    }


def mark(cfg: Config, note_id: str, served_ids: list[str], *, session_id: str = "") -> int:
    """Log the brief's surfaced ids as ``context_served(source='brief')``.

    ``session_id`` is the harness UUID or the session note id; it must
    resolve to an indexed session note (``id`` or ``source_session``) —
    every other ``context_served`` row is keyed by a ``ses-`` id, and a
    fabricated key would match no consumer and vanish on the next rebuild.
    Unresolvable → nothing is written and 0 is returned.

    Two writes: the durable one is a ``tool: "brief"`` retrieval event in
    the harness session's buffer (archived into ``retrieval_log.jsonl`` at
    Stop and projected to ``source='brief'`` by the indexer — JSONL stays
    truth), the immediate one is the ``context_served`` upsert so the rows
    exist now.
    """
    from thinkweave.core.indexer import Indexer
    from thinkweave.operations.retrieval_log import append_event

    ids = [i for i in dict.fromkeys(served_ids) if i]
    if not session_id or not ids:
        return 0
    ts = datetime.now(timezone.utc).isoformat()
    idx = Indexer(config=cfg)
    try:
        row = idx.db.execute(
            "SELECT id, json_extract(frontmatter, '$.source_session') AS source_session "
            "FROM notes WHERE type = 'session' AND (id = ? OR "
            "json_extract(frontmatter, '$.source_session') = ?) LIMIT 1",
            (session_id, session_id),
        ).fetchone()
        if row is None:
            return 0
        # The buffer is keyed by the harness UUID (what the Stop hook
        # archives), never by the ses- note id.
        harness = row["source_session"] or (session_id if session_id != row["id"] else "")
        if harness:
            append_event(
                cfg.weave_dir / "buffer" / f"{harness}.jsonl",
                {"ts": ts, "type": "retrieval", "tool": "brief", "args": {"note": note_id},
                 "returned_ids": ids},
            )
        idx.db.executemany(
            "INSERT OR REPLACE INTO context_served (session_id, note_id, source, ts) "
            "VALUES (?, ?, 'brief', ?)",
            [(row["id"], i, ts) for i in ids],
        )
        idx.db.commit()
    finally:
        idx.close()
    return len(ids)


# --------------------------------------------------------------------------- #
# helpers


def _fm(row) -> dict:
    try:
        return json.loads(row["frontmatter"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _ph(items: list) -> str:
    return ",".join("?" * len(items)) or "NULL"


def _lane(queue: dict, landings: dict, jobs: list[dict]) -> dict:
    """Per-lane verdict: zero landings is *data* — ran-nothing-kept vs dead.

    A job binds to a lane by exact token equality on its skill stem
    (``/thinkweave:newsletter`` → ``newsletter``, ``/drain news`` →
    ``drain``/``news``): the lane slug itself first, else its family
    (``newsletter-events`` → ``newsletter``). Never substring — ``news`` is
    inside ``newsletter``.
    """
    slug = queue["source_type"]
    family = slug.split("-")[0]

    def tokens(job: dict) -> set[str]:
        return {t.lstrip("/").split(":")[-1] for t in job["name"].split()}

    job = next((j for j in jobs if slug in tokens(j)), None) or next(
        (j for j in jobs if family in tokens(j)), None
    )
    landed = len(landings.get(slug, []))
    if landed:
        state = "kept"
    elif job is None:
        state = "unknown"
    elif job["stale"] or job["missing"]:
        state = "dead"
    else:
        state = "ran_nothing_kept"
    return {
        "source_type": slug, "landed": landed, "queue_depth": queue["depth"],
        "job": job["name"] if job else None, "state": state,
    }


def _predictions_due(db: sqlite3.Connection, now: datetime) -> list[str]:
    cutoff = (now - timedelta(days=1)).isoformat()
    rows = db.execute(
        "SELECT id, json_extract(frontmatter, '$.judged_at') AS judged_at FROM notes "
        "WHERE type = 'decision' AND json_extract(frontmatter, '$.prediction_match') = 'pending'"
    )
    return [r["id"] for r in rows if not r["judged_at"] or str(r["judged_at"]) < cutoff]


def _connections(
    cfg: Config, landings: dict, since: str, db: sqlite3.Connection
) -> tuple[list[dict], str | None]:
    """New↔old embedding-similarity hits, ≤ 2, at or above the cosine floor."""
    from thinkweave.retrieval.search import Search, SemanticSearchUnavailable

    new = [n for rows in landings.values() for n in rows][:10]
    if not new:
        return [], "nothing landed since the watermark"
    s = Search(config=cfg)
    hits: list[dict] = []
    try:
        for n in new:
            for h in s.similar(n["title"], limit=4):
                if h.id == n["id"] or h.date >= since or h.rank < cfg.brief_connection_min_score:
                    continue
                hits.append({"new_id": n["id"], "new_title": n["title"], "old_id": h.id,
                             "old_title": h.title, "score": round(h.rank, 3)})
    except SemanticSearchUnavailable as exc:
        return [], str(exc)
    finally:
        s.close()
    hits.sort(key=lambda h: -h["score"])
    return hits[:2], None if hits else "no old note above the similarity floor"
