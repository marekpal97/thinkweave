"""Tests for `weave doctor` — the vault coherence linter."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from thinkweave.synthesis.concepts import (
    DEAD_VOCAB_THRESHOLD,
    DOMAIN_MARKERS,
    _RESERVED_ONTOLOGY_KEYS,
    delete_concept_hub,
    demote_non_ontology_concepts,
    doctor_report,
    find_dead_vocabulary,
    find_isolated_notes,
    find_orphan_hubs,
    find_redundant_hub_candidates,
    find_tag_concept_overlap,
    find_unknown_tags,
    format_doctor_report,
    get_all_proposed_concepts,
    is_domain_concept,
    load_ontology,
    load_tag_vocabulary,
    promote_proposed_concept,
    prune_noisy_singletons,
    split_concepts_by_ontology,
)
from thinkweave.synthesis.concept_hub import ConceptHub, write_concept_hub
from thinkweave.core.config import Config
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


def _write_ontology(monkeypatch, content: str) -> Path:
    """Redirect the ontology loader to a temp file with the given YAML body.

    Monkeypatches both the seed and the vault override so the test's tmp
    file is the sole source of ontology data — no seed bleed-through.
    """
    path = Path(tempfile.mkdtemp()) / "ontology.yaml"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr("thinkweave.synthesis.concepts._seed_ontology_path", lambda: path)
    monkeypatch.setattr("thinkweave.synthesis.concepts._vault_ontology_path", lambda: path)
    monkeypatch.setattr("thinkweave.synthesis.concepts._ontology_path", lambda: path)
    return path


# ---------------------------------------------------------------------------
# Reserved-key filter on load_ontology
# ---------------------------------------------------------------------------


class TestReservedKeys:
    def test_tag_vocabulary_excluded_from_load_ontology(self, monkeypatch):
        _write_ontology(
            monkeypatch,
            "tag_vocabulary:\n  - todo\n  - parked\n\nswe-python:\n  - python\n",
        )
        ontology = load_ontology()
        assert "tag_vocabulary" not in ontology
        assert "swe-python" in ontology
        assert ontology["swe-python"] == ["python"]

    def test_load_tag_vocabulary_returns_canonical_set(self, monkeypatch):
        _write_ontology(
            monkeypatch,
            "tag_vocabulary:\n  - todo\n  - parked\n  - probe\n\nswe-python:\n  - python\n",
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
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        assert load_tag_vocabulary() == set()

    def test_reserved_keys_constant(self):
        assert "tag_vocabulary" in _RESERVED_ONTOLOGY_KEYS


# ---------------------------------------------------------------------------
# Seed/override layering — vault override does NOT silently shadow the seed
# when the vault is missing a top-level key (n-de89d808 regression).
# ---------------------------------------------------------------------------


class TestOntologyLayering:
    """Regression tests for the seed/vault layering semantic.

    Pre-fix (replace-on-key): once a vault override defined a top-level key,
    the seed's value for that key was dropped wholesale. Two failure modes:
    (1) seed-only keys never reaching production (workstream A regression);
    (2) shared keys silently losing seed-defined leaves when the vault's leaf
    list didn't include them.

    Post-fix (deep-merge): the seed is always read first, then for each
    top-level key the vault contributes, leaf lists are unioned (de-duped,
    seed order first). Seed-only keys still fall through. Empty vault lists
    are no longer a "suppress" signal — to remove a seed leaf, edit the
    seed; the vault override is purely additive.
    """

    def test_vault_override_missing_key_falls_through_to_seed(
        self, monkeypatch
    ):
        seed_path = Path(tempfile.mkdtemp()) / "ontology.yaml"
        seed_path.write_text(
            "tag_vocabulary:\n  - todo\n  - parked\n\n"
            "swe-python:\n  - python\n",
            encoding="utf-8",
        )
        vault_path = Path(tempfile.mkdtemp()) / "ontology.yaml"
        vault_path.write_text(
            "swe-python:\n  - python\n  - asyncio\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "thinkweave.synthesis.concepts._seed_ontology_path", lambda: seed_path
        )
        monkeypatch.setattr(
            "thinkweave.synthesis.concepts._vault_ontology_path", lambda: vault_path
        )

        # tag_vocabulary only in seed → falls through unchanged.
        assert load_tag_vocabulary() == {"todo", "parked"}
        # swe-python in both → unioned (seed first, then vault-only leaves).
        ontology = load_ontology()
        assert ontology["swe-python"] == ["python", "asyncio"]

    def test_deep_merge_preserves_seed_leaves_not_in_vault(
        self, monkeypatch
    ):
        """Regression for the silent-drop bug — vault override defining a
        domain with a partial leaf list MUST NOT drop seed leaves missing
        from the vault. Pre-fix, ``swe-python: [python, pandas]`` in the
        vault dropped seed leaves like ``pathlib`` and ``typing``."""
        seed_path = Path(tempfile.mkdtemp()) / "ontology.yaml"
        seed_path.write_text(
            "swe-python:\n  - python\n  - pathlib\n  - typing\n",
            encoding="utf-8",
        )
        vault_path = Path(tempfile.mkdtemp()) / "ontology.yaml"
        vault_path.write_text(
            "swe-python:\n  - python\n  - pandas\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "thinkweave.synthesis.concepts._seed_ontology_path", lambda: seed_path
        )
        monkeypatch.setattr(
            "thinkweave.synthesis.concepts._vault_ontology_path", lambda: vault_path
        )

        ontology = load_ontology()
        # Union, dedup by lowercase, seed leaves listed first.
        assert ontology["swe-python"] == ["python", "pathlib", "typing", "pandas"]

    def test_deep_merge_dedupes_case_insensitively(self, monkeypatch):
        seed_path = Path(tempfile.mkdtemp()) / "ontology.yaml"
        seed_path.write_text(
            "swe-python:\n  - Python\n  - pathlib\n", encoding="utf-8"
        )
        vault_path = Path(tempfile.mkdtemp()) / "ontology.yaml"
        vault_path.write_text(
            "swe-python:\n  - python\n  - asyncio\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            "thinkweave.synthesis.concepts._seed_ontology_path", lambda: seed_path
        )
        monkeypatch.setattr(
            "thinkweave.synthesis.concepts._vault_ontology_path", lambda: vault_path
        )

        ontology = load_ontology()
        # _parse_yaml_file already lowercases; assertion confirms no dup.
        assert ontology["swe-python"].count("python") == 1
        assert "asyncio" in ontology["swe-python"]
        assert "pathlib" in ontology["swe-python"]

    def test_explicit_path_argument_disables_layering(self, monkeypatch):
        # Tooling that wants a single source of truth passes path= directly.
        # No seed bleed-through even if the seed monkeypatch is in place.
        seed_path = Path(tempfile.mkdtemp()) / "ontology.yaml"
        seed_path.write_text(
            "tag_vocabulary:\n  - todo\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            "thinkweave.synthesis.concepts._seed_ontology_path", lambda: seed_path
        )

        explicit_path = Path(tempfile.mkdtemp()) / "ontology.yaml"
        explicit_path.write_text(
            "swe-python:\n  - python\n", encoding="utf-8"
        )

        # Explicit path → no layering, no tag_vocabulary from seed.
        assert load_tag_vocabulary(path=explicit_path) == set()
        assert load_ontology(path=explicit_path) == {"swe-python": ["python"]}


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
            "swe-python:\n  - python\n  - never-used\n",
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
            "tag_vocabulary:\n  - todo\n\nswe-python:\n  - python\n",
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
        from thinkweave.synthesis.concept_hub import concept_hub_path

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
        from thinkweave.synthesis.concept_hub import concept_hub_path

        # Ontology says python is canonical.
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")

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

    def test_archive_orphan_hubs_moves_files_into_archive_subdir(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        from thinkweave.synthesis.concept_hub import concept_hub_path, topics_dir
        from thinkweave.synthesis.concepts import (
            archive_orphan_hubs,
            hub_archive_dir,
        )

        _write_ontology(monkeypatch, "swe-python:\n  - python\n")

        # canonical hub + two orphans.
        for name in ("python", "demoted-a", "demoted-b"):
            hub = ConceptHub(
                concept=name,
                path=concept_hub_path(config, name),
                essence=f"essence for {name}",
            )
            write_concept_hub(hub)
        vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            extra_frontmatter={"concepts": ["python"]},
        )
        indexer.rebuild()

        moved = archive_orphan_hubs(config)
        archived_names = {c for c, _ in moved}
        assert archived_names == {"demoted-a", "demoted-b"}

        # Archive lives inside topics/ so non-recursive globs skip it.
        archive_dir = hub_archive_dir(config)
        assert archive_dir.parent == topics_dir(config)
        assert (archive_dir / "demoted-a.md").exists()
        assert (archive_dir / "demoted-b.md").exists()

        # Live topics dir no longer carries them.
        assert not concept_hub_path(config, "demoted-a").exists()
        assert not concept_hub_path(config, "demoted-b").exists()
        # Canonical hub untouched.
        assert concept_hub_path(config, "python").exists()

        # find_orphan_hubs walks topics non-recursively → archive invisible.
        assert find_orphan_hubs(config) == []

        # Idempotent re-run is a no-op.
        assert archive_orphan_hubs(config) == []

    def test_archive_orphan_hubs_dry_run_is_readonly(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        from thinkweave.synthesis.concept_hub import concept_hub_path
        from thinkweave.synthesis.concepts import archive_orphan_hubs

        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        hub = ConceptHub(
            concept="ghost",
            path=concept_hub_path(config, "ghost"),
            essence="orphan",
        )
        write_concept_hub(hub)
        indexer.rebuild()

        moved = archive_orphan_hubs(config, dry_run=True)
        assert [c for c, _ in moved] == ["ghost"]
        # File still in place — dry-run must not move anything.
        assert concept_hub_path(config, "ghost").exists()

    def test_archive_concept_hub_collision_preserves_prior_copy(
        self, vault: VaultManager, config: Config
    ):
        from thinkweave.synthesis.concept_hub import concept_hub_path
        from thinkweave.synthesis.concepts import (
            archive_concept_hub,
            hub_archive_dir,
        )

        # First archive of "twice".
        hub1 = ConceptHub(
            concept="twice",
            path=concept_hub_path(config, "twice"),
            essence="first round",
        )
        write_concept_hub(hub1)
        first = archive_concept_hub(config, "twice")
        assert first is not None and first.exists()

        # Re-create at canonical path (simulates re-promotion + new content)
        # then archive again.
        hub2 = ConceptHub(
            concept="twice",
            path=concept_hub_path(config, "twice"),
            essence="second round",
        )
        write_concept_hub(hub2)
        second = archive_concept_hub(config, "twice")
        assert second is not None and second.exists()
        # Prior archive copy preserved under .bak — no synthesis work lost.
        assert (hub_archive_dir(config) / "twice.md.bak").exists()

    def test_consolidate_parents_drops_domain_when_leaf_present(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        from thinkweave.synthesis.concepts import consolidate_parent_leaf_concepts

        # 2-tier ontology: swe-python is parent, pytest is its child.
        # ml-deep-learning is a separate domain — should NOT trigger drop
        # of swe-python just because ml-deep-learning is on the same note.
        _write_ontology(
            monkeypatch,
            "swe-python:\n  - python\n  - pytest\n"
            "ml-deep-learning:\n  - pytorch\n",
        )

        # parent + child on same note → parent gets dropped
        n1 = vault.create_note(
            note_type=NoteType.NOTE,
            title="parent+child",
            extra_frontmatter={"concepts": ["swe-python", "pytest"]},
        )
        # parent only → preserved (no child to make it redundant)
        n2 = vault.create_note(
            note_type=NoteType.NOTE,
            title="parent only",
            extra_frontmatter={"concepts": ["swe-python"]},
        )
        # parent + sibling-domain leaf → parent preserved (cross-domain
        # redundancy isn't this pass's job).
        n3 = vault.create_note(
            note_type=NoteType.NOTE,
            title="parent + sibling-domain leaf",
            extra_frontmatter={"concepts": ["swe-python", "pytorch"]},
        )
        indexer.rebuild()

        stats = consolidate_parent_leaf_concepts(config)

        assert stats["files_modified"] == 1
        assert stats["occurrences_dropped"] == 1
        assert stats["domains_touched"] == ["swe-python"]

        from thinkweave.core.vault import parse_frontmatter

        fm1, _ = parse_frontmatter(n1.read_text())
        assert fm1["concepts"] == ["pytest"]

        fm2, _ = parse_frontmatter(n2.read_text())
        assert fm2["concepts"] == ["swe-python"]

        fm3, _ = parse_frontmatter(n3.read_text())
        assert sorted(fm3["concepts"]) == ["pytorch", "swe-python"]

    def test_consolidate_parents_dry_run_is_readonly(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        from thinkweave.synthesis.concepts import consolidate_parent_leaf_concepts

        _write_ontology(monkeypatch, "swe-python:\n  - pytest\n")
        n = vault.create_note(
            note_type=NoteType.NOTE,
            title="dry",
            extra_frontmatter={"concepts": ["swe-python", "pytest"]},
        )
        indexer.rebuild()

        stats = consolidate_parent_leaf_concepts(config, dry_run=True)
        assert stats["files_modified"] == 1

        from thinkweave.core.vault import parse_frontmatter

        fm, _ = parse_frontmatter(n.read_text())
        assert sorted(fm["concepts"]) == ["pytest", "swe-python"]

    def test_demote_non_ontology_archives_orphan_hubs(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        from thinkweave.synthesis.concept_hub import concept_hub_path
        from thinkweave.synthesis.concepts import (
            demote_non_ontology_concepts,
            hub_archive_dir,
        )

        # Ontology canonicalises only `python`.
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")

        # A note tagged with a non-ontology concept (`legacy-term`).
        vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            extra_frontmatter={"concepts": ["python", "legacy-term"]},
        )
        # Hub file exists for the about-to-be-demoted term.
        hub = ConceptHub(
            concept="legacy-term",
            path=concept_hub_path(config, "legacy-term"),
            essence="will be archived on demotion",
        )
        write_concept_hub(hub)
        indexer.rebuild()

        stats = demote_non_ontology_concepts(config)

        assert "legacy-term" in stats["terms_demoted"]
        assert stats["hubs_archived"] == ["legacy-term"]
        # Live hub gone, archive copy in place.
        assert not concept_hub_path(config, "legacy-term").exists()
        assert (hub_archive_dir(config) / "legacy-term.md").exists()


class TestRedundantHubCandidates:
    def test_finds_overlapping_essences(
        self, vault: VaultManager, config: Config
    ):
        from thinkweave.synthesis.concept_hub import concept_hub_path

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
        from thinkweave.synthesis.concept_hub import concept_hub_path

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
            "tag_vocabulary:\n  - todo\n\nswe-python:\n  - python\n  - dead\n",
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


# ---------------------------------------------------------------------------
# find_isolated_notes + doctor `--isolation` opt-in
# ---------------------------------------------------------------------------


def _open_index(config: Config):
    import sqlite3

    db = sqlite3.connect(str(config.index_db))
    db.row_factory = sqlite3.Row
    return db


class TestFindIsolatedNotes:
    """find_isolated_notes returns notes with zero graph edges, bucketed."""

    def test_empty_vault_returns_zero(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        indexer.rebuild()
        db = _open_index(config)
        try:
            result = find_isolated_notes(db)
        finally:
            db.close()
        assert result["total"] == 0
        assert result["by_type"] == []
        assert result["examples"] == []

    def test_zero_concept_note_is_isolated(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(note_type=NoteType.NOTE, title="Lonely")
        indexer.rebuild()
        db = _open_index(config)
        try:
            result = find_isolated_notes(db)
        finally:
            db.close()

        assert result["total"] == 1
        buckets = dict(result["by_concept_count"])
        assert buckets["0"] == 1
        assert buckets["1"] == 0
        assert buckets["2+"] == 0
        assert len(result["examples"]) == 1
        assert result["examples"][0]["title"] == "Lonely"
        assert result["examples"][0]["concept_count"] == 0

    def test_two_notes_sharing_concept_are_not_isolated(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n  - asyncio\n")
        vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            extra_frontmatter={"concepts": ["python", "asyncio"]},
        )
        vault.create_note(
            note_type=NoteType.NOTE,
            title="B",
            extra_frontmatter={"concepts": ["python", "asyncio"]},
        )
        indexer.rebuild()
        db = _open_index(config)
        try:
            result = find_isolated_notes(db)
        finally:
            db.close()
        assert result["total"] == 0


class TestDoctorReportIsolation:
    """Doctor's --isolation surface — opt-in by design."""

    def test_isolation_omitted_by_default(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(note_type=NoteType.NOTE, title="Lonely")
        indexer.rebuild()
        report = doctor_report(config)
        assert report["isolated_notes"] is None
        assert "Isolated notes" not in format_doctor_report(report)

    def test_isolation_present_when_requested(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(note_type=NoteType.NOTE, title="Lonely")
        indexer.rebuild()
        report = doctor_report(config, include_isolation=True)
        assert report["isolated_notes"] is not None
        assert report["isolated_notes"]["total"] == 1

        text = format_doctor_report(report)
        assert "Isolated notes" in text
        assert "Lonely" in text

    def test_isolation_zero_message_when_graph_fully_connected(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            extra_frontmatter={"concepts": ["python"]},
        )
        vault.create_note(
            note_type=NoteType.NOTE,
            title="B",
            extra_frontmatter={"concepts": ["python"]},
        )
        indexer.rebuild()
        report = doctor_report(config, include_isolation=True)
        assert report["isolated_notes"]["total"] == 0
        assert "Isolated notes: 0" in format_doctor_report(report)


# ---------------------------------------------------------------------------
# prune_noisy_singletons — default step in /weave-resolve-concepts
# ---------------------------------------------------------------------------


class TestPruneNoisySingletons:
    """The singleton prune lifts what was previously a per-run script.

    Keep set: ontology entries (seed + vault override merged) plus any
    concept whose name contains a `DOMAIN_MARKERS` substring. Everything
    else with note count == 1 is pruned. Concepts with count >= 2 are
    untouched regardless.
    """

    def test_strips_singleton_not_in_ontology_or_domain(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        note = vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            extra_frontmatter={"concepts": ["python", "made-up-thing"]},
        )
        indexer.rebuild()

        stats = prune_noisy_singletons(config, dry_run=False)

        assert "made-up-thing" in stats["removed"]
        assert stats["instances_removed"] >= 1
        assert stats["files_modified"] >= 1

        from thinkweave.core.vault import parse_frontmatter

        fm, _ = parse_frontmatter(note.read_text(encoding="utf-8"))
        assert fm["concepts"] == ["python"]

    def test_ontology_singleton_kept(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        _write_ontology(monkeypatch, "swe-python:\n  - asyncio\n")
        # `asyncio` is in the ontology and only appears once — must survive.
        vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            extra_frontmatter={"concepts": ["asyncio"]},
        )
        indexer.rebuild()

        stats = prune_noisy_singletons(config, dry_run=False)

        assert "asyncio" not in stats["removed"]
        assert stats["kept_ontology"] >= 1

    def test_domain_marker_singleton_kept(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        # `neural-osc` matches the `neural` domain marker and is preserved
        # despite being a count-1 emergent term.
        assert is_domain_concept("neural-osc")
        vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            extra_frontmatter={"concepts": ["neural-osc"]},
        )
        indexer.rebuild()

        stats = prune_noisy_singletons(config, dry_run=False)

        assert "neural-osc" not in stats["removed"]
        assert stats["kept_domain"] >= 1

    def test_non_singleton_untouched(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        # `widget` appears on two notes — count >= 2 means it's never
        # considered a singleton, even though it's not in ontology and not
        # a domain match.
        assert not is_domain_concept("widget")
        vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            extra_frontmatter={"concepts": ["widget"]},
        )
        vault.create_note(
            note_type=NoteType.NOTE,
            title="B",
            extra_frontmatter={"concepts": ["widget"]},
        )
        indexer.rebuild()

        stats = prune_noisy_singletons(config, dry_run=False)

        assert "widget" not in stats["removed"]

    def test_dry_run_does_not_write(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        note = vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            extra_frontmatter={"concepts": ["made-up-thing"]},
        )
        indexer.rebuild()
        before = note.read_text(encoding="utf-8")

        stats = prune_noisy_singletons(config, dry_run=True)

        # Stats still report what would happen, but the file is unchanged.
        assert "made-up-thing" in stats["removed"]
        assert note.read_text(encoding="utf-8") == before

    def test_domain_markers_constant_is_immutable(self):
        # frozenset prevents accidental mutation across calls.
        assert isinstance(DOMAIN_MARKERS, frozenset)
        assert "neural" in DOMAIN_MARKERS

    def test_proposed_concepts_are_sanctuary(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        # Proposed is the holding pen for emergent vocabulary. A term
        # living only in proposed_concepts: must NOT be touched by the
        # singleton sweep, even at count=1. Cleaning the proposed pool
        # happens via promotion or explicit /weave-resolve-concepts review,
        # never automated count pruning — otherwise the demotion sweep's
        # work would be undone immediately.
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        note = vault.create_note(
            note_type=NoteType.NOTE,
            title="N",
            extra_frontmatter={"proposed_concepts": ["emergent-term"]},
        )
        indexer.rebuild()
        before = note.read_text(encoding="utf-8")

        stats = prune_noisy_singletons(config, dry_run=False)

        # Not in the singleton population (we count only canonical).
        assert "emergent-term" not in stats["removed"]
        # File is byte-identical.
        assert note.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# split_concepts_by_ontology — strict creation policy partitioner
# ---------------------------------------------------------------------------


class TestSplitConceptsByOntology:
    """The shared partitioner used by every concept write surface.

    Canonical lands in `concepts:`; everything else flows to
    `proposed_concepts:`. Pre-existing `proposed_concepts:` are
    preserved (and deduped against the canonical set).
    """

    def test_canonical_terms_kept_in_canonical(self, monkeypatch):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n  - asyncio\n")
        canonical, proposed = split_concepts_by_ontology(["python", "asyncio"])
        assert canonical == ["python", "asyncio"]
        assert proposed == []

    def test_unknown_terms_move_to_proposed(self, monkeypatch):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        canonical, proposed = split_concepts_by_ontology(["python", "made-up-thing"])
        assert canonical == ["python"]
        assert proposed == ["made-up-thing"]

    def test_existing_proposed_preserved(self, monkeypatch):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        canonical, proposed = split_concepts_by_ontology(
            ["python"], proposed=["already-proposed"]
        )
        assert canonical == ["python"]
        assert proposed == ["already-proposed"]

    def test_dedupe_within_and_across_lists(self, monkeypatch):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        canonical, proposed = split_concepts_by_ontology(
            ["python", "Python", "made-up", "made-up"],
            proposed=["made-up", "another"],
        )
        assert canonical == ["python"]
        # Order preserved; duplicates collapsed.
        assert proposed == ["made-up", "another"]

    def test_empty_inputs(self, monkeypatch):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        canonical, proposed = split_concepts_by_ontology(None)
        assert canonical == []
        assert proposed == []

    def test_canonical_wins_when_in_both(self, monkeypatch):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        # If a term is ontology-known but caller mistakenly listed it under
        # proposed, the canonical side wins and it's not duplicated.
        canonical, proposed = split_concepts_by_ontology(
            ["python"], proposed=["python"]
        )
        assert canonical == ["python"]
        assert proposed == []


# ---------------------------------------------------------------------------
# demote_non_ontology_concepts — one-shot retroactive sweep
# ---------------------------------------------------------------------------


class TestDemoteNonOntologyConcepts:
    """Vault sweep that retroactively applies the strict creation policy.

    Every note's `concepts:` list is partitioned: matches stay,
    non-matches move to `proposed_concepts:`. Hub pages and landing docs
    are skipped. Pure-deterministic — no LLM.
    """

    def test_non_ontology_concept_moves_to_proposed(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        note = vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            extra_frontmatter={"concepts": ["python", "made-up-thing"]},
        )
        indexer.rebuild()

        stats = demote_non_ontology_concepts(config, dry_run=False)

        assert stats["files_modified"] >= 1
        assert "made-up-thing" in stats["terms_demoted"]

        from thinkweave.core.vault import parse_frontmatter

        fm, _ = parse_frontmatter(note.read_text(encoding="utf-8"))
        assert fm["concepts"] == ["python"]
        assert fm["proposed_concepts"] == ["made-up-thing"]

    def test_pure_canonical_note_untouched(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n  - asyncio\n")
        note = vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            extra_frontmatter={"concepts": ["python", "asyncio"]},
        )
        indexer.rebuild()
        before = note.read_text(encoding="utf-8")

        stats = demote_non_ontology_concepts(config, dry_run=False)

        assert stats["files_modified"] == 0
        assert note.read_text(encoding="utf-8") == before

    def test_existing_proposed_preserved(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        note = vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            extra_frontmatter={
                "concepts": ["python", "made-up"],
                "proposed_concepts": ["already-proposed"],
            },
        )
        indexer.rebuild()

        demote_non_ontology_concepts(config, dry_run=False)

        from thinkweave.core.vault import parse_frontmatter

        fm, _ = parse_frontmatter(note.read_text(encoding="utf-8"))
        # New demotion appends to existing proposed list.
        assert "made-up" in fm["proposed_concepts"]
        assert "already-proposed" in fm["proposed_concepts"]

    def test_dry_run_does_not_write(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        note = vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            extra_frontmatter={"concepts": ["made-up"]},
        )
        indexer.rebuild()
        before = note.read_text(encoding="utf-8")

        stats = demote_non_ontology_concepts(config, dry_run=True)

        # Stats still describe the would-be demotion.
        assert "made-up" in stats["terms_demoted"]
        assert note.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# Promotion mechanic — proposed → canonical
# ---------------------------------------------------------------------------


class TestProposedConceptCounts:
    def test_aggregates_proposed_across_notes(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        for i in range(3):
            vault.create_note(
                note_type=NoteType.NOTE,
                title=f"N{i}",
                extra_frontmatter={"proposed_concepts": ["emerging-term"]},
            )
        vault.create_note(
            note_type=NoteType.NOTE,
            title="Solo",
            extra_frontmatter={"proposed_concepts": ["one-off"]},
        )
        indexer.rebuild()

        counts = get_all_proposed_concepts(indexer.db)

        assert counts["emerging-term"] == 3
        assert counts["one-off"] == 1

    def test_returns_empty_when_no_proposed(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        vault.create_note(
            note_type=NoteType.NOTE,
            title="A",
            extra_frontmatter={"concepts": ["python"]},
        )
        indexer.rebuild()

        assert get_all_proposed_concepts(indexer.db) == {}


class TestPromoteProposedConcept:
    """Promotion adds the term to vault ontology, walks notes to shift
    proposed_concepts → concepts, and ensures a hub skeleton exists.
    """

    def _seed_separate_paths(self, monkeypatch, seed_yaml: str, vault_yaml: str = ""):
        seed_path = Path(tempfile.mkdtemp()) / "seed.yaml"
        seed_path.write_text(seed_yaml, encoding="utf-8")
        vault_path = Path(tempfile.mkdtemp()) / "vault.yaml"
        if vault_yaml:
            vault_path.write_text(vault_yaml, encoding="utf-8")
        monkeypatch.setattr(
            "thinkweave.synthesis.concepts._seed_ontology_path", lambda: seed_path
        )
        monkeypatch.setattr(
            "thinkweave.synthesis.concepts._vault_ontology_path", lambda: vault_path
        )
        return vault_path

    def test_shifts_proposed_to_canonical_and_creates_hub(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        self._seed_separate_paths(
            monkeypatch, "swe-python:\n  - python\n"
        )
        note = vault.create_note(
            note_type=NoteType.NOTE,
            title="N",
            extra_frontmatter={"proposed_concepts": ["emerging-term"]},
        )
        indexer.rebuild()

        stats = promote_proposed_concept(
            config, "emerging-term", domain="ml-training"
        )

        assert stats["notes_modified"] == 1
        assert stats["ontology_updated"] is True
        assert stats["hub_created"] is True

        from thinkweave.core.vault import parse_frontmatter

        fm, _ = parse_frontmatter(note.read_text(encoding="utf-8"))
        assert "emerging-term" in fm["concepts"]
        assert "proposed_concepts" not in fm

        # Phase 3.1: promote writes the ontology override to the canonical
        # vault/config/ontology.yaml, regardless of where it was read from.
        canonical_path = config.config_dir / "ontology.yaml"
        assert canonical_path.exists()
        body = canonical_path.read_text(encoding="utf-8")
        assert "ml-training" in body
        assert "emerging-term" in body

        # Hub skeleton landed.
        from thinkweave.synthesis.concept_hub import concept_hub_path

        assert concept_hub_path(config, "emerging-term").exists()

    def test_preserves_existing_canonical_concepts(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        self._seed_separate_paths(monkeypatch, "swe-python:\n  - python\n")
        note = vault.create_note(
            note_type=NoteType.NOTE,
            title="N",
            extra_frontmatter={
                "concepts": ["python"],
                "proposed_concepts": ["new-term", "other"],
            },
        )
        indexer.rebuild()

        promote_proposed_concept(config, "new-term", domain="swe-python")

        from thinkweave.core.vault import parse_frontmatter

        fm, _ = parse_frontmatter(note.read_text(encoding="utf-8"))
        assert "python" in fm["concepts"]
        assert "new-term" in fm["concepts"]
        # `other` stays in proposed.
        assert fm["proposed_concepts"] == ["other"]

    def test_idempotent_when_already_canonical(
        self, vault: VaultManager, indexer: Indexer, config: Config, monkeypatch
    ):
        self._seed_separate_paths(
            monkeypatch,
            "swe-python:\n  - python\n  - already-canonical\n",
        )
        vault.create_note(
            note_type=NoteType.NOTE,
            title="N",
            extra_frontmatter={"concepts": ["already-canonical"]},
        )
        indexer.rebuild()

        stats = promote_proposed_concept(
            config, "already-canonical", domain="swe-python"
        )

        # No vault notes carry it as proposed, no ontology update needed
        # (already in seed).
        assert stats["notes_modified"] == 0
        assert stats["ontology_updated"] is False

    def test_rejects_empty_term_or_domain(
        self, config: Config, monkeypatch
    ):
        self._seed_separate_paths(monkeypatch, "swe-python:\n  - python\n")
        with pytest.raises(ValueError):
            promote_proposed_concept(config, "", domain="ml-training")
        with pytest.raises(ValueError):
            promote_proposed_concept(config, "x", domain="")
