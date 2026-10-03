"""The child-dispatch route: the session register's task rows and their
pairing, `weave task open|close|render|ledger`, and the
SubagentStart/SubagentStop hook handlers driven by synthetic payloads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.tasks.conftest import run_hook
from thinkweave.core.buffer import buffer_path
from thinkweave.core.config import Config
from thinkweave.core.harness import PROFILES
from thinkweave.core.task_contract import TASK_ID_RE, SessionRef, validate_task_note
from thinkweave.core.vault import parse_frontmatter
from thinkweave.operations import tasks
from thinkweave.operations.tasks import ChildStop, Register, Task, TaskStore
from thinkweave.surfaces.cli.parser import build_parser
from thinkweave.surfaces.cli.task import cmd_task


def register_rows(cfg: Config, key: str) -> list[dict]:
    """The task rows in one session's live buffer."""
    return Register(cfg, key, [buffer_path(cfg.weave_dir, key)]).rows()


def note_path(cfg: Config, task_id: str) -> Path:
    task = TaskStore(cfg).get(task_id)
    assert task is not None and task.path is not None
    return task.path


def a_task(task_id: str = "tsk-0a1b2c3d", grain: str = "per-dispatch") -> Task:
    return Task({
        "type": "note", "kind": "task", "id": task_id,
        "status": "open", "grain": grain, "rounds": [],
    })


