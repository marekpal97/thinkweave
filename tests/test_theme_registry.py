"""Tests for ``synthesis/theme_registry.py`` and its integration points.

Covers:
- rebuild() globs canonical themes, excludes candidates and archives
- load() round-trips: rebuild → load → structures match
- upsert() idempotent: same fields → same YAML
- upsert() updates existing entry (status change)
- is_canonical() True for registry entries, False otherwise
- remove() returns True/False correctly; YAML reflects deletion
- mint_theme_from_signal integration: new thm- upserts a row in themes.yaml
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

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager
from thinkweave.synthesis.theme_registry import (
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
        reg_path = config.vault_root / "config" / "themes.yaml"
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
            (config.vault_root / "config" / "themes.yaml").read_text(encoding="utf-8")
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
            (config.vault_root / "config" / "themes.yaml").read_text(encoding="utf-8")
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
        reg_path = config.vault_root / "config" / "themes.yaml"
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
            config.vault_root / "config" / "themes.yaml"
        ).read_text(encoding="utf-8")

        upsert(config, "thm-aaaa1111", fields)
        yaml_text_second = (
            config.vault_root / "config" / "themes.yaml"
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
            (config.vault_root / "config" / "themes.yaml").read_text(encoding="utf-8")
        )
        assert all(e["id"] != "thm-aaaa1111" for e in data["themes"])


# ---------------------------------------------------------------------------
# mint_theme_from_signal registry integration
# ---------------------------------------------------------------------------


class TestMintRegistryIntegration:
    def _cluster_ids(self, config, vault):
        from thinkweave.synthesis.theme_candidates import detect_signals

        for i in range(3):
            _make_substack_source(vault, f"S{i}")
        _index(config)
        return list(detect_signals(config)[0].cluster_source_ids)

    def test_mint_creates_registry_entry(self, config: Config, vault: VaultManager):
        from thinkweave.core.vault import parse_frontmatter
        from thinkweave.synthesis.theme_candidates import mint_theme_from_signal

        ids = self._cluster_ids(config, vault)
        path = mint_theme_from_signal(
            config, slug="ai-capex-unwind", essence="x",
            cluster_source_ids=ids, cluster_concepts=["ai-capex", "hyperscaler"],
        )
        fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        thm_id = fm["id"]
        reg = load(config)
        assert thm_id in reg
        assert reg[thm_id]["status"] == "active"

    def test_mint_registry_failure_does_not_raise(
        self, config: Config, vault: VaultManager, monkeypatch
    ):
        import thinkweave.synthesis.theme_registry as tr
        from thinkweave.synthesis.theme_candidates import mint_theme_from_signal

        def _bad_upsert(*a, **kw):
            raise RuntimeError("simulated registry failure")

        monkeypatch.setattr(tr, "upsert", _bad_upsert)
        ids = self._cluster_ids(config, vault)
        path = mint_theme_from_signal(
            config, slug="safe-theme", essence="x",
            cluster_source_ids=ids, cluster_concepts=["ai-capex"],
        )
        assert path.exists()


# ---------------------------------------------------------------------------
# dream.apply theme_mints registry sync
# ---------------------------------------------------------------------------


class TestDreamApplyMintRegistry:
    def test_theme_mint_via_apply_registers(
        self, config: Config, vault: VaultManager
    ):
        from thinkweave.operations.dream import apply
        from thinkweave.synthesis.theme_candidates import detect_signals

        for i in range(3):
            _make_substack_source(vault, f"S{i}")
        _index(config)
        ids = list(detect_signals(config)[0].cluster_source_ids)
        plan = {
            "theme_mints": [
                {"slug": "ai-capex-unwind",
                 "essence": "Hyperscaler capex pulls back through 2026.",
                 "source_ids": ids, "concepts": ["ai-capex", "hyperscaler"]}
            ]
        }
        result = apply(config, plan=plan, project="t")
        assert result.themes_minted == 1
        reg = load(config)
        assert any(v.get("status") == "active" for v in reg.values())


class TestCreateNoteThemeRefGate:
    def test_unknown_thm_ref_dropped_and_warns(
        self, config: Config, vault: VaultManager, caplog
    ):
        from thinkweave.operations.notes import create_note

        # Registry is empty — any thm- ref is unknown.
        with caplog.at_level(logging.WARNING, logger="thinkweave.operations.notes"):
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
        from thinkweave.operations.notes import create_note

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
        from thinkweave.operations.notes import create_note

        with caplog.at_level(logging.WARNING, logger="thinkweave.operations.notes"):
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
        from thinkweave.operations.notes import create_note

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
        from thinkweave.operations.notes import create_note

        with caplog.at_level(logging.WARNING, logger="thinkweave.operations.notes"):
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
        from thinkweave.operations.notes import create_note

        upsert(config, "thm-known0001", {"slug": "known", "status": "active"})

        with caplog.at_level(logging.WARNING, logger="thinkweave.operations.notes"):
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
        """End-to-end: mint a theme via mint_theme_from_signal, confirm it
        enters the registry, then create a note that relates_to it and
        verify the ref survives the create_note gate."""
        from thinkweave.core.vault import parse_frontmatter
        from thinkweave.operations.notes import create_note
        from thinkweave.synthesis.theme_candidates import (
            detect_signals,
            mint_theme_from_signal,
        )

        for i in range(3):
            _make_substack_source(vault, f"SRC{i}")
        _index(config)
        ids = list(detect_signals(config)[0].cluster_source_ids)
        theme_path = mint_theme_from_signal(
            config, slug="ai-capex-unwind", essence="x",
            cluster_source_ids=ids, cluster_concepts=["ai-capex", "hyperscaler"],
        )
        fm, _ = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
        thm_id = fm["id"]
        assert is_canonical(config, thm_id)

        note = create_note(
            config,
            note_type=NoteType.NOTE,
            title="Citing the theme",
            extra_frontmatter={"relates_to": [thm_id], "concepts": []},
        ).note
        relates = note.frontmatter.get("relates_to") or []
        assert thm_id in relates
