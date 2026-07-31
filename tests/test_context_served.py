"""Tests for the ``context_served`` SQLite projection (slice 3 of RLVR).

The indexer walks every session folder for a sibling ``retrieval_log.jsonl``
and upserts rows into ``context_served(session_id, note_id, source, ts)``. The
projection runs at the end of every ``rebuild()`` and also opportunistically
from ``index_file()`` when the indexed file is a session note (the wrap-finalize
incremental-index path relies on that).

Tests cover:

- Full rebuild from a vault where one session has both startup + onthefly events
- Sessions with no retrieval log are silently skipped
- Both startup and onthefly rows can land for the same (session, note) pair
- Idempotent: re-running rebuild doesn't double up rows
- ``index_file`` on a session note projects its sibling log on its own
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def config(vault_dir: Path) -> Config:
    return Config(vault_root=vault_dir)


@pytest.fixture
def vault(config: Config) -> VaultManager:
    vm = VaultManager(config=config)
    vm.ensure_dirs()
    return vm


def _seed_session(vault: VaultManager, log_lines: list[dict] | None) -> tuple[str, Path]:
    """Create a session note + optional retrieval_log.jsonl in its folder."""
    sess_path = vault.create_note(
        NoteType.SESSION,
        "S",
        body="## Summary\nseed\n",
        project="t",
        extra_frontmatter={"processed": True},
    )
    sess_id = vault.read_note(sess_path).id
    if log_lines is not None:
        (sess_path.parent / "retrieval_log.jsonl").write_text(
            "\n".join(json.dumps(line) for line in log_lines) + "\n",
            encoding="utf-8",
        )
    return sess_id, sess_path


def _select_all(idx: Indexer, session_id: str) -> list[dict]:
    rows = idx.db.execute(
        "SELECT note_id, source, ts FROM context_served "
        "WHERE session_id = ? ORDER BY source, note_id",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


class TestRebuildProjection:
    def test_full_rebuild_projects_both_sources(
        self, config: Config, vault: VaultManager
    ):
        sess_id, _ = _seed_session(vault, [
            {"ts": "2026-05-14T10:00:00Z", "type": "startup",
             "returned_ids": ["n-aaa111aa", "dec-bbb222bb"], "token_est": 5000},
            {"ts": "2026-05-14T10:05:00Z", "type": "retrieval",
             "tool": "mcp__thinkweave__weave_search",
             "returned_ids": ["n-ccc333cc"]},
            {"ts": "2026-05-14T10:06:00Z", "type": "retrieval",
             "tool": "mcp__thinkweave__weave_read",
             "returned_ids": ["dec-bbb222bb"]},  # same note, different source
        ])

        idx = Indexer(config=config)
        try:
            idx.rebuild(full=True)
            rows = _select_all(idx, sess_id)
        finally:
            idx.close()

        # Expected rows: 2 startup + 2 onthefly (dec-bbb222bb shows up in both)
        assert len(rows) == 4
        startup = [r for r in rows if r["source"] == "startup"]
        onthefly = [r for r in rows if r["source"] == "onthefly"]
        assert {r["note_id"] for r in startup} == {"n-aaa111aa", "dec-bbb222bb"}
        assert {r["note_id"] for r in onthefly} == {"n-ccc333cc", "dec-bbb222bb"}

    def test_session_without_log_yields_no_rows(
        self, config: Config, vault: VaultManager
    ):
        sess_id, _ = _seed_session(vault, None)
        idx = Indexer(config=config)
        try:
            idx.rebuild(full=True)
            rows = _select_all(idx, sess_id)
        finally:
            idx.close()
        assert rows == []

    def test_idempotent_rerun(self, config: Config, vault: VaultManager):
        sess_id, _ = _seed_session(vault, [
            {"ts": "ts1", "type": "retrieval",
             "tool": "mcp__thinkweave__weave_search",
             "returned_ids": ["n-aaa111aa"]},
        ])
        idx = Indexer(config=config)
        try:
            idx.rebuild(full=True)
            first = _select_all(idx, sess_id)
            idx.rebuild(full=False)
            second = _select_all(idx, sess_id)
            idx.rebuild(full=True)  # full again
            third = _select_all(idx, sess_id)
        finally:
            idx.close()
        assert len(first) == 1
        assert first == second == third  # INSERT OR REPLACE keeps it stable

    def test_malformed_lines_are_skipped(self, config: Config, vault: VaultManager):
        sess_path = vault.create_note(
            NoteType.SESSION, "S", body="## Summary\n", project="t",
        )
        sess_id = vault.read_note(sess_path).id
        (sess_path.parent / "retrieval_log.jsonl").write_text(
            "not json\n"
            + json.dumps({"type": "retrieval", "returned_ids": ["n-aaa111aa"]}) + "\n"
            + json.dumps({"type": "retrieval", "returned_ids": [None, 42, ""]}) + "\n"
            + json.dumps({"type": "garbage", "returned_ids": ["x-yyy"]}) + "\n",
            encoding="utf-8",
        )

        idx = Indexer(config=config)
        try:
            idx.rebuild(full=True)
            rows = _select_all(idx, sess_id)
        finally:
            idx.close()
        # Only the one valid retrieval row with one valid id should land.
        assert len(rows) == 1
        assert rows[0]["note_id"] == "n-aaa111aa"
        assert rows[0]["source"] == "onthefly"

    def test_full_rebuild_wipes_stale_rows(
        self, config: Config, vault: VaultManager
    ):
        # First seed a log, project it, then truncate the log and rebuild full.
        sess_path = vault.create_note(
            NoteType.SESSION, "S", body="## Summary\n", project="t",
        )
        sess_id = vault.read_note(sess_path).id
        log = sess_path.parent / "retrieval_log.jsonl"
        log.write_text(
            json.dumps({"type": "retrieval", "returned_ids": ["n-stale123"]}) + "\n",
            encoding="utf-8",
        )

        idx = Indexer(config=config)
        try:
            idx.rebuild(full=True)
            assert len(_select_all(idx, sess_id)) == 1
            # User edits the log (or it gets clipped) — rewrite without that id.
            log.write_text(
                json.dumps({"type": "retrieval", "returned_ids": ["n-fresh999"]}) + "\n",
                encoding="utf-8",
            )
            idx.rebuild(full=True)
            rows = _select_all(idx, sess_id)
        finally:
            idx.close()
        # Full rebuild deletes context_served first → stale row is gone.
        assert len(rows) == 1
        assert rows[0]["note_id"] == "n-fresh999"


class TestLoopPrimeProjection:
    def test_loop_prime_event_projects_distinct_source(
        self, config: Config, vault: VaultManager
    ):
        """A claim-time prime served-context event (issue_loop.py writes a
        retrieval event tagged tool='loop_prime') projects to
        context_served(source='loop-prime') — distinct from agent-pulled
        onthefly — so served ids are recoverable per run from the index."""
        sess_id, _ = _seed_session(vault, [
            {"ts": "2026-07-18T00:30:00Z", "type": "retrieval",
             "tool": "loop_prime",
             "args": {"run_id": "loop-20260718-abcd", "issue": 57},
             "returned_ids": ["n-prior111", "dec-abc222"]},
            {"ts": "2026-07-18T00:31:00Z", "type": "retrieval",
             "tool": "mcp__thinkweave__weave_search",
             "returned_ids": ["n-prior111"]},  # same note, agent-pulled later
        ])
        idx = Indexer(config=config)
        try:
            idx.rebuild(full=True)
            rows = _select_all(idx, sess_id)
        finally:
            idx.close()
        prime = {r["note_id"] for r in rows if r["source"] == "loop-prime"}
        onthefly = {r["note_id"] for r in rows if r["source"] == "onthefly"}
        assert prime == {"n-prior111", "dec-abc222"}
        # The later agent-pull is onthefly, NOT folded into loop-prime.
        assert onthefly == {"n-prior111"}

    def test_narrow_check_table_is_migrated_to_admit_loop_prime(
        self, config: Config, vault: VaultManager
    ):
        """A pre-#57 vault created context_served with a CHECK that rejects
        'loop-prime'. Opening the Indexer drops+recreates the derived table
        (SQLite can't ALTER a CHECK), so the loop-prime projection succeeds
        instead of raising IntegrityError."""
        sess_id, sess_path = _seed_session(vault, [
            {"type": "retrieval", "tool": "loop_prime", "returned_ids": ["n-p1"]},
        ])
        # Simulate the legacy schema: narrow CHECK without 'loop-prime'.
        idx0 = Indexer(config=config)
        idx0.db.execute("DROP TABLE context_served")
        idx0.db.executescript(
            "CREATE TABLE context_served ("
            " session_id TEXT NOT NULL, note_id TEXT NOT NULL,"
            " source TEXT NOT NULL CHECK(source IN ('startup','onthefly','prompttime')),"
            " ts TEXT, PRIMARY KEY (session_id, note_id, source));"
        )
        idx0.db.commit()
        idx0.close()

        # Reopening triggers the migration in _init_schema; rebuild then
        # projects the loop-prime row without a CHECK violation.
        idx = Indexer(config=config)
        try:
            idx.rebuild(full=True)
            rows = _select_all(idx, sess_id)
        finally:
            idx.close()
        assert [(r["note_id"], r["source"]) for r in rows] == [("n-p1", "loop-prime")]


class TestIndexFileProjection:
    def test_index_file_on_session_projects_log(
        self, config: Config, vault: VaultManager
    ):
        # The wrap-finalize hot path: archive_buffer writes retrieval_log.jsonl,
        # then incremental index_file picks it up via the session note's
        # parent dir.
        sess_path = vault.create_note(
            NoteType.SESSION, "S", body="## Summary\n", project="t",
        )
        sess_id = vault.read_note(sess_path).id
        # Initial index without the log (just like a fresh session note).
        idx = Indexer(config=config)
        try:
            idx.index_file(sess_path)
            assert _select_all(idx, sess_id) == []

            # Now archive_buffer writes the retrieval log next to session.md.
            (sess_path.parent / "retrieval_log.jsonl").write_text(
                json.dumps({"type": "retrieval", "returned_ids": ["n-after999"]}) + "\n",
                encoding="utf-8",
            )
            # The next incremental index_file picks it up.
            idx.index_file(sess_path)
            rows = _select_all(idx, sess_id)
        finally:
            idx.close()
        assert len(rows) == 1
        assert rows[0]["note_id"] == "n-after999"

    def test_index_file_on_non_session_does_not_touch_context(
        self, config: Config, vault: VaultManager
    ):
        # Indexing a non-session note shouldn't trigger projection (no
        # accidental log scan in the wrong folder).
        sess_id, sess_path = _seed_session(vault, [
            {"type": "retrieval", "returned_ids": ["n-aaaabbbb"]},
        ])
        # Also write a decision note in the same folder — wrap-finalize indexes
        # those, and they shouldn't double-project the session's log.
        dec_path = vault.create_note(
            NoteType.DECISION, "D",
            body="## Context\n\n## Decision\n", project="t",
            extra_frontmatter={
                "source_session": sess_id,
                "concepts": ["a", "b"],
            },
            output_dir=sess_path.parent,
        )
        idx = Indexer(config=config)
        try:
            # Index the session first so the log gets projected once.
            idx.index_file(sess_path)
            rows_after_sess = _select_all(idx, sess_id)
            # Indexing the decision must not re-trigger projection.
            idx.index_file(dec_path)
            rows_after_dec = _select_all(idx, sess_id)
        finally:
            idx.close()
        assert rows_after_sess == rows_after_dec
        # And the count is the expected 1 (no duplicates).
        assert len(rows_after_sess) == 1
