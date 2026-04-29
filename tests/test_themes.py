"""Tests for themes — first-class global narrative aggregators (Workstream B).

Covers:
- NoteType.THEME exists and has prefix `thm`.
- VaultManager.create_note routes themes to vault/themes/ regardless of project.
- themes.py frontmatter builder + body skeleton.
- landing.py themes_ledger + write_landing_docs(docs="themes") writes
  vault/THEMES.md.
- LANDING_FILENAMES includes THEMES.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.config import Config
from personal_mem.indexer import Indexer
from personal_mem.landing import (
    LANDING_FILENAMES,
    themes_ledger,
    write_landing_docs,
)
from personal_mem.schemas import NOTE_ID_PREFIXES, NoteType
from personal_mem.themes import (
    THEME_STATUSES,
    THEME_STATUS_ACTIVE,
    build_theme_frontmatter,
    render_theme_body_skeleton,
)
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


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestThemeSchema:
    def test_note_type_theme_exists(self):
        assert NoteType.THEME.value == "theme"

    def test_theme_id_prefix_is_thm(self):
        assert NOTE_ID_PREFIXES[NoteType.THEME] == "thm"

    def test_status_constants(self):
        assert THEME_STATUS_ACTIVE == "active"
        assert "active" in THEME_STATUSES
        assert "dormant" in THEME_STATUSES
        assert "resolved" in THEME_STATUSES


# ---------------------------------------------------------------------------
# Vault routing
# ---------------------------------------------------------------------------


class TestThemeVaultRouting:
    def test_theme_lands_at_vault_root(
        self, vault: VaultManager, vault_dir: Path
    ):
        path = vault.create_note(
            note_type=NoteType.THEME,
            title="AI capex unwind 2026",
            body=render_theme_body_skeleton("AI capex unwind 2026"),
            extra_frontmatter=build_theme_frontmatter(
                "AI capex unwind 2026",
                project="trade_ideas",
                concepts=["finance/regime", "finance/structure"],
            ),
        )

        # Themes go to vault/themes/, NOT projects/{project}/themes/.
        assert path.parent == vault_dir / "themes"
        assert "projects/trade_ideas" not in str(path)

    def test_theme_id_has_thm_prefix(self, vault: VaultManager):
        path = vault.create_note(
            note_type=NoteType.THEME,
            title="test theme",
            extra_frontmatter=build_theme_frontmatter("test theme"),
        )
        note = vault.read_note(path)
        assert note.id.startswith("thm-")

    def test_theme_ignores_project_for_filing(
        self, vault: VaultManager, vault_dir: Path
    ):
        """A theme with `project: X` still lives at vault/themes/, not projects/X/."""
        path = vault.create_note(
            note_type=NoteType.THEME,
            title="cross-project theme",
            project="some_project",
            extra_frontmatter=build_theme_frontmatter(
                "cross-project theme", project="some_project"
            ),
        )
        assert path.parent == vault_dir / "themes"
        # But the project field is preserved in frontmatter.
        note = vault.read_note(path)
        assert note.frontmatter.get("project") == "some_project"


# ---------------------------------------------------------------------------
# Frontmatter builder
# ---------------------------------------------------------------------------


class TestThemeFrontmatter:
    def test_minimal_frontmatter(self):
        fm = build_theme_frontmatter("AI capex unwind 2026")
        assert fm["title"] == "AI capex unwind 2026"
        assert fm["status"] == "active"
        assert fm["concepts"] == []
        assert fm["relates_to"] == []
        assert "project" not in fm

    def test_full_frontmatter(self):
        fm = build_theme_frontmatter(
            "AI capex unwind 2026",
            project="trade_ideas",
            concepts=["finance/regime", "finance/structure"],
            relates_to=["thm-aaaa1111"],
            status="dormant",
        )
        assert fm["project"] == "trade_ideas"
        assert fm["concepts"] == ["finance/regime", "finance/structure"]
        assert fm["relates_to"] == ["thm-aaaa1111"]
        assert fm["status"] == "dormant"

    def test_extra_fields_pass_through(self):
        fm = build_theme_frontmatter("X", custom_field="value")
        assert fm["custom_field"] == "value"

    def test_body_skeleton_has_three_sections(self):
        body = render_theme_body_skeleton("X")
        assert "## Essence" in body
        assert "## Catalyst log" in body
        assert "## Open questions" in body


# ---------------------------------------------------------------------------
# Landing — THEMES.md
# ---------------------------------------------------------------------------


class TestThemesLanding:
    def test_themes_in_landing_filenames(self):
        assert "THEMES.md" in LANDING_FILENAMES

    def test_empty_vault_renders_placeholder(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        indexer.rebuild()
        content = themes_ledger(config)
        assert "No themes recorded yet" in content

    def test_active_theme_appears_in_table(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            note_type=NoteType.THEME,
            title="AI capex unwind 2026",
            body=render_theme_body_skeleton("AI capex unwind 2026"),
            extra_frontmatter=build_theme_frontmatter(
                "AI capex unwind 2026",
                project="trade_ideas",
                concepts=["finance/regime"],
            ),
        )
        indexer.rebuild()
        content = themes_ledger(config)

        assert "## Active (1)" in content
        assert "AI capex unwind 2026" in content
        assert "trade_ideas" in content
        assert "Last catalyst" in content

    def test_dormant_themes_in_collapsed_section(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        vault.create_note(
            note_type=NoteType.THEME,
            title="Dormant theme",
            extra_frontmatter=build_theme_frontmatter(
                "Dormant theme", status="dormant"
            ),
        )
        indexer.rebuild()
        content = themes_ledger(config)
        assert "<details><summary>Dormant (1)</summary>" in content

    def test_write_landing_docs_themes(
        self, vault: VaultManager, indexer: Indexer, config: Config, vault_dir: Path
    ):
        vault.create_note(
            note_type=NoteType.THEME,
            title="A theme",
            extra_frontmatter=build_theme_frontmatter("A theme"),
        )
        indexer.rebuild()

        # No project required for themes.
        written = write_landing_docs(config, project="", docs="themes")
        assert "THEMES.md" in written
        assert written["THEMES.md"] == vault_dir / "THEMES.md"
        assert vault_dir.joinpath("THEMES.md").exists()
        text = vault_dir.joinpath("THEMES.md").read_text()
        assert "A theme" in text

    def test_unknown_doc_type_rejected(
        self, vault: VaultManager, config: Config
    ):
        with pytest.raises(ValueError):
            write_landing_docs(config, project="x", docs="bogus")
