"""``/learn`` deterministic seams (#171).

Seams: ``learn.validate_learn_note`` (the learn-note frontmatter contract),
``served.mark`` (``context_served`` keyed by ``ses-`` id, buffer keyed by
``source_session``; unresolvable → nothing), and ``learn.probe`` (a probe
row visible to ``recent_probe_questions`` / ``weave_prompts``). Retrieval
and the trajectory/material partition are the skill's job over
``weave_search``/``weave_concepts`` (dec-696bacfb) — no Python seam.
"""

from __future__ import annotations

import json

import pytest

from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.operations import learn, served
from thinkweave.operations.prompts import recent_probe_questions

HUB_ID = "n-hubkl-d01"
UUID = "11111111-2222-3333-4444-555555555555"
TOPIC = "kl divergence"
CONCEPTS = ["kl-divergence"]


def _ids(handle):
    idx = Indexer(config=handle.config)
    try:
        return {
            r["type"]: r["id"]
            for r in idx.db.execute("SELECT id, type FROM notes")
        }
    finally:
        idx.close()


@pytest.fixture
def arc(vault_factory):
    """A session note, a source, and a concept hub all about KL divergence."""
    handle = vault_factory(
        notes=[
            {
                "title": "Session on KL divergence",
                "note_type": NoteType.SESSION,
                "project": "thinkweave",
                "body": "We discussed kl divergence between two gaussians.",
                "extra_frontmatter": {
                    "source_session": UUID,
                    "concepts": CONCEPTS,
                },
            },
            {
                "title": "Paper on KL divergence",
                "note_type": NoteType.SOURCE,
                "body": "KL divergence is the expected log ratio.",
                "extra_frontmatter": {"concepts": CONCEPTS, "source_type": "paper"},
            },
            {  # ChatGPT import: a source by type, but the user's own history
                "title": "Entropy vs cross-entropy vs KL",
                "note_type": NoteType.SOURCE,
                "body": "User asked why kl divergence is asymmetric.",
                "extra_frontmatter": {"concepts": CONCEPTS, "source_type": "conversation"},
            },
        ]
    )
    for rel, ntype in (("topics/kl-divergence.md", "concept-hub"), ("math-probability.md", "domain-hub")):
        hub = handle.config.vault_root / "concepts" / rel
        hub.parent.mkdir(parents=True, exist_ok=True)
        hub.write_text(
            f"---\ntype: {ntype}\nid: n-hub{hub.stem[:4]}01\ntitle: {hub.stem}\n"
            "concepts: [kl-divergence]\n---\n\nHub essence about kl divergence.\n",
            encoding="utf-8",
        )
    return handle.indexed()


# ── AC1: learn-note contract ─────────────────────────────────────────

GOOD_FM = {
    "type": "note",
    "kind": "learn",
    "topic": TOPIC,
    "concepts": CONCEPTS + ["entropy"],
    "solid": [{"concept": "kl-divergence", "date": "2026-08-23"}],
    "shaky": [{"concept": "entropy", "date": "2026-08-23", "why": "mixed up sign"}],
    "friction": ["symbol table came after the equation"],
    "explain_back": "KL is how surprised you are using q when p is true.",
    "builds_on": ["n-prior0001"],
    "questions": ["What is KL(p||q) for identical p, q?"],
}


def test_validate_learn_note_accepts_fixture():
    assert learn.validate_learn_note(GOOD_FM) == []


def test_validate_learn_note_reports_every_problem():
    bad = dict(GOOD_FM)
    bad["kind"] = "lesson"
    bad["solid"] = [{"concept": "kl-divergence"}]  # undated
    bad["shaky"] = [{"concept": "entropy", "date": "2026-08-23"}]  # no why
    bad["explain_back"] = ""
    bad["builds_on"] = "n-prior0001"  # not a list
    problems = learn.validate_learn_note(bad)
    assert len(problems) == 5
    assert any("kind" in p for p in problems)
    assert any("solid[0]" in p for p in problems)
    assert any("shaky[0]" in p for p in problems)
    assert any("explain_back" in p for p in problems)
    assert any("builds_on" in p for p in problems)


# ── AC4: context_served(source='learn') ──────────────────────────────


def _served_rows(handle):
    idx = Indexer(config=handle.config)
    try:
        return idx.db.execute(
            "SELECT session_id, note_id, source FROM context_served ORDER BY note_id"
        ).fetchall()
    finally:
        idx.close()


@pytest.mark.parametrize("key", ["uuid", "ses"])
def test_mark_keys_rows_by_ses_id_and_buffer_by_uuid(arc, key):
    ids = _ids(arc)
    session = UUID if key == "uuid" else ids["session"]
    n = served.mark(arc.config, "learn", session, "n-learn0001", [ids["source"], HUB_ID])
    assert n == 2
    rows = [tuple(r) for r in _served_rows(arc)]
    assert rows == [
        (ids["session"], HUB_ID, "learn"),
        (ids["session"], ids["source"], "learn"),
    ]
    buf = arc.config.weave_dir / "buffer" / f"{UUID}.jsonl"
    ev = json.loads(buf.read_text().splitlines()[-1])
    assert ev["type"] == "retrieval" and ev["tool"] == "learn"
    assert set(ev["returned_ids"]) == {ids["source"], HUB_ID}
    assert ev["args"]["note"] == "n-learn0001"


def test_mark_unresolvable_session_writes_nothing(arc):
    assert served.mark(arc.config, "learn", "not-a-session", "n-x", ["n-y"]) == 0
    assert _served_rows(arc) == []
    assert not (arc.config.weave_dir / "buffer").exists()


def test_mark_survives_index_rebuild(arc):
    """The buffer line is the durable record: a full rebuild after the
    Stop-time archive re-projects the same rows."""
    from thinkweave.core.buffer import archive_buffer

    ids = _ids(arc)
    served.mark(arc.config, "learn", UUID, "n-learn0001", [HUB_ID])
    sess_dir = served.resolve_session(arc.config, UUID).session_dir
    archive_buffer(arc.config.weave_dir, UUID, sess_dir)
    arc.indexed()
    assert [tuple(r) for r in _served_rows(arc)] == [(ids["session"], HUB_ID, "learn")]


# ── AC5: unanswered question → probe ─────────────────────────────────


def test_probe_is_visible_to_weave_prompts(arc):
    ok = learn.probe(arc.config, UUID, "Why is KL not symmetric?")
    assert ok is True
    rows = recent_probe_questions(arc.config, "thinkweave")
    assert [r["text"] for r in rows] == ["Why is KL not symmetric?"]


def test_probe_unresolvable_session_writes_nothing(arc):
    assert learn.probe(arc.config, "nope", "x?") is False
    assert not (arc.config.weave_dir / "buffer").exists()
