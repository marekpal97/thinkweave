"""``weave task rounds``: one read over every task's rounds, joined to the
session chains they name; and ``applied`` on a decision through the update
path."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.task_contract import Round
from thinkweave.core.vault import VaultManager, parse_frontmatter
from thinkweave.operations import tasks
from thinkweave.surfaces.cli.parser import build_parser
from thinkweave.surfaces.cli.task import cmd_task
from thinkweave.surfaces.mcp.tools.notes import handle_update

PATH = "skills/DIAGNOSIS.md"
SHA = "0" * 39 + "1"
MANIFEST = {"harness": "claude-code", "rsi_rail": {"repo": "rsi-rail", "commit": SHA}}


def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _index(cfg: Config, path: Path) -> str:
    idx = Indexer(config=cfg)
    idx.index_file(path)
    idx.close()
    return str(parse_frontmatter(path.read_text(encoding="utf-8"))[0]["id"])


def _session(cfg: Config, *, days_ago: int, events: list[dict]) -> str:
    """A session note dated ``days_ago`` with ``events`` archived beside it."""
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    path = vm.create_note(
        NoteType.SESSION, "s", project="p",
        extra_frontmatter={"date": _ago(days_ago), "source_session": f"uuid-{days_ago}"},
    )
    (path.parent / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )
    return _index(cfg, path)


def _task(cfg: Config, title: str, round_: dict) -> str:
    task = tasks.TaskStore(cfg).mint("work", title, "p")
    task.put_round(Round.from_dict({"route": "session", **round_}, grain="work"))
    task.save(VaultManager(config=cfg))
    return task.id


def _ref(session_id: str) -> dict:
    return {"harness": "claude-code", "kind": "note", "value": session_id}


def _feedback(ts: str, text: str, about: str = "") -> tuple[dict, dict]:
    """A feedback event as wrap writes it, and the entry a round copies."""
    entry = {"register": "correction", "prompt_ref": text[:120], "ts": ts, "text": text}
    return {"type": "feedback", **entry, **({"about": about} if about else {})}, entry


@pytest.fixture()
def vault(cfg: Config) -> dict[str, str]:
    """Three tasks: one corrected two days ago under a manifest, whose
    session dispatched a child that hit four tool errors; one whose decision
    names ``PATH`` ten days ago; one sixty days old."""
    event, entry = _feedback(_ago(2), "no, read the round first", "the diagnosis skipped the round")
    recent = _session(cfg, days_ago=2, events=[
        event,
        {"ts": _ago(2), "tool": "Skill", "skill": "tweak", "declared": "tool"},
        {"ts": _ago(2), "type": "nudge", "kind": "unverified_claim", "phrase": "tests pass"},
    ])
    corrected = _task(cfg, "corrected", {
        "session_ref": _ref(recent), "feedback": [entry], "manifest": MANIFEST,
        "tool_errors": 3, "done": True, "did": {"attempts": 2},
    })
    child = tasks.TaskStore(cfg).get(
        tasks.open_child(cfg, session_key="uuid-2", project="p", title="child").task_id
    )
    child.put_round(Round.from_dict({"tool_errors": 4}, grain="per-dispatch"))
    child.save(VaultManager(config=cfg))

    vm = VaultManager(config=cfg)
    decision = _index(cfg, vm.create_note(
        NoteType.DECISION, "d", project="p", extra_frontmatter={"file_paths": [PATH]},
    ))
    decided = _task(cfg, "decided", {
        "session_ref": _ref(_session(cfg, days_ago=10, events=[])),
        "decisions": {"minted": [decision]},
    })

    old_event, old_entry = _feedback(_ago(60), f"{PATH} asks the wrong question")
    old = _task(cfg, "old", {
        "session_ref": _ref(_session(cfg, days_ago=60, events=[old_event])),
        "feedback": [old_entry],
    })
    return {
        "corrected": corrected, "decided": decided, "old": old,
        "decision": decision, "child": child.id,
    }


def _rounds(capsys, *flags: str) -> dict:
    cmd_task(build_parser().parse_args(["task", "rounds", *flags, "--json"]))
    return json.loads(capsys.readouterr().out)


def test_a_window_groups_rounds_by_task_with_the_session_chain_joined(vault, capsys):
    out = _rounds(capsys, "--since", "30d")

    assert set(out) == {vault["corrected"], vault["decided"]}
    assert out[vault["corrected"]]["title"] == "corrected"
    (entry,) = out[vault["corrected"]]["rounds"]
    assert entry["manifest"] == MANIFEST
    (feedback,) = entry["feedback"]
    assert feedback["register"] == "correction"
    assert feedback["about"] == "the diagnosis skipped the round"
    assert feedback["text"] == feedback["prompt_ref"] == "no, read the round first"
    assert [s["skill"] for s in entry["skills"]] == ["tweak"]
    assert [(n["kind"], n["phrase"]) for n in entry["nudges"]] == [
        ("unverified_claim", "tests pass")
    ]
    assert entry["tool_errors"] == 3 + 4
    assert entry["outcomes"] == {"done": True, "attempts": 2}
    assert entry["served"] == [] and entry["gaps"] == []


def test_a_task_flag_returns_that_task_only(vault, capsys):
    assert set(_rounds(capsys, "--task", vault["old"])) == {vault["old"]}


def test_a_dispatch_task_is_listed_only_by_name(vault, capsys):
    assert vault["child"] not in _rounds(capsys)
    (entry,) = _rounds(capsys, "--task", vault["child"])[vault["child"]]["rounds"]
    assert entry["tool_errors"] == 4


def test_a_path_keeps_rounds_whose_feedback_or_decisions_name_it(vault, capsys):
    out = _rounds(capsys, "--path", PATH)

    assert set(out) == {vault["decided"], vault["old"]}
    (entry,) = out[vault["decided"]]["rounds"]
    assert [(d["id"], d["file_paths"]) for d in entry["decisions"]] == [
        (vault["decision"], [PATH])
    ]


def test_a_session_the_index_lacks_is_a_gap_not_an_empty_round(cfg, capsys):
    orphan = _task(cfg, "orphan", {"session_ref": _ref("ses-00000000")})

    (entry,) = _rounds(capsys)[orphan]["rounds"]
    assert entry["gaps"] == ["session ses-00000000 is not in the index"]


def test_an_unreadable_window_exits_2(cfg, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_task(build_parser().parse_args(["task", "rounds", "--since", "soon"]))
    assert exc.value.code == 2


def test_applied_and_accepted_round_trip_through_weave_update(cfg: Config):
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    path = vm.create_note(NoteType.DECISION, "tweak", project="p")
    decision = _index(cfg, path)
    # A sha of digits only, which YAML would read back as a number unquoted.
    applied = {"repo": "rsi-rail", "commit": "0123456"}

    handle_update(cfg, {"id": decision, "frontmatter": {"applied": applied, "status": "accepted"}})

    fm = parse_frontmatter(path.read_text(encoding="utf-8"))[0]
    assert (fm["applied"], fm["status"]) == (applied, "accepted")
    idx = Indexer(config=cfg)
    row = idx.db.execute("SELECT frontmatter FROM notes WHERE id = ?", (decision,)).fetchone()
    idx.close()
    indexed = json.loads(row["frontmatter"])
    assert (indexed["applied"], indexed["status"]) == (applied, "accepted")

