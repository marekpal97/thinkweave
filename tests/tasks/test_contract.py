"""The ``kind: task`` note contract (#186): golden fixtures, the shape
validator's rejections, the unified envelope schema, the two new edge
types, and the reserved ``task_ref`` field on feedback events."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thinkweave.core.events import feedback_events
from thinkweave.core.indexer import EDGE_FIELD_MAP
from thinkweave.core.schemas import LIST_FRONTMATTER_KEYS, EdgeType
from thinkweave.core.task_contract import (
    FEEDBACK_TASK_REF_FIELD,
    envelope_return_name,
    validate_envelope,
    validate_task_note,
)
from thinkweave.core.vault import parse_frontmatter

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = ["devloop-rich", "envelope-thin", "declared-only"]


def load(name: str) -> dict:
    fm, _body = parse_frontmatter(
        (FIXTURES / f"{name}.md").read_text(encoding="utf-8")
    )
    return fm


# ---------------------------------------------------------------------------
# Golden fixtures


@pytest.mark.parametrize("name", GOLDEN)
def test_golden_fixture_validates_clean(name):
    assert validate_task_note(load(name)) == []


# ---------------------------------------------------------------------------
# Discriminator: kind: task on type: note, never a new NoteType


def test_requires_type_note_with_kind_task():
    fm = load("declared-only")
    fm["type"] = "task"
    errors = validate_task_note(fm)
    assert errors and "kind" in errors[0]

    fm = load("declared-only")
    del fm["kind"]
    assert validate_task_note(fm)


def test_discriminator_refused_before_key_noise():
    """A non-task note gets one discriminator error, not a key spray."""
    errors = validate_task_note({"type": "note", "kind": "trajectory"})
    assert len(errors) == 1


# ---------------------------------------------------------------------------
# session_ref is a qualified {harness, kind, value} triple


def test_rejects_bare_string_session_ref_in_round():
    fm = load("envelope-thin")
    fm["rounds"][0]["session_ref"] = "7d0e5f4a-9c3b-4e21"
    errors = validate_task_note(fm)
    assert any("session_ref" in e and "triple" in e for e in errors)


def test_rejects_bare_string_session_ref_in_envelope():
    row = {
        "task_id": "tsk-9b2d4e6f",
        "outcome": "ok",
        "session_ref": "7d0e5f4a",
    }
    errors = validate_envelope(row)
    assert any("session_ref" in e and "triple" in e for e in errors)


def test_rejects_incomplete_session_ref_triple():
    fm = load("envelope-thin")
    fm["rounds"][0]["session_ref"] = {"harness": "claude-code"}
    assert any("session_ref" in e for e in validate_task_note(fm))


# ---------------------------------------------------------------------------
# Devloop trace fields nest inside one work-grain round entry


def test_rejects_devloop_trace_fields_at_top_level():
    fm = load("declared-only")
    fm["criteria"] = [{"id": "AC1", "verdict": "met"}]
    fm["simplify"] = {"outcome": "applied"}
    fm["rounds"] = [
        {"gate": "review", "finding": "x", "severity": "major"}
    ]
    errors = validate_task_note(fm)
    assert any("criteria" in e for e in errors)
    assert any("simplify" in e for e in errors)
    assert any("gate" in e for e in errors)


def test_rejects_trace_fields_on_non_work_grain_round():
    fm = load("envelope-thin")
    fm["rounds"][0]["criteria"] = [{"id": "AC1", "verdict": "met"}]
    errors = validate_task_note(fm)
    assert any("criteria" in e and "work" in e for e in errors)


# ---------------------------------------------------------------------------
# Structured ledger: narrative and unknown fields are absent by schema


@pytest.mark.parametrize("key", ["summary", "insights", "narrative"])
def test_rejects_narrative_fields(key):
    fm = load("declared-only")
    fm[key] = "prose that belongs in a session note"
    assert any(key in e for e in validate_task_note(fm))


def test_rejects_bad_status_grain_and_id():
    fm = load("declared-only")
    fm["status"] = "done"
    fm["grain"] = "session"
    fm["id"] = "task-1"
    errors = validate_task_note(fm)
    assert any("status" in e for e in errors)
    assert any("grain" in e for e in errors)
    assert any("id" in e for e in errors)


def test_rounds_must_be_a_list():
    fm = load("declared-only")
    fm["rounds"] = "round one"
    assert any("rounds" in e for e in validate_task_note(fm))


def test_non_mapping_input_reports_instead_of_raising():
    assert validate_task_note("not a note")
    assert validate_envelope("not an envelope")


# ---------------------------------------------------------------------------
# Unified envelope schema


def test_minimal_envelope_validates_clean():
    assert validate_envelope({"task_id": "tsk-3f9a1c2e", "outcome": "ok"}) == []


def test_envelope_requires_task_id_and_outcome():
    assert any("task_id" in e for e in validate_envelope({"outcome": "ok"}))
    assert any(
        "outcome" in e for e in validate_envelope({"task_id": "tsk-3f9a1c2e"})
    )


def test_envelope_rejects_unknown_keys():
    row = {"task_id": "tsk-3f9a1c2e", "outcome": "ok", "summary": "prose"}
    assert any("summary" in e for e in validate_envelope(row))


def test_envelope_return_file_is_named_by_task_id():
    assert envelope_return_name("tsk-3f9a1c2e") == "tsk-3f9a1c2e.jsonl"


# ---------------------------------------------------------------------------
# task_ref: reserved on feedback events, zero behavior


def test_task_ref_reserved_field_passes_through_feedback_events(tmp_path):
    assert FEEDBACK_TASK_REF_FIELD == "task_ref"
    events = tmp_path / "events.jsonl"
    row = {
        "ts": "2026-09-22T10:00:00+00:00",
        "type": "feedback",
        "session_id": "s1",
        "register": "correction",
        "prompt_ref": "use the rounds ledger",
        "task_ref": "tsk-3f9a1c2e",
    }
    events.write_text(json.dumps(row) + "\n", encoding="utf-8")
    rows = feedback_events(events)
    assert len(rows) == 1
    assert rows[0]["task_ref"] == "tsk-3f9a1c2e"


# ---------------------------------------------------------------------------
# Edge vocabulary: consumes + feedback_for


def test_new_edge_types_registered():
    assert EdgeType.CONSUMES.value == "consumes"
    assert EdgeType.FEEDBACK_FOR.value == "feedback_for"
    assert EDGE_FIELD_MAP["consumes"] == "consumes"
    assert EDGE_FIELD_MAP["feedback_for"] == "feedback_for"
    assert "consumes" in LIST_FRONTMATTER_KEYS
    assert "feedback_for" in LIST_FRONTMATTER_KEYS
