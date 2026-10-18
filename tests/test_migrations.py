"""Tests for operations/migrations.py.

The migration we ship in Phase 3 D is ``migrate_todo_research_to_queue``:
``todo+research`` notes with a ``source_type`` move into the matching
per-type Queue and have ``todo`` stripped from their tags.
"""

from __future__ import annotations

import json
from pathlib import Path

from thinkweave.core.task_contract import Round
from thinkweave.core.vault import NoteType
from thinkweave.operations.migrations import (
    migrate_feedback_text,
    migrate_todo_research_to_queue,
)
from thinkweave.operations.tasks import TaskStore
from thinkweave.acquisition.sources.queue import Queue


def _write_note(
    vault: Path,
    name: str,
    *,
    tags: list[str],
    body: str,
    source_type: str = "",
    note_id: str = "",
) -> Path:
    fm_lines = ["---", f"title: {name}", "type: note"]
    if note_id:
        fm_lines.append(f"id: {note_id}")
    if source_type:
        fm_lines.append(f"source_type: {source_type}")
    fm_lines.append("tags: [" + ", ".join(tags) + "]")
    fm_lines.append("---")
    text = "\n".join(fm_lines) + "\n" + body
    path = vault / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_migrates_todo_research_paper_to_paper_queue(tmp_path: Path) -> None:
    note = _write_note(
        tmp_path,
        "to-read",
        tags=["todo", "research"],
        body="https://arxiv.org/abs/2305.10403\n\nGood paper",
        source_type="paper",
        note_id="n-1",
    )

    moved = migrate_todo_research_to_queue(tmp_path)
    assert moved == 1

    q = Queue.for_source_type("paper", tmp_path)
    items = q.peek(10)
    assert len(items) == 1
    assert items[0]["url"] == "https://arxiv.org/abs/2305.10403"
    assert items[0]["source_note_id"] == "n-1"

    # Note's todo tag has been stripped.
    txt = note.read_text(encoding="utf-8")
    assert "todo" not in txt.split("---")[1]
    assert "research" in txt.split("---")[1]


def test_legacy_note_without_source_type_falls_back_to_article(tmp_path: Path) -> None:
    _write_note(
        tmp_path,
        "old-link",
        tags=["todo", "research"],
        body="https://example.com/post",
    )
    migrate_todo_research_to_queue(tmp_path)

    q = Queue.for_source_type("article", tmp_path)
    assert len(q.peek(10)) == 1


def test_idempotent_no_double_enqueue(tmp_path: Path) -> None:
    _write_note(
        tmp_path,
        "to-read",
        tags=["todo", "research"],
        body="https://arxiv.org/abs/2305.10403",
        source_type="paper",
    )
    migrate_todo_research_to_queue(tmp_path)
    # Re-running shouldn't enqueue again — todo tag is stripped, body skipped.
    second = migrate_todo_research_to_queue(tmp_path)
    assert second == 0
    q = Queue.for_source_type("paper", tmp_path)
    assert len(q.peek(10)) == 1


def test_skip_notes_without_research_tag(tmp_path: Path) -> None:
    _write_note(
        tmp_path,
        "todo-only",
        tags=["todo"],
        body="https://example.com",
        source_type="article",
    )
    moved = migrate_todo_research_to_queue(tmp_path)
    assert moved == 0
    q = Queue.for_source_type("article", tmp_path)
    assert q.peek(10) == []


# ---------------------------------------------------------------------------
# migrate_feedback_text — full prompt text onto task-round feedback entries
# ---------------------------------------------------------------------------

SESSION = "s-1"
TS = "2026-10-02T10:00:00+00:00"
# Longer than the 120-character join key, so a fill proves it went past it.
PROMPT = "no, keep rounds sealed — the ledger references a session, " * 3


def _seed_round(
    handle,
    prompt: str,
    *,
    events: bool = True,
    entry: dict | None = None,
) -> str:
    """One wrapped session (note + prompt event) and one work task whose
    round carries a text-less feedback entry. Returns the task id."""
    ses = handle.vault.create_note(
        note_type=NoteType.SESSION,
        title="S",
        body="## Summary\nx\n",
        project="t",
        extra_frontmatter={"source_session": SESSION},
    )
    if events:
        row = {"ts": TS, "type": "prompt", "session_id": SESSION, "text": prompt}
        (ses.parent / "events.jsonl").write_text(
            json.dumps(row) + "\n", encoding="utf-8"
        )
    entry = entry or {"register": "correction", "prompt_ref": prompt[:120], "ts": TS}
    task = TaskStore(handle.config).mint("work", "T", "t", session_key=SESSION)
    task.put_round(
        Round.from_dict(
            {
                "route": "session",
                "session_ref": {
                    "harness": "claude-code",
                    "kind": "session_id",
                    "value": SESSION,
                },
                "feedback": [entry],
            },
            grain="work",
        )
    )
    task.save(handle.vault)
    return task.id


def _feedback(handle, task_id: str) -> dict:
    task = TaskStore(handle.config).get(task_id)
    assert task is not None
    return task.rounds[0].feedback[0]


def test_migrate_feedback_text_fills_from_the_matching_prompt(vault_factory) -> None:
    handle = vault_factory()
    task_id = _seed_round(handle, PROMPT)

    report = migrate_feedback_text(handle.config)

    assert (report.filled, report.no_events, report.no_match) == (1, 0, 0)
    assert _feedback(handle, task_id)["text"] == PROMPT


def test_migrate_feedback_text_caps_the_fill_at_4000_characters(vault_factory) -> None:
    handle = vault_factory()
    task_id = _seed_round(handle, "x" * 4500)

    report = migrate_feedback_text(handle.config)

    assert report.filled == 1
    assert _feedback(handle, task_id)["text"] == "x" * 4000


def test_migrate_feedback_text_reports_a_session_without_events_file(
    vault_factory,
) -> None:
    handle = vault_factory()
    task_id = _seed_round(handle, PROMPT, events=False)

    report = migrate_feedback_text(handle.config)

    assert (report.filled, report.no_events, report.no_match) == (0, 1, 0)
    assert "text" not in _feedback(handle, task_id)


def test_migrate_feedback_text_reports_a_prompt_nothing_matches(vault_factory) -> None:
    handle = vault_factory()
    task_id = _seed_round(handle, PROMPT, entry={"register": "correction",
                                                 "prompt_ref": "unrelated words",
                                                 "ts": TS})

    report = migrate_feedback_text(handle.config)

    assert (report.filled, report.no_events, report.no_match) == (0, 0, 1)
    assert "text" not in _feedback(handle, task_id)


def test_migrate_feedback_text_second_run_fills_nothing(vault_factory) -> None:
    handle = vault_factory()
    _seed_round(handle, PROMPT)

    first = migrate_feedback_text(handle.config)
    second = migrate_feedback_text(handle.config)

    assert first.filled == 1
    assert (second.filled, second.no_events, second.no_match) == (0, 0, 0)


def test_migrate_feedback_text_task_render_shows_the_full_text(
    vault_factory, monkeypatch, capsys
) -> None:
    handle = vault_factory()
    task_id = _seed_round(handle, PROMPT)
    migrate_feedback_text(handle.config)

    from thinkweave.surfaces.cli.parser import build_parser
    from thinkweave.surfaces.cli.task import cmd_task

    monkeypatch.setattr(
        "thinkweave.core.config.load_config", lambda: handle.config
    )
    cmd_task(build_parser().parse_args(["task", "render", task_id]))

    rendered = json.loads(capsys.readouterr().out)
    assert rendered["rounds"][0]["feedback"][0]["text"] == PROMPT
