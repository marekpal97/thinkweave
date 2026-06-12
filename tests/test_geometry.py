"""Tests for synthesis/geometry.py — the drift-v2 embedding substrate.

Centroids are computed from a hand-seeded ``.mem/embeddings.db`` (packed
float vectors, same writer as ``EmbeddingSearch``), so no API calls and
fully deterministic cosines.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.embeddings import EMBEDDINGS_SCHEMA, _pack_embedding
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager, parse_frontmatter
from personal_mem.synthesis import geometry


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


def _index(config: Config) -> None:
    idx = Indexer(config=config)
    idx.rebuild(full=True)
    idx.close()


def _seed_embeddings(config: Config, vectors: dict[str, list[float]]) -> None:
    config.mem_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(config.embeddings_db))
    db.executescript(EMBEDDINGS_SCHEMA)
    for note_id, vec in vectors.items():
        db.execute(
            "INSERT OR REPLACE INTO embeddings "
            "(note_id, content_hash, embedding, model, created_at) "
            "VALUES (?, 'h', ?, 'test', '2026-01-01')",
            (note_id, _pack_embedding(vec)),
        )
    db.commit()
    db.close()


def _note_with_concept(vm: VaultManager, concept: str, title: str) -> str:
    path = vm.create_note(
        NoteType.NOTE,
        title,
        body="body",
        project="t",
        extra_frontmatter={"concepts": [concept]},
    )
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    return fm["id"]


class TestCentroids:
    def test_centroid_is_unit_norm_mean(self, config, vault):
        ids = [_note_with_concept(vault, "alpha", f"a{i}") for i in range(3)]
        _index(config)
        _seed_embeddings(
            config,
            {ids[0]: [1.0, 0.0], ids[1]: [1.0, 0.0], ids[2]: [0.0, 1.0]},
        )
        cents = geometry.concept_centroids(config, embed_fallback=False)
        assert "alpha" in cents
        vec = cents["alpha"]
        # mean = (2/3, 1/3) → normalized
        assert vec[0] > vec[1] > 0
        assert abs(sum(x * x for x in vec) - 1.0) < 1e-6

    def test_sparse_concept_absent_without_fallback(self, config, vault):
        nid = _note_with_concept(vault, "rare", "only one")
        _index(config)
        _seed_embeddings(config, {nid: [1.0, 0.0]})
        cents = geometry.concept_centroids(config, embed_fallback=False)
        assert "rare" not in cents

    def test_no_embeddings_db_degrades_to_empty(self, config, vault):
        _note_with_concept(vault, "alpha", "a")
        _index(config)
        assert geometry.concept_centroids(config, embed_fallback=False) == {}


class TestThemeVectors:
    def test_theme_vector_lookup(self, config, vault):
        theme = vault.create_note(
            NoteType.THEME,
            "arc one",
            body="## Essence\n\nx\n\n## Catalyst log\n",
            extra_frontmatter={"status": "active"},
        )
        fm, _ = parse_frontmatter(theme.read_text(encoding="utf-8"))
        tid = fm["id"]
        _index(config)
        _seed_embeddings(config, {tid: [3.0, 4.0]})
        vecs = geometry.theme_vectors(config)
        assert tid in vecs
        assert abs(vecs[tid][0] - 0.6) < 1e-6  # 3/5 — normalized


class TestCosinePairs:
    def test_threshold_and_order(self):
        vectors = {
            "a": [1.0, 0.0],
            "b": [1.0, 0.0],
            "c": [0.9486832980505138, 0.31622776601683794],  # cos(a,c)≈0.95
            "d": [0.0, 1.0],
        }
        pairs = geometry.cosine_pairs(vectors, threshold=0.9)
        keys = [(a, b) for a, b, _ in pairs]
        assert ("a", "b") in keys and ("a", "c") in keys
        assert all(cos >= 0.9 for _, _, cos in pairs)
        # sorted descending
        cosines = [cos for _, _, cos in pairs]
        assert cosines == sorted(cosines, reverse=True)
        assert not any("d" in k for k in keys)


class TestEvidence:
    def test_evidence_packet_fields(self, config, vault, monkeypatch):
        for i in range(2):
            _note_with_concept(vault, "alpha", f"alpha note {i}")
        _note_with_concept(vault, "beta", "beta note")
        _index(config)
        # Fake ontology: alpha and beta share a domain.
        monkeypatch.setattr(
            "personal_mem.synthesis.concepts.load_ontology",
            lambda path=None: {"dom-x": ["alpha", "beta"], "dom-y": ["beta"]},
        )
        packets = geometry.build_concept_evidence(
            config, [("alpha", "beta", 0.91, "cosine 0.91")]
        )
        p = packets[0]
        assert p["same_domain"] is True
        assert p["note_counts"]["alpha"] == 2
        assert p["note_counts"]["beta"] == 1
        assert p["cooccurrence"] == 0
        assert len(p["sample_titles"]["alpha"]) == 2
        assert p["cosine"] == 0.91


class TestJudgedPairs:
    def _write_maintenance(self, config: Config, verdicts: dict) -> None:
        from personal_mem.operations.dream import append_maintenance_log

        append_maintenance_log(
            config, {"ts": "2026-06-11", "cycle_id": "c1", "verdicts": verdicts}
        )

    def test_reads_all_three_verdict_kinds(self, config, vault):
        self._write_maintenance(
            config,
            {
                "merges": [{"from": "embedings", "to": "embeddings"}],
                "theme_merges": [{"from_id": "thm-a", "to_id": "thm-b"}],
                "distinct_pairs": [
                    {"kind": "concept", "pair": ["derivative", "derivatives"]}
                ],
            },
        )
        judged = geometry.judged_pairs(config)
        assert geometry.pair_key("concept", "embeddings", "embedings") in judged
        assert geometry.pair_key("theme", "thm-b", "thm-a") in judged
        assert (
            geometry.pair_key("concept", "derivatives", "derivative") in judged
        )

    def test_corrupt_and_legacy_lines_skipped(self, config, vault):
        from personal_mem.operations.dream import maintenance_log_path

        path = maintenance_log_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "not json\n"
            + json.dumps({"cycle_id": "old", "summary": {"merges": 2}})
            + "\n",
            encoding="utf-8",
        )
        assert geometry.judged_pairs(config) == set()

    def test_missing_log_empty(self, config, vault):
        assert geometry.judged_pairs(config) == set()

    def test_pair_key_order_insensitive(self):
        assert geometry.pair_key("concept", "A", "b") == geometry.pair_key(
            "concept", "b", "a"
        )
        assert geometry.pair_key("concept", "a", "b") != geometry.pair_key(
            "theme", "a", "b"
        )
