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
        assert rich["asked"] == "github:marekpal97/thinkweave#217"
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


class TestLedgerRoute:
    """A loop run is one ``route: devloop`` round: the trajectory note is its
    session ref and the PR its deliverable."""

    def test_the_round_names_its_route_trajectory_and_deliverable(self):
        note = normalize_devloop_run(
            payload("devloop-run-rich.json"), task_id=TASK_ID,
            trajectory="n-7a7a7a7a",
        )
        assert validate_task_note(note) == []
        entry = note["rounds"][0]
        assert entry["route"] == "devloop"
        assert entry["session_ref"] == {
            "harness": "devloop", "kind": "note", "value": "n-7a7a7a7a",
        }
        assert entry["outputs"] == [
            {
                "kind": "pr",
                "ref": "https://github.com/marekpal97/thinkweave/pull/999",
                "role": "deliverable",
            }
        ]

    def test_a_run_without_a_pr_declares_no_output(self, thin):
        assert "outputs" not in thin["rounds"][0]

    def test_a_run_and_a_later_wrap_on_the_same_ref_build_one_task(self, cfg):
        from thinkweave.core.vault import parse_frontmatter
        from thinkweave.operations import hook_events, task_seam

        run = task_seam.record_devloop_run(
            cfg, payload("devloop-run-rich.json"), project="t",
            trajectory="n-7a7a7a7a",
        )
        decl = {
            "declared": [
                {
                    "title": "fix-up after review",
                    "asked": "github:marekpal97/thinkweave#217",
                    "round": {"did": {"attempts": 1}},
                }
            ]
        }
        result = task_seam.reconcile_tasks(
            cfg, decl, session_key="s-9", project="t",
            streams=[hook_events.register_path(cfg.weave_dir, "s-9")],
        )
        assert result.minted == [] and result.appended == [run]
        tasks = list(cfg.vault_root.rglob("tsk-*.md"))
        assert len(tasks) == 1
        fm, _ = parse_frontmatter(tasks[0].read_text(encoding="utf-8"))
        assert validate_task_note(fm) == []
        assert [r.get("route") for r in fm["rounds"]] == ["devloop", "session"]

    def test_rerecording_a_run_replaces_its_round(self, cfg):
        from thinkweave.core.vault import parse_frontmatter
        from thinkweave.operations import task_seam

        args = (cfg, payload("devloop-run-rich.json"))
        first = task_seam.record_devloop_run(*args, project="t", trajectory="n-7a7a7a7a")
        again = task_seam.record_devloop_run(*args, project="t", trajectory="n-7a7a7a7a")
        assert first == again
        fm, _ = parse_frontmatter(
            task_seam.find_stub(cfg, first).read_text(encoding="utf-8")
        )
        assert len(fm["rounds"]) == 1
