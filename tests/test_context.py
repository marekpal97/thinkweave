"""Tests for the project context payload builder (src/personal_mem/context.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.config import Config
from personal_mem.context import (
    CHARS_PER_TOKEN,
    SECTIONS,
    build_project_context,
    _extract_insight_titles,
    _extract_summary,
    _slice_markdown_section,
)
from personal_mem.indexer import Indexer
from personal_mem.schemas import NoteType
from personal_mem.vault import VaultManager


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


@pytest.fixture
def indexer(config: Config):
    idx = Indexer(config=config)
    yield idx
    idx.close()


def _populate_wrapped_session(
    vault: VaultManager, project: str, *, title: str, processed_at: str
) -> None:
    """Create a wrapped session note with a summary and candidate insights."""
    body = (
        "## Summary\n"
        "This session wired up the retrieval primitives. FTS5 quoting fix landed.\n"
        "\n"
        "## Candidate Insights\n"
        "\n- **First-class insight about FTS5** body text here\n"
        "\n- **RRF hybrid fusion is the right primitive** body\n"
        "\n- **Third insight title** body\n"
    )
    vault.create_note(
        NoteType.SESSION,
        title,
        body=body,
        project=project,
        extra_frontmatter={
            "processed": True,
            "processed_at": processed_at,
            "source_session": f"cc-{title}",
        },
    )


# ---------------------------------------------------------------------------
# Text parsing helpers
# ---------------------------------------------------------------------------


class TestExtractHelpers:
    def test_extract_summary_finds_summary_section(self):
        body = "## Summary\nFirst line.\nSecond line.\n\n## Next\nOther"
        result = _extract_summary(body)
        assert "First line" in result
        assert "Second line" in result
        assert "Other" not in result

    def test_extract_summary_fallback_to_first_paragraph(self):
        body = "Just a single paragraph with no heading."
        result = _extract_summary(body)
        assert result.startswith("Just a single paragraph")

    def test_extract_summary_empty(self):
        assert _extract_summary("") == ""
        assert _extract_summary("## Summary\n") == ""

    def test_extract_insight_titles_pulls_bold_titles(self):
        body = (
            "## Candidate Insights\n"
            "\n- **Title One** body text\n"
            "\n- **Title Two** more body\n"
            "\n## Next Section\n"
            "\n- **Should not appear** — outside insights\n"
        )
        titles = _extract_insight_titles(body)
        assert "Title One" in titles
        assert "Title Two" in titles
        assert "Should not appear" not in titles

    def test_extract_insight_titles_empty_when_no_section(self):
        assert _extract_insight_titles("## Summary\nNo insights") == []
        assert _extract_insight_titles("") == []

    def test_slice_markdown_section_extracts_open_block(self):
        text = (
            "# Backlog\n"
            "\n## Open\n"
            "\n- [ ] Item one\n"
            "\n- [ ] Item two\n"
            "\n## Closed\n"
            "\n- [x] Done\n"
        )
        result = _slice_markdown_section(text, "Open")
        assert "Item one" in result
        assert "Item two" in result
        assert "Done" not in result

    def test_slice_markdown_section_missing_heading(self):
        assert _slice_markdown_section("# Title\nBody", "Open") == ""


# ---------------------------------------------------------------------------
# build_project_context — end-to-end
# ---------------------------------------------------------------------------


class TestBuildProjectContext:
    def test_empty_vault_does_not_crash(self, config: Config, vault: VaultManager):
        """Fresh vault, no index, no sessions — should return minimal payload."""
        payload = build_project_context(config, project="ghost", budget_tokens=10000)
        assert isinstance(payload, str)
        assert "## Header" in payload
        assert "## Retrieval Hints" in payload
        # No sessions section content, but shouldn't crash.

    def test_emits_all_default_sections(
        self, config: Config, vault: VaultManager, indexer: Indexer
    ):
        _populate_wrapped_session(
            vault, "demo", title="Kickoff session", processed_at="2026-04-05"
        )
        vault.create_note(
            NoteType.DECISION,
            "Use FTS5",
            body="Rationale body",
            project="demo",
            extra_frontmatter={"status": "accepted", "summary": "FTS5 is fast enough"},
        )
        vault.create_note(
            NoteType.SOURCE,
            "Some paper",
            body="Body",
            project="demo",
        )
        vault.create_note(
            NoteType.NOTE,
            "An open probe",
            body="Question body",
            project="demo",
            tags=["probe"],
        )
        indexer.rebuild(full=True)

        payload = build_project_context(config, project="demo", budget_tokens=10000)

        # Every default section heading should appear.
        for heading in (
            "## Header",
            "## Available MCP Tools",
            "## Recent Wrapped Sessions",
            "## Recent Decisions",
            "## Open Probes",
            "## Concept Histogram",
            "## Recent Sources",
            "## Retrieval Hints",
        ):
            assert heading in payload, f"Missing section: {heading}"

        # Specific content pulled through
        assert "Kickoff session" in payload
        assert "Use FTS5" in payload
        assert "An open probe" in payload
        assert "Some paper" in payload

    def test_sections_override(
        self, config: Config, vault: VaultManager, indexer: Indexer
    ):
        """Caller can restrict which sections are emitted."""
        indexer.rebuild(full=True)
        payload = build_project_context(
            config,
            project="demo",
            sections=["header", "footer"],
            budget_tokens=10000,
        )
        assert "## Header" in payload
        assert "## Retrieval Hints" in payload
        assert "## Available MCP Tools" not in payload
        assert "## Recent Wrapped Sessions" not in payload

    def test_honours_token_budget(
        self, config: Config, vault: VaultManager, indexer: Indexer
    ):
        """When budget is tiny, sections should drop rather than explode."""
        # Populate a lot of content
        for i in range(20):
            vault.create_note(
                NoteType.NOTE,
                f"Probe {i}",
                body="x" * 500,
                project="big",
                tags=["probe"],
            )
        for i in range(15):
            vault.create_note(
                NoteType.DECISION,
                f"Decision {i}",
                body="Rationale " * 50,
                project="big",
                extra_frontmatter={"status": "accepted"},
            )
        indexer.rebuild(full=True)

        # Very small budget — should drop optional sections
        small_budget = 500  # tokens → 2000 chars
        payload = build_project_context(
            config, project="big", budget_tokens=small_budget
        )

        assert len(payload) <= small_budget * CHARS_PER_TOKEN + 200  # minor slack
        # Header and footer should survive — they're load-bearing
        assert "## Header" in payload

    def test_budget_drops_decorative_sections_first(
        self, config: Config, vault: VaultManager, indexer: Indexer
    ):
        """When dropping, decorative sections (sources, concepts, probes) go before
        load-bearing ones (state, sessions, tools)."""
        _populate_wrapped_session(
            vault, "demo", title="Session A", processed_at="2026-04-05"
        )
        for i in range(30):
            vault.create_note(
                NoteType.SOURCE,
                f"Source title that is long enough to matter number {i}",
                body="x" * 300,
                project="demo",
            )
        indexer.rebuild(full=True)

        # Budget tight enough to force drops. Tools manifest alone is ~2.4k chars,
        # so 800 tokens = 3200 chars cannot fit sources + probes on top of it.
        payload = build_project_context(
            config, project="demo", budget_tokens=800
        )

        # Sessions section is load-bearing — should survive.
        assert "## Recent Wrapped Sessions" in payload
        assert "## Header" in payload
        # Sources should be the first to drop under pressure.
        assert "## Recent Sources" not in payload

    def test_state_md_pulled_when_present(
        self,
        config: Config,
        vault: VaultManager,
        indexer: Indexer,
        vault_dir: Path,
    ):
        # Seed a STATE.md manually
        project_dir = vault_dir / "projects" / "withstate"
        project_dir.mkdir(parents=True)
        (project_dir / "STATE.md").write_text(
            "# State\n\nThis is the big picture.\n",
            encoding="utf-8",
        )
        indexer.rebuild(full=True)

        payload = build_project_context(
            config, project="withstate", budget_tokens=10000
        )
        assert "This is the big picture" in payload

    def test_sections_constant_exposed(self):
        """Sanity — the SECTIONS tuple enumerates the payload keys."""
        assert "header" in SECTIONS
        assert "tools" in SECTIONS
        assert "themes" in SECTIONS
        assert "footer" in SECTIONS
        assert len(SECTIONS) == 11
