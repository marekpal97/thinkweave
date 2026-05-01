"""Tests for `mem doctor` — the vault coherence linter."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from personal_mem.concepts import (
    DEAD_VOCAB_THRESHOLD,
    _RESERVED_ONTOLOGY_KEYS,
    delete_concept_hub,
    doctor_report,
    find_dead_vocabulary,
    find_orphan_hubs,
    find_redundant_hub_candidates,
    find_tag_concept_overlap,
    find_unknown_tags,
    format_doctor_report,
    load_ontology,
    load_tag_vocabulary,
)
from personal_mem.hubs import ConceptHub, write_concept_hub
from personal_mem.config import Config
from personal_mem.indexer import Indexer
from personal_mem.schemas import NoteType
from personal_mem.vault import VaultManager


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


def _write_ontology(monkeypatch, content: str) -> Path:
    """Redirect the ontology loader to a temp file with the given YAML body.

    Monkeypatches both the seed and the vault override so the test's tmp
    file is the sole source of ontology data — no seed bleed-through.
    """
    path = Path(tempfile.mkdtemp()) / "ontology.yaml"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr("personal_mem.concepts._seed_ontology_path", lambda: path)
    monkeypatch.setattr("personal_mem.concepts._vault_ontology_path", lambda: path)
    monkeypatch.setattr("personal_mem.concepts._ontology_path", lambda: path)
    return path


# ---------------------------------------------------------------------------
# Reserved-key filter on load_ontology
# ---------------------------------------------------------------------------


class TestReservedKeys:
    def test_tag_vocabulary_excluded_from_load_ontology(self, monkeypatch):
        _write_ontology(
            monkeypatch,
            "tag_vocabulary:\n  - todo\n  - parked\n\nswe/python:\n  - python\n",
        )
        ontology = load_ontology()
        assert "tag_vocabulary" not in ontology
        assert "swe/python" in ontology
        assert ontology["swe/python"] == ["python"]

    def test_load_tag_vocabulary_returns_canonical_set(self, monkeypatch):
        _write_ontology(
            monkeypatch,
            "tag_vocabulary:\n  - todo\n  - parked\n  - probe\n\nswe/python:\n  - python\n",
        )
        vocab = load_tag_vocabulary()
        assert vocab == {"todo", "parked", "probe"}

    def test_load_tag_vocabulary_strips_inline_comments(self, monkeypatch):
        _write_ontology(
            monkeypatch,
            "tag_vocabulary:\n"
            "  - todo            # workflow: open work\n"
            "  - probe           # workflow: question\n",
        )
        vocab = load_tag_vocabulary()
        assert vocab == {"todo", "probe"}

    def test_load_tag_vocabulary_empty_when_missing(self, monkeypatch):
        _write_ontology(monkeypatch, "swe/python:\n  - python\n")
        assert load_tag_vocabulary() == set()

    def test_reserved_keys_constant(self):
        assert "tag_vocabulary" in _RESERVED_ONTOLOGY_KEYS


# ---------------------------------------------------------------------------
# Seed/override layering — vault override does NOT silently shadow the seed
# when the vault is missing a top-level key (n-de89d808 regression).
# ---------------------------------------------------------------------------


class TestOntologyLayering:
    """Regression tests for the ontology-shadow gotcha.

    Pre-fix: once `{vault}/.mem/ontology.yaml` exists, the shipped seed is
    never read again — so any new top-level key shipped in the seed (e.g.
    `tag_vocabulary` from workstream A) was dead weight in production
    vaults initialised before the key existed.

    Post-fix: the seed is always read first, and the vault override is
    layered on top per top-level key. Vault wins for keys it explicitly
    defines; missing keys fall through to the seed.
    """

    def test_vault_override_missing_key_falls_through_to_seed(
        self, monkeypatch
    ):
        seed_path = Path(tempfile.mkdtemp()) / "ontology.yaml"
        seed_path.write_text(
            "tag_vocabulary:\n  - todo\n  - parked\n\n"
            "swe/python:\n  - python\n",
            encoding="utf-8",
        )
        # Vault override defines its own concept domain but does NOT define
        # tag_vocabulary — the pre-fix bug would return an empty vocab here.
        vault_path = Path(tempfile.mkdtemp()) / "ontology.yaml"
        vault_path.write_text(
            "swe/python:\n  - python\n  - asyncio\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "personal_mem.concepts._seed_ontology_path", lambda: seed_path
        )
        monkeypatch.setattr(
            "personal_mem.concepts._vault_ontology_path", lambda: vault_path
        )

        # tag_vocabulary missing from vault → comes from seed.
        assert load_tag_vocabulary() == {"todo", "parked"}
        # swe/python defined in vault → vault wins (asyncio added).
        ontology = load_ontology()
        assert ontology["swe/python"] == ["python", "asyncio"]

    def test_vault_override_with_explicit_empty_list_shadows_seed(
        self, monkeypatch
    ):
        # User explicitly removed tag_vocabulary by setting an empty list:
        # the seed must NOT be revived. Explicit user intent wins.
        seed_path = Path(tempfile.mkdtemp()) / "ontology.yaml"
        seed_path.write_text(
            "tag_vocabulary:\n  - todo\n  - parked\n",
            encoding="utf-8",
        )
        vault_path = Path(tempfile.mkdtemp()) / "ontology.yaml"
        vault_path.write_text(
            "tag_vocabulary: []\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "personal_mem.concepts._seed_ontology_path", lambda: seed_path
        )
        monkeypatch.setattr(
            "personal_mem.concepts._vault_ontology_path", lambda: vault_path
        )

        assert load_tag_vocabulary() == set()

    def test_explicit_path_argument_disables_layering(self, monkeypatch):
        # Tooling that wants a single source of truth passes path= directly.
        # No seed bleed-through even if the seed monkeypatch is in place.
        seed_path = Path(tempfile.mkdtemp()) / "ontology.yaml"
        seed_path.write_text(
            "tag_vocabulary:\n  - todo\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            "personal_mem.concepts._seed_ontology_path", lambda: seed_path
        )

        explicit_path = Path(tempfile.mkdtemp()) / "ontology.yaml"
        explicit_path.write_text(
            "swe/python:\n  - python\n", encoding="utf-8"
        )

        # Explicit path → no layering, no tag_vocabulary from seed.
        assert load_tag_vocabulary(path=explicit_path) == set()
        assert load_ontology(path=explicit_path) == {"swe/python": ["python"]}


# ---------------------------------------------------------------------------
# Tag/concept overlap
# ---------------------------------------------------------------------------


class TestTagConceptOverlap:
    def test_no_overlap_returns_empty(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            tags=["todo"],
            extra_frontmatter={"concepts": ["fts5"]},
        )
        indexer.rebuild()

        import sqlite3

        db = sqlite3.connect(str(config.index_db))
        db.row_factory = sqlite3.Row
        try:
            assert find_tag_concept_overlap(db) == []
        finally:
            db.close()

    def test_detects_overlap(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        # 'finance' used as both a tag and a concept on different notes.
        vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            tags=["finance"],
            extra_frontmatter={"concepts": ["options-strategy"]},
        )
        vault.create_note(
            note_type=NoteType.NOTE,
            title="B",
            tags=["til"],
            extra_frontmatter={"concepts": ["finance"]},
        )
        indexer.rebuild()

        import sqlite3

        db = sqlite3.connect(str(config.index_db))
        db.row_factory = sqlite3.Row
        try:
            overlap = find_tag_concept_overlap(db)
        finally:
            db.close()

        assert len(overlap) == 1
        term, tag_cnt, concept_cnt = overlap[0]
        assert term == "finance"
        assert tag_cnt == 1
        assert concept_cnt == 1


# ---------------------------------------------------------------------------
# Unknown tags
# ---------------------------------------------------------------------------


class TestUnknownTags:
    def test_returns_empty_when_no_vocabulary(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(note_type=NoteType.NOTE, title="A", tags=["todo"])
        indexer.rebuild()

        import sqlite3

        db = sqlite3.connect(str(config.index_db))
        db.row_factory = sqlite3.Row
        try:
            assert find_unknown_tags(db, set()) == []
        finally:
            db.close()

    def test_flags_tags_outside_vocabulary(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(note_type=NoteType.NOTE, title="A", tags=["todo"])
        vault.create_note(note_type=NoteType.NOTE, title="B", tags=["randomtag"])
        vault.create_note(note_type=NoteType.NOTE, title="C", tags=["randomtag"])
        indexer.rebuild()

        import sqlite3

        db = sqlite3.connect(str(config.index_db))
        db.row_factory = sqlite3.Row
        try:
            unknown = find_unknown_tags(db, {"todo", "parked"})
        finally:
            db.close()

        assert unknown == [("randomtag", 2)]


# ---------------------------------------------------------------------------
# Dead vocabulary
# ---------------------------------------------------------------------------


class TestDeadVocabulary:
    def test_flags_ontology_concepts_with_zero_notes(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        _write_ontology(
            monkeypatch,
            "swe/python:\n  - python\n  - never-used\n",
        )
        # Only `python` is referenced in a note.
        vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            extra_frontmatter={"concepts": ["python", "pytest"]},
        )
        indexer.rebuild()

        import sqlite3

        db = sqlite3.connect(str(config.index_db))
        db.row_factory = sqlite3.Row
        try:
            dead = find_dead_vocabulary(db, load_ontology())
        finally:
            db.close()

        # `python` has 1 note (< threshold 2), `never-used` has 0.
        names = [d[0] for d in dead]
        assert "never-used" in names
        assert "python" in names
        # Dead-first sort: zero counts before non-zero.
        assert dead[0][1] == 0


# ---------------------------------------------------------------------------
# doctor_report integration + formatting
# ---------------------------------------------------------------------------


class TestDoctorReport:
    def test_clean_vault_reports_no_issues(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        _write_ontology(
            monkeypatch,
            "tag_vocabulary:\n  - todo\n\nswe/python:\n  - python\n",
        )
        # Two notes citing `python` so it clears the dead-vocab threshold.
        vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            tags=["todo"],
            extra_frontmatter={"concepts": ["python"]},
        )
        vault.create_note(
            note_type=NoteType.NOTE,
            title="B",
            tags=["todo"],
            extra_frontmatter={"concepts": ["python"]},
        )
        indexer.rebuild()

        report = doctor_report(config)
        assert report["tag_concept_overlap"] == []
        assert report["unknown_tags"] == []
        assert report["dead_vocabulary"] == []
        assert report["vocabulary_size"] == 1
        assert "No coherence issues detected" in format_doctor_report(report)

class TestStaleHubPruning:
    def test_delete_concept_hub_removes_existing_file(
        self, vault: VaultManager, config: Config
    ):
        from personal_mem.hubs import concept_hub_path

        hub = ConceptHub(
            concept="oldname",
            path=concept_hub_path(config, "oldname"),
            essence="To be removed.",
        )
        write_concept_hub(hub)
        assert hub.path.exists()

        result = delete_concept_hub(config, "oldname")
        assert result is True
        assert not hub.path.exists()

    def test_delete_concept_hub_noop_when_missing(
        self, vault: VaultManager, config: Config
    ):
        result = delete_concept_hub(config, "never-existed")
        assert result is False

    def test_find_orphan_hubs_identifies_unused_hubs(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        from personal_mem.hubs import concept_hub_path

        # Ontology says python is canonical.
        _write_ontology(monkeypatch, "swe/python:\n  - python\n")

        # Two hub files: python (canonical) and orphan-thing (no notes, not in ontology).
        for name in ("python", "orphan-thing"):
            hub = ConceptHub(
                concept=name,
                path=concept_hub_path(config, name),
                essence=f"essence for {name}",
            )
            write_concept_hub(hub)

        # python concept has at least one note.
        vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            extra_frontmatter={"concepts": ["python"]},
        )
        indexer.rebuild()

        orphans = find_orphan_hubs(config)
        names = [c for c, _ in orphans]
        assert "orphan-thing" in names
        assert "python" not in names


class TestRedundantHubCandidates:
    def test_finds_overlapping_essences(
        self, vault: VaultManager, config: Config
    ):
        from personal_mem.hubs import concept_hub_path

        # Two hubs with substantially overlapping word sets.
        a = ConceptHub(
            concept="vector-database",
            path=concept_hub_path(config, "vector-database"),
            essence=(
                "A vector database stores high-dimensional embeddings and "
                "supports nearest-neighbour retrieval queries efficiently "
                "over large corpora."
            ),
        )
        b = ConceptHub(
            concept="embedding-store",
            path=concept_hub_path(config, "embedding-store"),
            essence=(
                "An embedding store keeps high-dimensional embeddings and "
                "supports nearest-neighbour retrieval queries efficiently "
                "over large datasets."
            ),
        )
        c = ConceptHub(
            concept="kalman-filter",
            path=concept_hub_path(config, "kalman-filter"),
            essence=(
                "A recursive Bayesian estimator that fuses noisy "
                "measurements with a linear dynamic model to track state."
            ),
        )
        for hub in (a, b, c):
            write_concept_hub(hub)

        candidates = find_redundant_hub_candidates(config, min_jaccard=0.4)
        pair_names = {tuple(sorted([x, y])) for x, y, _ in candidates}
        assert ("embedding-store", "vector-database") in pair_names
        # kalman-filter shouldn't pair with the embedding hubs.
        assert all("kalman-filter" not in p for p in pair_names)

    def test_returns_empty_when_essences_too_short(
        self, vault: VaultManager, config: Config
    ):
        from personal_mem.hubs import concept_hub_path

        hub = ConceptHub(
            concept="tiny",
            path=concept_hub_path(config, "tiny"),
            essence="short",
        )
        write_concept_hub(hub)

        # min_essence_chars defaults to 80 — this should be skipped.
        assert find_redundant_hub_candidates(config) == []


class TestDoctorReportDirtyVault:
    def test_dirty_vault_surfaces_all_issues(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        _write_ontology(
            monkeypatch,
            "tag_vocabulary:\n  - todo\n\nswe/python:\n  - python\n  - dead\n",
        )
        # Tag/concept overlap on `finance`.
        vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            tags=["finance"],
            extra_frontmatter={"concepts": ["finance"]},
        )
        # Unknown tag `randomtag`.
        vault.create_note(note_type=NoteType.NOTE, title="B", tags=["randomtag"])
        indexer.rebuild()

        report = doctor_report(config)
        assert any(t == "finance" for t, _, _ in report["tag_concept_overlap"])
        assert any(t == "randomtag" for t, _ in report["unknown_tags"])
        assert any(c == "dead" for c, _ in report["dead_vocabulary"])

        text = format_doctor_report(report)
        assert "finance" in text
        assert "randomtag" in text
        assert "dead" in text
