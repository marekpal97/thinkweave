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

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.synthesis.landing import (
    LANDING_FILENAMES,
    themes_ledger,
    write_landing_docs,
)
from personal_mem.core.schemas import NOTE_ID_PREFIXES, NoteType
from personal_mem.synthesis.theme_hub import (
    THEME_STATUSES,
    THEME_STATUS_ACTIVE,
    build_theme_frontmatter,
    render_theme_body_skeleton,
)
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


# ---------------------------------------------------------------------------
# Catalyst log parsing + temporal DAG integration
# ---------------------------------------------------------------------------


class TestCatalystLogParsing:
    def test_parse_empty_body(self):
        from personal_mem.synthesis.theme_hub import parse_theme_catalyst_log

        assert parse_theme_catalyst_log("# Title\n\n## Catalyst log\n\n") == []

    def test_parse_entries_with_linkage(self):
        from personal_mem.synthesis.theme_hub import parse_theme_catalyst_log

        body = (
            "# Theme\n\n"
            "## Catalyst log\n\n"
            "- 2026-04-15 · *new* — Hyperscaler capex cut — [[src-x]]\n"
            "- 2026-04-22 · *contradicts 2026-04-15* — MSFT pulls forward — [[src-y]]\n"
        )
        entries = parse_theme_catalyst_log(body)
        assert len(entries) == 2
        assert entries[0].flag == "new"
        assert entries[1].flag == "contradicts"
        assert entries[1].ref == "2026-04-15"


class TestThemeTemporalDAGInLanding:
    def test_themes_md_includes_per_theme_dag(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        from personal_mem.synthesis.theme_hub import render_theme_body_skeleton

        # Build a theme body with a populated catalyst log + linkage.
        body = (
            render_theme_body_skeleton("Test theme")
            + "\n\n"
            + "## Catalyst log\n\n"
            + "- 2026-04-15 · *new* — Catalyst A — [[src-aaa]]\n"
            + "- 2026-04-22 · *contradicts 2026-04-15* — Catalyst B — [[src-bbb]]\n"
        )
        # Note: skeleton already contains a `## Catalyst log` header — having
        # two is fine; _extract_section returns from the first match, so the
        # second (with content) might not be picked up. Reproduce realistic
        # content by replacing the placeholder, not appending.
        skeleton = render_theme_body_skeleton("Test theme")
        before, _, after = skeleton.partition("## Catalyst log")
        body = (
            before
            + "## Catalyst log\n\n"
            + "- 2026-04-15 · *new* — Catalyst A — [[src-aaa]]\n"
            + "- 2026-04-22 · *contradicts 2026-04-15* — Catalyst B — [[src-bbb]]\n\n"
            + "## Open questions\n"
        )

        vault.create_note(
            note_type=NoteType.THEME,
            title="Test theme",
            body=body,
            extra_frontmatter=build_theme_frontmatter("Test theme"),
        )
        indexer.rebuild()

        content = themes_ledger(config)
        # Per-theme DAG appears as a sub-section under the active table.
        assert "### Test theme" in content
        assert "```mermaid" in content
        # contradicts edge is rendered with the dotted arrow.
        assert "-.->" in content

    def test_themes_md_omits_dag_when_no_links(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        from personal_mem.synthesis.theme_hub import render_theme_body_skeleton

        # A theme with one catalyst, no refs, no decisions → no DAG.
        skeleton = render_theme_body_skeleton("Lonely theme")
        before, _, _ = skeleton.partition("## Catalyst log")
        body = (
            before
            + "## Catalyst log\n\n"
            + "- 2026-04-15 · *new* — Lonely catalyst — [[src-aaa]]\n\n"
            + "## Open questions\n"
        )
        vault.create_note(
            note_type=NoteType.THEME,
            title="Lonely theme",
            body=body,
            extra_frontmatter=build_theme_frontmatter("Lonely theme"),
        )
        indexer.rebuild()

        content = themes_ledger(config)
        # Theme appears in table but no per-theme DAG section.
        assert "Lonely theme" in content
        assert "### Lonely theme" not in content


class TestConceptHubEvolutionSection:
    def test_evolution_section_rendered_when_links_present(
        self, vault: VaultManager, config: Config, tmp_path
    ):
        # Direct test of render_concept_hub: build a hub with linked entries,
        # render, assert ## Evolution appears.
        from personal_mem.synthesis.concept_hub import (
            ConceptHub,
            LogEntry,
            render_concept_hub,
        )

        hub = ConceptHub(
            concept="testconcept",
            path=tmp_path / "testconcept.md",
            essence="The seed mental model.",
            log_entries=[
                LogEntry(
                    date="2026-01-01",
                    flag="new",
                    text="seed",
                    citation="n-aaa",
                ),
                LogEntry(
                    date="2026-02-01",
                    flag="extends",
                    ref="2026-01-01",
                    text="extends seed",
                    citation="n-bbb",
                ),
            ],
        )
        rendered = render_concept_hub(hub)
        assert "## Evolution" in rendered
        assert "```mermaid" in rendered

    def test_evolution_section_skipped_when_only_new(
        self, vault: VaultManager, config: Config, tmp_path
    ):
        from personal_mem.synthesis.concept_hub import (
            ConceptHub,
            LogEntry,
            render_concept_hub,
        )

        hub = ConceptHub(
            concept="testconcept2",
            path=tmp_path / "testconcept2.md",
            log_entries=[
                LogEntry(date="2026-01-01", flag="new", text="A", citation="n-1"),
                LogEntry(date="2026-02-01", flag="new", text="B", citation="n-2"),
            ],
        )
        rendered = render_concept_hub(hub)
        assert "## Evolution" not in rendered
