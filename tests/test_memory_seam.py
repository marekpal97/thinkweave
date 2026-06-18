"""Tests for ``synthesis/memory_seam.py`` — the CC-memory↔vault seam.

Covers the deterministic, embedding-free half: fact collection (content
hash + mtime keys), the dirty diff against the durable map (new /
content_changed / prior_unresolved / recheck_due / removed), the
project-type stale prior, state rebuild with carry-forward, and the
report render. Twin resolution + verdict judgment are the worker's job and
are not exercised here.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.synthesis import memory_seam as ms


# ---------------------------------------------------------------------------
# Fixtures — a fake CC memory tree + a vault Config
# ---------------------------------------------------------------------------


def _write_fact(
    weave_dir: Path, slug: str, *, weave_type: str, desc: str, body: str = "body text"
) -> Path:
    weave_dir.mkdir(parents=True, exist_ok=True)
    p = weave_dir / f"{slug}.md"
    p.write_text(
        f"---\nname: {slug}\ndescription: {desc}\nmetadata:\n  type: {weave_type}\n"
        f"---\n\n{body}\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def cc_tree(tmp_path: Path, monkeypatch):
    """A fake ~/.claude/projects/<proj>/memory tree with two facts."""
    projects = tmp_path / "projects"
    proj_mem = projects / "-home-x-python-projects-demo" / "memory"
    _write_fact(proj_mem, "feedback-style", weave_type="feedback",
                desc="prefer linear flows")
    _write_fact(proj_mem, "proj-status", weave_type="project",
                desc="14 tools as of April")
    # A MEMORY.md index that must be ignored.
    (proj_mem / "MEMORY.md").write_text("# index\n- pointer", encoding="utf-8")
    monkeypatch.setattr(ms, "CC_PROJECTS_ROOT", projects)
    monkeypatch.setattr(ms, "CC_GLOBAL_DIR", tmp_path / "no-global")
    return proj_mem


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(vault_root=tmp_path / "vault")


# ---------------------------------------------------------------------------
# collect_cc_facts
# ---------------------------------------------------------------------------


def test_collect_walks_facts_and_skips_index(cc_tree):
    facts = ms.collect_cc_facts()
    slugs = {f["slug"] for f in facts}
    assert slugs == {"feedback-style", "proj-status"}  # MEMORY.md skipped
    f = next(f for f in facts if f["slug"] == "proj-status")
    assert f["weave_type"] == "project"
    assert f["key"].endswith("::proj-status")
    assert f["content_hash"]  # non-empty hash
    assert "14 tools" in f["query"]


def test_content_hash_changes_on_edit(cc_tree):
    h1 = {f["slug"]: f["content_hash"] for f in ms.collect_cc_facts()}
    _write_fact(cc_tree, "proj-status", weave_type="project",
                desc="18 tools as of June")
    h2 = {f["slug"]: f["content_hash"] for f in ms.collect_cc_facts()}
    assert h1["proj-status"] != h2["proj-status"]
    assert h1["feedback-style"] == h2["feedback-style"]  # untouched


# ---------------------------------------------------------------------------
# stale_prior
# ---------------------------------------------------------------------------


def test_stale_prior_only_old_project_facts():
    now = datetime(2026, 6, 13, tzinfo=timezone.utc)
    old = now.timestamp() - 40 * 86400
    fresh = now.timestamp() - 5 * 86400
    assert ms.stale_prior(
        {"weave_type": "project", "mtime": old}, stale_age_days=30, now=now
    )
    assert not ms.stale_prior(
        {"weave_type": "project", "mtime": fresh}, stale_age_days=30, now=now
    )
    # feedback never flags, even when ancient
    assert not ms.stale_prior(
        {"weave_type": "feedback", "mtime": old}, stale_age_days=30, now=now
    )


# ---------------------------------------------------------------------------
# detect_dirty
# ---------------------------------------------------------------------------


def _detect(facts, state, **kw):
    kw.setdefault("stale_age_days", 30)
    kw.setdefault("recheck_days", 14)
    kw.setdefault("cap", 20)
    return ms.detect_dirty(facts, state, **kw)


def test_empty_state_marks_all_new(cc_tree):
    facts = ms.collect_cc_facts()
    surface = _detect(facts, {"facts": []})
    assert {d["reason"] for d in surface["dirty"]} == {"new"}
    assert surface["carried_count"] == 0
    assert surface["removed"] == []


def test_clean_fact_carries_forward(cc_tree):
    now = datetime.now(timezone.utc)
    facts = ms.collect_cc_facts()
    # Prior state: both facts confirmed-fresh, judged just now.
    prior = {
        "facts": [
            {
                "key": f["key"],
                "content_hash": f["content_hash"],
                "verdict": "confirmed-fresh",
                "judged_at": now.isoformat(),
            }
            for f in facts
        ]
    }
    surface = _detect(facts, prior, now=now)
    assert surface["dirty"] == []
    assert surface["carried_count"] == len(facts)


def test_unresolved_and_recheck_resurface(cc_tree):
    now = datetime.now(timezone.utc)
    facts = ms.collect_cc_facts()
    by_slug = {f["slug"]: f for f in facts}
    prior = {
        "facts": [
            {  # stale → always resurfaces
                "key": by_slug["proj-status"]["key"],
                "content_hash": by_slug["proj-status"]["content_hash"],
                "verdict": "stale",
                "judged_at": now.isoformat(),
            },
            {  # confirmed but judged 30d ago → recheck_due
                "key": by_slug["feedback-style"]["key"],
                "content_hash": by_slug["feedback-style"]["content_hash"],
                "verdict": "confirmed-fresh",
                "judged_at": (now - timedelta(days=30)).isoformat(),
            },
        ]
    }
    surface = _detect(facts, prior, now=now)
    reasons = {d["key"]: d["reason"] for d in surface["dirty"]}
    assert reasons[by_slug["proj-status"]["key"]] == "prior_unresolved"
    assert reasons[by_slug["feedback-style"]["key"]] == "recheck_due"


def test_removed_facts_detected(cc_tree):
    facts = ms.collect_cc_facts()
    prior = {"facts": [{
        "key": "gone::old-fact", "content_hash": "x", "verdict": "stale",
    }]}
    surface = _detect(facts, prior)
    assert "gone::old-fact" in surface["removed"]


def test_cap_bounds_dirty(cc_tree):
    facts = ms.collect_cc_facts()
    surface = _detect(facts, {"facts": []}, cap=1)
    assert len(surface["dirty"]) == 1
    assert surface["dirty_total"] == 2  # uncapped count preserved


# ---------------------------------------------------------------------------
# build_state + render
# ---------------------------------------------------------------------------


def test_build_state_merges_verdicts_and_carries(cc_tree):
    now = datetime.now(timezone.utc)
    facts = ms.collect_cc_facts()
    by_slug = {f["slug"]: f for f in facts}
    # Prior: feedback already confirmed; proj has no prior.
    prior = {"facts": [{
        "key": by_slug["feedback-style"]["key"],
        "verdict": "confirmed-fresh",
        "verdict_reason": "twin agrees",
        "twin": {"id": "n-1", "cosine": 0.8},
        "judged_at": (now - timedelta(days=1)).isoformat(),
    }]}
    # Worker rules only proj-status this cycle.
    verdicts = {by_slug["proj-status"]["key"]: {
        "verdict": "stale", "reason": "14 vs 18",
        "twin": {"id": "dec-2", "cosine": 0.75},
    }}
    state = ms.build_state(facts, prior, verdicts, stale_age_days=30, now=now)
    out = {r["key"]: r for r in state["facts"]}
    assert out[by_slug["proj-status"]["key"]]["verdict"] == "stale"
    # feedback carried forward untouched
    assert out[by_slug["feedback-style"]["key"]]["verdict"] == "confirmed-fresh"
    assert out[by_slug["feedback-style"]["key"]]["twin"]["id"] == "n-1"


def test_build_state_unjudged_when_no_verdict_no_prior(cc_tree):
    facts = ms.collect_cc_facts()
    state = ms.build_state(facts, {"facts": []}, {}, stale_age_days=30)
    assert all(r["verdict"] == "unjudged" for r in state["facts"])


def test_build_state_drops_removed(cc_tree):
    facts = ms.collect_cc_facts()
    prior = {"facts": [{"key": "gone::x", "verdict": "stale"}]}
    state = ms.build_state(facts, prior, {}, stale_age_days=30)
    keys = {r["key"] for r in state["facts"]}
    assert "gone::x" not in keys


def test_render_report_sections(cc_tree):
    now = datetime.now(timezone.utc)
    state = {
        "generated_at": now.isoformat(),
        "facts": [
            {"key": "p::a", "slug": "a", "label": "demo", "verdict": "stale",
             "verdict_reason": "14 vs 18", "twin": {"id": "dec-1", "cosine": 0.7}},
            {"key": "p::b", "slug": "b", "label": "demo",
             "verdict": "confirmed-fresh", "twin": {"id": "n-2", "cosine": 0.82}},
            {"key": "p::c", "slug": "c", "label": "demo",
             "verdict": "durable-unique", "twin": {}},
        ],
    }
    md = ms.render_report(state)
    assert "## ⚠ Stale (1)" in md
    assert "14 vs 18" in md
    assert "## ✓ Confirmed-fresh (1)" in md
    assert "Durable-unique: 1 facts" in md


# ---------------------------------------------------------------------------
# state I/O round-trip
# ---------------------------------------------------------------------------


def test_state_save_load_roundtrip(cfg):
    state = {"generated_at": "2026-06-13T00:00:00+00:00",
             "facts": [{"key": "p::a", "verdict": "stale"}]}
    ms.save_state(cfg, state)
    assert ms.load_state(cfg) == state


def test_load_state_missing_is_empty_shell(cfg):
    assert ms.load_state(cfg) == {"facts": []}


def test_load_state_corrupt_is_empty_shell(cfg):
    p = ms.state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert ms.load_state(cfg) == {"facts": []}


# ---------------------------------------------------------------------------
# Session-scoped serving lens
# ---------------------------------------------------------------------------


def _state_with(*facts):
    return {"generated_at": "2026-06-13T00:00:00+00:00", "facts": list(facts)}


def test_flagged_twin_index_only_actionable_with_twin():
    state = _state_with(
        {"key": "p::a", "slug": "a", "verdict": "stale",
         "twin": {"id": "dec-1"}},
        {"key": "p::b", "slug": "b", "verdict": "diverged",
         "twin": {"id": "n-2"}},
        {"key": "p::c", "slug": "c", "verdict": "confirmed-fresh",
         "twin": {"id": "n-3"}},                 # healthy — excluded
        {"key": "p::d", "slug": "d", "verdict": "stale", "twin": {}},  # no twin
        {"key": "p::e", "slug": "e", "verdict": "durable-unique"},     # no twin
    )
    idx = ms.flagged_twin_index(state)
    assert set(idx) == {"dec-1", "n-2"}
    assert idx["dec-1"][0]["slug"] == "a"


def test_session_guard_empty_when_no_served_or_no_hit(cfg):
    ms.save_state(cfg, _state_with(
        {"key": "p::a", "slug": "a", "verdict": "stale",
         "twin": {"id": "dec-1"}, "content_hash": "h"},
    ))
    assert ms.session_guard_section(cfg, []) == ""          # nothing served
    assert ms.session_guard_section(cfg, ["n-999"]) == ""   # served, no hit


def test_session_guard_fires_on_served_twin(cfg):
    ms.save_state(cfg, _state_with(
        {"key": "p::a", "slug": "stale-fact", "verdict": "stale",
         "verdict_reason": "14 tools vs 18", "twin": {"id": "dec-1"},
         "content_hash": "h", "scope": "project", "file": "a.md"},
        {"key": "p::b", "slug": "fresh-fact", "verdict": "confirmed-fresh",
         "twin": {"id": "n-2"}, "content_hash": "h"},
    ))
    # dec-1 is served → guard fires; n-2 is healthy → never surfaces.
    out = ms.session_guard_section(cfg, ["dec-1", "n-2", "src-9"])
    assert "Durable-memory guard" in out
    assert "stale-fact" in out
    assert "14 tools vs 18" in out
    assert "[[dec-1]]" in out
    assert "fresh-fact" not in out


def test_session_guard_flags_live_drift(cc_tree, cfg, monkeypatch):
    # A flagged fact whose CC file still exists but whose content_hash in the
    # map is stale → the live re-hash marks it "edited since this verdict".
    facts = ms.collect_cc_facts()
    proj = next(f for f in facts if f["slug"] == "proj-status")
    ms.save_state(cfg, _state_with({
        "key": proj["key"], "slug": "proj-status", "verdict": "stale",
        "verdict_reason": "drifted", "twin": {"id": "dec-1"},
        "content_hash": "STALEHASH",  # != current on-disk hash
        "scope": "project", "file": "proj-status.md",
    }))
    out = ms.session_guard_section(cfg, ["dec-1"])
    assert "proj-status" in out
    assert "edited since this verdict" in out


def test_recompute_fact_hash_matches_collect(cc_tree, cfg):
    facts = ms.collect_cc_facts()
    proj = next(f for f in facts if f["slug"] == "proj-status")
    fact = {"key": proj["key"], "file": "proj-status.md", "scope": "project"}
    assert ms.recompute_fact_hash(fact) == proj["content_hash"]
