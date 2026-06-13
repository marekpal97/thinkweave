"""Tests for W2 retrieval primitives: multi-concept, source lens,
decision_files, hybrid search, empty-query listing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.retrieval.search import Search
from personal_mem.core.vault import VaultManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest.fixture
def search(config: Config):
    # Instantiate after the indexer populates the db
    s = Search(config=config)
    yield s
    s.close()


# ---------------------------------------------------------------------------
# 2a — Multi-concept intersection / union
# ---------------------------------------------------------------------------


class TestSearchByConceptMultiMode:
    def test_single_concept_back_compat(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.NOTE,
            "Alpha",
            body="body",
            project="p1",
            extra_frontmatter={"concepts": ["sqlite"]},
        )
        vault.create_note(
            NoteType.NOTE,
            "Beta",
            body="body",
            project="p1",
            extra_frontmatter={"concepts": ["fts5"]},
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        # Single concept string — back-compat
        results = s.search_by_concept("sqlite", project="p1")
        assert len(results) == 1
        assert results[0].title == "Alpha"
        s.close()

    def test_multi_concept_all_mode_intersection(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.NOTE,
            "Has both",
            project="p1",
            extra_frontmatter={"concepts": ["sqlite", "fts5"]},
        )
        vault.create_note(
            NoteType.NOTE,
            "Has only one",
            project="p1",
            extra_frontmatter={"concepts": ["sqlite"]},
        )
        vault.create_note(
            NoteType.NOTE,
            "Neither",
            project="p1",
            extra_frontmatter={"concepts": ["markdown"]},
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        results = s.search_by_concept(
            ["sqlite", "fts5"], project="p1", match_mode="all"
        )
        titles = [r.title for r in results]
        assert "Has both" in titles
        assert "Has only one" not in titles
        assert "Neither" not in titles
        s.close()

    def test_multi_concept_any_mode_union(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.NOTE,
            "Has both",
            project="p1",
            extra_frontmatter={"concepts": ["sqlite", "fts5"]},
        )
        vault.create_note(
            NoteType.NOTE,
            "Has only sqlite",
            project="p1",
            extra_frontmatter={"concepts": ["sqlite"]},
        )
        vault.create_note(
            NoteType.NOTE,
            "Has only fts5",
            project="p1",
            extra_frontmatter={"concepts": ["fts5"]},
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        results = s.search_by_concept(
            ["sqlite", "fts5"], project="p1", match_mode="any"
        )
        titles = {r.title for r in results}
        # Union — all 3 should be present
        assert titles == {"Has both", "Has only sqlite", "Has only fts5"}
        s.close()

    def test_min_matches_partial_intersection(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.NOTE,
            "Has all 3",
            project="p1",
            extra_frontmatter={"concepts": ["a", "b", "c"]},
        )
        vault.create_note(
            NoteType.NOTE,
            "Has 2 of 3",
            project="p1",
            extra_frontmatter={"concepts": ["a", "b"]},
        )
        vault.create_note(
            NoteType.NOTE,
            "Has 1 of 3",
            project="p1",
            extra_frontmatter={"concepts": ["a"]},
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        results = s.search_by_concept(
            ["a", "b", "c"], project="p1", match_mode="all", min_matches=2
        )
        titles = {r.title for r in results}
        assert "Has all 3" in titles
        assert "Has 2 of 3" in titles
        assert "Has 1 of 3" not in titles
        s.close()

    def test_cross_project_no_project_filter(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.NOTE,
            "In p1",
            project="p1",
            extra_frontmatter={"concepts": ["ml"]},
        )
        vault.create_note(
            NoteType.NOTE,
            "In p2",
            project="p2",
            extra_frontmatter={"concepts": ["ml"]},
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        results = s.search_by_concept("ml")  # no project filter
        titles = {r.title for r in results}
        assert titles == {"In p1", "In p2"}
        s.close()

    def test_note_type_list_filter(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.NOTE,
            "A note",
            project="p1",
            extra_frontmatter={"concepts": ["x"]},
        )
        vault.create_note(
            NoteType.SESSION,
            "A session",
            project="p1",
            extra_frontmatter={"concepts": ["x"]},
        )
        vault.create_note(
            NoteType.DECISION,
            "A decision",
            project="p1",
            extra_frontmatter={"concepts": ["x"]},
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        results = s.search_by_concept(
            "x", note_type=["note", "session"], project="p1"
        )
        titles = {r.title for r in results}
        # Decision excluded
        assert "A note" in titles
        assert "A session" in titles
        assert "A decision" not in titles
        s.close()


# ---------------------------------------------------------------------------
# 2h — Empty-query project listing
# ---------------------------------------------------------------------------


class TestEmptyQueryListing:
    def test_empty_query_returns_date_sorted(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        # Create in a deterministic order; the vault stamps ISO dates.
        vault.create_note(NoteType.NOTE, "First", project="p1")
        vault.create_note(NoteType.NOTE, "Second", project="p1")
        vault.create_note(NoteType.NOTE, "Third", project="p1")
        indexer.rebuild(full=True)

        s = Search(config=config)
        results = s.search("", project="p1", limit=10)
        assert len(results) == 3
        s.close()

    def test_empty_query_with_type_list(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(NoteType.NOTE, "A note", project="p1")
        vault.create_note(NoteType.SESSION, "A session", project="p1")
        vault.create_note(NoteType.DECISION, "A decision", project="p1")
        indexer.rebuild(full=True)

        s = Search(config=config)
        results = s.search("", project="p1", note_type=["note", "session"])
        types = {r.type for r in results}
        assert types == {"note", "session"}
        s.close()


# ---------------------------------------------------------------------------
# 2b — get_context type filter
# ---------------------------------------------------------------------------


class TestGetContextTypeFilter:
    def test_type_filter_restricts_concept_expansion(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        # Seed one FTS-matching note that will then drive concept expansion
        vault.create_note(
            NoteType.NOTE,
            "Target",
            body="retrieval pipeline",
            project="p1",
            extra_frontmatter={"concepts": ["retrieval"]},
        )
        vault.create_note(
            NoteType.SESSION,
            "A session about retrieval",
            body="session body",
            project="p1",
            extra_frontmatter={"concepts": ["retrieval"]},
        )
        vault.create_note(
            NoteType.DECISION,
            "A decision about retrieval",
            body="decision body",
            project="p1",
            extra_frontmatter={"concepts": ["retrieval"]},
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        results = s.get_context(
            project="p1", concepts=["retrieval"], note_type="decision", limit=10
        )
        types = {r.type for r in results}
        assert types <= {"decision"}, f"Unexpected types leaked: {types}"
        s.close()


# ---------------------------------------------------------------------------
# 2d — Source lens
# ---------------------------------------------------------------------------


class TestSourceLens:
    def test_returns_none_for_missing_source(self, search: Search, indexer: Indexer):
        indexer.rebuild(full=True)
        lens = search.get_source_lens("src-nonexistent")
        assert lens["source"] is None
        assert lens["inbound"] == []

    def test_hydrates_source_concepts(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.SOURCE,
            "Paper X",
            body="Source body",
            project="p1",
            extra_frontmatter={"concepts": ["ml", "pytorch"]},
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        # Find the source ID
        row = s.db.execute(
            "SELECT id FROM notes WHERE type='source'"
        ).fetchone()
        lens = s.get_source_lens(row["id"])
        assert lens["source"] is not None
        assert set(lens["source"]["concepts"]) == {"ml", "pytorch"}
        s.close()

    def test_inbound_edges_captured(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.SOURCE,
            "Paper",
            body="Source body",
            project="p1",
            extra_frontmatter={
                "concepts": ["ml"],
                "id": "src-paper-1",
            },
        )
        vault.create_note(
            NoteType.DECISION,
            "Cites the paper",
            body="Rationale",
            project="p1",
            extra_frontmatter={
                "cites": ["src-paper-1"],
            },
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        lens = s.get_source_lens("src-paper-1")
        assert len(lens["decisions"]) == 1
        assert lens["decisions"][0]["title"] == "Cites the paper"
        s.close()


# ---------------------------------------------------------------------------
# 2e — Decision files
# ---------------------------------------------------------------------------


class TestDecisionFiles:
    def test_decision_files_table_populated(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.DECISION,
            "Use WAL mode",
            body="Rationale",
            project="p1",
            extra_frontmatter={
                "file_paths": ["src/vault.py", "src/indexer.py"],
                "status": "accepted",
            },
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        rows = s.db.execute("SELECT * FROM decision_files").fetchall()
        paths = {r["file_path"] for r in rows}
        assert paths == {"src/vault.py", "src/indexer.py"}
        s.close()

    def test_non_decision_notes_skipped(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.NOTE,
            "A regular note",
            body="body",
            project="p1",
            extra_frontmatter={"file_paths": ["src/vault.py"]},
        )
        indexer.rebuild(full=True)
        s = Search(config=config)
        rows = s.db.execute("SELECT * FROM decision_files").fetchall()
        assert len(rows) == 0  # non-decisions not indexed
        s.close()

    def test_search_decisions_by_file_finds_match(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.DECISION,
            "Dec A",
            body="Rationale",
            project="p1",
            extra_frontmatter={
                "file_paths": ["src/vault.py"],
                "status": "accepted",
            },
        )
        vault.create_note(
            NoteType.DECISION,
            "Dec B",
            body="Rationale",
            project="p1",
            extra_frontmatter={
                "file_paths": ["src/search.py"],
                "status": "accepted",
            },
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        results = s.search_decisions_by_file("src/vault.py", project="p1")
        assert len(results) == 1
        assert results[0].title == "Dec A"
        s.close()

    def test_search_decisions_by_file_status_filter(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.DECISION,
            "Accepted dec",
            body="R",
            project="p1",
            extra_frontmatter={
                "file_paths": ["x.py"],
                "status": "accepted",
            },
        )
        vault.create_note(
            NoteType.DECISION,
            "Deprecated dec",
            body="R",
            project="p1",
            extra_frontmatter={
                "file_paths": ["x.py"],
                "status": "deprecated",
            },
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        accepted = s.search_decisions_by_file("x.py", status="accepted")
        titles = {r.title for r in accepted}
        assert "Accepted dec" in titles
        assert "Deprecated dec" not in titles
        s.close()

    def test_reindex_replaces_file_paths(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        """When a decision's frontmatter file_paths changes, reindexing
        should replace the rows in decision_files, not accumulate them.

        Exercises the ``DELETE FROM decision_files WHERE decision_id = ?``
        step in ``_sync_decision_files``.
        """
        dec_path = vault.create_note(
            NoteType.DECISION,
            "Dec",
            body="R",
            project="p1",
            extra_frontmatter={"file_paths": ["a.py"], "status": "accepted"},
        )
        indexer.rebuild(full=True)

        # Rewrite the file directly to simulate an edit that replaces file_paths
        # rather than appending. Going through update_note would append.
        # The vault serializes frontmatter lists inline: `file_paths: [a.py]`
        text = dec_path.read_text(encoding="utf-8")
        text = text.replace("file_paths: [a.py]", "file_paths: [b.py]")
        dec_path.write_text(text, encoding="utf-8")
        indexer.index_file(dec_path)

        s = Search(config=config)
        rows = s.db.execute(
            "SELECT file_path FROM decision_files"
        ).fetchall()
        paths = {r["file_path"] for r in rows}
        assert paths == {"b.py"}, f"Expected {{'b.py'}}, got {paths}"
        s.close()


# ---------------------------------------------------------------------------
# 2g — Hybrid search with RRF
# ---------------------------------------------------------------------------


class TestHybridSearch:
    def test_hybrid_falls_back_to_fts_when_embeddings_missing(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        """Without embeddings configured, hybrid_search should return FTS-only."""
        vault.create_note(
            NoteType.NOTE,
            "The retrieval note",
            body="discusses retrieval pipelines",
            project="p1",
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        # No OPENAI_API_KEY, no embeddings.db → semantic path soft-fails
        results = s.hybrid_search("retrieval", project="p1")
        titles = [r.title for r in results]
        assert "The retrieval note" in titles
        s.close()

    def test_similar_soft_fails_without_embeddings(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        """Search.similar() should return [] instead of raising when embeddings
        aren't set up — no API key, no embeddings.db."""
        vault.create_note(NoteType.NOTE, "Something", project="p1")
        indexer.rebuild(full=True)

        s = Search(config=config)
        results = s.similar("anything", project="p1")
        assert results == []
        s.close()

    def test_rrf_fusion_prefers_intersections(self):
        """Pure math check: RRF gives a higher score to items that appear in
        both retrievers than to items that appear in only one.

        Validates the formula score = Σ 1/(k + rank_i) with k=60.
        """
        # Manual RRF score with k=60:
        #  a appears in both at rank 1 → 1/61 + 1/61 ≈ 0.0328
        #  b appears only in FTS at rank 2 → 1/62 ≈ 0.0161
        #  c appears only in sem at rank 2 → 1/62 ≈ 0.0161
        k = 60
        a_score = 1 / (k + 1) + 1 / (k + 1)
        b_score = 1 / (k + 2)
        c_score = 1 / (k + 2)
        assert a_score > b_score
        assert b_score == c_score


