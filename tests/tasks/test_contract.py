"""The ``kind: task`` note contract: golden fixtures, the shape
validator's rejections, the unified envelope schema, the two edge types,
and the declared records ``SessionRef`` and ``Round``."""

from __future__ import annotations

from pathlib import Path

import pytest

from thinkweave.core.indexer import EDGE_FIELD_MAP
from thinkweave.core.schemas import LIST_FRONTMATTER_KEYS, EdgeType
from thinkweave.core.task_contract import (
    Round,
    SessionRef,
    accepts_round,
    envelope_return_name,
    normalize_tracker_ref,
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
    fm["reviews"] = [{"gate": "review", "finding": "x"}]
    fm["rounds"] = [
        {"gate": "review", "finding": "x", "severity": "major"}
    ]
    errors = validate_task_note(fm)
    assert any("criteria" in e for e in errors)
    assert any("reviews" in e for e in errors)
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
# Edge vocabulary: consumes + feedback_for


def test_new_edge_types_registered():
    assert EdgeType.CONSUMES.value == "consumes"
    assert EdgeType.FEEDBACK_FOR.value == "feedback_for"
    assert EDGE_FIELD_MAP["consumes"] == "consumes"
    assert EDGE_FIELD_MAP["feedback_for"] == "feedback_for"
    assert "consumes" in LIST_FRONTMATTER_KEYS
    assert "feedback_for" in LIST_FRONTMATTER_KEYS


# ---------------------------------------------------------------------------
# Ledger round: references to what other surfaces own


def ledger_round() -> dict:
    return {
        "route": "session",
        "session_ref": {"harness": "claude-code", "kind": "note", "value": "ses-1a2b3c4d"},
        "notes": ["n-1a2b3c4d"],
        "decisions": {"minted": ["dec-1a2b3c4d"]},
        "feedback": [{"register": "correction", "prompt_ref": "no", "ts": "t"}],
        "outputs": [
            {"kind": "pr", "ref": "https://github.com/o/r/pull/1", "role": "deliverable"},
            {"kind": "file", "ref": "src/x.py", "role": "intermediate"},
            {"kind": "url", "ref": "https://deck.example", "role": "deliverable"},
        ],
        "did": {"commits": ["abc1234"], "paths": ["src/x.py"], "attempts": 1},
        "children": ["tsk-0000aaaa"],
        "tools": {"Bash": 4, "Edit": 2},
        "tool_errors": 1,
    }


def test_ledger_round_validates_clean():
    fm = load("declared-only")
    fm["rounds"] = [ledger_round()]
    assert validate_task_note(fm) == []


def test_route_and_output_vocabularies_are_closed():
    fm = load("declared-only")
    entry = ledger_round()
    entry["route"] = "herdr"
    entry["outputs"] = [{"kind": "blob", "ref": "x", "role": "final"}]
    fm["rounds"] = [entry]
    errors = validate_task_note(fm)
    assert any("route" in e for e in errors)
    assert any("kind" in e for e in errors)
    assert any("role" in e for e in errors)


def test_work_grain_outputs_need_a_role():
    fm = load("declared-only")
    fm["rounds"] = [{"outputs": [{"kind": "file", "ref": "src/x.py"}]}]
    assert any("role" in e for e in validate_task_note(fm))


def test_child_task_outputs_carry_no_role():
    fm = load("envelope-thin")
    fm["rounds"][0]["outputs"] = [{"kind": "note", "ref": "n-1a2b3c4d"}]
    assert validate_task_note(fm) == []
    fm["rounds"][0]["outputs"] = []
    assert validate_task_note(fm) == []


@pytest.mark.parametrize(
    "value, expected",
    [
        ("#7", "github:o/r#7"),
        ("https://github.com/a/b/issues/12", "github:a/b#12"),
        ("github:a/b#12", "github:a/b#12"),
        ("jira:ENG-42", "jira:ENG-42"),
        ("audit the clusters", "audit the clusters"),
    ],
)
def test_tracker_refs_normalize(value, expected):
    assert normalize_tracker_ref(value, repo="o/r") == expected


def test_bare_issue_number_without_a_repo_stays_bare():
    assert normalize_tracker_ref("#7", repo="") == "#7"


# ---------------------------------------------------------------------------
# Declared records: SessionRef and Round validate once, at from_dict


def test_session_ref_constructors_name_their_kind():
    assert SessionRef.agent("claude-code", "a1").to_dict() == {
        "harness": "claude-code", "kind": "agent_id", "value": "a1",
    }
    assert SessionRef.session("codex", "s1").kind == "session_id"
    assert SessionRef.note("devloop", "n-1").kind == "note"


def test_session_ref_from_dict_refuses_a_bare_string():
    with pytest.raises(ValueError, match="triple"):
        SessionRef.from_dict("7d0e5f4a")
    ref = SessionRef.note("claude-code", "ses-1a2b3c4d")
    assert SessionRef.from_dict(ref.to_dict()) == ref


def test_round_round_trips_its_mapping_unchanged():
    entry = ledger_round()
    parsed = Round.from_dict(entry, grain="work")
    assert parsed.session_ref == SessionRef.note("claude-code", "ses-1a2b3c4d")
    assert parsed.to_dict() == entry


def test_round_from_dict_names_every_bad_field():
    entry = {**ledger_round(), "route": "herdr", "nonsense": 1}
    with pytest.raises(ValueError) as exc:
        Round.from_dict(entry, grain="work")
    assert "route" in str(exc.value) and "nonsense" in str(exc.value)


def test_trace_fields_are_refused_off_the_work_grain():
    with pytest.raises(ValueError, match="work-grain"):
        Round.from_dict({"criteria": []}, grain="per-dispatch")


@pytest.mark.parametrize(
    "fm, route, expected",
    [
        ({"status": "open", "grain": "work"}, "session", True),
        ({"status": "open", "grain": "work"}, "dispatch", False),
        ({"status": "open", "grain": "per-dispatch"}, "dispatch", True),
        ({"status": "open", "grain": "batch"}, "dispatch", True),
        ({"status": "closed", "grain": "work"}, "session", False),
        ({"status": "open", "grain": "work"}, "devloop", True),
    ],
)
def test_round_acceptance_by_route(fm, route, expected):
    assert accepts_round({"kind": "task", **fm}, route) is expected


def test_task_note_outcome_is_not_a_contract_field():
    fm = load("declared-only")
    fm["outcome"] = [{"label": "merged-clean"}]
    assert any("outcome" in e for e in validate_task_note(fm))
