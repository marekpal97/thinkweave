"""Tests for ``weave index --embed --only-new`` — the keep-warm path.

Covers ``EmbeddingSearch.compute_all(only_new=True)``: the cheap
nightly refresh path that should only embed notes whose ``updated_at``
is strictly greater than the most recent ``embeddings.created_at``.

The embedding API client is monkeypatched so no real OpenAI calls fire.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.embeddings import EmbeddingSearch
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


@pytest.fixture
def indexer(config: Config):
    idx = Indexer(config=config)
    yield idx
    idx.close()


def _fake_embed(texts):
    """Deterministic 4-d vector per text — content doesn't matter,
    we only care which notes get embedded."""
    return [[float(len(t)), 1.0, 2.0, 3.0] for t in texts]


class TestComputeAllOnlyNew:
    def test_only_new_skips_already_embedded_notes(
        self,
        vault: VaultManager,
        indexer: Indexer,
        config: Config,
        monkeypatch,
    ):
        """First call (full scan, empty embeddings db) embeds N notes.
        Add one new note + re-run with --only-new. Only the new note
        should be embedded; the original N should be untouched."""
        for i in range(3):
            vault.create_note(
                note_type=NoteType.NOTE,
                title=f"Note {i}",
                body=f"body {i}",
            )
        indexer.rebuild()

        monkeypatch.setattr(
            EmbeddingSearch, "_call_api", lambda self, texts: _fake_embed(texts)
        )

        es = EmbeddingSearch(config=config)
        first = es.compute_all()
        assert first["computed"] == 3
        assert first["skipped"] == 0
        es.close()

        # Brief sleep so the new note's updated_at strictly exceeds
        # the embeddings.created_at timestamp captured above.
        time.sleep(0.05)

        vault.create_note(
            note_type=NoteType.NOTE,
            title="Note 3 (new)",
            body="body 3",
        )
        indexer.rebuild()

        # --only-new run: cutoff = max(embeddings.created_at) → only
        # the newest note enters the candidate set.
        es = EmbeddingSearch(config=config)
        second = es.compute_all(only_new=True)
        assert second["computed"] == 1, second
        assert second["scanned"] == 1, second
        assert second["cutoff"], "cutoff should be derived from prior embed run"
        es.close()

    def test_only_new_falls_back_to_full_scan_on_empty_db(
        self,
        vault: VaultManager,
        indexer: Indexer,
        config: Config,
        monkeypatch,
    ):
        """With no prior embeddings, --only-new has no cutoff — it
        should embed everything (the keep-warm cron's first run)."""
        for i in range(2):
            vault.create_note(
                note_type=NoteType.NOTE,
                title=f"Note {i}",
                body=f"body {i}",
            )
        indexer.rebuild()

        monkeypatch.setattr(
            EmbeddingSearch, "_call_api", lambda self, texts: _fake_embed(texts)
        )

        es = EmbeddingSearch(config=config)
        stats = es.compute_all(only_new=True)
        # No prior embeddings → cutoff is empty → full scan.
        assert stats["cutoff"] == ""
        assert stats["computed"] == 2
        es.close()

    def test_explicit_since_overrides_derived_cutoff(
        self,
        vault: VaultManager,
        indexer: Indexer,
        config: Config,
        monkeypatch,
    ):
        """A caller passing --since with a known timestamp gets that
        cutoff regardless of what's already in embeddings."""
        for i in range(2):
            vault.create_note(
                note_type=NoteType.NOTE,
                title=f"Note {i}",
                body=f"body {i}",
            )
        indexer.rebuild()

        monkeypatch.setattr(
            EmbeddingSearch, "_call_api", lambda self, texts: _fake_embed(texts)
        )

        # Far-future cutoff matches no notes → zero embeds.
        es = EmbeddingSearch(config=config)
        stats = es.compute_all(since="2999-01-01T00:00:00+00:00")
        assert stats["cutoff"] == "2999-01-01T00:00:00+00:00"
        assert stats["scanned"] == 0
        assert stats["computed"] == 0
        es.close()
