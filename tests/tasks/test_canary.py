"""The synthetic canary: one artificial task dispatched through the
real seam — SubagentStart on stdin, a scripted performer appending envelope
rows to the return file the descriptor names, SubagentStop on stdin — in a
temp vault. Asserts event rows, stub fields, envelope schema, round compile,
and zero harness ids in filenames or join keys."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.tasks.conftest import run_hook
from thinkweave.core.config import Config
from thinkweave.core.task_contract import (
    TASK_ID_RE,
    validate_envelope,
    validate_task_note,
)
from thinkweave.core.vault import parse_frontmatter
from thinkweave.core.buffer import buffer_path
from thinkweave.operations.tasks import Register

SESSION = "99999999-8888-4777-a666-555544443333"
AGENT = "agent-0f9e8d7c6b5a"


def test_canary(cfg: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("THINKWEAVE_PROJECT", "canary")
    # 1. Dispatch: SubagentStart through the real handler.
    reply = run_hook(
        monkeypatch,
        "subagent_start",
        {
            "session_id": SESSION,
            "cwd": str(tmp_path),
            "hook_event_name": "SubagentStart",
            "agent_id": AGENT,
            "agent_type": "Explore",
            "prompt": "count the canary beans",
        },
    )
    descriptor = json.loads(
        reply["hookSpecificOutput"]["additionalContext"]
    )["thinkweave_task"]
    task_id = descriptor["task_id"]
    assert TASK_ID_RE.fullmatch(task_id)
    assert descriptor["grain"] == "per-dispatch"

    # 2. Scripted performer: append envelope rows to the named return file.
    performer_rows = [
        {"task_id": task_id, "outcome": "ok", "outputs": ["notes/beans.md"]},
        {"task_id": task_id, "outcome": "ok", "role": "Explore"},
    ]
    return_file = Path(descriptor["envelope_return"])
    assert return_file.name == f"{task_id}.jsonl"
    return_file.parent.mkdir(parents=True, exist_ok=True)
    with open(return_file, "a", encoding="utf-8") as f:
        for row in performer_rows:
            f.write(json.dumps(row) + "\n")

    # 3. Boundary close: SubagentStop through the real handler.
    run_hook(
        monkeypatch,
        "subagent_stop",
        {
            "session_id": SESSION,
            "cwd": str(tmp_path),
            "hook_event_name": "SubagentStop",
            "agent_id": AGENT,
            "agent_type": "Explore",
        },
    )

    # 4. Event rows: one open, one close, correlated by task id only.
    register = buffer_path(cfg.weave_dir, SESSION)
    rows = Register(cfg, SESSION, [register]).rows()
    assert [r["type"] for r in rows] == ["task_open", "task_close"]
    assert {r["task_id"] for r in rows} == {task_id}
    for row in rows:
        # The harness agent id travels only as a qualified annotation.
        assert row["session_ref"] == {
            "harness": "claude-code",
            "kind": "agent_id",
            "value": AGENT,
        }

    # 5. Stub fields: a conforming, closed task note with the compiled round.
    stubs = list(cfg.vault_root.rglob(f"{task_id}.md"))
    assert len(stubs) == 1
    fm, _ = parse_frontmatter(stubs[0].read_text(encoding="utf-8"))
    assert validate_task_note(fm) == []
    assert fm["id"] == task_id
    assert fm["status"] == "closed"
    assert fm["grain"] == "per-dispatch"
    assert fm["role"] == "Explore"

    # 6. Envelope schema + round compile.
    assert fm["rounds"][0]["envelopes"] == performer_rows
    for row in fm["rounds"][0]["envelopes"]:
        assert validate_envelope(row) == []

    # 7. Zero harness ids in filenames or join keys.
    for artifact in (stubs[0], return_file):
        assert SESSION not in artifact.name
        assert AGENT not in artifact.name
    assert join_keys_only(rows, task_id)

    # 8. No per-task lifecycle file: lifecycle rows live only in the register.
    for path in cfg.weave_dir.rglob("*.jsonl"):
        if path == register:
            continue
        assert not Register(cfg, SESSION, [path]).rows(), (
            f"lifecycle rows leaked outside the register: {path}"
        )


def join_keys_only(rows: list[dict], task_id: str) -> bool:
    """Every correlating field is the vault-minted task id; harness values
    appear nowhere outside the qualified ``session_ref`` triple and the
    register's own ``session_id`` stamp."""
    for row in rows:
        for key, value in row.items():
            if key in ("session_ref", "session_id"):
                continue
            if isinstance(value, str) and AGENT in value:
                return False
        if row["task_id"] != task_id:
            return False
    return True
