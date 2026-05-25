"""Tests for ``synthesis/theme_registry.py`` and its integration points.

Covers:
- rebuild() globs canonical themes, excludes candidates and archives
- load() round-trips: rebuild → load → structures match
- upsert() idempotent: same fields → same YAML
- upsert() updates existing entry (status change)
- is_canonical() True for registry entries, False otherwise
- remove() returns True/False correctly; YAML reflects deletion
- promote_candidate integration: new thm- appears in themes.yaml
- dream.apply theme_status_changes updates registry
- create_note: unknown thm- refs dropped + warning logged
- create_note: known thm- ref preserved
- create_note: empty/missing relates_to → no-op
- Integration: fresh vault mint→registry→create_note round-trip
"""

from __future__ import annotations

import logging
import yaml
from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager
from personal_mem.synthesis.theme_registry import (
    is_canonical,
    load,
    rebuild,
    remove,
    upsert,
)


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


def _index(config: Config) -> None:
    idx = Indexer(config=config)
    idx.rebuild(full=True)
    idx.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_canonical_theme(
    config: Config,
    thm_id: str,
    slug: str,
    status: str = "active",
    concepts: list[str] | None = None,
    project: str = "",
    parent: str = "",
) -> Path:
    """Write a minimal canonical theme file at vault/themes/<thm_id>-<slug>.md."""
    themes_dir = config.vault_root / "themes"
    themes_dir.mkdir(parents=True, exist_ok=True)
    path = themes_dir / f"{thm_id}-{slug}.md"
    concepts_line = f"concepts: [{', '.join(concepts or [])}]"
    lines = [
        "---",
        "type: theme",
        f"id: {thm_id}",
        f'title: "{slug.replace("-", " ")}"',
        f"status: {status}",
        concepts_line,
    ]
    if project:
        lines.append(f"project: {project}")
    if parent:
        lines.append(f"parent: {parent}")
    lines += ["---", "", f"# {slug}", "", "## Essence", "", "content", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_candidate_theme(config: Config, cand_id: str) -> Path:
    """Write a minimal candidate stub at vault/themes/_candidates/."""
    cdir = config.vault_root / "themes" / "_candidates"
    cdir.mkdir(parents=True, exist_ok=True)
    path = cdir / f"{cand_id}-test.md"
    path.write_text(
        "---\ntype: theme\nid: " + cand_id + "\nstatus: candidate\n---\n",
        encoding="utf-8",
    )
    return path


def _write_archived_theme(config: Config, cand_id: str) -> Path:
    """Write a minimal candidate stub at vault/themes/_candidates/_archive/."""
    adir = config.vault_root / "themes" / "_candidates" / "_archive"
    adir.mkdir(parents=True, exist_ok=True)
    path = adir / f"{cand_id}-arch.md"
    path.write_text(
        "---\ntype: theme\nid: " + cand_id + "\nstatus: candidate\n---\n",
        encoding="utf-8",
    )
    return path


def _make_substack_source(vault: VaultManager, title: str) -> Path:
    return vault.create_note(
        note_type=NoteType.SOURCE,
        title=title,
        body=f"# {title}",
        extra_frontmatter={
            "source_type": "substack",
            "concepts": ["ai-capex", "hyperscaler"],
        },
    )


# ---------------------------------------------------------------------------
# rebuild()
# ---------------------------------------------------------------------------


class TestRebuild:
    def test_produces_yaml_with_canonical_themes(
        self, config: Config, vault: VaultManager
    ):
        _write_canonical_theme(
            config, "thm-aaaa1111", "test-theme", concepts=["finance-regime"]
        )
        _write_canonical_theme(
            config, "thm-bbbb2222", "other-theme", concepts=["geopolitics"]
        )

        n = rebuild(config)

        assert n == 2
        reg_path = config.vault_root / ".mem" / "themes.yaml"
        assert reg_path.exists()
        data = yaml.safe_load(reg_path.read_text(encoding="utf-8"))
        ids = {e["id"] for e in data["themes"]}
        assert ids == {"thm-aaaa1111", "thm-bbbb2222"}

    def test_excludes_candidates(self, config: Config, vault: VaultManager):
        _write_canonical_theme(config, "thm-aaaa1111", "canonical")
        _write_candidate_theme(config, "cand-deadbeef")

        n = rebuild(config)

        assert n == 1
        data = yaml.safe_load(
            (config.vault_root / ".mem" / "themes.yaml").read_text(encoding="utf-8")
        )
        ids = [e["id"] for e in data["themes"]]
        assert "cand-deadbeef" not in ids

    def test_excludes_archived_candidates(self, config: Config, vault: VaultManager):
        _write_canonical_theme(config, "thm-aaaa1111", "canonical")
        _write_archived_theme(config, "cand-archived")

        n = rebuild(config)

        assert n == 1

    def test_empty_vault_writes_empty_list(self, config: Config, vault: VaultManager):
        n = rebuild(config)
        assert n == 0
        data = yaml.safe_load(
            (config.vault_root / ".mem" / "themes.yaml").read_text(encoding="utf-8")
        )
        assert data["themes"] == []

    def test_preserves_status_and_concepts(
        self, config: Config, vault: VaultManager
    ):
        _write_canonical_theme(
            config,
            "thm-cccc3333",
            "dormant-theme",
            status="dormant",
            concepts=["macro-trading", "geopolitics"],
            project="finance",
        )

        rebuild(config)
        reg = load(config)

        entry = reg["thm-cccc3333"]
        assert entry["status"] == "dormant"
        assert set(entry["concepts"]) == {"macro-trading", "geopolitics"}
        assert entry["project"] == "finance"


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


class TestLoad:
    def test_round_trips_via_rebuild(self, config: Config, vault: VaultManager):
        _write_canonical_theme(
            config,
            "thm-aaaa1111",
            "test-theme",
            concepts=["finance-regime"],
            project="trading",
        )
        _write_canonical_theme(
            config, "thm-bbbb2222", "other", concepts=["geopolitics"], parent="thm-aaaa1111"
        )

        rebuild(config)
        reg = load(config)

        assert "thm-aaaa1111" in reg
        assert "thm-bbbb2222" in reg
        assert reg["thm-aaaa1111"]["project"] == "trading"
        assert reg["thm-bbbb2222"]["parent"] == "thm-aaaa1111"

    def test_returns_empty_dict_when_file_missing(
        self, config: Config, vault: VaultManager
    ):
        assert load(config) == {}

    def test_returns_empty_dict_on_empty_file(
        self, config: Config, vault: VaultManager
    ):
        reg_path = config.vault_root / ".mem" / "themes.yaml"
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        reg_path.write_text("", encoding="utf-8")
        assert load(config) == {}


# ---------------------------------------------------------------------------
# upsert()
# ---------------------------------------------------------------------------


class TestUpsert:
    def test_idempotent_same_fields(self, config: Config, vault: VaultManager):
        fields = {
            "slug": "ai-capex-unwind",
            "status": "active",
            "concepts": ["ai-capex"],
            "parent": None,
            "project": "",
        }
        upsert(config, "thm-aaaa1111", fields)
        yaml_text_first = (
            config.vault_root / ".mem" / "themes.yaml"
        ).read_text(encoding="utf-8")

        upsert(config, "thm-aaaa1111", fields)
        yaml_text_second = (
            config.vault_root / ".mem" / "themes.yaml"
        ).read_text(encoding="utf-8")

        assert yaml_text_first == yaml_text_second

    def test_updates_existing_entry_status(
        self, config: Config, vault: VaultManager
    ):
        upsert(config, "thm-aaaa1111", {"slug": "x", "status": "active"})
        upsert(config, "thm-aaaa1111", {"slug": "x", "status": "dormant"})

        reg = load(config)
        assert reg["thm-aaaa1111"]["status"] == "dormant"

    def test_inserts_new_entry(self, config: Config, vault: VaultManager):
        upsert(config, "thm-aaaa1111", {"slug": "first", "status": "active"})
        upsert(config, "thm-bbbb2222", {"slug": "second", "status": "active"})

        reg = load(config)
        assert "thm-aaaa1111" in reg
        assert "thm-bbbb2222" in reg

    def test_fills_defaults_for_missing_fields(
        self, config: Config, vault: VaultManager
    ):
        upsert(config, "thm-aaaa1111", {})  # no fields at all

        reg = load(config)
        entry = reg["thm-aaaa1111"]
        assert entry["id"] == "thm-aaaa1111"
        assert entry["slug"] == ""
        assert entry["status"] == "active"
        assert entry["concepts"] == []
        assert entry["parent"] is None
        assert entry["project"] == ""

    def test_partial_update_preserves_existing_fields(
        self, config: Config, vault: VaultManager
    ):
        """Partial upsert (just status) must preserve slug, concepts, etc."""
        upsert(
            config,
            "thm-aaaa1111",
            {
                "slug": "ai-capex-unwind",
                "status": "active",
                "concepts": ["ai-capex", "hyperscaler"],
                "project": "finance",
            },
        )
        # Now only update status — other fields should survive.
        upsert(config, "thm-aaaa1111", {"status": "dormant"})

        reg = load(config)
        entry = reg["thm-aaaa1111"]
        assert entry["status"] == "dormant"
        assert entry["slug"] == "ai-capex-unwind"
        assert set(entry["concepts"]) == {"ai-capex", "hyperscaler"}
        assert entry["project"] == "finance"


# ---------------------------------------------------------------------------
# is_canonical()
# ---------------------------------------------------------------------------


class TestIsCanonical:
    def test_returns_true_for_registered_theme(
        self, config: Config, vault: VaultManager
    ):
        upsert(config, "thm-aaaa1111", {"slug": "x"})
        assert is_canonical(config, "thm-aaaa1111") is True

    def test_returns_false_for_unknown_id(
        self, config: Config, vault: VaultManager
    ):
        upsert(config, "thm-aaaa1111", {"slug": "x"})
        assert is_canonical(config, "thm-nonexistent") is False

    def test_returns_false_when_registry_missing(
        self, config: Config, vault: VaultManager
    ):
        assert is_canonical(config, "thm-aaaa1111") is False


# ---------------------------------------------------------------------------
# remove()
# ---------------------------------------------------------------------------


class TestRemove:
    def test_returns_true_and_deletes_entry(
        self, config: Config, vault: VaultManager
    ):
        upsert(config, "thm-aaaa1111", {"slug": "x"})
        upsert(config, "thm-bbbb2222", {"slug": "y"})

        result = remove(config, "thm-aaaa1111")

        assert result is True
        reg = load(config)
        assert "thm-aaaa1111" not in reg
        assert "thm-bbbb2222" in reg  # sibling untouched

    def test_returns_false_for_nonexistent_entry(
        self, config: Config, vault: VaultManager
    ):
        upsert(config, "thm-aaaa1111", {"slug": "x"})
        assert remove(config, "thm-ghost") is False

    def test_yaml_reflects_deletion(self, config: Config, vault: VaultManager):
        upsert(config, "thm-aaaa1111", {"slug": "x"})
        remove(config, "thm-aaaa1111")

        data = yaml.safe_load(
            (config.vault_root / ".mem" / "themes.yaml").read_text(encoding="utf-8")
        )
        assert all(e["id"] != "thm-aaaa1111" for e in data["themes"])


# ---------------------------------------------------------------------------
# promote_candidate integration
# ---------------------------------------------------------------------------


class TestPromoteCandidateIntegration:
    def test_promote_creates_registry_entry(
        self, config: Config, vault: VaultManager
    ):
        from personal_mem.synthesis.theme_candidates import (
            promote_candidate,
            scan_candidates,
        )

        for i in range(3):
            _make_substack_source(vault, f"S{i}")
        _index(config)
        scan_candidates(config, source_type="substack")

        cdir = config.vault_root / "themes" / "_candidates"
        cand_path = next(cdir.glob("cand-*.md"))
        cand_id = cand_path.stem.split("-")[0] + "-" + cand_path.stem.split("-")[1]

        target_path = promote_candidate(
            config,
            cand_id,
            title="AI Capex Unwind 2026",
        )

        assert target_path.exists()
        reg = load(config)
        # Extract the thm-id from the generated file's frontmatter
        from personal_mem.core.vault import parse_frontmatter
        fm, _ = parse_frontmatter(target_path.read_text(encoding="utf-8"))
        thm_id = fm["id"]

        assert thm_id in reg, (
            f"{thm_id} not found in registry after promote_candidate"
        )
        assert reg[thm_id]["status"] == "active"

    def test_promote_registry_failure_does_not_raise(
        self, config: Config, vault: VaultManager, monkeypatch
    ):
        """Registry update failures must not propagate from promote_candidate."""
        from personal_mem.synthesis.theme_candidates import (
            promote_candidate,
            scan_candidates,
        )
        import personal_mem.synthesis.theme_registry as tr

        def _bad_upsert(*a, **kw):
            raise RuntimeError("simulated registry failure")

        monkeypatch.setattr(tr, "upsert", _bad_upsert)

        for i in range(3):
            _make_substack_source(vault, f"T{i}")
        _index(config)
        scan_candidates(config, source_type="substack")

        cdir = config.vault_root / "themes" / "_candidates"
        cand_path = next(cdir.glob("cand-*.md"))
        cand_id = cand_path.stem.split("-")[0] + "-" + cand_path.stem.split("-")[1]

        # Should not raise even though registry is broken.
        result_path = promote_candidate(config, cand_id, title="Safe Theme")
        assert result_path.exists()


# ---------------------------------------------------------------------------
# dream.apply theme_status_changes
# ---------------------------------------------------------------------------


class TestDreamApplyRegistrySync:
    def test_theme_status_change_updates_registry(
        self, config: Config, vault: VaultManager
    ):
        from personal_mem.operations.dream import apply

        _write_canonical_theme(
            config, "thm-feed1234", "test-theme", concepts=["finance-regime"]
        )
        upsert(config, "thm-feed1234", {"status": "active", "slug": "test-theme"})
        _index(config)

        plan = {
            "theme_status_changes": [
                {
                    "theme_id": "thm-feed1234",
                    "new_status": "dormant",
                    "reason": "no catalysts",
                }
            ]
        }
        result = apply(config, plan=plan, project="")

        assert result.theme_status_changes == 1
        reg = load(config)
        assert reg["thm-feed1234"]["status"] == "dormant"

    def test_theme_status_change_registry_failure_does_not_cascade(
        self, config: Config, vault: VaultManager, monkeypatch
    ):
        from personal_mem.operations.dream import apply
        import personal_mem.synthesis.theme_registry as tr

        call_count = {"n": 0}

        def _bad_upsert(*a, **kw):
            call_count["n"] += 1
            raise RuntimeError("simulated registry failure")

        monkeypatch.setattr(tr, "upsert", _bad_upsert)

        _write_canonical_theme(
            config, "thm-feed5678", "fragile-theme", concepts=["finance-regime"]
        )
        _index(config)

        plan = {
            "theme_status_changes": [
                {"theme_id": "thm-feed5678", "new_status": "dormant"}
            ]
        }
        result = apply(config, plan=plan, project="")

        # The status change still succeeded even though registry update failed.
        assert result.theme_status_changes == 1
        assert result.errors == [] or not any(
            "registry" in e.lower() for e in result.errors
        )
        # Registry upsert was called (even though it raised)
        assert call_count["n"] >= 1


# ---------------------------------------------------------------------------
# create_note soft validation gate
# ---------------------------------------------------------------------------


class TestCreateNoteThemeRefGate:
    def test_unknown_thm_ref_dropped_and_warns(
        self, config: Config, vault: VaultManager, caplog
    ):
        from personal_mem.operations.notes import create_note

        # Registry is empty — any thm- ref is unknown.
        with caplog.at_level(logging.WARNING, logger="personal_mem.operations.notes"):
            note = create_note(
                config,
                note_type=NoteType.NOTE,
                title="Test note",
                extra_frontmatter={
                    "relates_to": ["thm-ghost"],
                    "concepts": [],
                },
            ).note

        # The unknown ref should be dropped.
        fm = note.frontmatter
        relates = fm.get("relates_to") or []
        assert "thm-ghost" not in relates, (
            "Unknown thm- ref must be dropped from relates_to"
        )
        # A warning was logged.
        assert any("thm-ghost" in r.message for r in caplog.records), (
            "Expected a warning about the unknown thm- ref"
        )

    def test_known_thm_ref_preserved(
        self, config: Config, vault: VaultManager
    ):
        from personal_mem.operations.notes import create_note

        # Register a theme first.
        upsert(config, "thm-real1234", {"slug": "real-theme", "status": "active"})

        note = create_note(
            config,
            note_type=NoteType.NOTE,
            title="Note with valid ref",
            extra_frontmatter={
                "relates_to": ["thm-real1234"],
                "concepts": [],
            },
        ).note

        fm = note.frontmatter
        relates = fm.get("relates_to") or []
        assert "thm-real1234" in relates, (
            "Known thm- ref must survive the gate"
        )

    def test_empty_relates_to_no_warning(
        self, config: Config, vault: VaultManager, caplog
    ):
        from personal_mem.operations.notes import create_note

        with caplog.at_level(logging.WARNING, logger="personal_mem.operations.notes"):
            create_note(
                config,
                note_type=NoteType.NOTE,
                title="Note without relates_to",
                extra_frontmatter={"concepts": []},
            )

        thm_warnings = [
            r for r in caplog.records
            if "relates_to" in r.message.lower() or "thm-" in r.message
        ]
        assert thm_warnings == [], "No warning expected for missing relates_to"

    def test_missing_relates_to_no_crash(
        self, config: Config, vault: VaultManager
    ):
        from personal_mem.operations.notes import create_note

        # Should not crash — no relates_to key at all.
        note = create_note(
            config,
            note_type=NoteType.NOTE,
            title="Plain note",
            extra_frontmatter=None,
        ).note
        assert note.title == "Plain note"

    def test_non_thm_relates_to_not_filtered(
        self, config: Config, vault: VaultManager, caplog
    ):
        """Non-thm- refs (e.g. plain note IDs) pass through unfiltered."""
        from personal_mem.operations.notes import create_note

        with caplog.at_level(logging.WARNING, logger="personal_mem.operations.notes"):
            note = create_note(
                config,
                note_type=NoteType.NOTE,
                title="Note with non-theme ref",
                extra_frontmatter={"relates_to": ["n-somenoteid"], "concepts": []},
            ).note

        fm = note.frontmatter
        relates = fm.get("relates_to") or []
        assert "n-somenoteid" in relates

    def test_mixed_refs_partial_filter(
        self, config: Config, vault: VaultManager, caplog
    ):
        """Known thm- survives; unknown thm- is dropped; non-thm- passes through."""
        from personal_mem.operations.notes import create_note

        upsert(config, "thm-known0001", {"slug": "known", "status": "active"})

        with caplog.at_level(logging.WARNING, logger="personal_mem.operations.notes"):
            note = create_note(
                config,
                note_type=NoteType.NOTE,
                title="Mixed refs",
                extra_frontmatter={
                    "relates_to": ["thm-known0001", "thm-ghost", "n-regular"],
                    "concepts": [],
                },
            ).note

        relates = note.frontmatter.get("relates_to") or []
        assert "thm-known0001" in relates
        assert "thm-ghost" not in relates
        assert "n-regular" in relates


# ---------------------------------------------------------------------------
# Integration test: fresh vault → mint → registry → create_note
# ---------------------------------------------------------------------------


class TestIntegrationFreshVault:
    def test_mint_then_create_note_with_valid_ref(
        self, config: Config, vault: VaultManager
    ):
        """End-to-end: mint a theme via promote_candidate, confirm it enters
        the registry, then create a note that relates_to that theme and
        verify the ref survives the gate."""
        from personal_mem.synthesis.theme_candidates import (
            promote_candidate,
            scan_candidates,
        )
        from personal_mem.operations.notes import create_note
        from personal_mem.core.vault import parse_frontmatter

        # 1. Seed enough sources for a candidate.
        for i in range(3):
            _make_substack_source(vault, f"SRC{i}")
        _index(config)
        scan_candidates(config, source_type="substack")

        # 2. Promote the candidate → canonical theme.
        cdir = config.vault_root / "themes" / "_candidates"
        cand_path = next(cdir.glob("cand-*.md"))
        cand_id = cand_path.stem.split("-")[0] + "-" + cand_path.stem.split("-")[1]
        theme_path = promote_candidate(
            config, cand_id, title="AI Capex Unwind 2026"
        )
        assert theme_path.exists()

        # 3. Verify the registry was updated.
        fm, _ = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
        thm_id = fm["id"]
        assert is_canonical(config, thm_id), (
            f"Registry must contain {thm_id} after promote_candidate"
        )

        # 4. Create a note with relates_to the canonical theme.
        note = create_note(
            config,
            note_type=NoteType.NOTE,
            title="Citing the theme",
            extra_frontmatter={
                "relates_to": [thm_id],
                "concepts": [],
            },
        ).note

        # 5. The ref must survive.
        relates = note.frontmatter.get("relates_to") or []
        assert thm_id in relates, (
            f"{thm_id} should survive the create_note gate (it's registered)"
        )
