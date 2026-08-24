"""``weave brief`` — the deterministic collect behind ``/brief`` (#170).

Seams: ``brief.collect()``'s return dict (= the ``--json`` contract the
skill narrates from, including ``render_plan``), ``brief.find_watermark()``,
and the ``context_served`` rows ``brief.mark()`` writes. Hand-built fixtures;
expected values from the issue's acceptance criteria.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.operations import brief, served

NOW = datetime.now(timezone.utc)


def _digest(tv, when: datetime) -> None:
    d = tv.config.vault_root / "digests"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{when.date().isoformat()}-concept.md").write_text(
        "---\ntype: digest\ngrain: concept\n---\n", encoding="utf-8"
    )


def _quiet(vault_factory):
    tv = vault_factory()
    _digest(tv, NOW)
    return tv.indexed()


def test_collect_contract_keys_and_explicit_empties(vault_factory):
    tv = _quiet(vault_factory)
    out = brief.collect(tv.config, now=NOW, crontab_text="")
    assert set(out) == set(brief.CONTRACT_KEYS)
    assert out["watermark"] is None and out["since_reason"] == "first_run_24h"
    assert out["since"] == (NOW - timedelta(hours=24)).isoformat()
    assert out["banner"] is None
    assert out["timeline"] == {"sessions": [], "decisions": []}
    assert out["landings"] == {}
    assert out["catalysts"] == [] and out["theme_movements"] == []
    assert out["essence_rewrites"] == []
    assert out["attention"] == {
        "predictions_due": [], "proposed_near_threshold": [],
        "proposed_near_threshold_total": 0, "pressured_unanswered": [],
    }
    assert out["connections"] == [] and isinstance(out["connections_reason"], str)
    assert out["focus"] == {"concepts": [], "active_projects": [], "asked_below_floor": []}
    assert isinstance(out["strategies"], list)
    assert out["health"]["ok"] is True
    # Quiet day: only IN BRIEF renders (AC3 — a ≤6-line brief).
    assert out["render_plan"] == ["in_brief"]
    assert out["served_ids"] == []
    json.dumps(out)  # the --json contract must serialise


def test_contradicting_landing_leads(vault_factory):
    tv = _quiet(vault_factory)
    path = tv.vault.create_note(
        NoteType.SOURCE, "New result", project="p",
        extra_frontmatter={"source_type": "paper", "concepts": ["x-term"]},
    )
    tv.indexed()
    src_id = tv.vault.read_note(path).id
    idx = Indexer(config=tv.config)
    try:
        idx.db.execute(
            "INSERT INTO hub_log_entries (hub_id, hub_kind, entry_date, flag, ref_date, "
            "cited_note_id, text, seq) VALUES (?, 'concept', ?, 'contradicts', '2026-01-01', ?, "
            "'pushes back on the held position', 1)",
            ("x-term", NOW.date().isoformat(), src_id),
        )
        idx.db.commit()
    finally:
        idx.close()

    out = brief.collect(tv.config, now=NOW, crontab_text="")
    assert out["landings"] == {
        "paper": [{"id": src_id, "title": "New result", "concepts": ["x-term"], "theme_id": None}]
    }
    assert out["catalysts"][0]["flag"] == "contradicts"
    assert out["catalysts"][0]["cited_note_id"] == src_id
    content = [s for s in out["render_plan"] if s not in ("in_brief", "health")]
    assert content[0] == "contradictions"
    assert "papers" in out["render_plan"]
    assert src_id in out["served_ids"]


def test_stale_digest_banner_first_but_brief_still_produced(vault_factory):
    tv = vault_factory()
    _digest(tv, NOW - timedelta(days=10))
    tv.with_note("landed", note_type=NoteType.SOURCE, project="p",
                 extra_frontmatter={"source_type": "article", "concepts": ["a", "b"]})
    tv.indexed()
    out = brief.collect(tv.config, now=NOW, crontab_text="")
    assert out["banner"] and "digest" in out["banner"] and "10d" in out["banner"]
    assert out["render_plan"][:2] == ["in_brief", "health"]
    assert list(out["landings"]) == ["article"]  # still briefs from raw landings


def test_second_run_uses_watermark_and_mark_logs_context_served(vault_factory):
    tv = _quiet(vault_factory)
    first = brief.collect(tv.config, now=NOW, crontab_text="")
    assert first["watermark"] is None

    when = NOW - timedelta(hours=3)
    path = tv.vault.create_note(
        NoteType.DIGEST, brief.note_title(when), body="# brief\n",
        extra_frontmatter={"kind": "brief", "date": when.isoformat()},
    )
    assert path.name == f"brief-{when.strftime('%Y-%m-%d-%H%M')}.md"
    tv.indexed()
    note_id = tv.vault.read_note(path).id

    second = brief.collect(tv.config, now=NOW, crontab_text="")
    assert second["watermark"] == {"id": note_id, "date": when.isoformat()}
    assert second["since"] == when.isoformat() and second["since_reason"] == "watermark"
    # A brief file never counts as the nightly digest (health freshness).
    assert second["health"]["digest"]["latest"] == NOW.date().isoformat()

    tv.with_note("live", note_type=NoteType.SESSION, project="p",
                 extra_frontmatter={"source_session": "cc-1"})
    tv.indexed()
    ses_id = tv.vault.read_note(
        next(tv.config.vault_root.glob("projects/p/sessions/*/session.md"))
    ).id
    written = served.mark(tv.config, "brief", "cc-1", note_id, ["n-aaaaaaaa", "dec-bbbbbbbb"])
    assert written == 2
    idx = Indexer(config=tv.config)
    try:
        rows = idx.db.execute(
            "SELECT session_id, note_id, source FROM context_served ORDER BY note_id"
        ).fetchall()
    finally:
        idx.close()
    assert [tuple(r) for r in rows] == [
        (ses_id, "dec-bbbbbbbb", "brief"), (ses_id, "n-aaaaaaaa", "brief")
    ]
    # Durable twin: the buffer event the indexer re-projects on rebuild.
    buf = (tv.config.weave_dir / "buffer" / "cc-1.jsonl").read_text(encoding="utf-8")
    ev = json.loads(buf.strip().splitlines()[-1])
    assert ev["type"] == "retrieval" and ev["tool"] == "brief"
    assert ev["returned_ids"] == ["n-aaaaaaaa", "dec-bbbbbbbb"]


def _rows(tv):
    idx = Indexer(config=tv.config)
    try:
        return [tuple(r) for r in idx.db.execute(
            "SELECT session_id, note_id, source FROM context_served ORDER BY note_id"
        )]
    finally:
        idx.close()


def test_mark_resolves_harness_uuid_to_session_note(vault_factory):
    tv = _quiet(vault_factory)
    path = tv.vault.create_note(NoteType.SESSION, "live", project="p",
                                extra_frontmatter={"source_session": "11111111-uuid"})
    tv.indexed()
    ses_id = tv.vault.read_note(path).id

    assert served.mark(tv.config, "brief", "11111111-uuid", "dig-x", ["n-aaaaaaaa"]) == 1
    assert _rows(tv) == [(ses_id, "n-aaaaaaaa", "brief")]
    buf = tv.config.weave_dir / "buffer" / "11111111-uuid.jsonl"
    assert json.loads(buf.read_text().strip())["returned_ids"] == ["n-aaaaaaaa"]


def test_mark_unresolvable_session_writes_nothing(vault_factory):
    tv = _quiet(vault_factory)
    assert served.mark(tv.config, "brief", "", "dig-x", ["n-aaaaaaaa"]) == 0
    assert served.mark(tv.config, "brief", "nope-uuid", "dig-x", ["n-aaaaaaaa"]) == 0
    assert _rows(tv) == []
    if (tv.config.weave_dir / "buffer").exists():
        assert not list((tv.config.weave_dir / "buffer").glob("*.jsonl"))


def test_served_ids_cover_theme_hubs_and_theme_movement_citations(vault_factory):
    tv = _quiet(vault_factory)
    thm_path = tv.vault.create_note(NoteType.THEME, "a theme")
    thm_id = tv.vault.read_note(thm_path).id
    src_path = tv.vault.create_note(
        NoteType.SOURCE, "filed", project="p",
        extra_frontmatter={"source_type": "news", "relates_to": [thm_id]},
    )
    tv.indexed()
    src_id = tv.vault.read_note(src_path).id
    idx = Indexer(config=tv.config)
    try:
        idx.db.execute(
            "INSERT INTO hub_log_entries (hub_id, hub_kind, entry_date, flag, ref_date, "
            "cited_note_id, text, seq) VALUES (?, 'theme', ?, 'new', NULL, 'src-old00001', 't', 1)",
            (thm_id, NOW.date().isoformat()),
        )
        idx.db.commit()
    finally:
        idx.close()
    out = brief.collect(tv.config, now=NOW, crontab_text="")
    assert {thm_id, src_id, "src-old00001"} <= set(out["served_ids"])


def test_near_threshold_filters_malformed_and_caps(vault_factory):
    tv = _quiet(vault_factory)
    n = tv.config.dream_promotion_threshold - 1
    bad = ['["agentic-ai', "pl/macro", 'liquidity"]', "two words"]
    for i in range(n):
        tv.with_note(f"v{i}", project="p",
                     extra_frontmatter={"proposed_concepts": ["good-term", *bad]})
    tv.indexed()
    out = brief.collect(tv.config, now=NOW, crontab_text="")
    assert out["attention"]["proposed_near_threshold"] == [
        {"concept": "good-term", "count": n, "threshold": n + 1}
    ]
    assert len(out["attention"]["proposed_near_threshold"]) <= brief._NEAR_CAP


def test_contradictions_key_is_filtered_ordered_subset(vault_factory):
    tv = _quiet(vault_factory)
    tv.indexed()
    idx = Indexer(config=tv.config)
    try:
        for seq, flag in enumerate(("agrees", "extends", "contradicts")):
            idx.db.execute(
                "INSERT INTO hub_log_entries (hub_id, hub_kind, entry_date, flag, ref_date, "
                "cited_note_id, text, seq) VALUES ('h', 'concept', ?, ?, NULL, NULL, 't', ?)",
                (NOW.date().isoformat(), flag, seq),
            )
        idx.db.commit()
    finally:
        idx.close()
    out = brief.collect(tv.config, now=NOW, crontab_text="")
    assert [c["flag"] for c in out["contradictions"]] == ["contradicts", "extends"]


def test_skill_section_table_matches_section_order():
    from pathlib import Path

    text = Path(__file__).resolve().parents[1].joinpath("commands", "brief.md").read_text()
    text = text[text.index("## 2. Narrate"):]
    positions = [text.index(f"| `{key}` |") for key in brief.SECTION_ORDER]
    assert positions == sorted(positions)
    assert "14-day" in text or "14d" in text


def test_mark_with_session_note_id_names_buffer_by_source_session(vault_factory):
    tv = _quiet(vault_factory)
    path = tv.vault.create_note(NoteType.SESSION, "live", project="p",
                                extra_frontmatter={"source_session": "2222-uuid"})
    tv.indexed()
    ses_id = tv.vault.read_note(path).id
    assert served.mark(tv.config, "brief", ses_id, "dig-x", ["n-aaaaaaaa"]) == 1
    assert _rows(tv) == [(ses_id, "n-aaaaaaaa", "brief")]
    assert (tv.config.weave_dir / "buffer" / "2222-uuid.jsonl").exists()
    assert not (tv.config.weave_dir / "buffer" / f"{ses_id}.jsonl").exists()


def test_lane_binds_all_matching_jobs_weakest_link():
    jobs = [
        {"id": "17 */4 * * * /discover news", "name": "/discover news", "stale": False, "missing": False},
        {"id": "0 7,19 * * * /drain news", "name": "/drain news", "stale": True, "missing": False},
        {"id": "0 6 * * * /thinkweave:newsletter", "name": "/thinkweave:newsletter", "stale": False, "missing": True},
        {"id": "0 9 * * * /youtube", "name": "/youtube", "stale": False, "missing": False},
    ]
    q = lambda s: {"source_type": s, "depth": 0}  # noqa: E731
    news = brief._lane(q("news"), {}, jobs)
    # both news feeders bind; the dead drain poisons the lane (weakest link)
    assert news["jobs"] == ["17 */4 * * * /discover news", "0 7,19 * * * /drain news"]
    assert news["dead_jobs"] == ["0 7,19 * * * /drain news"]
    assert news["state"] == "dead"
    nl = brief._lane(q("newsletter-events"), {}, jobs)
    assert nl["jobs"] == ["0 6 * * * /thinkweave:newsletter"] and nl["state"] == "dead"
    yt = brief._lane(q("youtube-concepts"), {}, jobs)
    assert yt["state"] == "ran_nothing_kept" and yt["dead_jobs"] == []
    assert brief._lane(q("paper"), {}, jobs) == {
        "source_type": "paper", "landed": 0, "queue_depth": 0,
        "jobs": [], "dead_jobs": [], "state": "unknown",
    }
    # binding is by exact stem token, not substring, and order-independent
    assert brief._lane(q("news"), {}, jobs[::-1])["jobs"][::-1] == news["jobs"]


def test_near_threshold_total_survives_cap(vault_factory):
    tv = _quiet(vault_factory)
    n = tv.config.dream_promotion_threshold - 1
    terms = [f"term-{i:02d}" for i in range(brief._NEAR_CAP + 3)]
    for i in range(n):
        tv.with_note(f"v{i}", project="p", extra_frontmatter={"proposed_concepts": terms})
    tv.indexed()
    att = brief.collect(tv.config, now=NOW, crontab_text="")["attention"]
    assert len(att["proposed_near_threshold"]) == brief._NEAR_CAP
    assert att["proposed_near_threshold_total"] == brief._NEAR_CAP + 3


def test_theme_movements_flag_rows_already_shown_in_contradictions(vault_factory):
    tv = _quiet(vault_factory)
    tv.indexed()
    idx = Indexer(config=tv.config)
    try:
        for seq, flag in enumerate(("new", "contradicts")):
            idx.db.execute(
                "INSERT INTO hub_log_entries (hub_id, hub_kind, entry_date, flag, ref_date, "
                "cited_note_id, text, seq) VALUES ('thm-t', 'theme', ?, ?, NULL, NULL, 't', ?)",
                (NOW.date().isoformat(), flag, seq),
            )
        idx.db.commit()
    finally:
        idx.close()
    out = brief.collect(tv.config, now=NOW, crontab_text="")
    shown = {t["flag"]: t["shown_in_contradictions"] for t in out["theme_movements"]}
    assert shown == {"new": False, "contradicts": True}
    assert [c["flag"] for c in out["contradictions"]] == ["contradicts"]


def test_bare_brief_requires_a_subaction():
    import pytest

    from thinkweave.surfaces.cli.parser import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["brief"])