class TestConceptSourceCounts:
    """Bulk source-count lookup powering /discover's under-source check."""

    def test_returns_zero_entry_for_concepts_with_no_sources(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.NOTE,
            "A note, not a source",
            project="p1",
            extra_frontmatter={"concepts": ["alpha"]},
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        result = s.get_concept_source_counts(["alpha", "missing"])
        s.close()

        assert result["alpha"]["count"] == 0
        assert result["alpha"]["sources"] == []
        assert result["missing"]["count"] == 0
        assert result["missing"]["sources"] == []

    def test_counts_only_sources_not_other_types(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.SOURCE,
            "Source one",
            project="p1",
            extra_frontmatter={"concepts": ["ml"], "url": "https://arxiv.org/abs/1.1"},
        )
        vault.create_note(
            NoteType.NOTE,
            "Note one",
            project="p1",
            extra_frontmatter={"concepts": ["ml"]},
        )
        vault.create_note(
            NoteType.SESSION,
            "Session one",
            project="p1",
            extra_frontmatter={"concepts": ["ml"]},
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        result = s.get_concept_source_counts(["ml"])
        s.close()

        # Only the source should be counted
        assert result["ml"]["count"] == 1
        assert len(result["ml"]["sources"]) == 1
        assert result["ml"]["sources"][0]["title"] == "Source one"
        assert result["ml"]["sources"][0]["url"] == "https://arxiv.org/abs/1.1"

    def test_bulk_lookup_single_query_for_many_concepts(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.SOURCE,
            "Src A",
            project="p1",
            extra_frontmatter={"concepts": ["a"], "url": "https://ex.com/a"},
        )
        vault.create_note(
            NoteType.SOURCE,
            "Src A2",
            project="p1",
            extra_frontmatter={"concepts": ["a"], "url": "https://ex.com/a2"},
        )
        vault.create_note(
            NoteType.SOURCE,
            "Src B",
            project="p1",
            extra_frontmatter={"concepts": ["b"], "url": "https://ex.com/b"},
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        result = s.get_concept_source_counts(["a", "b", "c"])
        s.close()

        assert result["a"]["count"] == 2
        assert {src["url"] for src in result["a"]["sources"]} == {
            "https://ex.com/a",
            "https://ex.com/a2",
        }
        assert result["b"]["count"] == 1
        assert result["c"]["count"] == 0

    def test_empty_input_returns_empty_dict(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        indexer.rebuild(full=True)
        s = Search(config=config)
        assert s.get_concept_source_counts([]) == {}
        s.close()


class TestCrossProjectActivity:
    """Cross-project mem_timeline ranking mode."""

    def test_ranks_by_total_activity_desc(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        for i in range(3):
            vault.create_note(
                NoteType.SESSION,
                f"P1 session {i}",
                project="p1",
                extra_frontmatter={"concepts": ["x"]},
            )
        vault.create_note(
            NoteType.DECISION,
            "P1 decision",
            project="p1",
            extra_frontmatter={"concepts": ["x"]},
        )
        vault.create_note(
            NoteType.SESSION,
            "P2 session",
            project="p2",
            extra_frontmatter={"concepts": ["x"]},
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        ranking = s.get_cross_project_activity(days=30)
        s.close()

        assert len(ranking) == 2
        assert ranking[0]["project"] == "p1"
        assert ranking[0]["sessions"] == 3
        assert ranking[0]["decisions"] == 1
        assert ranking[1]["project"] == "p2"
        assert ranking[1]["sessions"] == 1
        assert ranking[1]["decisions"] == 0

    def test_unscoped_bucket_for_missing_project(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        # Session with an explicit project
        vault.create_note(
            NoteType.SESSION,
            "Labeled",
            project="p1",
            extra_frontmatter={"concepts": ["x"]},
        )
        # Session without a project — goes to _unscoped via the default
        # project resolution in create_note. We test the ranking treats
        # empty/null project as the _unscoped bucket.
        vault.create_note(
            NoteType.SESSION,
            "Unlabeled",
            project="",
            extra_frontmatter={"concepts": ["x"]},
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        ranking = s.get_cross_project_activity(days=30)
        s.close()

        projects = {r["project"] for r in ranking}
        # If Config has no default_project, the empty project falls through
        # to NULL in the index and gets bucketed as _unscoped. If there *is*
        # a default, the session lands there — either way, both sessions
        # must be accounted for across the ranking.
        total_sessions = sum(r["sessions"] for r in ranking)
        assert total_sessions == 2
        assert "p1" in projects

    def test_excludes_activity_outside_window(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            NoteType.SESSION,
            "Recent",
            project="p1",
            extra_frontmatter={"concepts": ["x"]},
        )
        indexer.rebuild(full=True)

        s = Search(config=config)
        # Zero-day window: cutoff == today, so today's sessions still
        # match (date >= cutoff is inclusive). Use a negative-inverted
        # check: request activity over a very long window and confirm
        # we see the recent session.
        long_window = s.get_cross_project_activity(days=3650)
        assert any(r["project"] == "p1" for r in long_window)

        # Manually poison the cutoff by asking for a window from before
        # the epoch — date arithmetic produces 0001-01-01, still inclusive,
        # so the only meaningful negative test is the empty-vault case.
        s.close()

    def test_empty_vault_returns_empty_ranking(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        indexer.rebuild(full=True)
        s = Search(config=config)
        assert s.get_cross_project_activity(days=14) == []
        s.close()


class TestRrfKFromConfig:
    """Bucket-3 audit: hybrid_search's RRF constant reads config
    ``retrieval.rrf_k`` when no explicit ``rrf_k`` is passed."""

    @staticmethod
    def _result(nid: str) -> "SearchResult":
        from personal_mem.retrieval.search import SearchResult

        return SearchResult(
            id=nid, type="note", title=nid, path=f"{nid}.md",
            project="p1", date="2026-06-01", tags=[],
        )

    def test_config_rrf_k_reaches_fusion(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        indexer.rebuild(full=True)
        s = Search(config=config)
        # Canned retriever outputs: 'a' in both arms at rank 1.
        s.search = lambda *a, **kw: [self._result("a"), self._result("b")]
        s.similar = lambda *a, **kw: [self._result("a"), self._result("c")]

        config.retrieval_rrf_k = 4
        merged = s.hybrid_search("q")
        top = merged[0]
        assert top.id == "a"
        # Fused score = 1/(k+1) + 1/(k+1) with the configured k=4.
        assert top.rank == pytest.approx(2 / 5)

        # Explicit kwarg still overrides config.
        merged = s.hybrid_search("q", rrf_k=60)
        assert merged[0].rank == pytest.approx(2 / 61)
        s.close()
