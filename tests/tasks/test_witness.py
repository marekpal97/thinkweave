"""The witness: every round carries the manifest it ran under, and the
session's event rows record what the rail reads (skill invocations,
subagent boundaries, nudge firings, pinned feedback)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.conftest import commit_file, init_repo, run_git
from tests.tasks.conftest import run_hook
from thinkweave.core import manifest
from thinkweave.core.buffer import append_event, buffer_path
from thinkweave.core.config import Config
from thinkweave.core.events import extract_deterministic
from thinkweave.core.harness import active
from thinkweave.core.schemas import NoteType
from thinkweave.core.task_contract import Round, validate_task_note
from thinkweave.core.vault import VaultManager, find_session_note_by_source, parse_frontmatter
from thinkweave.operations import tasks
from thinkweave.operations.extract import extract_session
from thinkweave.operations.wrap import finalize_wrap
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


def _prompt(monkeypatch, cwd: Path, text: str, **extra) -> dict:
    payload = {"session_id": SESSION, "cwd": str(cwd), "hook_event_name": "UserPromptSubmit",
               "prompt": text, **extra}
    reply = run_hook(monkeypatch, "user_prompt_submit", payload)
    monkeypatch.setattr("sys.stdout", sys.__stdout__)
    return reply


def _session_folder(cfg: Config) -> Path:
    note = find_session_note_by_source(VaultManager(config=cfg), SESSION)
    assert note is not None
    note = Path(note)
    return (note if note.is_absolute() else cfg.vault_root / note).parent


def _declare(cfg: Config, decl: dict, folder: Path | None = None) -> dict:
    """The wrap declaration pass over this session's streams; the one round."""
    streams = [buffer_path(cfg.weave_dir, SESSION)]
    if folder is not None:
        streams.append(folder / "events.jsonl")
    result = tasks.apply_declaration(
        cfg, decl, session_key=SESSION, project="t", streams=streams,
        folders=[folder] if folder else [],
    )
    assert result.errors == []
    (note,) = _task_notes(cfg)
    assert validate_task_note(note) == []
    (entry,) = note["rounds"]
    return entry


class TestInteractiveOrder:
    def test_a_mid_session_wrap_round_counts_the_skill(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        """hook rows (a Skill among them) → weave_extract note →
        wrap-finalize with a ``continuing`` declaration → Stop: the note
        never got ``tools`` from a Stop fold, the round still counts it."""
        minted = tasks.apply_declaration(
            cfg, {"declared": [{"title": "skill witness", "round": {}}]},
            session_key="s-earlier", project="t", streams=[], folders=[],
        )
        (task_id,) = minted.minted
        _prompt(monkeypatch, tmp_path, "/wrap")
        project = _session_folder(cfg).parent.parent.name

        out = extract_session(cfg, session_id=SESSION, project=project, summary="ok",
                              insights=[], decisions=[])
        assert out.error == ""
        folder = _session_folder(cfg)
        assert "tools" not in parse_frontmatter((folder / "session.md").read_text())[0]

        result = finalize_wrap(
            cfg, session_id=SESSION, project=project, prune=False,
            tasks={"declared": [{"continuing": task_id, "round": {}}]},
        )
        assert result.tasks["appended"] == [task_id]
        run_hook(monkeypatch, "stop", {"session_id": SESSION, "cwd": str(tmp_path),
                                       "hook_event_name": "Stop"})

        (note,) = _task_notes(cfg)
        entry = next(r for r in note["rounds"] if r.get("manifest"))
        assert entry["tools"] == {"Skill": 1}


class TestManifestStamp:
    def test_a_rail_fixed_after_prompt_one_stamps_prompt_two(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        rail = tmp_path / "rail"
        rail.mkdir()
        run_git("init", "-q", "-b", "main", str(rail), cwd=tmp_path)
        run_git("config", "user.email", "scratch@example.com", cwd=rail)
        run_git("config", "user.name", "Scratch", cwd=rail)
        monkeypatch.setattr(cfg, "rsi_rail", rail)

        reply = _prompt(monkeypatch, tmp_path, "first, before the rail has a commit")
        assert "rsi-rail" in reply["systemMessage"]
        assert not [r for r in _rows(cfg, SESSION) if r.get("type") == "manifest"]

        head = commit_file(rail, "policy", "the first policy")
        _prompt(monkeypatch, tmp_path, "second, after the fix")
        (stamp,) = [r for r in _rows(cfg, SESSION) if r.get("type") == "manifest"]
        assert stamp["manifest"]["rsi_rail"]["commit"] == head

        entry = _declare(cfg, {"declared": [{"title": "late rail", "round": {}}]})
        assert entry["manifest"]["rsi_rail"] == {"repo": str(rail), "commit": head, "dirty": False}

    def test_the_stamp_is_still_once_per_session(self, cfg: Config, monkeypatch, tmp_path: Path):
        _prompt(monkeypatch, tmp_path, "one")
        _prompt(monkeypatch, tmp_path, "two")
        assert len([r for r in _rows(cfg, SESSION) if r.get("type") == "manifest"]) == 1


class TestHarnessReadout:
    def test_the_round_manifest_carries_the_hook_model_and_the_settings(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        settings = active().user_settings
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({
            "model": "opus", "effortLevel": "high", "outputStyle": "Explanatory",
            "tui": "fullscreen", "permissions": {"allow": ["Bash"]}, "env": {"SECRET": "x"},
        }), encoding="utf-8")

        _prompt(monkeypatch, tmp_path, "hello", model="claude-opus-5-5")
        entry = _declare(cfg, {"declared": [{"title": "readout", "round": {}}]})

        stamp = entry["manifest"]
        assert stamp["harness_settings"] == {
            "model": "claude-opus-5-5", "effortLevel": "high",
            "outputStyle": "Explanatory", "tui": "fullscreen",
        }
        assert "UserPromptSubmit" in stamp["hook_events"]
        profile = active()
        assert stamp["literals"] == {
            "hook_budget_ms": profile.hook_budget_ms,
            "additional_context_cap_chars": profile.additional_context_cap_chars,
        }
        assert "permissions" not in json.dumps(stamp) and "SECRET" not in json.dumps(stamp)

    def test_the_model_falls_back_to_the_one_an_earlier_hook_reported(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        run_hook(monkeypatch, "session_start", {
            "session_id": SESSION, "cwd": str(tmp_path), "hook_event_name": "SessionStart",
            "source": "startup", "model": "claude-sonnet-5",
        })
        _prompt(monkeypatch, tmp_path, "no model on this payload")
        (stamp,) = [r for r in _rows(cfg, SESSION) if r.get("type") == "manifest"]
        assert stamp["manifest"]["harness_settings"]["model"] == "claude-sonnet-5"

    def test_collect_without_a_settings_file_names_only_the_reported_model(
        self, tmp_path: Path
    ):
        found = manifest.collect(Config(vault_root=tmp_path / "vault"), active().id, model="m-1")
        assert found["harness_settings"] == {"model": "m-1"}
        assert "harness_settings" not in manifest.collect(
            Config(vault_root=tmp_path / "vault"), active().id
        )


def _work_note(entry: Round) -> dict:
    return {"type": "note", "kind": "task", "id": "tsk-0badc0de", "status": "open",
            "grain": "work", "rounds": [entry.to_dict()]}
