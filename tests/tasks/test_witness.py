"""The witness: every round carries the manifest it ran under, and the
session's event rows record what the rail reads (skill invocations,
subagent boundaries, nudge firings, pinned feedback)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import init_repo, run_git
from tests.tasks.conftest import run_hook
from thinkweave.core import manifest
from thinkweave.core.buffer import append_event, buffer_path
from thinkweave.core.config import Config
from thinkweave.core.events import extract_deterministic
from thinkweave.core.harness import active
from thinkweave.core.schemas import NoteType
from thinkweave.core.task_contract import Round, validate_task_note
from thinkweave.core.vault import VaultManager, parse_frontmatter
from thinkweave.operations import tasks
from thinkweave.surfaces.hooks import handler

FIXTURES = Path(__file__).parent / "fixtures"
SESSION = "11111111-2222-4333-8444-555566667777"
SHA = "a" * 40
TS = "2026-10-28T10:00:00+00:00"


def _rows(cfg: Config, session: str) -> list[dict]:
    path = buffer_path(cfg.weave_dir, session)
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def _task_notes(cfg: Config) -> list[dict]:
    return [
        parse_frontmatter(p.read_text(encoding="utf-8"))[0]
        for p in sorted(cfg.vault_root.rglob("tsk-*.md"))
    ]


class TestContract:
    def test_a_round_carries_its_manifest(self):
        entry = Round.from_dict(
            {"manifest": {"rsi_rail": {"repo": "x", "commit": SHA, "dirty": False},
                          "harness": "claude-code"}},
            grain="work",
        )
        assert entry.manifest["rsi_rail"]["commit"] == SHA
        assert entry.to_dict()["manifest"]["harness"] == "claude-code"

    def test_a_short_rail_commit_is_refused_by_field(self):
        with pytest.raises(ValueError, match=r"manifest\.rsi_rail\.commit"):
            Round.from_dict(
                {"manifest": {"rsi_rail": {"repo": "x", "commit": "abc", "dirty": False}}},
                grain="work",
            )

    def test_a_manifest_is_valid_on_a_dispatch_round_too(self):
        entry = Round.from_dict({"manifest": {"harness": "pi"}}, grain="per-dispatch")
        assert entry.manifest == {"harness": "pi"}

    def test_a_stage_record_keeps_its_skill(self):
        payload = {"frontmatter": {
            "issue": 1,
            "skills": [{"id": "implementer", "role": "implementer", "skill": "tdd",
                        "outcome": "ok", "fix_rounds_attributed": 0}],
        }}
        entry = Round.from_devloop(payload, task_id="tsk-0badc0de")
        assert entry.skills[0]["skill"] == "tdd"


class TestCollect:
    def test_the_manifest_names_the_rail_commit_and_dirt(self, tmp_path: Path):
        rail = init_repo(tmp_path / "rail")
        head = run_git("rev-parse", "HEAD", cwd=rail)
        cfg = Config(vault_root=tmp_path / "vault", rsi_rail=rail)

        clean = manifest.collect(cfg, "pi")
        assert clean["harness"] == "pi"
        assert clean["rsi_rail"] == {"repo": str(rail), "commit": head, "dirty": False}
        assert clean["readers"]["thinkweave"]
        assert validate_task_note(_work_note(Round.from_dict({"manifest": clean}, grain="work"))) == []

        (rail / "loose.md").write_text("untracked", encoding="utf-8")
        assert manifest.collect(cfg, "pi")["rsi_rail"]["dirty"] is True

    def test_no_rail_configured_means_no_rail_entry(self, tmp_path: Path):
        found = manifest.collect(Config(vault_root=tmp_path / "vault"), "claude-code")
        assert "rsi_rail" not in found
        assert found["harness"] == "claude-code"

    def test_a_rail_that_is_not_a_repo_is_announced(self, tmp_path: Path):
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(RuntimeError, match="rsi-rail"):
            manifest.collect(Config(vault_root=tmp_path / "vault", rsi_rail=plain), "pi")


class TestFold:
    def test_nudge_and_skill_rows_are_counted(self):
        events = [
            {"ts": TS, "type": "prompt", "text": "/wrap"},
            {"ts": TS, "tool": "Skill", "skill": "wrap", "declared": "prompt"},
            {"ts": TS, "tool": "Skill", "skill": "drain", "declared": "tool"},
            {"ts": TS, "type": "nudge", "name": "stop-verify"},
            {"ts": TS, "tool": "Edit", "file": "src/a.py"},
        ]
        assert extract_deterministic(events).tools == {"Skill": 2, "nudge": 1}
        assert extract_deterministic([]).tools == {}

    def test_the_rows_land_on_the_session_round(self, cfg: Config, monkeypatch):
        """hook row → Stop fold → session note → wrap declaration → round."""
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        vm.create_note(
            NoteType.SESSION, "Witnessed", project="t",
            extra_frontmatter={"source_session": SESSION},
        )
        stamp = {"harness": "claude-code", "rsi_rail": {"repo": "/r", "commit": SHA, "dirty": True}}
        for row in (
            {"ts": TS, "type": "prompt", "text": "/wrap", "session_id": SESSION, "cwd": "/p"},
            {"ts": TS, "tool": "Skill", "skill": "wrap", "declared": "prompt"},
            {"ts": TS, "type": "manifest", "session_id": SESSION, "manifest": stamp},
            {"ts": TS, "type": "nudge", "name": "stop-verify"},
        ):
            append_event(buffer_path(cfg.weave_dir, SESSION), row, harness="claude-code")
        monkeypatch.setattr("sys.stdout", __import__("io").StringIO())
        handler._handle_stop({"session_id": SESSION})

        session = next(
            n for n in vm.list_notes(note_type=NoteType.SESSION, limit=10)
            if n.frontmatter.get("source_session") == SESSION
        )
        assert session.frontmatter["tools"] == {"Skill": 1, "nudge": 1}
        folder = (vm.root / session.path).parent

        decl = {"declared": [{"title": "witnessed", "round": {}}]}
        result = tasks.apply_declaration(
            cfg, decl, session_key=SESSION, project="t",
            streams=[folder / "events.jsonl"], folders=[folder],
        )
        assert result.errors == []
        (note,) = _task_notes(cfg)
        (entry,) = note["rounds"]
        assert entry["tools"] == {"Skill": 1, "nudge": 1}
        assert entry["manifest"] == stamp


class TestSubagentRows:
    def _payload(self, tmp_path: Path, event: str, agent_id: str = "agent-a1") -> dict:
        return {"session_id": SESSION, "cwd": str(tmp_path), "hook_event_name": event,
                "agent_id": agent_id, "agent_type": "Explore"}

    def test_start_and_stop_rows_enter_the_buffer(self, cfg: Config, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("THINKWEAVE_PROJECT", "canary")
        reply = run_hook(monkeypatch, "subagent_start", self._payload(tmp_path, "SubagentStart"))
        task_id = json.loads(reply["hookSpecificOutput"]["additionalContext"])["thinkweave_task"]["task_id"]
        run_hook(monkeypatch, "subagent_stop", self._payload(tmp_path, "SubagentStop"))

        rows = [r for r in _rows(cfg, SESSION) if r.get("type", "").startswith("subagent")]
        assert [(r["type"], r["task_id"], r["agent_id"]) for r in rows] == [
            ("subagent_start", task_id, "agent-a1"),
            ("subagent_stop", task_id, "agent-a1"),
        ]
        assert rows[0]["role"] == "Explore"
        assert rows[1]["kind"] == "pending"


class TestDevloopRoute:
    def test_record_run_keeps_the_stage_skill_and_stamps_the_manifest(self, cfg: Config):
        run = json.loads((FIXTURES / "devloop-run-rich.json").read_text(encoding="utf-8"))
        run["frontmatter"]["skills"][0]["skill"] = "tdd"
        tasks.record_run(cfg, run, project="t")

        (note,) = _task_notes(cfg)
        assert validate_task_note(note) == []
        (entry,) = note["rounds"]
        assert [s.get("skill") for s in entry["skills"]] == ["tdd", None]
        assert entry["manifest"]["harness"] == active().id
        assert "rsi_rail" not in entry["manifest"]


def _work_note(entry: Round) -> dict:
    return {"type": "note", "kind": "task", "id": "tsk-0badc0de", "status": "open",
            "grain": "work", "rounds": [entry.to_dict()]}
