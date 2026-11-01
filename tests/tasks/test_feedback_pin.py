"""``weave task feedback``: verdicts pinned mid-session — to the live
buffer or to a turn Stop already archived — land on the round at the next
wrap."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.tasks.conftest import run_hook
from thinkweave.core.buffer import append_event, buffer_path
from thinkweave.core.config import Config
from thinkweave.core.events import feedback_events, iter_jsonl
from thinkweave.core.vault import VaultManager, find_session_note_by_source
from thinkweave.operations import wrap
from thinkweave.surfaces.cli.parser import build_parser
from thinkweave.surfaces.cli.task import cmd_task

TS = "2026-10-28T10:00:00+00:00"
SESSION = "33333333-2222-4333-8444-555566667777"


def _rows(cfg: Config, session: str) -> list[dict]:
    path = buffer_path(cfg.weave_dir, session)
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def _turns(monkeypatch, cfg: Config, cwd: Path, texts: list[str]) -> Path:
    """One hook-captured turn per text, Stop closing each; returns the
    session folder Stop archived the turns into."""
    stdout = sys.stdout
    for text in texts:
        payload = {"session_id": SESSION, "cwd": str(cwd)}
        run_hook(monkeypatch, "user_prompt_submit",
                 {**payload, "hook_event_name": "UserPromptSubmit", "prompt": text})
        run_hook(monkeypatch, "stop", {**payload, "hook_event_name": "Stop"})
    monkeypatch.setattr("sys.stdout", stdout)  # hand capture back to capsys
    note =find_session_note_by_source(VaultManager(config=cfg), SESSION)
    assert note is not None
    note = Path(note)
    return (note if note.is_absolute() else cfg.vault_root / note).parent


def _cli(argv: list[str]) -> None:
    cmd_task(build_parser().parse_args(argv))


class TestFeedbackEntryPoint:
    def test_a_verdict_pins_to_the_live_buffer(self, cfg: Config):
        live = buffer_path(cfg.weave_dir, "s-1")
        prompt = {"ts": TS, "type": "prompt", "text": "no, use a dict here", "session_id": "s-1"}
        append_event(live, prompt, harness="claude-code")

        result = wrap.pin_verdicts(
            cfg, "s-1", [{"prompt": "no, use a dict", "register": "correction", "about": "the shape"}]
        )
        assert result.verdicts_written == 1 and result.errors == []
        (feedback,) = [r for r in _rows(cfg, "s-1") if r.get("type") == "feedback"]
        assert feedback["register"] == "correction"
        assert feedback["ts"] == TS and feedback["about"] == "the shape"
        assert feedback["prompt_ref"] == "no, use a dict here"

    def test_an_unmatched_verdict_is_announced(self, cfg: Config):
        result = wrap.pin_verdicts(cfg, "s-2", [{"prompt": "never said", "register": "correction"}])
        assert result.verdicts_unmatched == 1
        assert _rows(cfg, "s-2") == []


class TestPriorTurns:
    """Stop archives the live buffer at the end of every turn, so a
    correction about an earlier turn names a prompt in ``events.jsonl``."""

    def test_a_verdict_naming_turn_one_pins_once_and_reaches_the_round(
        self, cfg: Config, monkeypatch, tmp_path: Path, capsys
    ):
        folder = _turns(monkeypatch, cfg, tmp_path, [
            "turn one: keep rounds sealed", "turn two: add a test", "turn three: ship it",
        ])
        # Turn one's Stop archived the buffer; later Stops mirror and keep it.
        assert not any(r.get("text", "").startswith("turn one") for r in _rows(cfg, SESSION))
        (turn_one,) = [
            r for r in iter_jsonl(folder / "events.jsonl")
            if r.get("type") == "prompt" and r["text"].startswith("turn one")
        ]
        verdict = json.dumps([{"prompt": "turn one", "register": "correction",
                               "about": "rounds must stay sealed"}])

        _cli(["task", "feedback", verdict, "--session", SESSION])
        assert "1 written, 0 already labeled, 0 unmatched" in capsys.readouterr().out
        _cli(["task", "feedback", verdict, "--session", SESSION])
        assert "0 written, 1 already labeled, 0 unmatched" in capsys.readouterr().out
        (pinned,) = feedback_events(folder / "events.jsonl")
        assert pinned["ts"] == turn_one["ts"]

        result = wrap.finalize_wrap(
            cfg, session_id=SESSION, project=folder.parent.parent.name, prune=False,
            tasks={"declared": [{"title": "sealed rounds", "round": {}}]},
        )
        (task_id,) = result.tasks["minted"]
        _cli(["task", "render", task_id])
        (entry,) = json.loads(capsys.readouterr().out)["rounds"]
        assert [(f["register"], f["ts"]) for f in entry["feedback"]] == [
            ("correction", turn_one["ts"])
        ]

    def test_a_verdict_in_neither_place_is_unmatched_and_writes_nothing(
        self, cfg: Config, monkeypatch, tmp_path: Path
    ):
        folder = _turns(monkeypatch, cfg, tmp_path, ["turn one", "turn two"])
        live = buffer_path(cfg.weave_dir, SESSION)
        archived, buffered = (folder / "events.jsonl").read_bytes(), live.read_bytes()

        result = wrap.pin_verdicts(
            cfg, SESSION, [{"prompt": "never said", "register": "correction"}]
        )
        assert (result.verdicts_written, result.verdicts_unmatched) == (0, 1)
        assert (folder / "events.jsonl").read_bytes() == archived
        assert live.read_bytes() == buffered
