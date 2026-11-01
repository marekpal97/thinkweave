"""Tests for the project context payload builder (src/thinkweave/context.py)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from thinkweave.core.config import Config, ContextStartup, StartupSection
from thinkweave.core.harness import SESSION_START_BUDGET_TOKENS
from thinkweave.retrieval.context import (
    CHARS_PER_TOKEN,
    DROP_ORDER,
    SECTIONS,
    SESSION_START_GUARD_HEADROOM_CHARS,
    SESSION_START_SECTIONS,
    build_project_context,
    serving_plan,
    session_start_budget_tokens,
    _extract_insight_titles,
    _extract_summary,
    _slice_markdown_section,
)
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager


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

    def test_omit_empty_drops_placeholders_keeps_content(
        self, config: Config, vault: VaultManager, indexer: Indexer
    ):
        """omit_empty drops "nothing yet" placeholders but keeps real sections."""
        vault.create_note(
            NoteType.DECISION,
            "Use FTS5",
            body="Rationale body",
            project="demo",
            extra_frontmatter={"status": "accepted", "summary": "FTS5 is fast enough"},
        )
        indexer.rebuild(full=True)
        wanted = ["header", "state", "backlog", "decisions", "probes", "sources"]

        full = build_project_context(config, project="demo", sections=wanted)
        assert "## State of Play" in full
        assert "## Open Probes" in full

        trimmed = build_project_context(
            config, project="demo", sections=wanted, omit_empty=True
        )
        assert "## Header" in trimmed
        assert "Use FTS5" in trimmed
        for heading in ("## State of Play", "## Backlog", "## Open Probes",
                        "## Recent Sources"):
            assert heading not in trimmed, heading

    def test_omit_empty_keeps_index_fault_visible(
        self, config: Config, vault: VaultManager
    ):
        """A missing index is a fault, not an empty section — it stays."""
        payload = build_project_context(
            config, project="demo", sections=["decisions"], omit_empty=True
        )
        assert "index not built" in payload

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

        # Budget tight enough to force drops. Tools manifest alone is ~3.2k chars,
        # so 1200 tokens = 4800 chars must drop the decorative sections (sources,
        # probes) while leaving header / tools / sessions / footer intact.
        payload = build_project_context(
            config, project="demo", budget_tokens=1200
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
        assert "tasks" in SECTIONS
        assert "themes" in SECTIONS
        assert "footer" in SECTIONS
        assert len(SECTIONS) == 12


# ---------------------------------------------------------------------------
# The harness inline cap
# ---------------------------------------------------------------------------


# Claude Code renders an ``additionalContext`` this long inline; above it it
# files the text and shows the model a 2KB preview. Hand-written from that
# harness behaviour, independent of the profile field it is compared against.
CLAUDE_CODE_CAP = 10_000


class TestHookContextCap:
    """The SessionStart payload must land inline, not as a file preview."""

    def test_claude_code_profile_declares_the_inline_cap(self):
        from thinkweave.core import harness

        assert harness.claude_code().additional_context_cap_chars == CLAUDE_CODE_CAP

    def test_derived_budget_leaves_room_for_the_guard(self):
        # The handler prepends the memory-seam guard to the payload, so the
        # payload's own budget has to leave the guard's share of the cap.
        spent = session_start_budget_tokens(CLAUDE_CODE_CAP) * CHARS_PER_TOKEN
        assert spent + SESSION_START_GUARD_HEADROOM_CHARS <= CLAUDE_CODE_CAP

    def test_unknown_cap_falls_back_to_the_default_budget(self):
        assert session_start_budget_tokens(None) == SESSION_START_BUDGET_TOKENS

    def test_session_start_leads_with_the_continuation_cues(self):
        assert SESSION_START_SECTIONS[:4] == (
            "header", "tasks", "sessions", "decisions"
        )

    def test_session_start_omits_tools_footer_and_state(self):
        # A section that is neither served at startup nor named here would be
        # lost silently — the explicit exclusion set is the guard.
        assert set(SECTIONS) - set(SESSION_START_SECTIONS) == {
            "tools",
            "footer",
            "state",
        }

    def test_state_absent_from_the_startup_payload_and_present_in_the_snapshot(
        self,
        config: Config,
        vault: VaultManager,
        indexer: Indexer,
        vault_dir: Path,
    ):
        project_dir = vault_dir / "projects" / "withstate"
        project_dir.mkdir(parents=True)
        (project_dir / "STATE.md").write_text(
            "# State\n\nThe big picture of this project.\n", encoding="utf-8"
        )
        _populate_wrapped_session(
            vault, "withstate", title="Kickoff session", processed_at="2026-04-05"
        )
        indexer.rebuild(full=True)

        startup = build_project_context(
            config,
            project="withstate",
            sections=list(SESSION_START_SECTIONS),
            budget_tokens=session_start_budget_tokens(CLAUDE_CODE_CAP),
            omit_empty=True,
        )
        assert "## State of Play" not in startup
        assert "The big picture" not in startup

        snapshot = build_project_context(config, project="withstate")
        assert "## State of Play" in snapshot
        assert "The big picture" in snapshot

    def test_tool_events_only_session_is_not_listed(
        self, config: Config, vault: VaultManager, indexer: Indexer
    ):
        """A session whose only summary is the no-metadata fallback says
        nothing to a new session, so it is skipped — and it is the NEWER of
        the two here, so a builder that lists by recency would show it."""
        _populate_wrapped_session(
            vault, "demo", title="Session with a summary", processed_at="2026-04-05"
        )
        vault.create_note(
            NoteType.SESSION,
            "Session with no metadata",
            body="## Summary\n7 tool events recorded.\n",
            project="demo",
            extra_frontmatter={
                "processed": True,
                "processed_at": "2026-04-06",
                "source_session": "cc-quiet",
            },
        )
        indexer.rebuild(full=True)

        payload = build_project_context(
            config,
            project="demo",
            sections=list(SESSION_START_SECTIONS),
            budget_tokens=session_start_budget_tokens(CLAUDE_CODE_CAP),
            omit_empty=True,
        )
        assert "Session with a summary" in payload
        assert "Session with no metadata" not in payload
        assert "tool events recorded" not in payload

    def test_a_dropped_section_that_fits_the_room_left_comes_back(
        self, config: Config, vault: VaultManager, indexer: Indexer, vault_dir: Path
    ):
        """One oversized section must not evict the sections that do fit.

        The backlog landing doc here is 9,000 chars against a 8,000-char
        budget, so it can never fit beside anything. The ranked walk drops
        sources before it reaches the backlog; once the backlog is gone the
        sources fit again, so they must be back in the payload.
        """
        project_dir = vault_dir / "projects" / "demo"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "BACKLOG.md").write_text(
            "# Backlog\n\n## Open\n\n" + "- [ ] Item worth reading. " * 400 +
            "\n\n## Closed\n\n- [x] Done\n",
            encoding="utf-8",
        )
        vault.create_note(NoteType.SOURCE, "Some paper", body="Body", project="demo")
        indexer.rebuild(full=True)

        payload = build_project_context(config, project="demo", budget_tokens=2_000)

        assert "## Backlog" not in payload
        assert "## Recent Sources" in payload
        assert "Some paper" in payload

    def test_dropped_sections_are_announced_in_the_payload(
        self, config: Config, vault: VaultManager, indexer: Indexer, vault_dir: Path
    ):
        """A payload that lost sections says which, so the reader knows what
        to fetch on demand rather than assuming the vault is quiet."""
        project_dir = vault_dir / "projects" / "demo"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "BACKLOG.md").write_text(
            "# Backlog\n\n## Open\n\n" + "- [ ] Item worth reading. " * 400 + "\n",
            encoding="utf-8",
        )
        indexer.rebuild(full=True)

        payload = build_project_context(config, project="demo", budget_tokens=2_000)

        assert "Backlog (Open Items)" in payload
        assert "omitted" in payload


class TestToolManifestConsolidation:
    """The SessionStart payload advertises the consolidated MCP surface.

    Regression for the audit finding that ``_build_tools_manifest`` and
    ``_build_footer`` were still emitting deprecated tool names. The
    canonical surface (17 tools) is documented in CLAUDE.md §7.

    The deprecation aliases (``weave_concept_search``, ``weave_source_lens``,
    ``weave_decisions_for_file``, ``weave_concepts_merge``,
    ``weave_concepts_drift``, ``weave_concepts_tighten``) still resolve at
    the MCP server, but they MUST NOT appear in the SessionStart payload
    — that's what tells the agent which surface to call.
    """

    # Deprecated names — folded into weave_concepts(action=...) and
    # weave_graph(filter=...). Must not appear in the rendered payload.
    DEPRECATED_TOOLS = (
        "weave_concept_search",
        "weave_source_lens",
        "weave_decisions_for_file",
        "weave_concepts_merge",
        "weave_concepts_drift",
        "weave_concepts_tighten",
    )

    def test_no_deprecated_tool_names(self, config: Config, vault: VaultManager):
        payload = build_project_context(config, project="test", budget_tokens=10000)
        for name in self.DEPRECATED_TOOLS:
            assert name not in payload, (
                f"deprecated tool name {name!r} still advertised in SessionStart payload"
            )

    def test_manifest_matches_mcp_registry(self):
        """Bidirectional contract: manifest ↔ the MCP server's DISPATCH registry.

        The "Keep this list in sync with mcp/server.py" comment in
        ``_build_tools_manifest`` was the only guard. Expected names come
        from the server's actual dispatch table, not a re-listing.
        """
        from thinkweave.retrieval.context import _build_tools_manifest
        from thinkweave.surfaces.mcp.tools import DISPATCH

        body = _build_tools_manifest().body
        registered = set(DISPATCH)
        advertised = set(re.findall(r"weave_[a-z_]+", body))
        assert not registered - advertised, (
            f"MCP tools registered but not advertised in the manifest: "
            f"{sorted(registered - advertised)!r}"
        )
        assert not advertised - registered, (
            f"manifest advertises tool names the MCP server doesn't register: "
            f"{sorted(advertised - registered)!r}"
        )

    def test_canonical_dispatch_keywords_present(
        self, config: Config, vault: VaultManager
    ):
        """Surface the consolidation hints — weave_graph filter + weave_concepts action."""
        payload = build_project_context(config, project="test", budget_tokens=10000)
        # weave_graph dispatch — the four filter variants advertised in CLAUDE.md §2.
        assert "source_lens" in payload
        assert "decisions_for_file" in payload
        assert "concept_walk" in payload
        # weave_concepts dispatch — at least the new actions named explicitly.
        assert "action" in payload


# ---------------------------------------------------------------------------
# [context.startup] — the payload shape from config (#352)
# ---------------------------------------------------------------------------


def _startup(**kw) -> ContextStartup:
    sections = kw.pop("section", {})
    return ContextStartup(
        section={k: StartupSection(**v) for k, v in sections.items()}, **kw
    )


@pytest.fixture
def frozen_clock(monkeypatch):
    """Pin the header's timestamp so two renders compare byte for byte."""
    from datetime import datetime as real

    class Frozen(real):
        @classmethod
        def now(cls, tz=None):
            return real(2026, 11, 1, 12, 0, tzinfo=tz)

    monkeypatch.setattr("thinkweave.retrieval.context.datetime", Frozen)


