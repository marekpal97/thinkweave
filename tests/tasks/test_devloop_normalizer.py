"""The devloop route: one emitted trajectory payload compiles to one
work-grain ``route: devloop`` round — envelope rows from the stage log, the
semantic trace nested inside the round, nothing at top level. The devloop
rail stays untouched; the fixtures pin its emitted schema."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thinkweave.core.task_contract import (
    DEVLOOP_TRACE_KEYS,
    Round,
    devloop_ask,
    validate_task_note,
)
from thinkweave.core.vault import parse_frontmatter
from thinkweave.core.buffer import buffer_path
from thinkweave.operations import tasks
from thinkweave.operations.tasks import TaskStore

FIXTURES = Path(__file__).parent / "fixtures"
TASK_ID = "tsk-217aaaaa"


def payload(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def work_note(entry: Round) -> dict:
    """A minimal open work-grain note holding one round."""
    return {
        "type": "note", "kind": "task", "id": TASK_ID, "status": "open",
        "grain": "work", "rounds": [entry.to_dict()],
    }


def note_fm(cfg, task_id: str) -> dict:
    task = TaskStore(cfg).get(task_id)
    assert task is not None and task.path is not None
    return parse_frontmatter(task.path.read_text(encoding="utf-8"))[0]


def recorded(cfg, name: str, **kwargs) -> dict:
    """Record one run through the route; returns its task note frontmatter."""
    landed = tasks.record_run(cfg, payload(name), project="t", **kwargs)
    return note_fm(cfg, landed.task_id)


@pytest.fixture()
def rich() -> dict:
    return Round.from_devloop(payload("devloop-run-rich.json"), task_id=TASK_ID).to_dict()


@pytest.fixture()
def thin() -> dict:
    return Round.from_devloop(payload("devloop-run-thin.json"), task_id=TASK_ID).to_dict()


class TestRichRun:
    def test_the_round_passes_the_contract_validator(self):
        entry = Round.from_devloop(payload("devloop-run-rich.json"), task_id=TASK_ID)
        assert validate_task_note(work_note(entry)) == []

    def test_one_open_work_grain_round(self, cfg):
        note = recorded(cfg, "devloop-run-rich.json")
        assert validate_task_note(note) == []
        assert note["status"] == "open"  # a run never closes its task
        assert note["grain"] == "work"
        assert note["asked"] == "github:marekpal97/thinkweave#184"  # its epic
        assert note["title"] == "Task object as a ledger"  # its epic's
        assert len(note["rounds"]) == 1

    def test_trace_fields_nest_inside_the_round_never_at_top_level(self, cfg, rich):
        note = recorded(cfg, "devloop-run-rich.json")
        assert not DEVLOOP_TRACE_KEYS & note.keys()
        assert rich["reviews"] == [
            {
                "gate": "review",
                "finding": "trace fields at top level",
                "severity": "major",
                "disposition": "fixed",
                "fixed_by": "2",
            }
        ]

    def test_each_stage_record_is_one_envelope_row(self, rich):
        assert rich["envelopes"] == [
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
        assert rich["skills"] == [
            {
                "id": "implementer",
                "role": "implementer",
                "posture": "writer",
                "outcome": "ok",
                "fix_rounds_attributed": 1,
            },
            {
                "id": "judge",
                "role": "judge",
                "posture": "reader",
                "outcome": "ok",
                "fix_rounds_attributed": 1,
            },
        ]

    def test_null_criterion_counts_are_dropped_not_carried(self, rich):
        assert rich["criteria"] == [
            {"id": "AC1", "verdict": "met", "flipped_by_round": 1},
            {"id": "AC2", "verdict": "met"},
        ]

    def test_emitter_extras_beyond_the_contract_vocabulary_do_not_leak(self, rich):
        assert "edge_cases" not in rich
        assert "tdd" not in rich

    def test_served_and_outputs_delta_land_on_the_round(self, rich):
        assert rich["served"] == ["dec-1c903854", "dec-ba94712f"]
        assert rich["did"] == {
            "paths": ["src/thinkweave/core/task_contract.py"],
            "attempts": 2,
            "commits": [  # the branch's SHAs, oldest first
                "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
                "b2c3d4e5f60718293a4b5c6d7e8f901234567890",
                "c3d4e5f60718293a4b5c6d7e8f90123456789012",
            ],
        }


class TestEpicTask:
    """A run lands on its epic's task; a run with no epic on its issue's."""

    def test_the_ask_is_the_epic_else_the_issue(self):
        run = payload("devloop-run-rich.json")
        assert devloop_ask(run) == "https://github.com/marekpal97/thinkweave/issues/184"
        del run["frontmatter"]["epic_url"]
        assert devloop_ask(run) == "https://github.com/marekpal97/thinkweave/issues/217"
        assert devloop_ask(payload("devloop-run-thin.json")) == "#218"

    def test_two_issues_of_one_epic_share_one_task(self, cfg):
        first = payload("devloop-run-rich.json")
        second = payload("devloop-run-rich.json")
        second["frontmatter"]["issue"] = 218
        second["frontmatter"]["issue_url"] = "https://github.com/marekpal97/thinkweave/issues/218"
        a = tasks.record_run(cfg, first, project="t", trajectory="n-1a1a1a1a")
        b = tasks.record_run(cfg, second, project="t", trajectory="n-2b2b2b2b")
        assert a.task_id == b.task_id
        note = note_fm(cfg, a.task_id)
        assert note["asked"] == "github:marekpal97/thinkweave#184"
        assert [(r["route"], r["asked"]) for r in note["rounds"]] == [
            ("devloop", "github:marekpal97/thinkweave#217"),
            ("devloop", "github:marekpal97/thinkweave#218"),
        ]

    def test_a_run_without_an_epic_lands_on_its_issue_task(self, cfg):
        run = payload("devloop-run-rich.json")
        del run["frontmatter"]["epic_url"]
        note = note_fm(cfg, tasks.record_run(cfg, run, project="t").task_id)
        assert note["asked"] == "github:marekpal97/thinkweave#217"


class TestThinRun:
    """The emitter drops unknown and unprovided keys; absence is normal."""

    def test_the_round_passes_the_contract_validator(self):
        entry = Round.from_devloop(payload("devloop-run-thin.json"), task_id=TASK_ID)
        assert validate_task_note(work_note(entry)) == []

    def test_missing_sections_stay_absent(self, thin):
        assert thin["envelopes"] == []
        assert thin["did"] == {"paths": [], "attempts": 0}
        assert "served" not in thin
        assert not DEVLOOP_TRACE_KEYS & thin.keys()

    def test_an_older_emitter_run_takes_the_trajectory_title(self, cfg):
        note = recorded(cfg, "devloop-run-thin.json")
        assert "commits" not in note["rounds"][0]["did"]
        assert note["title"] == "loop trajectory #218: drop the dead flag"


class TestRefusals:
    def test_a_payload_without_frontmatter_is_refused(self):
        with pytest.raises(ValueError, match="frontmatter"):
            Round.from_devloop({"title": "no frontmatter"}, task_id=TASK_ID)

    def test_a_stage_record_without_an_outcome_is_refused_by_field(self):
        run = payload("devloop-run-thin.json")
        run["frontmatter"]["skills"] = [
            {"id": "implementer", "role": "implementer", "outcome": "",
             "fix_rounds_attributed": 0}
        ]
        with pytest.raises(ValueError, match="outcome"):
            Round.from_devloop(run, task_id=TASK_ID)

    def test_a_refused_payload_writes_nothing(self, cfg):
        with pytest.raises(ValueError):
            tasks.record_run(cfg, {"title": "x"}, project="t")
        assert not list(cfg.vault_root.rglob("tsk-*.md"))


class TestLedgerRoute:
    """A loop run is one ``route: devloop`` round: the trajectory note is its
    session ref and the PR its deliverable."""

    def test_the_round_names_its_route_trajectory_and_deliverable(self):
        entry = Round.from_devloop(
            payload("devloop-run-rich.json"), task_id=TASK_ID,
            trajectory="n-7a7a7a7a",
        )
        assert validate_task_note(work_note(entry)) == []
        assert entry.route == "devloop"
        assert entry.to_dict()["session_ref"] == {
            "harness": "devloop", "kind": "note", "value": "n-7a7a7a7a",
        }
        assert entry.outputs == [
            {
                "kind": "pr",
                "ref": "https://github.com/marekpal97/thinkweave/pull/999",
                "role": "deliverable",
            }
        ]

    def test_a_run_without_a_pr_declares_no_output(self, thin):
        assert "outputs" not in thin

    def test_a_run_and_a_later_wrap_on_the_same_ref_build_one_task(self, cfg):
        run = tasks.record_run(
            cfg, payload("devloop-run-rich.json"), project="t",
            trajectory="n-7a7a7a7a",
        ).task_id
        decl = {
            "declared": [
                {
                    "title": "fix-up after review",
                    "asked": "github:marekpal97/thinkweave#184",
                    "round": {"did": {"attempts": 1}},
                }
            ]
        }
        result = tasks.apply_declaration(
            cfg, decl, session_key="s-9", project="t",
            streams=[buffer_path(cfg.weave_dir, "s-9")],
        )
        assert result.minted == [] and result.appended == [run]
        notes = list(cfg.vault_root.rglob("tsk-*.md"))
        assert len(notes) == 1
        fm, _ = parse_frontmatter(notes[0].read_text(encoding="utf-8"))
        assert validate_task_note(fm) == []
        assert [r.get("route") for r in fm["rounds"]] == ["devloop", "session"]

    def test_a_run_without_an_epic_url_lands_on_its_epics_task(self, cfg, monkeypatch):
        epic = "github:marekpal97/funloops#89"
        parents = {("marekpal97/thinkweave", "217"): epic}
        monkeypatch.setattr(
            tasks, "_gh_parent", lambda repo, number: parents.get((repo, number), "")
        )
        decl = {"declared": [{"title": "epic", "asked": epic, "round": {}}]}
        (epic_task,) = tasks.apply_declaration(
            cfg, decl, session_key="s-9", project="t",
            streams=[buffer_path(cfg.weave_dir, "s-9")],
        ).minted
        run = payload("devloop-run-rich.json")
        run["frontmatter"]["epic_url"] = ""
        landed = tasks.record_run(cfg, run, project="t")
        assert landed.task_id == epic_task and landed.warnings == ()

    def test_a_failed_epic_lookup_is_announced(self, cfg, monkeypatch):
        import subprocess

        def unreachable(repo, number):
            raise subprocess.CalledProcessError(1, ["gh"], stderr="gh: offline")

        monkeypatch.setattr(tasks, "_gh_parent", unreachable)
        run = payload("devloop-run-rich.json")
        run["frontmatter"]["epic_url"] = ""
        landed = tasks.record_run(cfg, run, project="t")
        assert note_fm(cfg, landed.task_id)["asked"] == "github:marekpal97/thinkweave#217"
        (warning,) = landed.warnings
        assert "gh: offline" in warning

    def test_rerecording_a_run_replaces_its_round(self, cfg):
        args = (cfg, payload("devloop-run-rich.json"))
        first = tasks.record_run(*args, project="t", trajectory="n-7a7a7a7a")
        again = tasks.record_run(*args, project="t", trajectory="n-7a7a7a7a")
        assert first.task_id == again.task_id
        assert len(note_fm(cfg, first.task_id)["rounds"]) == 1

    def test_the_cli_records_a_run_from_its_payload_file(self, cfg, capsys):
        from thinkweave.surfaces.cli.parser import build_parser
        from thinkweave.surfaces.cli.task import cmd_task

        args = build_parser().parse_args([
            "task", "record-run", str(FIXTURES / "devloop-run-rich.json"),
            "--trajectory", "n-7a7a7a7a", "--project", "t",
        ])
        cmd_task(args)
        task_id = capsys.readouterr().out.strip()
        assert TaskStore(cfg).get(task_id) is not None

    def test_a_run_with_no_session_writes_no_register_row(self, cfg):
        landed = tasks.record_run(cfg, payload("devloop-run-rich.json"), project="t")
        assert not list(cfg.weave_dir.rglob("*.jsonl"))
        _, body = parse_frontmatter(
            TaskStore(cfg).get(landed.task_id).path.read_text(encoding="utf-8")
        )
        assert body.startswith("## Rounds") and "- devloop" in body

    def test_a_run_with_a_session_records_its_open_there(self, cfg):
        from thinkweave.operations.tasks import Register

        landed = tasks.record_run(
            cfg, payload("devloop-run-rich.json"), project="t", session_key="s-loop"
        )
        rows = Register(cfg, "s-loop", [buffer_path(cfg.weave_dir, "s-loop")]).rows()
        assert [(r["type"], r["task_id"]) for r in rows] == [("task_open", landed.task_id)]

    def test_an_unresolvable_bare_issue_ref_is_announced(
        self, cfg, tmp_path, monkeypatch, capsys
    ):
        from thinkweave.surfaces.cli.parser import build_parser
        from thinkweave.surfaces.cli.task import cmd_task

        monkeypatch.chdir(tmp_path)  # no git remote to resolve "#218" against
        cmd_task(build_parser().parse_args([
            "task", "record-run", str(FIXTURES / "devloop-run-thin.json"), "--project", "t",
        ]))
        out = capsys.readouterr()
        assert "#218" in out.err and "kept bare" in out.err
        assert note_fm(cfg, out.out.strip())["asked"] == "#218"
