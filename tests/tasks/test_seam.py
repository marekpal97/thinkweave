"""The dispatch seam (#187): task lifecycle event writers, the register
ledger projection, `weave task open|close|render`, and the
SubagentStart/SubagentStop hook handlers driven by synthetic payloads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.tasks.conftest import run_hook
from thinkweave.core.config import Config
from thinkweave.core.harness import PROFILES
from thinkweave.core.task_contract import TASK_ID_RE, validate_task_note
from thinkweave.core.vault import parse_frontmatter
from thinkweave.operations import hook_events, task_seam
from thinkweave.surfaces.cli.parser import build_parser
from thinkweave.surfaces.cli.task import cmd_task


def register_rows(cfg: Config, key: str) -> list[dict]:
    return hook_events.task_rows(hook_events.register_path(cfg.weave_dir, key))


# ---------------------------------------------------------------------------
# Event writers


class TestEventWriters:
    def test_open_and_close_rows_carry_the_declared_shape(self):
        ref = task_seam.agent_ref("claude-code", "agent-abc")
        opened = hook_events.task_open_event(
            "tsk-0a1b2c3d",
            "2026-09-22T10:00:00+00:00",
            session_id="s-1",
            grain="per-dispatch",
            session_ref=ref,
        )
        assert opened["type"] == hook_events.TASK_OPEN
        assert opened["task_id"] == "tsk-0a1b2c3d"
        assert opened["grain"] == "per-dispatch"
        assert opened["session_ref"] == {
            "harness": "claude-code",
            "kind": "agent_id",
            "value": "agent-abc",
        }

        closed = hook_events.task_close_event(
            "tsk-0a1b2c3d", "2026-09-22T10:05:00+00:00", session_id="s-1"
        )
        assert closed["type"] == hook_events.TASK_CLOSE
        assert "orphan" not in closed

    def test_append_lands_in_the_session_register_only(self, cfg: Config):
        event = hook_events.task_open_event(
            "tsk-0a1b2c3d", "2026-09-22T10:00:00+00:00",
            session_id="s-1", grain="per-dispatch",
        )
        path = hook_events.append_task_event(cfg.weave_dir, "s-1", event)
        assert path == cfg.weave_dir / "buffer" / "s-1.jsonl"
        assert hook_events.task_rows(path) == [event]

    def test_task_rows_skips_foreign_and_malformed_lines(self, tmp_path: Path):
        path = tmp_path / "events.jsonl"
        path.write_text(
            json.dumps({"type": "prompt", "text": "hi"})
            + "\nnot json\n"
            + json.dumps({"type": "task_open", "task_id": "tsk-0a1b2c3d"})
            + "\n",
            encoding="utf-8",
        )
        rows = hook_events.task_rows(path)
        assert [r["type"] for r in rows] == ["task_open"]
        assert hook_events.task_rows(tmp_path / "absent.jsonl") == []


# ---------------------------------------------------------------------------
# Ledger projection — pairing by task id, never by order or timing


class TestLedger:
    OPEN = {"type": "task_open", "task_id": "tsk-0a1b2c3d"}
    CLOSE = {"type": "task_close", "task_id": "tsk-0a1b2c3d"}

    def test_pairs_by_task_id_regardless_of_row_order(self):
        for rows in ([self.OPEN, self.CLOSE], [self.CLOSE, self.OPEN]):
            ledger = task_seam.task_ledger(rows)
            assert ledger["tsk-0a1b2c3d"]["open"] == self.OPEN
            assert ledger["tsk-0a1b2c3d"]["close"] == self.CLOSE

    def test_orphan_rows_without_a_task_id_stay_out_of_the_ledger(self):
        rows = [{"type": "task_close", "task_id": "", "orphan": True}]
        assert task_seam.task_ledger(rows) == {}

    def test_pending_open_matches_on_the_qualified_ref(self):
        ref_a = task_seam.agent_ref("claude-code", "agent-a")
        ref_b = task_seam.agent_ref("claude-code", "agent-b")
        rows = [
            {"type": "task_open", "task_id": "tsk-aaaaaaaa", "session_ref": ref_a},
            {"type": "task_open", "task_id": "tsk-bbbbbbbb", "session_ref": ref_b},
        ]
        assert task_seam.pending_open(rows, ref_b) == "tsk-bbbbbbbb"
        assert task_seam.pending_open(rows, ref_a) == "tsk-aaaaaaaa"

    def test_pending_open_ignores_already_closed_tasks(self):
        ref = task_seam.agent_ref("claude-code", "agent-a")
        rows = [
            {"type": "task_open", "task_id": "tsk-aaaaaaaa", "session_ref": ref},
            {"type": "task_close", "task_id": "tsk-aaaaaaaa"},
        ]
        assert task_seam.pending_open(rows, ref) == ""

    def test_closed_task_matches_the_ref_of_a_paired_close(self):
        ref = task_seam.agent_ref("claude-code", "agent-a")
        rows = [
            {"type": "task_open", "task_id": "tsk-aaaaaaaa", "session_ref": ref},
            {"type": "task_close", "task_id": "tsk-aaaaaaaa"},
        ]
        assert task_seam.closed_task(rows, ref) == "tsk-aaaaaaaa"
        other = task_seam.agent_ref("claude-code", "agent-b")
        assert task_seam.closed_task(rows, other) == ""
        assert task_seam.closed_task(rows[:1], ref) == ""  # still open


# ---------------------------------------------------------------------------
# open / close at the operations seam (the hook-less, task-id-only route)


class TestOpenClose:
    def test_open_mints_stub_and_register_row(self, cfg: Config):
        dispatch = task_seam.open_task(
            cfg, session_key="s-1", project="proj", title="count beans"
        )
        assert TASK_ID_RE.match(dispatch.task_id)

        stub = task_seam.find_stub(cfg, dispatch.task_id)
        assert stub is not None and stub.name == f"{dispatch.task_id}.md"
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
        dispatch = task_seam.open_task(cfg, session_key="s-1", project="proj")
        envelope = {
            "task_id": dispatch.task_id,
            "outcome": "ok",
            "outputs": ["notes/x.md"],
        }
        path = Path(dispatch.envelope_return)
        assert path.name == f"{dispatch.task_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")

        result = task_seam.close_task(cfg, dispatch.task_id, session_key="s-1")
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

    def test_close_without_envelope_file_records_an_empty_round(self, cfg: Config):
        dispatch = task_seam.open_task(cfg, session_key="s-1", project="proj")
        result = task_seam.close_task(cfg, dispatch.task_id, session_key="s-1")
        assert result.errors == ()
        assert result.envelopes == 0
        fm, _ = parse_frontmatter(Path(result.note).read_text(encoding="utf-8"))
        assert fm["status"] == "closed" and fm["rounds"][0]["envelopes"] == []

    def test_close_announces_invalid_envelope_rows(self, cfg: Config):
        dispatch = task_seam.open_task(cfg, session_key="s-1", project="proj")
        path = Path(dispatch.envelope_return)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"task_id": dispatch.task_id, "outcome": "ok"})
            + "\n"
            + json.dumps({"nonsense": True})
            + "\n",
            encoding="utf-8",
        )
        result = task_seam.close_task(cfg, dispatch.task_id, session_key="s-1")
        assert result.errors  # the degraded row is reported, never swallowed
        assert result.envelopes == 1  # the valid row still compiles
        types = [r["type"] for r in register_rows(cfg, "s-1")]
        assert types == ["task_open", "task_close"]  # boundary truth recorded

    def test_close_of_unknown_task_raises(self, cfg: Config):
        with pytest.raises(ValueError):
            task_seam.close_task(cfg, "tsk-deadbeef", session_key="s-1")

    def test_no_per_task_lifecycle_file(self, cfg: Config):
        dispatch = task_seam.open_task(cfg, session_key="s-1", project="proj")
        task_seam.close_task(cfg, dispatch.task_id, session_key="s-1")
        register = hook_events.register_path(cfg.weave_dir, "s-1")
        for path in cfg.weave_dir.rglob("*.jsonl"):
            if path == register:
                continue
            assert not hook_events.task_rows(path), (
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
        assert TASK_ID_RE.match(out)
        assert task_seam.find_stub(cfg, out) is not None

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
        ledger = task_seam.task_ledger(register_rows(cfg, "s-1"))
        assert ledger[task_id]["open"] and ledger[task_id]["close"]

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

    def test_close_exits_nonzero_on_envelope_errors(self, cfg: Config, capsys):
        self._dispatch(["task", "open", "--session", "s-1", "--project", "p"])
        task_id = capsys.readouterr().out.strip()
        path = task_seam.envelope_path(cfg, task_id)
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
        assert TASK_ID_RE.match(descriptor["task_id"])
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

        ledger = task_seam.task_ledger(register_rows(cfg, SESSION))
        closed = [t for t, e in ledger.items() if e["close"]]
        still_open = [t for t, e in ledger.items() if not e["close"]]
        assert len(closed) == 1 and len(still_open) == 1
        fm, _ = parse_frontmatter(
            task_seam.find_stub(cfg, closed[0]).read_text(encoding="utf-8")
        )
        assert fm["status"] == "closed"

    def test_second_stop_for_a_closed_task_is_not_an_orphan(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        # Claude Code fires SubagentStop twice per subagent (observed live
        # 2026-09-28): the second stop carries the same agent_id, seconds
        # after its task was closed. It must not land as a spurious orphan.
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

    def test_missing_session_id_is_a_noop(self, cfg: Config, monkeypatch, tmp_path):
        reply = run_hook(monkeypatch, "subagent_start", {"cwd": str(tmp_path)})
        assert reply == {}
        assert not (cfg.weave_dir / "buffer").exists()


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
