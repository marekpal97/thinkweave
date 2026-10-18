"""Tests for operations/migrations.py.

The migration we ship in Phase 3 D is ``migrate_todo_research_to_queue``:
``todo+research`` notes with a ``source_type`` move into the matching
per-type Queue and have ``todo`` stripped from their tags.
"""

from __future__ import annotations

from pathlib import Path

from thinkweave.core.indexer import Indexer
from thinkweave.core.vault import parse_frontmatter
from thinkweave.operations.migrations import (
    migrate_decision_source_sessions,
    migrate_todo_research_to_queue,
)
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


# --- migrate_decision_source_sessions -------------------------------------

HARNESS_UUID = "0f25944e-40ba-4ddc-b705-899b2d3ef833"


def _session_folder(vault: Path, folder: str, project: str = "proj") -> Path:
    d = vault / "projects" / project / "sessions" / folder
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_session(folder: Path, session_id: str) -> Path:
    path = folder / "session.md"
    path.write_text(
        "---\n"
        "type: session\n"
        f"id: {session_id}\n"
        "date: 2026-04-06T10:00:00+00:00\n"
        "project: proj\n"
        "---\n\n# Session\n",
        encoding="utf-8",
    )
    return path


def _write_decision(
    folder: Path, name: str, dec_id: str, source_session: str | None
) -> Path:
    lines = [
        "---",
        "type: decision",
        f"id: {dec_id}",
        "date: 2026-04-06T10:05:00+00:00",
        "project: proj",
        "status: proposed",
    ]
    if source_session is not None:
        lines.append(f"source_session: {source_session}")
    lines.append("---")
    path = folder / f"{name}.md"
    path.write_text("\n".join(lines) + "\n\n# Decision\n", encoding="utf-8")
    return path


def _indexed_source_session(vault_handle, dec_id: str):
    idx = Indexer(config=vault_handle.config)
    try:
        row = idx.db.execute(
            "SELECT json_extract(frontmatter, '$.source_session') "
            "FROM notes WHERE id = ?",
            (dec_id,),
        ).fetchone()
    finally:
        idx.close()
    return row[0] if row else None


def test_migrate_decision_source_session_fills_from_colocated_note(
    vault_factory,
) -> None:
    tv = vault_factory()
    folder = _session_folder(tv.dir, f"{HARNESS_UUID}-2026-04-06")
    _write_session(folder, "ses-aaa111")
    dec = _write_decision(folder, "pick-sqlite", "dec-bbb222", HARNESS_UUID)
    tv.indexed()

    report = migrate_decision_source_sessions(tv.config)

    assert report.filled == 1
    assert report.unresolved == ()
    fm, _ = parse_frontmatter(dec.read_text(encoding="utf-8"))
    assert fm["source_session"] == "ses-aaa111"
    # The touched note is reindexed: the index serves the new value.
    assert _indexed_source_session(tv, "dec-bbb222") == "ses-aaa111"


def test_migrate_decision_source_session_fills_a_missing_key(vault_factory) -> None:
    tv = vault_factory()
    folder = _session_folder(tv.dir, f"{HARNESS_UUID}-2026-04-06")
    _write_session(folder, "ses-aaa111")
    dec = _write_decision(folder, "pick-sqlite", "dec-bbb222", None)
    tv.indexed()

    report = migrate_decision_source_sessions(tv.config)

    assert report.filled == 1
    fm, _ = parse_frontmatter(dec.read_text(encoding="utf-8"))
    assert fm["source_session"] == "ses-aaa111"


def test_migrate_decision_source_session_resolves_folder_slug(vault_factory) -> None:
    tv = vault_factory()
    real = _session_folder(tv.dir, "ses-ddd444-2026-05-01")
    _write_session(real, "ses-ddd444")
    # A same-slug folder whose session.md is gone: the slug still resolves
    # through the path against the indexed session ids.
    orphan = _session_folder(tv.dir, "ses-ddd444-2026-05-02")
    _write_decision(orphan, "pick-sqlite", "dec-bbb222", None)
    tv.indexed()

    report = migrate_decision_source_sessions(tv.config)

    assert report.filled == 1
    assert _indexed_source_session(tv, "dec-bbb222") == "ses-ddd444"


def test_migrate_decision_source_session_lists_unresolved(vault_factory) -> None:
    tv = vault_factory()
    misc = _session_folder(tv.dir, "misc")
    dec = _write_decision(misc, "lone-decision", "dec-ccc333", None)
    before = dec.read_text(encoding="utf-8")
    tv.indexed()

    report = migrate_decision_source_sessions(tv.config)

    assert report.filled == 0
    assert report.unresolved == ("dec-ccc333",)
    # Never guessed: the note is left byte-identical.
    assert dec.read_text(encoding="utf-8") == before


def test_migrate_decision_source_session_second_run_fills_zero(vault_factory) -> None:
    tv = vault_factory()
    folder = _session_folder(tv.dir, f"{HARNESS_UUID}-2026-04-06")
    _write_session(folder, "ses-aaa111")
    _write_decision(folder, "pick-sqlite", "dec-bbb222", HARNESS_UUID)
    tv.indexed()

    first = migrate_decision_source_sessions(tv.config)
    second = migrate_decision_source_sessions(tv.config)

    assert first.filled == 1
    assert second.filled == 0
    assert second.unresolved == ()
