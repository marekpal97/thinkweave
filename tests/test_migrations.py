"""Tests for operations/migrations.py.

The migration we ship in Phase 3 D is ``migrate_todo_research_to_queue``:
``todo+research`` notes with a ``source_type`` move into the matching
per-type Queue and have ``todo`` stripped from their tags.
"""

from __future__ import annotations

from pathlib import Path

from thinkweave.operations.migrations import migrate_todo_research_to_queue
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