def write_rows(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The register: task rows in the session's shared event log


class TestRegisterRows:
    def test_open_and_close_rows_carry_the_declared_shape(self, cfg: Config):
        register = Register(cfg, "s-1")
        ref = SessionRef.agent("claude-code", "agent-abc")
        register.open(a_task(), ref)
        register.close(a_task())
        opened, closed = register_rows(cfg, "s-1")
        assert opened["type"] == tasks.TASK_OPEN
        assert opened["task_id"] == "tsk-0a1b2c3d"
        assert opened["session_id"] == "s-1"
        assert opened["grain"] == "per-dispatch"
        assert opened["session_ref"] == {
            "harness": "claude-code",
            "kind": "agent_id",
            "value": "agent-abc",
        }
        assert closed["type"] == tasks.TASK_CLOSE
        assert "orphan" not in closed and "session_ref" not in closed

    def test_rows_land_in_the_session_buffer_only(self, cfg: Config):
        Register(cfg, "s-1").open(a_task())
        files = list(cfg.weave_dir.rglob("*.jsonl"))
        assert files == [cfg.weave_dir / "buffer" / "s-1.jsonl"]

    def test_rows_skip_foreign_and_malformed_lines(self, cfg: Config, tmp_path: Path):
        path = tmp_path / "events.jsonl"
        path.write_text(
            json.dumps({"type": "prompt", "text": "hi"})
            + "\nnot json\n"
            + json.dumps({"type": "task_open", "task_id": "tsk-0a1b2c3d"})
            + "\n",
            encoding="utf-8",
        )
        rows = Register(cfg, "s-1", [path]).rows()
        assert [r["type"] for r in rows] == ["task_open"]
        assert Register(cfg, "s-1", [tmp_path / "absent.jsonl"]).rows() == []


# ---------------------------------------------------------------------------
# Pairing — by task id, never by order or timing


class TestPairing:
    OPEN = {"type": "task_open", "task_id": "tsk-0a1b2c3d"}
    CLOSE = {"type": "task_close", "task_id": "tsk-0a1b2c3d"}

    def register(self, cfg: Config, tmp_path: Path, rows: list[dict]) -> Register:
        return Register(cfg, "s-1", [write_rows(tmp_path / "events.jsonl", rows)])

    def test_pairs_by_task_id_regardless_of_row_order(self, cfg, tmp_path):
        for rows in ([self.OPEN, self.CLOSE], [self.CLOSE, self.OPEN]):
            assert self.register(cfg, tmp_path, rows).unclosed() == []
        assert self.register(cfg, tmp_path, [self.OPEN]).unclosed() == ["tsk-0a1b2c3d"]

    def test_orphan_rows_without_a_task_id_stay_out_of_the_pairing(self, cfg, tmp_path):
        rows = [{"type": "task_close", "task_id": "", "orphan": True}]
        register = self.register(cfg, tmp_path, rows)
        assert register.unclosed() == [] and register.listing() == []

    def test_a_stop_pairs_with_the_open_carrying_its_qualified_ref(self, cfg, tmp_path):
        ref_a = SessionRef.agent("claude-code", "agent-a")
        ref_b = SessionRef.agent("claude-code", "agent-b")
        register = self.register(cfg, tmp_path, [
            {"type": "task_open", "task_id": "tsk-aaaaaaaa", "session_ref": ref_a.to_dict()},
            {"type": "task_open", "task_id": "tsk-bbbbbbbb", "session_ref": ref_b.to_dict()},
        ])
        assert register.resolve_stop(ref_b) == ChildStop("pending", "tsk-bbbbbbbb")
        assert register.resolve_stop(ref_a) == ChildStop("pending", "tsk-aaaaaaaa")

    def test_a_stop_for_an_already_closed_task_is_its_duplicate(self, cfg, tmp_path):
        ref = SessionRef.agent("claude-code", "agent-a")
        opened = {"type": "task_open", "task_id": "tsk-aaaaaaaa", "session_ref": ref.to_dict()}
        closed = {"type": "task_close", "task_id": "tsk-aaaaaaaa"}
        register = self.register(cfg, tmp_path, [opened, closed])
        assert register.resolve_stop(ref) == ChildStop("duplicate", "tsk-aaaaaaaa")
        other = SessionRef.agent("claude-code", "agent-b")
        assert register.resolve_stop(other) == ChildStop("orphan")
        assert register.resolve_stop(None) == ChildStop("orphan")
        still_open = self.register(cfg, tmp_path, [opened])
        assert still_open.resolve_stop(ref).kind == "pending"


# ---------------------------------------------------------------------------
# open / close at the operations seam (the hook-less, task-id-only route)


class TestOpenClose:
    def test_open_mints_stub_and_register_row(self, cfg: Config):
        dispatch = tasks.open_child(
            cfg, session_key="s-1", project="proj", title="count beans"
        )
        assert TASK_ID_RE.fullmatch(dispatch.task_id)

        stub = note_path(cfg, dispatch.task_id)
        assert stub.name == f"{dispatch.task_id}.md"
        fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
        assert validate_task_note(fm) == []
        assert fm["status"] == "open"
        assert fm["grain"] == "per-dispatch"
        assert fm["title"] == "count beans"

        rows = register_rows(cfg, "s-1")
        assert [r["type"] for r in rows] == ["task_open"]
        assert rows[0]["task_id"] == dispatch.task_id

        # The return directory exists at open — the performer's first append
        # must not fail on a fresh deployment.
        assert Path(dispatch.envelope_return).parent.is_dir()

    def test_close_compiles_the_round_and_flips_status(self, cfg: Config):
        dispatch = tasks.open_child(cfg, session_key="s-1", project="proj")
        envelope = {
            "task_id": dispatch.task_id,
            "outcome": "ok",
            "outputs": ["notes/x.md"],
        }
        path = Path(dispatch.envelope_return)
        assert path.name == f"{dispatch.task_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")

        result = tasks.close_child(cfg, dispatch.task_id, session_key="s-1")
        assert result.errors == ()
        assert result.envelopes == 1

        fm, _ = parse_frontmatter(
            Path(result.note).read_text(encoding="utf-8")
        )
        assert validate_task_note(fm) == []
        assert fm["status"] == "closed"
        assert fm["rounds"][0]["envelopes"] == [envelope]

        types = [r["type"] for r in register_rows(cfg, "s-1")]
        assert types == ["task_open", "task_close"]

    def test_close_renders_the_ledger_body(self, cfg: Config):
        dispatch = tasks.open_child(cfg, session_key="s-1", project="proj")
        _, body = parse_frontmatter(Path(dispatch.note).read_text(encoding="utf-8"))
        assert "_No rounds yet._" in body
        result = tasks.close_child(cfg, dispatch.task_id, session_key="s-1")
        _, body = parse_frontmatter(Path(result.note).read_text(encoding="utf-8"))
        lines = [line for line in body.splitlines() if line.startswith("- ")]
        assert body.startswith("## Rounds") and len(lines) == 1
        assert lines[0].startswith("- dispatch")

    def test_close_of_a_closed_task_is_refused(self, cfg: Config):
        dispatch = tasks.open_child(cfg, session_key="s-1", project="proj")
        tasks.close_child(cfg, dispatch.task_id, session_key="s-1")
        with pytest.raises(ValueError, match="closed"):
            tasks.close_child(cfg, dispatch.task_id, session_key="s-1")
        assert len(register_rows(cfg, "s-1")) == 2

    def test_close_without_envelope_file_records_an_empty_round(self, cfg: Config):
        dispatch = tasks.open_child(cfg, session_key="s-1", project="proj")
        result = tasks.close_child(cfg, dispatch.task_id, session_key="s-1")
        assert result.errors == ()
        assert result.envelopes == 0
        fm, _ = parse_frontmatter(Path(result.note).read_text(encoding="utf-8"))
        assert fm["status"] == "closed" and fm["rounds"][0]["envelopes"] == []

    def test_close_announces_invalid_envelope_rows(self, cfg: Config):
        dispatch = tasks.open_child(cfg, session_key="s-1", project="proj")
        path = Path(dispatch.envelope_return)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"task_id": dispatch.task_id, "outcome": "ok"})
            + "\n"
            + json.dumps({"nonsense": True})
            + "\n",
            encoding="utf-8",
        )
        result = tasks.close_child(cfg, dispatch.task_id, session_key="s-1")
        assert result.errors  # the degraded row is reported, never swallowed
        assert result.envelopes == 1  # the valid row still compiles
        types = [r["type"] for r in register_rows(cfg, "s-1")]
        assert types == ["task_open", "task_close"]  # boundary truth recorded

    def test_close_of_unknown_task_raises(self, cfg: Config):
        with pytest.raises(ValueError):
            tasks.close_child(cfg, "tsk-deadbeef", session_key="s-1")

    def test_no_per_task_lifecycle_file(self, cfg: Config):
        dispatch = tasks.open_child(cfg, session_key="s-1", project="proj")
        tasks.close_child(cfg, dispatch.task_id, session_key="s-1")
        register = buffer_path(cfg.weave_dir, "s-1")
        for path in cfg.weave_dir.rglob("*.jsonl"):
            if path == register:
                continue
            assert not Register(cfg, "s-1", [path]).rows(), (
                f"lifecycle rows leaked outside the register: {path}"
            )