@pytest.fixture
def busy(vault: VaultManager, indexer: Indexer, vault_dir: Path) -> str:
    """A project with a ~1.2k-char backlog, a wrapped session and a source."""
    project_dir = vault_dir / "projects" / "busy"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "BACKLOG.md").write_text(
        "# Backlog\n\n## Open\n\n"
        + "".join(f"- [ ] Backlog item {i:02d} worth reading.\n" for i in range(36))
        + "\n## Closed\n\n- [x] Done\n",
        encoding="utf-8",
    )
    _populate_wrapped_session(
        vault, "busy", title="Kickoff session", processed_at="2026-04-05"
    )
    vault.create_note(NoteType.SOURCE, "Some paper", body="Body", project="busy")
    indexer.rebuild(full=True)
    return "busy"


class TestContextStartupConfig:
    def test_unset_is_byte_identical_to_the_module_defaults(
        self, vault_dir: Path, busy: str, frozen_clock
    ):
        """No ``[context.startup]`` serves exactly what the module defaults
        serve, on both the startup route and the snapshot route."""
        unset = Config(vault_root=vault_dir)
        spelled = Config(
            vault_root=vault_dir,
            context_startup=_startup(sections=SECTIONS, drop_order=DROP_ORDER),
        )
        assert build_project_context(unset, busy) == build_project_context(spelled, busy)

        plan = serving_plan(unset)
        assert plan.startup_sections() == list(SESSION_START_SECTIONS)
        assert plan.startup_budget_chars(CLAUDE_CODE_CAP) == (
            session_start_budget_tokens(CLAUDE_CODE_CAP) * CHARS_PER_TOKEN
        )
        assert plan.startup_budget_chars(None) == (
            SESSION_START_BUDGET_TOKENS * CHARS_PER_TOKEN
        )
        for budget in (300, 600, 10_000):
            assert build_project_context(
                unset,
                busy,
                sections=list(SESSION_START_SECTIONS),
                budget_tokens=budget,
                omit_empty=True,
            ) == build_project_context(
                spelled,
                busy,
                sections=list(SESSION_START_SECTIONS),
                budget_tokens=budget,
                omit_empty=True,
            )

    def test_sections_override_is_the_default_section_set(
        self, vault_dir: Path, busy: str
    ):
        """Configured sections replace the default set; an explicit
        ``sections`` argument still wins."""
        cfg = Config(
            vault_root=vault_dir,
            context_startup=_startup(sections=("header", "backlog")),
        )
        payload = build_project_context(cfg, busy)
        headings = re.findall(r"^## (.+)$", payload, re.M)
        assert headings == ["Header", "Backlog (Open Items)"]
        assert serving_plan(cfg).startup_sections() == ["header", "backlog"]

        explicit = build_project_context(cfg, busy, sections=["sources"])
        assert re.findall(r"^## (.+)$", explicit, re.M) == ["Recent Sources"]

    def test_budget_chars_sets_the_startup_budget(self, vault_dir: Path):
        cfg = Config(vault_root=vault_dir, context_startup=_startup(budget_chars=1500))
        assert serving_plan(cfg).startup_budget_chars(CLAUDE_CODE_CAP) == 1500
        assert serving_plan(cfg).startup_budget_chars(None) == 1500

    def test_a_pinned_section_survives_the_budget(self, vault_dir: Path, busy: str):
        """Unpinned, the backlog is the section the budget drops; pinned, it
        is served whole and the other sections give way."""
        wanted = ["header", "sessions", "backlog", "sources"]
        plain = Config(vault_root=vault_dir)
        pinned = Config(
            vault_root=vault_dir,
            context_startup=_startup(section={"backlog": {"pin": True}}),
        )
        budget = 450  # tokens: 1,800 chars

        dropped = build_project_context(plain, busy, sections=wanted, budget_tokens=budget)
        assert "Backlog item 35" not in dropped

        kept = build_project_context(pinned, busy, sections=wanted, budget_tokens=budget)
        assert len(kept) <= budget * CHARS_PER_TOKEN
        assert "Backlog item 00" in kept and "Backlog item 35" in kept

    def test_cap_cuts_one_section_body(self, vault_dir: Path, busy: str):
        cfg = Config(
            vault_root=vault_dir,
            context_startup=_startup(section={"backlog": {"cap": 300}}),
        )
        payload = build_project_context(cfg, busy, sections=["backlog"])
        body = payload.split("## Backlog (Open Items)\n\n", 1)[1]
        assert len(body) <= 300
        assert "truncated to fit budget" in body
        assert "Backlog item 00" in body
        assert "Backlog item 35" not in body

    def test_drop_order_override_picks_what_goes_first(
        self, vault_dir: Path, busy: str
    ):
        """By default sources drop before the backlog; reversed, the backlog
        goes and the sources stay."""
        wanted = ["header", "backlog", "sources"]
        full = build_project_context(
            Config(vault_root=vault_dir), busy, sections=wanted, budget_tokens=10_000
        )
        # Just under the full payload: dropping either section fits.
        budget = (len(full) - 20) // CHARS_PER_TOKEN

        default = build_project_context(
            Config(vault_root=vault_dir), busy, sections=wanted, budget_tokens=budget
        )
        reordered = build_project_context(
            Config(
                vault_root=vault_dir,
                context_startup=_startup(drop_order=("backlog", "sources")),
            ),
            busy,
            sections=wanted,
            budget_tokens=budget,
        )
        assert "## Recent Sources" not in default
        assert "## Backlog (Open Items)" in default
        assert "## Recent Sources" in reordered
        assert "## Backlog (Open Items)" not in reordered

    def test_the_inventory_renders_every_startup_key_read_at_session_start(self):
        from thinkweave.surfaces.cli.policy import render_toml

        rendered = render_toml()
        for line in (
            "# [context.startup]",
            "# [context.startup.tasks]",
        ):
            assert line in rendered
        for key in ("sections", "budget_chars", "drop_order", "pin", "cap"):
            rows = [ln for ln in rendered.splitlines() if ln.startswith(f"# {key} = ")]
            assert rows and all("read at: SessionStart" in ln for ln in rows), key


