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
                concepts=["finance-regime", "finance-structure"],
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
            concepts=["finance-regime", "finance-structure"],
            relates_to=["thm-aaaa1111"],
            status="dormant",
        )
        assert fm["project"] == "trade_ideas"
        assert fm["concepts"] == ["finance-regime", "finance-structure"]
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

    def test_parent_omitted_when_empty(self):
        fm = build_theme_frontmatter("Standalone theme")
        assert "parent" not in fm

    def test_parent_field_written_when_supplied(self):
        fm = build_theme_frontmatter(
            "Memory chip supercycle 2026",
            parent="thm-aaaa1111",
        )
        assert fm["parent"] == "thm-aaaa1111"


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
                concepts=["finance-regime"],
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


class TestCatalogSection:
    """Active themes get a `## Catalog (active)` section after the table.

    Stable shape so the news triage helper can locate-by-heading and
    pass it as cached context: `### {title}` heading, bullet block with
    id/parent/concepts/last-catalyst, blockquote essence excerpt.
    """

    def test_catalog_renders_per_active_theme(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        from personal_mem.synthesis.theme_hub import (
            render_theme_body_skeleton,
        )

        # A theme with a real essence (skeleton placeholder is intentionally
        # filtered out — we don't pollute the catalog with prompt text).
        skeleton = render_theme_body_skeleton("AI capex unwind 2026")
        before, _, after = skeleton.partition("## Essence")
        body = (
            before
            + "## Essence\n\n"
            + "Hyperscalers pulled forward GPU spend in 2024-2025; "
            + "sustained ROI hasn't materialized; 2026 is the year "
            + "that thesis is re-tested.\n\n"
            + "## Catalyst log\n\n"
            + "## Open questions\n"
        )
        vault.create_note(
            note_type=NoteType.THEME,
            title="AI capex unwind 2026",
            body=body,
            extra_frontmatter=build_theme_frontmatter(
                "AI capex unwind 2026",
                concepts=["semiconductors", "thematic-investing"],
            ),
        )
        indexer.rebuild()
        content = themes_ledger(config)

        # Catalog section exists.
        assert "## Catalog (active)" in content
        # Card heading.
        assert "### AI capex unwind 2026" in content
        # Concepts bullet rendered with backticks.
        assert "`semiconductors`" in content
        assert "`thematic-investing`" in content
        # Essence excerpt rendered as blockquote.
        assert "> Hyperscalers pulled forward GPU spend" in content
        # Top-level marker present.
        assert "_(top-level)_" in content

    def test_catalog_skips_skeleton_essence(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        """A theme with the unedited skeleton essence (italic placeholder
        text) gets card structure but no blockquote — we don't pollute
        the catalog with prompt instructions."""
        from personal_mem.synthesis.theme_hub import (
            render_theme_body_skeleton,
        )

        body = render_theme_body_skeleton("Unedited theme")
        vault.create_note(
            note_type=NoteType.THEME,
            title="Unedited theme",
            body=body,
            extra_frontmatter=build_theme_frontmatter("Unedited theme"),
        )
        indexer.rebuild()
        content = themes_ledger(config)

        assert "### Unedited theme" in content
        # Skeleton text starts with `_Replace with…` — must not bleed
        # into the catalog. The blockquote is omitted entirely.
        assert "_Replace with the working thesis" not in content
        assert "> _Replace" not in content

    def test_catalog_renders_parent_link(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        """Child themes show their parent as a wikilink in the card,
        not as `(top-level)`."""
        parent_path = vault.create_note(
            note_type=NoteType.THEME,
            title="Parent theme",
            extra_frontmatter=build_theme_frontmatter("Parent theme"),
        )
        parent_id = vault.read_note(parent_path).id

        vault.create_note(
            note_type=NoteType.THEME,
            title="Child theme",
            extra_frontmatter=build_theme_frontmatter(
                "Child theme",
                parent=parent_id,
            ),
        )
        indexer.rebuild()
        content = themes_ledger(config)

        # Find the child's card and check the parent line.
        child_marker = "### Child theme"
        assert child_marker in content
        idx = content.index(child_marker)
        # Within the next ~400 chars (card body), parent line must
        # reference the parent, not "(top-level)". Links are path-based
        # (resolve by file location) with the parent id as display text.
        card_body = content[idx : idx + 400]
        assert f"|{parent_id}]]" in card_body
        assert "(Parent theme)" in card_body


class TestThemeHierarchy:
    """Two-tier hierarchy: parent themes group narrower children.

    Mirrors how the concept ontology nests broad → narrow. The
    `parent: thm-X` frontmatter field is the only edge; `themes_ledger`
    renders parents at depth 0 and children indented with `↳ `. Stable
    order is preserved; orphaned children (parent not in the rendered
    set) are promoted to roots so nothing is dropped.
    """

    def test_landing_renders_child_indented_under_parent(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        parent = vault.create_note(
            note_type=NoteType.THEME,
            title="AI capex unwind 2026",
            extra_frontmatter=build_theme_frontmatter("AI capex unwind 2026"),
        )
        # Read the parent's id so we can wire the child to it.
        parent_note = vault.read_note(parent)
        parent_id = parent_note.id

        vault.create_note(
            note_type=NoteType.THEME,
            title="Memory chip supercycle 2026",
            extra_frontmatter=build_theme_frontmatter(
                "Memory chip supercycle 2026",
                parent=parent_id,
            ),
        )
        indexer.rebuild()
        content = themes_ledger(config)

        # Parent appears un-indented; child has the ↳ marker.
        assert "AI capex unwind 2026" in content
        assert "↳ " in content
        # The child link cell uses the prefix.
        assert "↳ [[themes/" in content
        # Parent comes before child in the table.
        assert content.index("AI capex unwind 2026") < content.index(
            "Memory chip supercycle 2026"
        )

    def test_orphan_child_renders_at_root(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        """If a child's parent is missing from the rendered set, the
        child still appears (at depth 0) — never silently dropped."""
        vault.create_note(
            note_type=NoteType.THEME,
            title="Orphan child",
            extra_frontmatter=build_theme_frontmatter(
                "Orphan child",
                parent="thm-doesnotexist",
            ),
        )
        indexer.rebuild()
        content = themes_ledger(config)

        assert "Orphan child" in content
        # No indent prefix on the orphan — promoted to root.
        # (We can't assert ↳ absent because other tests may have ↳;
        # instead, verify the link line for "Orphan child" doesn't
        # carry the ↳ prefix.)
        for line in content.split("\n"):
            if "Orphan child" in line and line.startswith("|"):
                assert "↳" not in line


class TestThemeMintParent:
    """`mint_theme_from_signal(parent=...)` writes parent into the new
    theme's frontmatter; omitting it leaves the field absent."""

    def _cluster_ids(self, config, vault):
        from personal_mem.core.indexer import Indexer
        from personal_mem.core.vault import parse_frontmatter

        paths = []
        for n in range(3):
            paths.append(
                vault.create_note(
                    note_type=NoteType.SOURCE,
                    title=f"S{n}",
                    extra_frontmatter={
                        "source_type": "substack",
                        "concepts": ["finance-regime", "liquidity"],
                    },
                )
            )
        idx = Indexer(config=config)
        idx.rebuild()
        idx.close()
        ids = []
        for p in paths:
            fm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
            ids.append(fm["id"])
        return ids

    def test_parent_written(self, config, vault):
        from personal_mem.core.vault import parse_frontmatter
        from personal_mem.synthesis.theme_candidates import mint_theme_from_signal

        ids = self._cluster_ids(config, vault)
        path = mint_theme_from_signal(
            config, slug="child", essence="x",
            cluster_source_ids=ids, cluster_concepts=["finance-regime"],
            parent="thm-aaaa1111",
        )
        fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert fm["parent"] == "thm-aaaa1111"

    def test_parent_omitted(self, config, vault):
        from personal_mem.core.vault import parse_frontmatter
        from personal_mem.synthesis.theme_candidates import mint_theme_from_signal

        ids = self._cluster_ids(config, vault)
        path = mint_theme_from_signal(
            config, slug="orphan", essence="x",
            cluster_source_ids=ids, cluster_concepts=["finance-regime"],
        )
        fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert "parent" not in fm


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
        # Theme appears in table + catalog, but no per-theme Mermaid DAG.
        # (The catalog section emits `### {title}` for every active theme
        # — the original assertion conflated "no DAG" with "no H3", which
        # is wrong now that the catalog renders structured sub-blocks.)
        assert "Lonely theme" in content
        assert "```mermaid" not in content


class TestConceptHubThreadedRendering:
    """Concept hubs render the catalyst log as a threaded markdown tree —
    `new` entries are top-level bullets and non-`new` entries indent under
    their predecessor with a `↳` cue. This replaces the prior Mermaid
    `## Evolution` section, which was unreadable past ~30 entries and
    produced churny diffs on every append.
    """

    def test_extends_entry_renders_indented_under_anchor(
        self, vault: VaultManager, config: Config, tmp_path
    ):
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

        # Mermaid is gone.
        assert "## Evolution" not in rendered
        assert "```mermaid" not in rendered

        # Threaded layout: anchor at depth 0, descendant nested with ↳.
        assert "- 2026-01-01 · *new* — seed — [[n-aaa]]" in rendered
        assert (
            "    - ↳ 2026-02-01 · *extends 2026-01-01* — extends seed — [[n-bbb]]"
            in rendered
        )
        # The descendant comes after its anchor in the rendered text.
        assert rendered.index("seed — [[n-aaa]]") < rendered.index(
            "extends seed — [[n-bbb]]"
        )

    def test_flat_log_when_no_links(
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
        assert "```mermaid" not in rendered
        # Both anchors at depth 0 — no indentation, no arrow.
        assert "↳" not in rendered
