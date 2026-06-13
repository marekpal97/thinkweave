"""Tests for the ``prompts`` / ``prompt_concepts`` SQL projection.

The user's question stream as a queryable substrate: the indexer walks
every session folder for a sibling ``events.jsonl`` and projects its
prompt events into ``prompts(session_id, seq, ts, text, classification,
project)``, attributing concepts (``prompt_concepts``) to probe rows via
the same substring rule the live probe-pressure path uses
(``core.events.match_probe_concepts``). JSONL stays truth — the table is
rebuilt by ``weave index --full`` and kept fresh by per-file re-index.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.events import match_probe_concepts
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(vault_root=tmp_path / "vault")


@pytest.fixture
def vault(config: Config) -> VaultManager:
    vm = VaultManager(config=config)
    vm.ensure_dirs()
    return vm


PROBE_TEXT = "How does the embedding-cache invalidation work?"
INSTRUCTION_TEXT = "Refactor the indexer rebuild loop"


def _event(text: str, ts: str = "2026-06-10T10:00:00+00:00") -> dict:
    return {
        "type": "prompt",
        "text": text,
        "session_id": "cc-uuid-1",
        "ts": ts,
        "cwd": "/tmp",
    }


def _seed_session(
    vault: VaultManager, events: list[dict] | None
) -> tuple[str, Path]:
    """Create a session note + optional events.jsonl in its folder."""
    sess_path = vault.create_note(
        NoteType.SESSION,
        "S",
        body="## Summary\nseed\n",
        project="t",
        extra_frontmatter={"processed": True},
    )
    sess_id = vault.read_note(sess_path).id
    if events is not None:
        (sess_path.parent / "events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n",
            encoding="utf-8",
        )
    return sess_id, sess_path


def _seed_vocabulary_note(vault: VaultManager) -> None:
    """Put ``embedding-cache`` into the indexed proposed-concept pool."""
    vault.create_note(
        NoteType.NOTE,
        "Vocab seed",
        body="seed\n",
        project="t",
        extra_frontmatter={"proposed_concepts": ["embedding-cache"]},
    )


def _prompt_rows(idx: Indexer, session_id: str) -> list[dict]:
    return [
        dict(r)
        for r in idx.db.execute(
            "SELECT seq, ts, text, classification, project FROM prompts "
            "WHERE session_id = ? ORDER BY seq",
            (session_id,),
        )
    ]


def _concept_rows(idx: Indexer, session_id: str) -> list[tuple[int, str]]:
    return [
        (r["seq"], r["concept"])
        for r in idx.db.execute(
            "SELECT seq, concept FROM prompt_concepts "
            "WHERE session_id = ? ORDER BY seq, concept",
            (session_id,),
        )
    ]


class TestMatchProbeConcepts:
    def test_substring_match_keeps_hyphenated_slugs(self):
        vocab = {"embedding-cache", "fts5", "ai"}
        assert match_probe_concepts(PROBE_TEXT, vocab) == {"embedding-cache"}

    def test_short_slugs_filtered(self):
        # 1–2 char slugs match everything; the 3-char floor drops them.
        assert match_probe_concepts("ai everywhere", {"ai", "-", "a"}) == set()

    def test_empty_text(self):
        assert match_probe_concepts("  ", {"embedding-cache"}) == set()


class TestRebuildProjection:
    def test_full_rebuild_projects_and_attributes(
        self, config: Config, vault: VaultManager
    ):
        _seed_vocabulary_note(vault)
        sess_id, _ = _seed_session(
            vault, [_event(PROBE_TEXT), _event(INSTRUCTION_TEXT)]
        )

        idx = Indexer(config=config)
        try:
            idx.rebuild(full=True)
            rows = _prompt_rows(idx, sess_id)
            assert [r["seq"] for r in rows] == [0, 1]
            assert rows[0]["text"] == PROBE_TEXT
            assert rows[0]["classification"] == "probe"
            assert rows[0]["project"] == "t"
            assert rows[1]["classification"] is None
            # Concept attribution lands on the probe row only.
            assert (0, "embedding-cache") in _concept_rows(idx, sess_id)
            assert all(seq == 0 for seq, _ in _concept_rows(idx, sess_id))
        finally:
            idx.close()

    def test_rebuild_idempotent(self, config: Config, vault: VaultManager):
        _seed_vocabulary_note(vault)
        sess_id, _ = _seed_session(vault, [_event(PROBE_TEXT)])

        idx = Indexer(config=config)
        try:
            idx.rebuild(full=True)
            idx.rebuild(full=True)
            assert len(_prompt_rows(idx, sess_id)) == 1
        finally:
            idx.close()

    def test_session_without_events_skipped(
        self, config: Config, vault: VaultManager
    ):
        sess_id, _ = _seed_session(vault, None)
        idx = Indexer(config=config)
        try:
            idx.rebuild(full=True)
            assert _prompt_rows(idx, sess_id) == []
        finally:
            idx.close()


class TestIndexFileProjection:
    def test_reindex_replaces_rows(self, config: Config, vault: VaultManager):
        """Delete-then-insert: re-projection after the events file grows."""
        _seed_vocabulary_note(vault)
        sess_id, sess_path = _seed_session(vault, [_event(INSTRUCTION_TEXT)])

        idx = Indexer(config=config)
        try:
            idx.rebuild(full=True)
            assert len(_prompt_rows(idx, sess_id)) == 1

            events = [
                _event(INSTRUCTION_TEXT),
                _event(PROBE_TEXT, ts="2026-06-10T11:00:00+00:00"),
            ]
            (sess_path.parent / "events.jsonl").write_text(
                "\n".join(json.dumps(e) for e in events) + "\n",
                encoding="utf-8",
            )
            idx.index_file(sess_path)

            rows = _prompt_rows(idx, sess_id)
            assert len(rows) == 2
            assert rows[1]["classification"] == "probe"
            assert (1, "embedding-cache") in _concept_rows(idx, sess_id)
        finally:
            idx.close()
