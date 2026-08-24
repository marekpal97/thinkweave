"""``operations.focus`` — the three-layer focus merge (#170, AC2).

Seams: ``focus.rank()``'s return dict and ``focus.active_projects()``.
Expected values are hand-computed from the issue's precedence table:
asked (probe pressure) > done (concept edges in window) > declared (floor).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.operations import focus
from thinkweave.operations.dream import scan

NOW = datetime.now(timezone.utc)


def _probe(handle, project: str, text: str) -> None:
    sess_dir = handle.config.vault_root / "projects" / project / "sessions" / "ses-x"
    sess_dir.mkdir(parents=True, exist_ok=True)
    ts = (NOW - timedelta(hours=1)).isoformat()
    with (sess_dir / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "prompt", "text": text, "session_id": "cc-1", "ts": ts}) + "\n")
        f.write(json.dumps({"type": "probe", "session_id": "cc-1", "ts": ts, "prompt_ref": text}) + "\n")


def test_rank_asked_outranks_declared_only(vault_factory):
    tv = vault_factory()
    prio = tv.config.vault_root / "config" / "PRIORITIES.yaml"
    prio.parent.mkdir(parents=True, exist_ok=True)
    prio.write_text("focus:\n  research_concepts: [declared-only-term]\n", encoding="utf-8")
    (tv.config.vault_root / "RESEARCH_FOCUS.md").write_text(
        "# Focus\n\n## Concept Gaps\n\n- `gap-term` — nothing sourced yet\n\n## Other\n- ignored\n",
        encoding="utf-8",
    )
    # Asked layer: a probe naming the concept; vocabulary via proposed_concepts.
    tv.with_note(
        "vocab", project="p",
        extra_frontmatter={"proposed_concepts": ["wal-mode-x"], "concepts": ["wal-mode-x"]},
    )
    _probe(tv, "p", "How does wal-mode-x interact with the indexer?")
    _probe(tv, "p", "Is wal-mode-x safe with two writers?")  # floor is 2 (brief_attention_pressure)
    # Done layer only: a session carrying a concept edge, never probed.
    tv.with_note("work", note_type=NoteType.SESSION, project="p",
                 extra_frontmatter={"concepts": ["done-term"]})
    tv.indexed()

    out = focus.rank(tv.config, now=NOW)
    by = {c["concept"]: c for c in out["concepts"]}
    order = [c["concept"] for c in out["concepts"]]

    assert order.index("wal-mode-x") < order.index("done-term") < order.index("declared-only-term")
    assert by["wal-mode-x"]["asked"] == 2 and by["wal-mode-x"]["declared_only"] is False
    assert "Is wal-mode-x safe with two writers?" in by["wal-mode-x"]["probes"]
    assert by["done-term"]["done"] >= 1
    assert by["declared-only-term"]["declared_only"] is True
    assert by["gap-term"]["declared_only"] is True and by["gap-term"]["declared"] is True
    assert out["active_projects"] == ["p"]


def test_active_projects_matches_dream(vault_factory):
    tv = vault_factory()
    tv.with_note("alpha work", note_type=NoteType.SESSION, project="alpha")
    tv.with_note("meta", note_type=NoteType.SESSION, project="_unscoped")
    prio = tv.config.vault_root / "config" / "PRIORITIES.yaml"
    prio.parent.mkdir(parents=True, exist_ok=True)
    prio.write_text("focus:\n  active_projects: [pinned-proj]\n", encoding="utf-8")
    tv.indexed()

    dream_ap = scan(tv.config, project="t").knowledge_delta["active_focus"]["active_projects"]
    idx = Indexer(config=tv.config)
    try:
        ours = focus.active_projects(
            idx.db, now=NOW, window_days=tv.config.salience_activity_window_days,
            pins=["pinned-proj"],
        )
    finally:
        idx.close()
    assert ours == dream_ap == ["alpha", "pinned-proj"]


def test_concept_gaps_parser():
    text = "## Concept Gaps\n- `a-b` note\n* c-d: more\n\n## Next\n- not-me\n"
    assert focus.concept_gaps(text) == ["a-b", "c-d"]
    assert focus.concept_gaps("no section") == []


def test_asked_tier_has_entry_floor(vault_factory):
    """A single probe must not outrank heavy behavioural evidence; at the
    floor (``brief_attention_pressure``, default 2) it does."""
    tv = vault_factory()
    tv.with_note("vocab", project="p",
                 extra_frontmatter={"proposed_concepts": ["once-term", "twice-term"]})
    for _ in range(20):
        tv.with_note("work", note_type=NoteType.SESSION, project="p",
                     extra_frontmatter={"concepts": ["heavy-term"]})
    _probe(tv, "p", "What about once-term and twice-term?")
    _probe(tv, "p", "Again: twice-term?")
    tv.indexed()

    order = [c["concept"] for c in focus.rank(tv.config, now=NOW)["concepts"]]
    assert order.index("twice-term") < order.index("heavy-term") < order.index("once-term")


def test_research_focus_filename_is_resolved_from_landing_files(vault_factory):
    tv = vault_factory()
    cfg_dir = tv.config.vault_root / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "sources.yaml").write_text("landing_files:\n  research_focus: FOCUS.md\n")
    (tv.config.vault_root / "FOCUS.md").write_text("## Concept Gaps\n- renamed-gap\n")
    tv.indexed()
    assert "renamed-gap" in [c["concept"] for c in focus.rank(tv.config, now=NOW)["concepts"]]


def test_asked_below_floor_is_reported_not_ranked(vault_factory):
    tv = vault_factory()
    tv.with_note("vocab", project="p", extra_frontmatter={"proposed_concepts": ["once-term"]})
    _probe(tv, "p", "What is once-term?")
    tv.indexed()
    out = focus.rank(tv.config, now=NOW)
    assert out["asked_below_floor"] == [
        {"concept": "once-term", "asked": 1, "probes": ["What is once-term?"]}
    ]
    assert [c for c in out["concepts"] if c["concept"] == "once-term"][0]["asked"] == 1
