"""Slice 3 — C19b canonical-note PageRank.

Pure-Python power iteration over the per-concept-induced subgraph.
Stored in ``graph_ranks`` table, surfaced via
``weave_concepts(action='canonical_for', concept=X)``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager
from thinkweave.synthesis.centrality import (
    canonical_for,
    compute_all_concept_pageranks,
    compute_concept_pagerank,
    store_concept_pagerank,
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(vault_root=tmp_path / "vault")


@pytest.fixture
def vault(cfg: Config) -> VaultManager:
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    return vm


@pytest.fixture
def indexer(cfg: Config):
    idx = Indexer(config=cfg)
    yield idx
    idx.close()


def _rebuild(indexer: Indexer) -> None:
    indexer.rebuild(full=True)


class TestComputeConceptPagerank:
    def test_empty_subgraph_returns_empty(
        self, vault: VaultManager, indexer: Indexer
    ):
        _rebuild(indexer)
        assert compute_concept_pagerank(indexer.db, "nonexistent") == {}

    def test_single_note_subgraph_returns_empty(
        self, vault: VaultManager, indexer: Indexer
    ):
        vault.create_note(
            NoteType.NOTE, "A", body="x", project="p",
            extra_frontmatter={"concepts": ["alpha"]},
        )
        _rebuild(indexer)
        # Only 1 note has "alpha" — subgraph below the 2-note floor.
        assert compute_concept_pagerank(indexer.db, "alpha") == {}

    def test_hub_outranks_satellite(
        self, vault: VaultManager, indexer: Indexer
    ):
        """A hub note linked to many others outranks its satellites.
        Use a star-shaped subgraph: H at center, 4 satellites all
        sharing ``shared-concept`` with H but not with each other.
        H's degree (4) > each satellite's degree (1), so H wins."""
        # H has many concepts so it links to each satellite
        vault.create_note(
            NoteType.NOTE, "H", body="hub", project="p",
            extra_frontmatter={
                "concepts": [
                    "shared-concept", "extra-1", "extra-2",
                    "extra-3", "extra-4",
                ],
            },
        )
        # Each satellite shares shared-concept + ONE unique extra with H
        vault.create_note(
            NoteType.NOTE, "S1", body="s1", project="p",
            extra_frontmatter={"concepts": ["shared-concept", "extra-1"]},
        )
        vault.create_note(
            NoteType.NOTE, "S2", body="s2", project="p",
            extra_frontmatter={"concepts": ["shared-concept", "extra-2"]},
        )
        vault.create_note(
            NoteType.NOTE, "S3", body="s3", project="p",
            extra_frontmatter={"concepts": ["shared-concept", "extra-3"]},
        )
        vault.create_note(
            NoteType.NOTE, "S4", body="s4", project="p",
            extra_frontmatter={"concepts": ["shared-concept", "extra-4"]},
        )
        _rebuild(indexer)

        scores = compute_concept_pagerank(indexer.db, "shared-concept")
        # Find H's id by title via the indexer.
        h_id = indexer.db.execute(
            "SELECT id FROM notes WHERE title = 'H'"
        ).fetchone()["id"]
        # H should have the highest score in the subgraph.
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        assert sorted_scores[0][0] == h_id
        # PageRank should be normalized roughly to sum=1 (within tol).
        assert abs(sum(scores.values()) - 1.0) < 0.05

    def test_max_nodes_skips_broad_concept(
        self, vault: VaultManager, indexer: Indexer
    ):
        for i in range(8):
            vault.create_note(
                NoteType.NOTE, f"N{i}", body=str(i), project="p",
                extra_frontmatter={"concepts": ["everywhere"]},
            )
        _rebuild(indexer)
        # max_nodes=5 should skip the 8-note subgraph entirely.
        scores = compute_concept_pagerank(
            indexer.db, "everywhere", max_nodes=5
        )
        assert scores == {}


class TestStorageAndQuery:
    def test_store_and_retrieve(
        self, vault: VaultManager, indexer: Indexer
    ):
        # Create two notes so they appear in the index for the canonical_for
        # JOIN.
        vault.create_note(
            NoteType.NOTE, "A", body="x", project="p",
            extra_frontmatter={"concepts": ["alpha"]},
        )
        vault.create_note(
            NoteType.NOTE, "B", body="y", project="p",
            extra_frontmatter={"concepts": ["alpha"]},
        )
        _rebuild(indexer)
        # Direct mock-y store of synthetic scores.
        a_id = indexer.db.execute(
            "SELECT id FROM notes WHERE title = 'A'"
        ).fetchone()["id"]
        b_id = indexer.db.execute(
            "SELECT id FROM notes WHERE title = 'B'"
        ).fetchone()["id"]
        store_concept_pagerank(
            indexer.db, "alpha", {a_id: 0.7, b_id: 0.3}
        )

        rows = canonical_for(indexer.db, "alpha", limit=10)
        assert len(rows) == 2
        assert rows[0]["id"] == a_id
        assert rows[0]["score"] == 0.7
        assert rows[1]["id"] == b_id

    def test_canonical_for_unknown_concept_empty(
        self, vault: VaultManager, indexer: Indexer
    ):
        _rebuild(indexer)
        assert canonical_for(indexer.db, "nothing") == []

    def test_store_replaces_prior_scores(
        self, vault: VaultManager, indexer: Indexer
    ):
        vault.create_note(
            NoteType.NOTE, "A", body="x", project="p",
            extra_frontmatter={"concepts": ["alpha"]},
        )
        _rebuild(indexer)
        a_id = indexer.db.execute(
            "SELECT id FROM notes WHERE title = 'A'"
        ).fetchone()["id"]
        store_concept_pagerank(indexer.db, "alpha", {a_id: 0.1})
        store_concept_pagerank(indexer.db, "alpha", {a_id: 0.5})
        rows = canonical_for(indexer.db, "alpha")
        assert len(rows) == 1
        assert rows[0]["score"] == 0.5


class TestComputeAll:
    def test_walks_all_concepts(
        self, vault: VaultManager, indexer: Indexer
    ):
        # Two valid subgraphs: alpha (3 notes) and beta (2 notes).
        # singleton (1 note) skipped by min_notes=2.
        for i in range(3):
            vault.create_note(
                NoteType.NOTE, f"A{i}", body="x", project="p",
                extra_frontmatter={"concepts": ["alpha"]},
            )
        for i in range(2):
            vault.create_note(
                NoteType.NOTE, f"B{i}", body="y", project="p",
                extra_frontmatter={"concepts": ["beta"]},
            )
        vault.create_note(
            NoteType.NOTE, "SOLO", body="z", project="p",
            extra_frontmatter={"concepts": ["singleton"]},
        )
        _rebuild(indexer)
        out = compute_all_concept_pageranks(indexer.db)
        # alpha and beta both have ≥2 notes and edges between them.
        assert "alpha" in out
        assert "beta" in out
        assert "singleton" not in out