class TestSessionStartReadsTheConfig:
    def test_hook_serves_the_configured_sections_and_records_the_cap(
        self, tmp_path: Path, monkeypatch
    ):
        """The hook takes sections and budget from ``[context.startup]`` and
        the startup buffer row carries the cap in force."""
        import json

        from thinkweave.core.config import load_config
        from thinkweave.surfaces.hooks import handler as h

        vault = tmp_path / "vault"
        (vault / "config").mkdir(parents=True)
        (vault / "config" / "config.toml").write_text(
            '[context.startup]\nsections = ["header", "tasks"]\nbudget_chars = 1500\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("THINKWEAVE_VAULT", str(vault))
        monkeypatch.setenv("THINKWEAVE_PROJECT", "t")
        monkeypatch.delenv("THINKWEAVE_WEAVE_DIR", raising=False)
        spent: dict = {}

        def spy(*args, **kwargs):
            spent.update(kwargs)
            return "## Header\n\n- project t\n"

        monkeypatch.setattr("thinkweave.retrieval.context.build_project_context", spy)
        monkeypatch.setattr(h, "_output", lambda **kw: None)

        h._handle_session_start(
            {"session_id": "uuid-ctx1", "source": "startup", "cwd": str(tmp_path)}
        )

        assert spent["sections"] == ["header", "tasks"]
        assert spent["budget_tokens"] * CHARS_PER_TOKEN == 1500
        buffer = load_config().weave_dir / "buffer" / "uuid-ctx1.jsonl"
        rows = [json.loads(ln) for ln in buffer.read_text().splitlines() if ln]
        startup = [r for r in rows if r.get("type") == "startup"]
        assert startup and startup[0]["cap"] == 1500