# ---------------------------------------------------------------------------
# CLI verbs


class TestCli:
    def _dispatch(self, argv: list[str]):
        args = build_parser().parse_args(argv)
        assert args.command == "task"
        cmd_task(args)
        return args

    def test_open_prints_the_minted_task_id(self, cfg: Config, capsys):
        self._dispatch(["task", "open", "--session", "s-1", "--project", "p"])
        out = capsys.readouterr().out.strip()
        assert TASK_ID_RE.fullmatch(out)
        assert TaskStore(cfg).get(out) is not None

    def test_render_prints_the_descriptor(self, cfg: Config, capsys):
        self._dispatch(["task", "open", "--session", "s-1", "--project", "p"])
        task_id = capsys.readouterr().out.strip()
        self._dispatch(["task", "render", task_id])
        descriptor = json.loads(capsys.readouterr().out)
        assert descriptor["task_id"] == task_id
        assert descriptor["grain"] == "per-dispatch"
        assert Path(descriptor["envelope_return"]).name == f"{task_id}.jsonl"

    def test_close_correlates_by_task_id_alone(self, cfg: Config, capsys):
        self._dispatch(["task", "open", "--session", "s-1", "--project", "p"])
        task_id = capsys.readouterr().out.strip()
        self._dispatch(["task", "close", task_id, "--session", "s-1"])
        assert task_id in capsys.readouterr().out
        types = {r["type"] for r in register_rows(cfg, "s-1") if r["task_id"] == task_id}
        assert types == {"task_open", "task_close"}

    def test_ledger_lists_the_sessions_boundaries(self, cfg: Config, capsys):
        self._dispatch(["task", "open", "--session", "s-1", "--project", "p"])
        open_id = capsys.readouterr().out.strip()
        self._dispatch(["task", "open", "--session", "s-1", "--project", "p"])
        closed_id = capsys.readouterr().out.strip()
        self._dispatch(["task", "close", closed_id, "--session", "s-1"])
        capsys.readouterr()

        self._dispatch(["task", "ledger", "--session", "s-1"])
        items = {
            row["task_id"]: row
            for row in map(json.loads, capsys.readouterr().out.splitlines())
        }
        assert items[open_id]["closed"] is False
        assert items[closed_id]["closed"] is True
        assert items[open_id]["grain"] == "per-dispatch"
        assert items[open_id]["status"] == "open"

    def test_ledger_reads_the_archived_stream_after_wrap(
        self, cfg: Config, capsys
    ):
        self._dispatch(["task", "open", "--session", "s-1", "--project", "p"])
        task_id = capsys.readouterr().out.strip()
        folder = cfg.vault_root / "projects" / "p" / "sessions" / "some-slug"
        folder.mkdir(parents=True)
        (folder / "session.md").write_text(
            "---\ntype: session\nsource_session: s-1\n---\n", encoding="utf-8"
        )
        buffer_path(cfg.weave_dir, "s-1").rename(
            folder / "events.jsonl"
        )

        self._dispatch(["task", "ledger", "--session", "s-1"])
        rows = [
            json.loads(line)
            for line in capsys.readouterr().out.splitlines()
        ]
        assert [r["task_id"] for r in rows] == [task_id]

    def test_close_exits_nonzero_on_envelope_errors(self, cfg: Config, capsys):
        self._dispatch(["task", "open", "--session", "s-1", "--project", "p"])
        task_id = capsys.readouterr().out.strip()
        path = Path(tasks.dispatch_descriptor(cfg, task_id).envelope_return)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"nonsense": True}) + "\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            self._dispatch(["task", "close", task_id, "--session", "s-1"])


