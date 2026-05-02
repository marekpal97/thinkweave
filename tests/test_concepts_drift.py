"""Tests for the ontology drift report (src/personal_mem/concepts.py)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from personal_mem.synthesis.concepts import (
    DRIFT_COUNT_THRESHOLD,
    drift_report,
    format_drift_report,
    hubs_marker_path,
)
from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
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


def _stub_ontology(monkeypatch, domains: dict[str, list[str]]) -> Path:
    """Redirect the ontology loader to an in-memory ontology dict.

    Returns a Path to a fake ontology file (used for staleness mtime tests).
    """
    import tempfile

    path = Path(tempfile.mkdtemp()) / "ontology.yaml"
    lines = []
    for domain, concepts in domains.items():
        lines.append(f"{domain}: [{', '.join(concepts)}]")
    path.write_text("\n".join(lines), encoding="utf-8")

    monkeypatch.setattr(
        "personal_mem.synthesis.concepts._seed_ontology_path", lambda: path
    )
    monkeypatch.setattr(
        "personal_mem.synthesis.concepts._vault_ontology_path", lambda: path
    )
    monkeypatch.setattr(
        "personal_mem.synthesis.concepts._ontology_path", lambda: path
    )
    return path


# ---------------------------------------------------------------------------
# Near-duplicate detection
# ---------------------------------------------------------------------------


class TestDriftNearDuplicates:
    def test_detects_near_duplicate_pair(
        self,
        config: Config,
        vault: VaultManager,
        indexer: Indexer,
        monkeypatch,
    ):
        _stub_ontology(monkeypatch, {})  # empty ontology, so nothing is excluded

        vault.create_note(
            NoteType.NOTE,
            "A",
            project="p",
            extra_frontmatter={"concepts": ["neural-network"]},
        )
        vault.create_note(
            NoteType.NOTE,
            "B",
            project="p",
            extra_frontmatter={"concepts": ["neural-networks"]},
        )
        indexer.rebuild(full=True)

        report = drift_report(config)
        dupes = report["near_duplicates"]
        pair_set = {tuple(sorted((a, b))) for a, b, _ in dupes}
        assert ("neural-network", "neural-networks") in pair_set

    def test_clean_vault_has_no_duplicates(
        self,
        config: Config,
        vault: VaultManager,
        indexer: Indexer,
        monkeypatch,
    ):
        _stub_ontology(monkeypatch, {})

        vault.create_note(
            NoteType.NOTE,
            "A",
            project="p",
            extra_frontmatter={"concepts": ["completely-distinct-thing"]},
        )
        vault.create_note(
            NoteType.NOTE,
            "B",
            project="p",
            extra_frontmatter={"concepts": ["utterly-unrelated-other"]},
        )
        indexer.rebuild(full=True)

        report = drift_report(config)
        assert report["near_duplicates"] == []


# ---------------------------------------------------------------------------
# New concept candidates
# ---------------------------------------------------------------------------


class TestDriftNewConceptCandidates:
    def test_concept_above_threshold_not_in_ontology(
        self,
        config: Config,
        vault: VaultManager,
        indexer: Indexer,
        monkeypatch,
    ):
        _stub_ontology(monkeypatch, {"math/calc": ["gradient"]})

        # Create 5 notes with "recursive-cte" — crosses the threshold of 5
        for i in range(5):
            vault.create_note(
                NoteType.NOTE,
                f"Note {i}",
                project="p",
                extra_frontmatter={"concepts": ["recursive-cte"]},
            )
        indexer.rebuild(full=True)

        report = drift_report(config, threshold=5)
        candidates = [c for c, _ in report["new_concept_candidates"]]
        assert "recursive-cte" in candidates

    def test_concept_already_in_ontology_is_not_a_candidate(
        self,
        config: Config,
        vault: VaultManager,
        indexer: Indexer,
        monkeypatch,
    ):
        _stub_ontology(monkeypatch, {"math/calc": ["gradient"]})

        for i in range(6):
            vault.create_note(
                NoteType.NOTE,
                f"Note {i}",
                project="p",
                extra_frontmatter={"concepts": ["gradient"]},
            )
        indexer.rebuild(full=True)

        report = drift_report(config)
        candidates = [c for c, _ in report["new_concept_candidates"]]
        assert "gradient" not in candidates  # already ontology-resident

    def test_below_threshold_ignored(
        self,
        config: Config,
        vault: VaultManager,
        indexer: Indexer,
        monkeypatch,
    ):
        _stub_ontology(monkeypatch, {})

        # Only 2 notes — below threshold of 5
        for i in range(2):
            vault.create_note(
                NoteType.NOTE,
                f"Note {i}",
                project="p",
                extra_frontmatter={"concepts": ["small-concept"]},
            )
        indexer.rebuild(full=True)

        report = drift_report(config, threshold=5)
        candidates = [c for c, _ in report["new_concept_candidates"]]
        assert "small-concept" not in candidates


# ---------------------------------------------------------------------------
# Ontology staleness
# ---------------------------------------------------------------------------


class TestOntologyStale:
    def test_stale_when_no_marker_exists(
        self,
        config: Config,
        vault: VaultManager,
        indexer: Indexer,
        monkeypatch,
    ):
        _stub_ontology(monkeypatch, {"math/calc": ["x"]})
        indexer.rebuild(full=True)

        # No marker file → stale by default
        assert not hubs_marker_path(config).exists()
        report = drift_report(config)
        assert report["ontology_stale"] is True

    def test_fresh_when_marker_newer_than_ontology(
        self,
        config: Config,
        vault: VaultManager,
        indexer: Indexer,
        monkeypatch,
    ):
        ontology_path = _stub_ontology(monkeypatch, {"math/calc": ["x"]})
        indexer.rebuild(full=True)

        # Touch marker AFTER the ontology to make it fresh
        marker = hubs_marker_path(config)
        marker.parent.mkdir(parents=True, exist_ok=True)
        time.sleep(0.01)
        marker.touch()

        report = drift_report(config)
        assert report["ontology_stale"] is False

    def test_stale_when_ontology_edited_after_hubs(
        self,
        config: Config,
        vault: VaultManager,
        indexer: Indexer,
        monkeypatch,
    ):
        ontology_path = _stub_ontology(monkeypatch, {"math/calc": ["x"]})
        indexer.rebuild(full=True)

        marker = hubs_marker_path(config)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()

        time.sleep(0.01)
        # Edit the ontology after marker was written
        ontology_path.write_text("math/calc: [x, y]\n", encoding="utf-8")

        report = drift_report(config)
        assert report["ontology_stale"] is True


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


class TestFormatDriftReport:
    def test_empty_report_returns_clean_message(self):
        report = {
            "near_duplicates": [],
            "new_concept_candidates": [],
            "ontology_stale": False,
        }
        assert format_drift_report(report) == "No drift detected."

    def test_formatter_includes_each_section(self):
        report = {
            "near_duplicates": [("a", "aa", "substring")],
            "new_concept_candidates": [("recursive-cte", 7)],
            "ontology_stale": True,
        }
        text = format_drift_report(report)
        assert "Near-duplicate" in text
        assert "'a' ≈ 'aa'" in text
        assert "recursive-cte" in text
        assert "ontology.yaml is newer" in text


# ---------------------------------------------------------------------------
# Empty vault edge case
# ---------------------------------------------------------------------------


class TestDriftEmptyVault:
    def test_empty_vault_does_not_crash(
        self, config: Config, vault: VaultManager, indexer: Indexer, monkeypatch
    ):
        _stub_ontology(monkeypatch, {})
        indexer.rebuild(full=True)

        report = drift_report(config)
        assert report["near_duplicates"] == []
        assert report["new_concept_candidates"] == []
        # ontology_stale may be True or False depending on whether the stub
        # file exists — either is valid for an empty vault.
