"""Tests for ``synthesis/theme_candidates.py`` — source-coupled
theme-candidate floating, archival, and promotion."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager, parse_frontmatter
from personal_mem.synthesis.theme_candidates import (
    CANDIDATES_ARCHIVE_NAME,
    CANDIDATES_DIR_NAME,
    archive_stale_candidates,
    promote_candidate,
    scan_candidates,
)


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


def _make_substack_source(
    vault: VaultManager,
    title: str,
    *,
    concepts: list[str],
) -> Path:
    """Create a source note carrying source_type=substack and concepts."""
    return vault.create_note(
        note_type=NoteType.SOURCE,
        title=title,
        body=f"# {title}\n",
        extra_frontmatter={
            "source_type": "substack",
            "concepts": concepts,
        },
    )


class TestScanCandidatesEventGrain:
    """Substack is the canonical event-grain type. Three sources
    sharing two concepts trigger a candidate stub."""

    def test_cluster_of_three_creates_candidate(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        for i in range(3):
            _make_substack_source(
                vault, f"Source {i}", concepts=["ai-capex", "hyperscaler"]
            )
        indexer.rebuild()

        outcome = scan_candidates(config, source_type="substack")

        assert len(outcome.candidates_created) == 1
        path = outcome.candidates_created[0]
        assert path.exists()
        assert (config.vault_root / "themes" / CANDIDATES_DIR_NAME) in path.parents

        fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert fm["status"] == "candidate"
        assert fm["source_type"] == "substack"
        assert fm["candidacy"] == "inferred-from-substack"
        assert fm["cluster_size"] == 3
        # Body lists the cluster sources as wikilinks.
        assert "ai-capex" in body
        assert "hyperscaler" in body

    def test_two_sources_below_threshold(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        _make_substack_source(vault, "A", concepts=["ai-capex", "hyperscaler"])
        _make_substack_source(vault, "B", concepts=["ai-capex", "hyperscaler"])
        indexer.rebuild()

        outcome = scan_candidates(config, source_type="substack")

        assert outcome.candidates_created == []

    def test_concept_overlap_below_threshold(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        # Three sources but each only shares ONE concept with the others —
        # below the default min_shared_concepts=2.
        _make_substack_source(vault, "A", concepts=["ai-capex"])
        _make_substack_source(vault, "B", concepts=["ai-capex"])
        _make_substack_source(vault, "C", concepts=["ai-capex"])
        indexer.rebuild()

        outcome = scan_candidates(config, source_type="substack")

        assert outcome.candidates_created == []


class TestScanCandidatesNonEventGrain:
    """Concept-grain (paper/repo/article) and none-grain (conversation)
    sources never produce candidates, even if they cluster."""

    def test_paper_sources_skipped(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        for i in range(3):
            vault.create_note(
                note_type=NoteType.SOURCE,
                title=f"Paper {i}",
                extra_frontmatter={
                    "source_type": "paper",
                    "concepts": ["ai-capex", "hyperscaler"],
                },
            )
        indexer.rebuild()

        outcome = scan_candidates(config, source_type="paper")

        assert outcome.candidates_created == []

    def test_unspecified_source_type_scans_all_event_grain(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        # A paper cluster + a substack cluster: only the substack one fires.
        for i in range(3):
            vault.create_note(
                note_type=NoteType.SOURCE,
                title=f"Paper {i}",
                extra_frontmatter={
                    "source_type": "paper",
                    "concepts": ["transformer", "attention"],
                },
            )
        for i in range(3):
            _make_substack_source(
                vault, f"Substack {i}", concepts=["ai-capex", "hyperscaler"]
            )
        indexer.rebuild()

        outcome = scan_candidates(config)

        assert len(outcome.candidates_created) == 1
        fm, _ = parse_frontmatter(
            outcome.candidates_created[0].read_text(encoding="utf-8")
        )
        assert fm["source_type"] == "substack"


class TestScanCandidatesDeduplication:
    """Coverage by an existing canonical theme, or by an active
    candidate, prevents a duplicate stub from being written."""

    def test_existing_theme_covers_cluster(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        for i in range(3):
            _make_substack_source(
                vault, f"S{i}", concepts=["ai-capex", "hyperscaler"]
            )
        # An existing canonical theme that cites the same concepts.
        vault.create_note(
            note_type=NoteType.THEME,
            title="AI capex unwind",
            extra_frontmatter={
                "concepts": ["ai-capex", "hyperscaler"],
                "status": "active",
            },
        )
        indexer.rebuild()

        outcome = scan_candidates(config, source_type="substack")

        assert outcome.candidates_created == []
        assert outcome.clusters_skipped_covered == 1

    def test_existing_candidate_dedupes(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        for i in range(3):
            _make_substack_source(
                vault, f"S{i}", concepts=["ai-capex", "hyperscaler"]
            )
        indexer.rebuild()

        first = scan_candidates(config, source_type="substack")
        assert len(first.candidates_created) == 1

        # Re-scan immediately: the active candidate dedupes.
        second = scan_candidates(config, source_type="substack")
        assert second.candidates_created == []
        assert second.clusters_skipped_existing_candidate >= 1


class TestArchiveStaleCandidates:
    def test_recent_candidate_kept(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        for i in range(3):
            _make_substack_source(
                vault, f"S{i}", concepts=["ai-capex", "hyperscaler"]
            )
        indexer.rebuild()
        scan_candidates(config, source_type="substack")

        moved = archive_stale_candidates(config, stale_days=30)

        assert moved == []

    def test_aged_candidate_archived(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        import os
        from datetime import datetime, timedelta, timezone

        for i in range(3):
            _make_substack_source(
                vault, f"S{i}", concepts=["ai-capex", "hyperscaler"]
            )
        indexer.rebuild()
        scan_candidates(config, source_type="substack")

        cdir = config.vault_root / "themes" / CANDIDATES_DIR_NAME
        cand_path = next(cdir.glob("cand-*.md"))
        old_ts = (
            datetime.now(timezone.utc) - timedelta(days=60)
        ).timestamp()
        os.utime(cand_path, (old_ts, old_ts))

        moved = archive_stale_candidates(config, stale_days=30)

        assert len(moved) == 1
        archive_dir = cdir / CANDIDATES_ARCHIVE_NAME
        assert (archive_dir / cand_path.name).exists()
        assert not cand_path.exists()


class TestPromoteCandidate:
    def test_mints_thm_id_and_removes_candidate(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        for i in range(3):
            _make_substack_source(
                vault, f"S{i}", concepts=["ai-capex", "hyperscaler"]
            )
        indexer.rebuild()
        scan_candidates(config, source_type="substack")

        cdir = config.vault_root / "themes" / CANDIDATES_DIR_NAME
        cand_path = next(cdir.glob("cand-*.md"))
        cand_id = cand_path.stem.split("-")[0] + "-" + cand_path.stem.split("-")[1]

        target_path = promote_candidate(
            config,
            cand_id,
            title="AI capex unwind 2026",
            essence="Hyperscalers pulled forward GPU spend; 2026 is when ROI gets tested.",
        )

        assert target_path.exists()
        assert target_path.name.startswith("thm-")
        assert "ai-capex-unwind-2026" in target_path.name
        assert not cand_path.exists()

        fm, body = parse_frontmatter(target_path.read_text(encoding="utf-8"))
        assert fm["status"] == "active"
        assert fm["promoted_from"] == cand_id
        assert fm["id"].startswith("thm-")
        assert "ai-capex" in fm["concepts"]
        assert "hyperscaler" in fm["concepts"]
        assert "## Essence" in body
        assert "## Catalyst log" in body
        assert "Hyperscalers pulled forward" in body

    def test_missing_candidate_raises(self, config: Config):
        with pytest.raises(FileNotFoundError):
            promote_candidate(config, "cand-doesnotexist", title="Whatever")
