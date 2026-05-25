"""Tests for the new retrieval filter parity (Workstream F2).

Covers:
- `mem_search`/`Search.search` with `concepts=[…]` filter (text + concept).
- `since` / `until` ISO date filters on search, get_context, search_by_concept.
- `note_type` / `project` projection filters on get_related (graph).
- Empty-query list mode under various filter combinations.
- Themes show up in retrieval primitives that accept `type` filters.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.retrieval.search import Search
from personal_mem.synthesis.theme_hub import build_theme_frontmatter, render_theme_body_skeleton
from personal_mem.core.vault import VaultManager


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
    s = Search(config=config)
    yield s
    s.close()


# ---------------------------------------------------------------------------
# `concepts` filter on mem_search
# ---------------------------------------------------------------------------


class TestSearchConceptsFilter:
    def test_text_query_with_concept_filter(
        self, vault: VaultManager, indexer: Indexer, search: Search
    ):
        # Two notes both contain the word "graph" but only one has the
        # concept `recursive-cte`.
        vault.create_note(
            note_type=NoteType.NOTE,
            title="Recursive graph traversal",
            body="Graph walks via recursive CTE.",
            extra_frontmatter={"concepts": ["recursive-cte", "graph"]},
        )
        vault.create_note(
            note_type=NoteType.NOTE,
            title="Plain graph note",
            body="Graph stuff.",
            extra_frontmatter={"concepts": ["graph"]},
        )
        indexer.rebuild()

        # Without concept filter — both match.
        all_results = search.search("graph")
        ids = {r.id for r in all_results}
        assert len(ids) == 2

        # With concept filter — only one.
        cte_results = search.search("graph", concepts=["recursive-cte"])
        cte_ids = {r.id for r in cte_results}
        assert len(cte_ids) == 1


# ---------------------------------------------------------------------------
# Date-window filters
# ---------------------------------------------------------------------------


class TestDateWindowFilters:
    def test_since_filter_excludes_older(
        self, vault: VaultManager, indexer: Indexer, search: Search
    ):
        # Create with explicit dates via extra_frontmatter.
        vault.create_note(
            note_type=NoteType.NOTE,
            title="Old",
            extra_frontmatter={"date": "2024-01-01", "concepts": ["x"]},
        )
        vault.create_note(
            note_type=NoteType.NOTE,
            title="New",
            extra_frontmatter={"date": "2026-04-01", "concepts": ["x"]},
        )
        indexer.rebuild()

        results = search.search("", since="2025-01-01")
        titles = {r.title for r in results}
        assert "New" in titles
        assert "Old" not in titles

    def test_until_filter_excludes_newer(
        self, vault: VaultManager, indexer: Indexer, search: Search
    ):
        vault.create_note(
            note_type=NoteType.NOTE,
            title="Old",
            extra_frontmatter={"date": "2024-01-01", "concepts": ["x"]},
        )
        vault.create_note(
            note_type=NoteType.NOTE,
            title="New",
            extra_frontmatter={"date": "2026-04-01", "concepts": ["x"]},
        )
        indexer.rebuild()

        results = search.search("", until="2025-01-01")
        titles = {r.title for r in results}
        assert "Old" in titles
        assert "New" not in titles

    def test_concept_search_date_window(
        self, vault: VaultManager, indexer: Indexer, search: Search
    ):
        vault.create_note(
            note_type=NoteType.NOTE,
            title="Old python",
            extra_frontmatter={"date": "2024-01-01", "concepts": ["python"]},
        )
        vault.create_note(
            note_type=NoteType.NOTE,
            title="New python",
            extra_frontmatter={"date": "2026-04-01", "concepts": ["python"]},
        )
        indexer.rebuild()

        windowed = search.search_by_concept(
            "python", since="2025-01-01"
        )
        titles = {r.title for r in windowed}
        assert "New python" in titles
        assert "Old python" not in titles

    def test_get_context_date_window(
        self, vault: VaultManager, indexer: Indexer, search: Search
    ):
        # Recency layer should respect since/until.
        vault.create_note(
            note_type=NoteType.NOTE,
            title="Old",
            extra_frontmatter={"date": "2024-01-01"},
        )
        vault.create_note(
            note_type=NoteType.NOTE,
            title="New",
            extra_frontmatter={"date": "2026-04-01"},
        )
        indexer.rebuild()

        ctx = search.get_context(since="2025-01-01")
        titles = {r.title for r in ctx}
        assert "New" in titles
        assert "Old" not in titles


# ---------------------------------------------------------------------------
# Graph projection filters
# ---------------------------------------------------------------------------


class TestGraphFilters:
    def test_note_type_filters_returned_nodes(
        self, vault: VaultManager, indexer: Indexer, search: Search
    ):
        # Build a small graph: note A -> source B, note A -> decision C.
        a = vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            extra_frontmatter={"id": "n-aaaa1111"},
        )
        b = vault.create_note(
            note_type=NoteType.SOURCE,
            title="B-source",
            extra_frontmatter={
                "id": "src-bbbb2222",
                "source_type": "article",
                "url": "",
                "cites": ["n-aaaa1111"],
            },
        )
        c = vault.create_note(
            note_type=NoteType.DECISION,
            title="C-decision",
            extra_frontmatter={
                "id": "dec-cccc3333",
                "derived_from": ["n-aaaa1111"],
            },
        )
        indexer.rebuild()

        # Filter to source nodes only — should not include the decision.
        nodes = search.get_related("n-aaaa1111", depth=1, note_type="source")
        ids = {n.id for n in nodes}
        assert "src-bbbb2222" in ids
        assert "dec-cccc3333" not in ids


# ---------------------------------------------------------------------------
# Empty-query list mode under filter combinations
# ---------------------------------------------------------------------------


class TestEmptyQueryListMode:
    def test_empty_query_returns_recent_first(
        self, vault: VaultManager, indexer: Indexer, search: Search
    ):
        vault.create_note(
            note_type=NoteType.NOTE,
            title="Old",
            extra_frontmatter={"date": "2024-01-01"},
        )
        vault.create_note(
            note_type=NoteType.NOTE,
            title="New",
            extra_frontmatter={"date": "2026-04-01"},
        )
        indexer.rebuild()

        results = search.search("", limit=10)
        # Most recent first.
        assert results[0].title == "New"

    def test_empty_query_with_project_filter(
        self, vault: VaultManager, indexer: Indexer, search: Search
    ):
        vault.create_note(note_type=NoteType.NOTE, title="A", project="x")
        vault.create_note(note_type=NoteType.NOTE, title="B", project="y")
        indexer.rebuild()

        results = search.search("", project="x")
        titles = {r.title for r in results}
        assert "A" in titles
        assert "B" not in titles


# ---------------------------------------------------------------------------
# Themes in retrieval
# ---------------------------------------------------------------------------


class TestThemesInRetrieval:
    def test_search_filters_to_themes(
        self, vault: VaultManager, indexer: Indexer, search: Search
    ):
        vault.create_note(
            note_type=NoteType.NOTE,
            title="A regular note",
        )
        vault.create_note(
            note_type=NoteType.THEME,
            title="A theme",
            body=render_theme_body_skeleton("A theme"),
            extra_frontmatter=build_theme_frontmatter("A theme"),
        )
        indexer.rebuild()

        results = search.search("", note_type="theme")
        titles = {r.title for r in results}
        assert "A theme" in titles
        assert "A regular note" not in titles

    def test_project_snapshot_includes_active_themes(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        from personal_mem.retrieval.context import build_project_context

        vault.create_note(
            note_type=NoteType.THEME,
            title="Active theme",
            body=render_theme_body_skeleton("Active theme"),
            extra_frontmatter=build_theme_frontmatter(
                "Active theme", project="trade_ideas"
            ),
        )
        vault.create_note(
            note_type=NoteType.THEME,
            title="Dormant theme",
            extra_frontmatter=build_theme_frontmatter(
                "Dormant theme", status="dormant"
            ),
        )
        indexer.rebuild()

        payload = build_project_context(config, project="trade_ideas")
        assert "## Active Themes" in payload
        assert "Active theme" in payload
        assert "Dormant theme" not in payload  # status filter
