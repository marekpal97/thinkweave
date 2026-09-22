"""The open-tasks SessionStart serving row (#218).

Continuation identity needs the open task set in front of the model at
session start, so ``build_project_context`` gains a ``tasks`` section: one
line per open ``kind: task`` note (id, title, issue ref, status), omitted
entirely when nothing is open. Served task ids project to
``context_served`` under their own ``source`` value (``open-tasks``) —
distinct from the surrounding ``startup`` payload and from agent-pulled
``onthefly`` — and the derived-table migration recreates the CHECK so
older vaults admit the new source.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager
from thinkweave.operations.retrieval_log import parse_returned_ids
from thinkweave.retrieval.context import build_project_context


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(vault_root=tmp_path / "vault")


@pytest.fixture
def vault(config: Config) -> VaultManager:
    vm = VaultManager(config=config)
    vm.ensure_dirs()
    return vm


def _seed_task(
    vault: VaultManager,
    task_id: str,
    title: str,
    *,
    status: str = "open",
    asked: str = "",
) -> None:
    fm: dict = {
        "kind": "task",
        "status": status,
        "grain": "work",
        "rounds": [],
        "title": title,
    }
    if asked:
        fm["asked"] = asked
    vault.create_note(
        NoteType.NOTE,
        title=task_id,
        project="t",
        extra_frontmatter=fm,
        note_id=task_id,
    )


def _reindex(config: Config) -> None:
    idx = Indexer(config=config)
    try:
        idx.rebuild(full=True)
    finally:
        idx.close()


class TestOpenTasksSection:
    def test_payload_lists_open_tasks(self, config: Config, vault: VaultManager):
        _seed_task(vault, "tsk-aaaa1111", "Ship the serving row", asked="#218")
        _seed_task(vault, "tsk-bbbb2222", "Done already", status="closed")
        _reindex(config)

        payload = build_project_context(config, "t")

        assert "## Open Tasks" in payload
        assert "tsk-aaaa1111" in payload
        assert "Ship the serving row" in payload
        assert "#218" in payload
        assert "open" in payload
        # Closed tasks never surface.
        assert "tsk-bbbb2222" not in payload

    def test_section_omitted_when_no_open_tasks(
        self, config: Config, vault: VaultManager
    ):
        _seed_task(vault, "tsk-bbbb2222", "Done already", status="closed")
        _reindex(config)

        payload = build_project_context(config, "t")

        assert "## Open Tasks" not in payload
        assert "tsk-" not in payload

    def test_served_task_ids_are_captured(self, config: Config, vault: VaultManager):
        """The SessionStart capture rail parses served ids out of the payload
        text; task ids must be recognized or the serving never projects."""
        _seed_task(vault, "tsk-aaaa1111", "Ship the serving row", asked="#218")
        _reindex(config)

        payload = build_project_context(config, "t")

        assert "tsk-aaaa1111" in parse_returned_ids(payload)


def _seed_session(vault: VaultManager, log_lines: list[dict]) -> str:
    sess_path = vault.create_note(
        NoteType.SESSION, "S", body="## Summary\nseed\n", project="t"
    )
    (sess_path.parent / "retrieval_log.jsonl").write_text(
        "\n".join(json.dumps(line) for line in log_lines) + "\n",
        encoding="utf-8",
    )
    return vault.read_note(sess_path).id


def _select_all(idx: Indexer, session_id: str) -> list[tuple[str, str]]:
    rows = idx.db.execute(
        "SELECT note_id, source FROM context_served "
        "WHERE session_id = ? ORDER BY source, note_id",
        (session_id,),
    ).fetchall()
    return [(r["note_id"], r["source"]) for r in rows]


class TestOpenTasksProjection:
    def test_startup_task_ids_project_distinct_source(
        self, config: Config, vault: VaultManager
    ):
        sess_id = _seed_session(vault, [
            {"ts": "2026-09-23T08:00:00Z", "type": "startup",
             "returned_ids": ["n-aaa111aa", "tsk-aaaa1111"], "token_est": 100},
        ])
        idx = Indexer(config=config)
        try:
            idx.rebuild(full=True)
            rows = _select_all(idx, sess_id)
        finally:
            idx.close()
        assert rows == [
            ("tsk-aaaa1111", "open-tasks"),
            ("n-aaa111aa", "startup"),
        ]

    def test_agent_pulled_task_id_stays_onthefly(
        self, config: Config, vault: VaultManager
    ):
        sess_id = _seed_session(vault, [
            {"ts": "2026-09-23T08:01:00Z", "type": "retrieval",
             "tool": "mcp__thinkweave__weave_read",
             "returned_ids": ["tsk-aaaa1111"]},
        ])
        idx = Indexer(config=config)
        try:
            idx.rebuild(full=True)
            rows = _select_all(idx, sess_id)
        finally:
            idx.close()
        assert rows == [("tsk-aaaa1111", "onthefly")]

    def test_narrow_check_table_is_migrated_to_admit_open_tasks(
        self, config: Config, vault: VaultManager
    ):
        """A pre-#218 vault created context_served with a CHECK that rejects
        'open-tasks'. Opening the Indexer drops+recreates the derived table
        (SQLite can't ALTER a CHECK) and re-projects, so the open-tasks row
        lands instead of raising IntegrityError."""
        sess_id = _seed_session(vault, [
            {"type": "startup", "returned_ids": ["tsk-aaaa1111"]},
        ])
        idx0 = Indexer(config=config)
        idx0.db.execute("DROP TABLE context_served")
        idx0.db.executescript(
            "CREATE TABLE context_served ("
            " session_id TEXT NOT NULL, note_id TEXT NOT NULL,"
            " source TEXT NOT NULL CHECK(source IN ('startup','onthefly',"
            "'prompttime','loop-prime','codex-startup')),"
            " ts TEXT, PRIMARY KEY (session_id, note_id, source));"
        )
        idx0.db.commit()
        idx0.close()

        idx = Indexer(config=config)
        try:
            idx.rebuild(full=True)
            rows = _select_all(idx, sess_id)
        finally:
            idx.close()
        assert rows == [("tsk-aaaa1111", "open-tasks")]
