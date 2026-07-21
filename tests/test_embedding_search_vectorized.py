"""Tests for the vectorized (numpy) fast path in ``EmbeddingSearch.search``.

The stdlib loop (``cosine_similarity`` + Python ``for``) is the fallback used
when numpy isn't importable; the vectorized path must return identical
rankings/scores within float tolerance, honour the same model/dim guard, and
score zero-norm vectors as 0.0 rather than raising.

The embedding API client is monkeypatched so no real calls fire.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.embeddings import EmbeddingSearch, _unpack_embedding, cosine_similarity
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager

np = pytest.importorskip("numpy")


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
    """Deterministic 4-d vector per text (matches the default configured
    provider's dimensionality expectations for a config-less test vault)."""
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


def _seeded_search(vault, indexer, config, monkeypatch) -> EmbeddingSearch:
    """Embed 2 real notes, then hand-insert a handful of synthetic vectors
    with known geometry so we can assert exact rankings/scores."""
    for i in range(2):
        vault.create_note(note_type=NoteType.NOTE, title=f"N{i}", body=f"b{i}")
    indexer.rebuild()
    monkeypatch.setattr(
        EmbeddingSearch, "_call_api", lambda self, texts: _fake_embed(texts)
    )
    es = EmbeddingSearch(config=config)
    es.compute_all()
    return es


def test_vectorized_matches_stdlib_ranking_and_scores(vault, indexer, config, monkeypatch):
    """The numpy fast path must reproduce the stdlib loop's ranking and
    scores (within float tolerance) over a small synthetic DB."""
    es = _seeded_search(vault, indexer, config, monkeypatch)
    model = es._provider().model
    _insert_row(es, "synthetic-a", [1.0, 2.0, 3.0, 4.0], model)
    _insert_row(es, "synthetic-b", [4.0, 3.0, 2.0, 1.0], model)
    _insert_row(es, "synthetic-c", [-1.0, -2.0, -3.0, -4.0], model)

    query_emb = es._call_api(["some query"])[0]

    # Vectorized path (numpy importable in this environment).
    vectorized_hits = es.search("some query", limit=10)

    # Stdlib path: recompute directly via cosine_similarity over the same
    # rows, mirroring the fallback loop's exact logic.
    stdlib_scores = []
    for row in es.db.execute("SELECT note_id, embedding FROM embeddings WHERE model = ?", (model,)):
        cached = _unpack_embedding(row["embedding"])
        if len(cached) != len(query_emb):
            continue
        stdlib_scores.append((row["note_id"], cosine_similarity(query_emb, cached)))
    stdlib_scores.sort(key=lambda x: x[1], reverse=True)

    assert [nid for nid, _ in vectorized_hits] == [nid for nid, _ in stdlib_scores]
    for (nid_v, score_v), (nid_s, score_s) in zip(vectorized_hits, stdlib_scores):
        assert nid_v == nid_s
        assert score_v == pytest.approx(score_s, abs=1e-6)
    es.close()


def test_vectorized_skips_mismatched_dim_rows(vault, indexer, config, monkeypatch):
    """A row whose blob length doesn't match the query's dimensionality is
    skipped in the vectorized path, same as the stdlib fallback."""
    es = _seeded_search(vault, indexer, config, monkeypatch)
    model = es._provider().model
    _insert_row(es, "wrong-dim", [1.0, 2.0, 3.0], model)  # 3-d, query is 4-d

    hits = es.search("some query", limit=10)
    hit_ids = {nid for nid, _ in hits}

    assert "wrong-dim" not in hit_ids
    assert len(hit_ids) == 2  # only the two originally-embedded notes
    es.close()


def test_vectorized_zero_norm_scores_zero(vault, indexer, config, monkeypatch):
    """A cached zero vector must score 0.0 rather than raising (division by
    a zero norm)."""
    es = _seeded_search(vault, indexer, config, monkeypatch)
    model = es._provider().model
    _insert_row(es, "zero-vec", [0.0, 0.0, 0.0, 0.0], model)

    hits = dict(es.search("some query", limit=10))

    assert "zero-vec" in hits
    assert hits["zero-vec"] == 0.0
    es.close()
