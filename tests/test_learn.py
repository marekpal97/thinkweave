"""``/learn`` deterministic seams (#171).

Seams: ``validate_learn_note`` (the learn-note frontmatter contract,
``surfaces/cli/learn.py``) and ``record_probe`` (``operations/prompts.py``
— a probe row visible to ``recent_probe_questions`` / ``weave_prompts``).
Retrieval and the trajectory/material partition are the skill's job over
``weave_search``/``weave_concepts``, and its MCP calls land in
``context_served`` through the standard retrieval logging — no mark step,
no Python seam (dec-696bacfb).
"""

from __future__ import annotations

import pytest

from thinkweave.core.schemas import NoteType
from thinkweave.operations.prompts import record_probe, recent_probe_questions, resolve_session
from thinkweave.surfaces.cli.learn import validate_learn_note

UUID = "11111111-2222-3333-4444-555555555555"
TOPIC = "kl divergence"
CONCEPTS = ["kl-divergence"]


@pytest.fixture
def arc(vault_factory):
    """A session note about KL divergence, keyed to a harness UUID."""
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
        ]
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
    assert validate_learn_note(GOOD_FM) == []


def test_validate_learn_note_reports_every_problem():
    bad = dict(GOOD_FM)
    bad["kind"] = "lesson"
    bad["solid"] = [{"concept": "kl-divergence"}]  # undated
    bad["shaky"] = [{"concept": "entropy", "date": "2026-08-23"}]  # no why
    bad["explain_back"] = ""
    bad["builds_on"] = "n-prior0001"  # not a list
    problems = validate_learn_note(bad)
    assert len(problems) == 5
    assert any("kind" in p for p in problems)
    assert any("solid[0]" in p for p in problems)
    assert any("shaky[0]" in p for p in problems)
    assert any("explain_back" in p for p in problems)
    assert any("builds_on" in p for p in problems)


# ── AC5: unanswered question → probe ─────────────────────────────────


def test_probe_is_visible_to_weave_prompts(arc):
    ok = record_probe(arc.config, UUID, "Why is KL not symmetric?")
    assert ok is True
    rows = recent_probe_questions(arc.config, "thinkweave")
    assert [r["text"] for r in rows] == ["Why is KL not symmetric?"]


def test_probe_resolves_ses_id_too(arc):
    ref = resolve_session(arc.config, UUID)
    assert ref is not None
    assert record_probe(arc.config, ref.ses_id, "Second question?") is True
    # events land in the buffer keyed by the harness UUID, never the ses- id
    assert (arc.config.weave_dir / "buffer" / f"{UUID}.jsonl").exists()
    assert not (arc.config.weave_dir / "buffer" / f"{ref.ses_id}.jsonl").exists()


def test_probe_unresolvable_session_writes_nothing(arc):
    assert record_probe(arc.config, "nope", "x?") is False
    assert not (arc.config.weave_dir / "buffer").exists()


def test_learn_mark_subcommand_is_gone():
    """dec-696bacfb: no mark — MCP retrieval calls log context_served."""
    from thinkweave.surfaces.cli.parser import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["learn", "mark", "--session", "x", "--note", "n", "--served", "a"])
