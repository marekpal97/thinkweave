"""The devloop route's normalizer (#217): one emitted trajectory payload
compiles to one work-grain task note round — envelope rows from the stage
log, the semantic trace nested inside the round, nothing at top level. The
devloop rail stays untouched; the fixtures pin its emitted schema."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thinkweave.core.task_contract import (
    DEVLOOP_TRACE_KEYS,
    normalize_devloop_run,
    validate_task_note,
)

FIXTURES = Path(__file__).parent / "fixtures"
TASK_ID = "tsk-217aaaaa"


def payload(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture()
def rich() -> dict:
    return normalize_devloop_run(payload("devloop-run-rich.json"), task_id=TASK_ID)


@pytest.fixture()
def thin() -> dict:
    return normalize_devloop_run(payload("devloop-run-thin.json"), task_id=TASK_ID)


class TestRichRun:
    def test_the_note_passes_the_contract_validator(self, rich):
        assert validate_task_note(rich) == []

    def test_one_open_work_grain_round(self, rich):
        assert rich["id"] == TASK_ID
        assert rich["status"] == "open"  # closure is the PR merge, not this compile
        assert rich["grain"] == "work"
        assert rich["asked"] == "#217"
        assert rich["title"] == "loop trajectory #217: devloop envelope normalizer"
        assert len(rich["rounds"]) == 1

    def test_trace_fields_nest_inside_the_round_never_at_top_level(self, rich):
        # top-level "rounds" is the ledger itself; the trace's other names
        # must not appear beside it
        assert DEVLOOP_TRACE_KEYS & rich.keys() == {"rounds"}
        entry = rich["rounds"][0]
        assert entry["rounds"] == [
            {
                "gate": "review",
                "finding": "trace fields at top level",
                "severity": "major",
                "disposition": "fixed",
                "fixed_by": "2",
            }
        ]
        assert entry["simplify"] == {
            "outcome": "applied",
            "cuts": [{"what": "helper dataclass", "why": "one consumer"}],
            "kept": [{"what": "closed key tables", "why": "the contract surface"}],
            "lines_delta": -42,
        }

    def test_each_stage_record_is_one_envelope_row(self, rich):
        rows = rich["rounds"][0]["envelopes"]
        assert rows == [
            {
                "task_id": TASK_ID,
                "outcome": "ok",
                "role": "implementer",
                "harness": "claude-code",
                "model": "claude-fable-5",
                "session_ref": {
                    "harness": "claude-code",
                    "kind": "session_id",
                    "value": "56c88c16-ae09-4cb6-ad3c-076ea2be3a4e",
                },
                "cost": {"tokens": 184000, "duration": 1260},
            },
            {"task_id": TASK_ID, "outcome": "ok", "role": "judge"},
        ]

    def test_dispatch_join_keys_stay_out_of_the_skills_trace(self, rich):
        assert rich["rounds"][0]["skills"] == [
            {
                "id": "implementer",
                "role": "implementer",
                "outcome": "ok",
                "fix_rounds_attributed": 1,
            },
            {"id": "judge", "role": "judge", "outcome": "ok", "fix_rounds_attributed": 1},
        ]

    def test_null_criterion_counts_are_dropped_not_carried(self, rich):
        assert rich["rounds"][0]["criteria"] == [
            {"id": "AC1", "verdict": "met", "flipped_by_round": 1},
            {"id": "AC2", "verdict": "met"},
        ]

    def test_emitter_extras_beyond_the_contract_vocabulary_do_not_leak(self, rich):
        entry = rich["rounds"][0]
        assert "stack_simplify" not in entry
        assert "edge_cases" not in entry
        assert "tdd" not in entry

    def test_served_and_outputs_delta_land_on_the_round(self, rich):
        entry = rich["rounds"][0]
        assert entry["served"] == ["dec-1c903854", "dec-ba94712f"]
        assert entry["did"] == {
            "paths": ["src/thinkweave/core/task_contract.py"],
            "attempts": 2,
        }


class TestThinRun:
    """The emitter drops unknown and unprovided keys; absence is normal."""

    def test_the_note_passes_the_contract_validator(self, thin):
        assert validate_task_note(thin) == []

    def test_missing_sections_stay_absent(self, thin):
        entry = thin["rounds"][0]
        assert entry["envelopes"] == []
        assert entry["did"] == {"paths": [], "attempts": 0}
        assert "served" not in entry
        assert not DEVLOOP_TRACE_KEYS & entry.keys()


class TestRefusals:
    def test_a_payload_without_frontmatter_is_refused(self):
        with pytest.raises(ValueError, match="frontmatter"):
            normalize_devloop_run({"title": "no frontmatter"}, task_id=TASK_ID)

    def test_a_stage_record_without_an_outcome_is_refused_by_field(self):
        run = payload("devloop-run-thin.json")
        run["frontmatter"]["skills"] = [
            {"id": "implementer", "role": "implementer", "outcome": "",
             "fix_rounds_attributed": 0}
        ]
        with pytest.raises(ValueError, match="outcome"):
            normalize_devloop_run(run, task_id=TASK_ID)