# ---------------------------------------------------------------------------
# Hook handlers — synthetic payloads on stdin, no live session, no model


SESSION = "11111111-2222-4333-8444-555566667777"


class TestHookHandlers:
    def _start_payload(self, tmp_path: Path, agent_id: str = "agent-a1") -> dict:
        return {
            "session_id": SESSION,
            "cwd": str(tmp_path),
            "hook_event_name": "SubagentStart",
            "agent_id": agent_id,
            "agent_type": "Explore",
        }

    def test_subagent_start_opens_and_hands_back_the_descriptor(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        monkeypatch.setenv("THINKWEAVE_PROJECT", "canary")
        reply = run_hook(
            monkeypatch, "subagent_start", self._start_payload(tmp_path)
        )
        descriptor = json.loads(
            reply["hookSpecificOutput"]["additionalContext"]
        )["thinkweave_task"]
        assert TASK_ID_RE.fullmatch(descriptor["task_id"])
        rows = register_rows(cfg, SESSION)
        assert [r["type"] for r in rows] == ["task_open"]
        assert rows[0]["session_ref"]["value"] == "agent-a1"

    def test_subagent_stop_closes_the_matching_open(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        monkeypatch.setenv("THINKWEAVE_PROJECT", "canary")
        run_hook(monkeypatch, "subagent_start", self._start_payload(tmp_path, "agent-a1"))
        run_hook(monkeypatch, "subagent_start", self._start_payload(tmp_path, "agent-a2"))
        stop = dict(self._start_payload(tmp_path, "agent-a1"))
        stop["hook_event_name"] = "SubagentStop"
        run_hook(monkeypatch, "subagent_stop", stop)

        entries = Register(cfg, SESSION).listing()
        closed = [e.task_id for e in entries if e.closed]
        still_open = [e.task_id for e in entries if not e.closed]
        assert len(closed) == 1 and len(still_open) == 1
        fm, _ = parse_frontmatter(
            note_path(cfg, closed[0]).read_text(encoding="utf-8")
        )
        assert fm["status"] == "closed"

    def test_second_stop_for_a_closed_task_is_not_an_orphan(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        # Claude Code can deliver SubagentStop twice per subagent: the second
        # stop carries the same agent_id, seconds after its task was closed.
        # It must not land as a spurious orphan.
        monkeypatch.setenv("THINKWEAVE_PROJECT", "canary")
        run_hook(monkeypatch, "subagent_start", self._start_payload(tmp_path, "agent-a1"))
        stop = dict(self._start_payload(tmp_path, "agent-a1"))
        stop["hook_event_name"] = "SubagentStop"
        run_hook(monkeypatch, "subagent_stop", stop)
        run_hook(monkeypatch, "subagent_stop", dict(stop))

        rows = register_rows(cfg, SESSION)
        assert [r["type"] for r in rows] == ["task_open", "task_close"]
        assert not any(r.get("orphan") for r in rows)

    def test_unmatched_stop_is_flagged_as_orphan(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        monkeypatch.setenv("THINKWEAVE_PROJECT", "canary")
        stop = self._start_payload(tmp_path, "agent-never-opened")
        stop["hook_event_name"] = "SubagentStop"
        run_hook(monkeypatch, "subagent_stop", stop)
        rows = register_rows(cfg, SESSION)
        assert len(rows) == 1
        assert rows[0]["type"] == "task_close"
        assert rows[0]["orphan"] is True
        assert rows[0]["task_id"] == ""
        assert rows[0]["session_ref"]["value"] == "agent-never-opened"

    def test_stop_after_the_parent_turn_archived_the_register_still_closes(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        # Background dispatch: the parent's turn ends (Stop archives the live
        # buffer into the session folder) while both subagents still run.
        monkeypatch.setenv("THINKWEAVE_PROJECT", "canary")
        run_hook(monkeypatch, "subagent_start", self._start_payload(tmp_path, "agent-a1"))
        run_hook(monkeypatch, "subagent_start", self._start_payload(tmp_path, "agent-a2"))
        run_hook(monkeypatch, "stop", {"session_id": SESSION, "cwd": str(tmp_path)})
        assert not buffer_path(cfg.weave_dir, SESSION).exists()

        for agent in ("agent-a1", "agent-a2"):
            stop = self._start_payload(tmp_path, agent)
            stop["hook_event_name"] = "SubagentStop"
            run_hook(monkeypatch, "subagent_stop", stop)

        register = Register(cfg, SESSION)
        assert not any(r.get("orphan") for r in register.rows())
        entries = register.listing()
        assert len(entries) == 2
        for entry in entries:
            assert entry.closed, entry.task_id
            task_id = entry.task_id
            fm, _ = parse_frontmatter(
                note_path(cfg, task_id).read_text(encoding="utf-8")
            )
            assert fm["status"] == "closed"

    def test_worktree_subagent_files_its_stub_under_the_parent_repo(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        monkeypatch.delenv("THINKWEAVE_PROJECT", raising=False)
        monkeypatch.delenv("PERSONAL_MEM_PROJECT", raising=False)
        repo = tmp_path / "tw-dogfood2"
        (repo / ".git").mkdir(parents=True)
        worktree = repo / ".claude" / "worktrees" / "agent-a8c6a44dd35bd23d8"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: ../../../.git/worktrees/x\n")

        payload = self._start_payload(worktree / "src")
        reply = run_hook(monkeypatch, "subagent_start", payload)
        note = json.loads(reply["hookSpecificOutput"]["additionalContext"])[
            "thinkweave_task"
        ]["note"]
        assert Path(note).is_relative_to(cfg.vault_root / "projects" / "tw_dogfood2")

    def test_missing_session_id_announces_the_dropped_open(
        self, cfg: Config, monkeypatch, tmp_path
    ):
        reply = run_hook(monkeypatch, "subagent_start", {"cwd": str(tmp_path)})
        assert "subagent_start" in reply["systemMessage"]
        assert "session id" in (cfg.weave_dir / "hooks.log").read_text()


# ---------------------------------------------------------------------------
# HarnessProfile tier — hook-less harnesses degrade to task-id-only


class TestHarnessTier:
    def test_declared_correlation_tiers(self, tmp_path: Path):
        tiers = {
            pid: PROFILES[pid](tmp_path / "home").task_correlation
            for pid in PROFILES
        }
        assert tiers["claude-code"] == "boundary"
        assert tiers["codex"] == "boundary"
        assert tiers["pi"] == "task-id-only"
        assert tiers["opencode"] == "task-id-only"


class TestTaskStoreLookup:
    """``TaskStore.get`` runs inside UserPromptSubmit when a dispatched prompt
    names a task id. It resolves through the index that ``Task.save`` writes,
    and its fallback is bounded to the task filing folders — never a
    vault-wide walk (a miss on that measured 29s on a DrvFs vault)."""

    def test_saved_task_resolves_through_the_index(self, cfg: Config, monkeypatch):
        dispatch = tasks.open_child(
            cfg, session_key="s-1", project="proj", title="count beans"
        )
        assert cfg.index_db.exists()

        def no_walk(self, *a, **k):
            raise AssertionError("TaskStore.get must not glob the vault")

        monkeypatch.setattr(Path, "glob", no_walk)
        monkeypatch.setattr(Path, "rglob", no_walk)
        task = TaskStore(cfg).get(dispatch.task_id)
        assert task is not None and task.id == dispatch.task_id

    def test_unindexed_task_falls_back_to_the_filing_folders(
        self, cfg: Config, monkeypatch
    ):
        dispatch = tasks.open_child(
            cfg, session_key="s-1", project="proj", title="count beans"
        )
        cfg.index_db.unlink()
        monkeypatch.setattr(
            Path, "rglob", lambda *a, **k: pytest.fail("vault-wide rglob")
        )
        task = TaskStore(cfg).get(dispatch.task_id)
        assert task is not None and task.id == dispatch.task_id

    def test_miss_never_walks_outside_the_filing_folders(self, cfg: Config):
        # A note outside projects/*/sessions/*/ is not a task filing location.
        stray = cfg.vault_root / "sources" / "tsk-deadbeef.md"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("---\nid: tsk-deadbeef\n---\n", encoding="utf-8")
        assert TaskStore(cfg).get("tsk-deadbeef") is None
        assert TaskStore(cfg).get("") is None
