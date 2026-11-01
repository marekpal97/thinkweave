"""The ``turn_end`` event: Stop closes each turn in the session's ledger."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from thinkweave.core.buffer import append_event, buffer_path
from thinkweave.core.config import Config
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager


@pytest.fixture
def session(tmp_path: Path, monkeypatch):
    """A session note for harness session ``h-1``, the handler aimed at its vault."""
    from thinkweave.surfaces.hooks import handler

    cfg = Config(vault_root=tmp_path / "vault")
    monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
    monkeypatch.setattr(handler, "_LOG_DIR", None)
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    note = vm.create_note(
        NoteType.SESSION, "S", project="p", extra_frontmatter={"source_session": "h-1"}
    )
    return cfg, note.parent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _turn(cfg: Config, prompt: str, *files: str) -> None:
    live = buffer_path(cfg.weave_dir, "h-1")
    append_event(live, {"ts": _now(), "type": "prompt", "text": prompt}, harness="")
    for f in files:
        append_event(live, {"ts": _now(), "tool": "Edit", "file": f, "context": ""},
                     harness="")


def _archived(folder: Path, name: str) -> list[dict]:
    path = folder / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_stop_closes_each_turn_with_its_sequence_capped_message_and_files(session):
    from thinkweave.surfaces.hooks import handler

    cfg, folder = session
    _turn(cfg, "fix the parser", "src/parser.py")
    handler._handle_stop({"session_id": "h-1", "last_assistant_message": "x" * 3000})
    _turn(cfg, "now the tests", "tests/test_parser.py", "src/parser.py")
    handler._handle_stop({"session_id": "h-1", "last_assistant_message": "done"})

    ends = [e for e in _archived(folder, "events.jsonl") if e.get("type") == "turn_end"]
    assert [(e["turn"], len(e["message"]), e["files"]) for e in ends] == [
        (1, 2000, ["src/parser.py"]),
        (2, 4, ["tests/test_parser.py", "src/parser.py"]),
    ]
    assert not [e for e in _archived(folder, "retrieval_log.jsonl")
                if e.get("type") == "turn_end"]


def test_a_second_stop_on_the_same_turn_writes_no_second_turn_end(session):
    from thinkweave.surfaces.hooks import handler

    cfg, folder = session
    _turn(cfg, "fix the parser")
    handler._handle_stop({"session_id": "h-1", "last_assistant_message": "first"})
    handler._handle_stop({"session_id": "h-1", "last_assistant_message": "again"})

    ends = [e for e in _archived(folder, "events.jsonl") if e.get("type") == "turn_end"]
    assert [(e["turn"], e["message"]) for e in ends] == [(1, "first")]


def test_the_prompt_gate_numbers_its_verdict_with_the_turn_stop_will_close(
    session, monkeypatch
):
    import thinkweave.operations.prompt_time_retrieval as ptr
    from thinkweave.core.config import DecisionSite
    from thinkweave.core.decision_provider import FakeDecisionProvider
    from thinkweave.surfaces.hooks import handler

    cfg, _folder = session
    _turn(cfg, "fix the parser please")
    handler._handle_stop({"session_id": "h-1", "last_assistant_message": "ok"})
    _turn(cfg, "and now explain the parser to me")

    class _Hit:
        id, title, type, snippet = "n-cand0001", "Parser notes", "note", ""

    cfg.decision_sites["prompt_gate"] = DecisionSite(enabled=True)
    monkeypatch.setattr(ptr, "_pool", lambda *a, **k: ptr.Pool([_Hit()], False))
    monkeypatch.setattr("thinkweave.operations.decisions.build_from_vault",
                        lambda *a, **k: FakeDecisionProvider())
    ptr.build_enrichment(cfg, "h-1", "and now explain the parser to me")

    gate = [e for e in ptr._read_buffer_events(cfg, "h-1") if e.get("type") == "decision"]
    assert [e["turn"] for e in gate] == [2]


def test_the_ledger_closes_the_open_turn_with_a_declared_turn_end(session):
    from thinkweave.operations.decisions import Ledger, TurnEnd

    cfg, folder = session
    _turn(cfg, "fix the parser", "src/parser.py")

    end = Ledger.read(cfg, "h-1", folder).turn_end("done")

    assert isinstance(end, TurnEnd)
    assert (end.turn, end.message, end.files) == (1, "done", ["src/parser.py"])
    assert end.as_event()["type"] == "turn_end"
