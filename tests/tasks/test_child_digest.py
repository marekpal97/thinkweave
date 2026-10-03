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
from thinkweave.core.task_contract import validate_task_note
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
SESSION = "11111111-2222-4333-8444-555566667777"


def stub_fm(cfg: Config, task_id: str) -> dict:
    task = TaskStore(cfg).get(task_id)
    assert task is not None and task.path is not None
    return parse_frontmatter(task.path.read_text(encoding="utf-8"))[0]


def digest_of(path: Path, since: str = "") -> ChildDigest:
    return ChildDigest.read(TranscriptSource(path, since))


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

    def test_no_token_counts_are_written(self):
        for path in (FOREGROUND, WORKTREE, HERDR, PARTIAL):
            fields = digest_of(path).round_fields([])
            assert "tokens" not in json.dumps(fields)


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
        assert "digest" not in fm["rounds"][0]
