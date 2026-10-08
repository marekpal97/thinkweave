"""The child task digest: one transcript read at a child's close boundary.

Fixtures under ``fixtures/transcripts/`` are hand-built in the shapes Claude
Code writes: a foreground subagent, a worktree subagent, a dispatched
session with two prompts, and a transcript with junk and unknown fields.
Expected values are read off the fixture rows by hand.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from tests.tasks.conftest import run_hook
from thinkweave.core.config import Config
from thinkweave.core.task_contract import SessionRef, validate_task_note
from thinkweave.core.vault import parse_frontmatter
from thinkweave.operations import tasks
from thinkweave.operations.tasks import ChildDigest, TaskStore, TranscriptSource
from thinkweave.surfaces.cli.parser import build_parser
from thinkweave.surfaces.cli.task import cmd_task

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"
FOREGROUND = FIXTURES / "agent-a1f0e2d3c4b5a6978.jsonl"
WORKTREE = FIXTURES / "agent-a8c6a44dd35bd23d8.jsonl"
HERDR = FIXTURES / "herdr-two-prompts.jsonl"
PARTIAL = FIXTURES / "partial.jsonl"
PI = (
    Path(__file__).parents[1] / "fixtures" / "harness_transcripts" / "pi"
    / "2026-10-04T15-46-02-748Z_0199cccc-0000-7000-8000-000000000002.jsonl"
)
SESSION = "11111111-2222-4333-8444-555566667777"
CODEX = next(
    (Path(__file__).parents[1] / "fixtures" / "harness_transcripts" / "codex").glob(
        "rollout-2026-10-03*.jsonl"
    )
)


def stub_fm(cfg: Config, task_id: str) -> dict:
    task = TaskStore(cfg).get(task_id)
    assert task is not None and task.path is not None
    return parse_frontmatter(task.path.read_text(encoding="utf-8"))[0]


def digest_of(path: Path, since: str = "", harness: str = "claude-code") -> ChildDigest:
    return ChildDigest.read(TranscriptSource(path, since, SessionRef.session(harness, "s-x")))


def cli(argv: list[str]) -> None:
    cmd_task(build_parser().parse_args(argv))


class TestDigest:
    def test_foreground_subagent(self):
        digest = digest_of(FOREGROUND)
        assert digest.asked == (
            "Write a news brief for the queue item in /tmp/q-1.json."
        )
        assert digest.description == "Write news brief: market wrap"
        assert digest.model == "claude-sonnet-5-5"
        assert digest.role == "research-news-worker"
        assert digest.version == "2.1.287"
        assert digest.tools == {
            "Read": 1, "Bash": 1, "mcp__thinkweave__weave_create": 1,
        }
        assert digest.tool_errors == 1
        assert digest.duration == 26.813  # 06:09:20.990 → 06:09:47.803
        fields = digest.round_fields([])
        assert {"kind": "note", "ref": "src-a10e8018"} in fields["outputs"]
        assert fields["cost"] == {"duration": 26.813}

    def test_worktree_subagent(self):
        digest = digest_of(WORKTREE)
        fields = digest.round_fields([])
        assert fields["did"] == {
            "paths": ["src/dogfood/cli.py"], "commits": ["f7cd49a"],
        }
        assert {"kind": "commit", "ref": "f7cd49a"} in fields["outputs"]
        assert {"kind": "file", "ref": "src/dogfood/cli.py"} in fields["outputs"]
        (envelope,) = digest.claims("tsk-0a1b2c3d")
        assert envelope["outcome"] == "success"

    def test_dispatched_session_reads_only_the_bound_slice(self):
        digest = digest_of(HERDR, since="2026-09-30T22:05:18.900+00:00")
        assert digest.asked.endswith("Task: tsk-0b1d2e3f")
        fields = digest.round_fields([])
        # The first prompt's __init__.py write and failed Bash stay out.
        assert fields["did"] == {
            "paths": ["deck/pitch.md"], "commits": ["1a1343a"],
        }
        assert fields["tools"] == {"Write": 1, "Bash": 1}
        assert fields["tool_errors"] == 0

    def test_partial_transcript_never_raises(self):
        digest = digest_of(PARTIAL)
        assert digest.version == "9.9.9"
        assert digest.model == "claude-x"
        assert digest.gaps  # the degraded read announces itself
        missing = digest_of(FIXTURES / "nope.jsonl")
        assert missing.gaps and not missing.asked

    def test_a_zone_less_timestamp_is_left_out_not_fatal(self, tmp_path: Path):
        rows = [json.loads(line) for line in FOREGROUND.read_text().splitlines() if line.strip()]
        stamped = [r for r in rows if r.get("timestamp")]
        stamped[1]["timestamp"] = stamped[1]["timestamp"].rstrip("Z")[:19]
        transcript = tmp_path / FOREGROUND.name
        transcript.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        digest = digest_of(transcript, since=stamped[0]["timestamp"])
        assert not any("aborted" in gap for gap in digest.gaps)
        assert any("zone-less" in gap for gap in digest.gaps)
        assert digest.model == "claude-sonnet-5-5"
        assert digest.tools == {
            "Read": 1, "Bash": 1, "mcp__thinkweave__weave_create": 1,
        }

    def test_a_harness_without_a_reader_names_the_gap_not_zero_tools(self):
        digest = digest_of(FOREGROUND, harness="opencode")
        assert any("opencode" in g and "digest reader" in g for g in digest.gaps)
        fields = digest.round_fields([])
        assert "tools" not in fields and "tool_errors" not in fields
        assert fields["digest"]["gaps"] == list(digest.gaps)

    def test_a_pi_worker_transcript_digests_its_live_branch_from_the_task_prompt(self):
        digest = digest_of(PI, since="2026-10-04T15:46:11.000Z", harness="pi")
        assert digest.asked.startswith("Task tsk-20bdc8ce: Add --indent N")
        assert digest.model == "moonshotai/kimi-k2.6"
        # The warm-up turn and the abandoned fork's write stay out.
        assert digest.tools == {"bash": 3, "read": 1, "edit": 1, "write": 1, "weave_create": 1}
        assert digest.tool_errors == 1
        assert digest.duration == 18.792  # 15:46:11.313 → 15:46:30.105
        fields = digest.round_fields([])
        assert fields["did"] == {
            "paths": ["src/dogfood/__init__.py", "tests/test_version.py"],
            "commits": ["3e141e8"],
        }
        assert {"kind": "note", "ref": "src-a10e8018"} in fields["outputs"]
        assert digest.gaps == ("no version in the transcript",)

    def test_a_missing_pi_transcript_returns_the_gap(self, tmp_path: Path):
        digest = digest_of(tmp_path / "gone.jsonl", harness="pi")
        assert digest.tools is None
        assert any("transcript not found" in g for g in digest.gaps)

    def test_no_token_counts_are_written(self):
        for path in (FOREGROUND, WORKTREE, HERDR, PARTIAL):
            fields = digest_of(path).round_fields([])
            assert "tokens" not in json.dumps(fields)


class TestCodexDigest:
    """Expected values read off the hand-built 0.160 rollout fixture."""

    def test_bound_worker_reads_only_its_prompt_slice(self):
        digest = digest_of(CODEX, since="2026-10-03T10:05:00.400+00:00", harness="codex")
        assert digest.asked == "Task: tsk-0c0d0e0f\nAdd --pretty to 'dogfood version --json'."
        assert digest.version == "0.160.0"
        assert digest.model == "gpt-5.6-terra"
        assert digest.tools == {
            "Bash": 2, "apply_patch": 1,
            "mcp__thinkweave__weave_create": 1, "mcp__thinkweave__weave_extract": 1,
        }
        assert digest.tool_errors == 2
        assert digest.duration == 30.0  # 10:05:00 → 10:05:30
        assert digest.gaps == ()
        fields = digest.round_fields([])
        assert fields["did"] == {"paths": ["src/dogfood/__init__.py"], "commits": ["1a2b3c4"]}
        assert {"kind": "note", "ref": "dec-1234abcd"} in fields["outputs"]

    def test_unsliced_rollout_counts_the_warm_up_too(self):
        digest = digest_of(CODEX, harness="codex")
        assert digest.asked == "Warm up: list the repo."
        assert digest.tools["Bash"] == 3 and digest.tools["apply_patch"] == 2

    def test_tool_calls_without_item_records_are_a_gap(self, tmp_path: Path):
        rows = [json.loads(line) for line in CODEX.read_text().splitlines()]
        kept = [r for r in rows if r["payload"].get("type") != "item_completed"
                or r["payload"]["item"]["type"] == "UserMessage"]
        transcript = tmp_path / CODEX.name
        transcript.write_text("".join(json.dumps(r) + "\n" for r in kept), encoding="utf-8")
        digest = digest_of(transcript, harness="codex")
        assert digest.tools == {}
        assert any("6 tool call(s)" in gap for gap in digest.gaps)


class TestWiring:
    def test_subagent_stop_fills_the_child_stub(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        project_dir = tmp_path / "projects" / "-work-repo"
        sub = project_dir / SESSION / "subagents"
        sub.mkdir(parents=True)
        for name in (WORKTREE.name, WORKTREE.with_suffix(".meta.json").name):
            shutil.copy(FIXTURES / name, sub / name)
        payload = {
            "session_id": SESSION,
            "cwd": str(tmp_path),
            "transcript_path": str(project_dir / f"{SESSION}.jsonl"),
            "agent_id": "a8c6a44dd35bd23d8",
            "agent_type": "general-purpose",
        }
        reply = run_hook(monkeypatch, "subagent_start", payload)
        task_id = json.loads(reply["hookSpecificOutput"]["additionalContext"])[
            "thinkweave_task"
        ]["task_id"]
        run_hook(monkeypatch, "subagent_stop", payload)

        fm = stub_fm(cfg, task_id)
        assert validate_task_note(fm) == []
        assert fm["status"] == "closed"
        assert fm["asked"].startswith("Tweak the greet help text")
        assert fm["model"] == "claude-opus-5-5"
        (entry,) = fm["rounds"]
        assert entry["did"]["paths"] == ["src/dogfood/cli.py"]
        assert entry["envelopes"][0]["outcome"] == "success"
        assert entry["digest"]["version"] == "2.1.287"

    def test_prompt_binds_a_dispatched_session_and_close_digests_it(
        self, cfg: Config, monkeypatch, tmp_path: Path, capsys
    ):
        cli(["task", "open", "--session", "s-1", "--project", "p",
             "--asked", "Write the pitch deck"])
        task_id = capsys.readouterr().out.strip()
        assert stub_fm(cfg, task_id)["asked"] == "Write the pitch deck"

        transcript = tmp_path / "worker.jsonl"
        rows = HERDR.read_text(encoding="utf-8").replace("tsk-0b1d2e3f", task_id)
        transcript.write_text(rows, encoding="utf-8")
        monkeypatch.setattr(
            "thinkweave.surfaces.hooks.handler._prompt_time_enrichment",
            lambda *a, **k: None,
        )
        run_hook(monkeypatch, "user_prompt_submit", {
            "session_id": "s-worker",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
            "prompt": f"Write a short pitch deck. Task: {task_id}",
        })
        monkeypatch.setattr(tasks, "_now", lambda: "2026-09-30T23:00:00+00:00")
        cli(["task", "close", task_id, "--session", "s-1"])

        fm = stub_fm(cfg, task_id)
        assert validate_task_note(fm) == []
        assert fm["asked"] == "Write the pitch deck"  # the dispatcher's word stands
        (entry,) = fm["rounds"]
        assert entry["did"]["paths"] == ["deck/pitch.md"]
        assert entry["session_ref"] == {
            "harness": "claude-code", "kind": "session_id", "value": "s-worker",
        }

    def test_a_codex_worker_binds_and_closes_with_tools_counted(
        self, cfg: Config, monkeypatch, tmp_path: Path, capsys
    ):
        cli(["task", "open", "--session", "s-1", "--project", "p"])
        task_id = capsys.readouterr().out.strip()
        transcript = tmp_path / CODEX.name
        transcript.write_text(
            CODEX.read_text(encoding="utf-8").replace("tsk-0c0d0e0f", task_id),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "thinkweave.surfaces.hooks.handler._prompt_time_enrichment",
            lambda *a, **k: None,
        )
        run_hook(monkeypatch, "user_prompt_submit", {
            "session_id": "01a10380-0000-7000-8000-000000000002",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
            "prompt": f"Task: {task_id}\nAdd --pretty to 'dogfood version --json'.",
        }, harness="codex")
        monkeypatch.setattr(tasks, "_now", lambda: "2026-10-03T10:06:00+00:00")
        cli(["task", "close", task_id, "--session", "s-1"])

        (entry,) = stub_fm(cfg, task_id)["rounds"]
        assert entry["session_ref"]["harness"] == "codex"
        assert entry["tools"]["Bash"] == 2
        assert entry["did"]["commits"] == ["1a2b3c4"]
        assert entry["digest"]["gaps"] == []

    def test_a_corrupt_binding_closes_with_a_recorded_gap(
        self, cfg: Config, tmp_path: Path, capsys
    ):
        cli(["task", "open", "--session", "s-1", "--project", "p"])
        task_id = capsys.readouterr().out.strip()
        binding = cfg.weave_dir / "tasks" / f"{task_id}.bind.json"
        binding.write_text("{not json", encoding="utf-8")
        cli(["task", "close", task_id, "--session", "s-1"])
        err = capsys.readouterr().err
        assert f"{task_id}.bind.json unreadable" in err
        fm = stub_fm(cfg, task_id)
        assert fm["status"] == "closed"
        gaps = fm["rounds"][0]["digest"]["gaps"]
        assert any(f"{task_id}.bind.json unreadable" in g for g in gaps)
        assert tasks._NO_TRANSCRIPT in gaps

    def _bind(self, cfg: Config, task_id: str, transcript: str, since: str) -> list[str]:
        return tasks.bind_session(
            cfg, f"Task: {task_id}", harness="claude-code", session_key="s-worker",
            transcript_path=transcript, since=since,
        )

    def test_a_gap_only_binding_yields_to_a_later_real_one(
        self, cfg: Config, monkeypatch, capsys
    ):
        cli(["task", "open", "--session", "s-1", "--project", "p"])
        task_id = capsys.readouterr().out.strip()
        assert self._bind(cfg, task_id, "", "2026-09-30T21:39:47.600+00:00") == [task_id]
        assert self._bind(cfg, task_id, str(HERDR), "2026-09-30T22:05:18.900+00:00") == [task_id]
        monkeypatch.setattr(tasks, "_now", lambda: "2026-09-30T23:00:00+00:00")
        cli(["task", "close", task_id, "--session", "s-1"])

        (entry,) = stub_fm(cfg, task_id)["rounds"]
        assert entry["digest"]["gaps"] == []
        # The session's first task prompt still opens the slice.
        assert entry["did"] == {
            "paths": ["src/dogfood/__init__.py", "deck/pitch.md"], "commits": ["1a1343a"],
        }
        assert entry["tools"] == {"Write": 2, "Bash": 2}

    def test_a_real_binding_is_never_replaced(self, cfg: Config, monkeypatch, capsys):
        cli(["task", "open", "--session", "s-1", "--project", "p"])
        task_id = capsys.readouterr().out.strip()
        assert self._bind(cfg, task_id, str(HERDR), "2026-09-30T22:05:18.900+00:00") == [task_id]
        assert self._bind(cfg, task_id, "", "2026-09-30T22:06:00+00:00") == []
        assert self._bind(cfg, task_id, str(PARTIAL), "2026-09-30T22:07:00+00:00") == []
        monkeypatch.setattr(tasks, "_now", lambda: "2026-09-30T23:00:00+00:00")
        cli(["task", "close", task_id, "--session", "s-1"])

        (entry,) = stub_fm(cfg, task_id)["rounds"]
        assert entry["did"] == {"paths": ["deck/pitch.md"], "commits": ["1a1343a"]}


class TestNoSilentRound:
    def test_a_close_with_no_transcript_writes_the_gap_onto_the_round(
        self, cfg: Config, capsys
    ):
        cli(["task", "open", "--session", "s-1", "--project", "p"])
        task_id = capsys.readouterr().out.strip()
        cli(["task", "close", task_id, "--session", "s-1"])
        (entry,) = stub_fm(cfg, task_id)["rounds"]
        assert entry["digest"]["gaps"] == [tasks._NO_TRANSCRIPT]

    def _pi_worker_close(self, cfg, monkeypatch, tmp_path, capsys, transcript: str) -> dict:
        cli(["task", "open", "--session", "s-1", "--project", "p"])
        task_id = capsys.readouterr().out.strip()
        monkeypatch.setattr(
            "thinkweave.surfaces.hooks.handler._prompt_time_enrichment",
            lambda *a, **k: None,
        )
        payload = {
            "session_id": "pi-worker",
            "cwd": str(tmp_path),
            "prompt": f"Task {task_id}: Add --indent N to 'dogfood version --json'.",
        }
        if transcript:
            payload["transcript_path"] = transcript
        run_hook(monkeypatch, "user_prompt_submit", payload, harness="pi")
        monkeypatch.setattr(tasks, "_now", lambda: "2026-10-04T16:00:00+00:00")
        cli(["task", "close", task_id, "--session", "s-1"])
        fm = stub_fm(cfg, task_id)
        assert validate_task_note(fm) == []
        (entry,) = fm["rounds"]
        assert entry["session_ref"] == {
            "harness": "pi", "kind": "session_id", "value": "pi-worker",
        }
        return entry

    def test_a_pi_worker_prompt_binds_its_transcript_and_closes_with_a_digest(
        self, cfg: Config, monkeypatch, tmp_path: Path, capsys
    ):
        entry = self._pi_worker_close(cfg, monkeypatch, tmp_path, capsys, str(PI))
        assert entry["tools"]["bash"] == 3
        assert entry["did"]["commits"] == ["3e141e8"]

    def test_a_pi_prompt_without_a_transcript_path_names_the_gap(
        self, cfg: Config, monkeypatch, tmp_path: Path, capsys
    ):
        entry = self._pi_worker_close(cfg, monkeypatch, tmp_path, capsys, "")
        gaps = entry["digest"]["gaps"]
        assert "the prompt hook carried no transcript path" in gaps
        assert "tools" not in entry
        err = capsys.readouterr().err
        assert all(f"digest: {g}" in err for g in gaps)
