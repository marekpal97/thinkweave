"""Tests for the model/dimension guard in ``EmbeddingSearch.search``.

A cache can hold rows from more than one embedding model mid-migration
(before ``weave index --embed --reset``). Cosine across models — even at
equal dimensionality — is meaningless, and across dimensionalities the
stdlib ``zip`` in ``cosine_similarity`` would silently truncate to the
shorter vector and return a plausible-but-wrong score. ``search`` must
therefore compare the query only against same-model, same-dim rows.

The embedding API client is monkeypatched so no real calls fire.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.embeddings import EmbeddingSearch
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


@pytest.fixture
def indexer(config: Config):
    idx = Indexer(config=config)
    yield idx
    idx.close()


def _fake_embed(texts):
    """Deterministic 4-d vector per text — matches the configured
    provider's space (``text-embedding-3-small`` by default in a
    config-less test vault)."""
    return [[float(len(t)), 1.0, 2.0, 3.0] for t in texts]


def _insert_row(es: EmbeddingSearch, note_id: str, vec, model: str) -> None:
    es.db.execute(
        "INSERT OR REPLACE INTO embeddings "
        "(note_id, content_hash, embedding, model, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (note_id, "h", struct.pack(f"{len(vec)}f", *vec), model,
         "2026-01-01T00:00:00+00:00"),
    )
    es.db.commit()


def _embed_two_notes(vault, indexer, config, monkeypatch) -> EmbeddingSearch:
    for i in range(2):
        vault.create_note(note_type=NoteType.NOTE, title=f"N{i}", body=f"b{i}")
    indexer.rebuild()
    monkeypatch.setattr(
        EmbeddingSearch, "_call_api", lambda self, texts: _fake_embed(texts)
    )
    es = EmbeddingSearch(config=config)
    stats = es.compute_all()
    assert stats["computed"] == 2
    return es


def test_search_excludes_foreign_model_rows(vault, indexer, config, monkeypatch):
    """A row from a different model (here a 3-d ada-002 stand-in) is
    filtered out by the ``WHERE model = ?`` clause — and crucially does
    not crash the cosine despite the dim mismatch."""
    es = _embed_two_notes(vault, indexer, config, monkeypatch)
    _insert_row(es, "poison-foreign-model", [9.0, 9.0, 9.0], "text-embedding-ada-002")

    hits = es.search("some query", limit=10)
    hit_ids = {nid for nid, _ in hits}

    assert "poison-foreign-model" not in hit_ids
    assert len(hit_ids) == 2  # only the two same-model notes
    es.close()


def test_search_skips_mislabelled_wrong_dim_row(vault, indexer, config, monkeypatch):
    """Belt-and-suspenders: a row whose ``model`` matches the configured
    model but whose stored vector is the wrong dimensionality is skipped
    rather than silently zip-truncated."""
    es = _embed_two_notes(vault, indexer, config, monkeypatch)
    # Same model label as the configured provider, but a 3-d vector.
    _insert_row(es, "poison-bad-dim", [1.0, 2.0, 3.0], "text-embedding-3-small")

    hits = es.search("some query", limit=10)
    hit_ids = {nid for nid, _ in hits}

    assert "poison-bad-dim" not in hit_ids
    assert len(hit_ids) == 2
    es.close()


def test_clear_empties_cache(vault, indexer, config, monkeypatch):
    """``clear()`` (backing ``--embed --reset``) drops every row and
    returns the count removed."""
    es = _embed_two_notes(vault, indexer, config, monkeypatch)
    removed = es.clear()
    assert removed == 2
    remaining = es.db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert remaining == 0
    es.close()
