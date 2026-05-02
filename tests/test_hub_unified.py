"""Tests for the unified ``Hub`` spine shared by concept and theme hubs.

These lock the contract that concept_hub.py and theme_hub.py both
delegate parse/render of the shared ``## Essence`` + ``## Catalyst log``
skeleton to ``synthesis/hub.py``. Deleting the spine should break tests
on both surfaces identically — that's the integration property.

Also covers the idempotent ``## Learning log`` → ``## Catalyst log``
migration wired into ``mem index --full``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.synthesis.hub import (
    ALLOWED_FLAGS,
    CATALYST_LOG_HEADING,
    LEGACY_LEARNING_LOG_HEADING,
    Hub,
    HubLogEntry,
    extract_section,
    migrate_hub_log_heading,
    parse_log_entries,
)


# ---------------------------------------------------------------------------
# HubLogEntry
# ---------------------------------------------------------------------------


class TestHubLogEntry:
    def test_render_new_entry(self):
        entry = HubLogEntry(
            date="2026-04-15",
            flag="new",
            text="A claim",
            citation="n-abc",
        )
        line = entry.render()
        assert line == "- 2026-04-15 · *new* — A claim — [[n-abc]]"

    def test_render_with_ref(self):
        entry = HubLogEntry(
            date="2026-04-22",
            flag="extends",
            ref="2026-04-15",
            text="A refinement",
            citation="n-xyz",
        )
        line = entry.render()
        assert "*extends 2026-04-15*" in line
        assert "[[n-xyz]]" in line

    def test_citations_property_returns_list(self):
        entry = HubLogEntry(date="2026-01-01", flag="new", citation="n-1")
        assert entry.citations == ["n-1"]

    def test_citations_property_empty_when_no_citation(self):
        entry = HubLogEntry(date="2026-01-01", flag="new")
        assert entry.citations == []


# ---------------------------------------------------------------------------
# Hub.parse / Hub.render
# ---------------------------------------------------------------------------


class TestHubParse:
    def test_parse_missing_file_returns_empty_hub(self, tmp_path: Path):
        hub = Hub.parse(tmp_path / "absent.md", hub_id="absent-id")
        assert hub.id == "absent-id"
        assert hub.essence == ""
        assert hub.log == []

    def test_parse_extracts_title_from_h1(self, tmp_path: Path):
        path = tmp_path / "x.md"
        path.write_text("# My Title\n\n## Essence\n\nWhat it is.\n")
        hub = Hub.parse(path)
        assert hub.title == "My Title"
        assert hub.essence == "What it is."

    def test_parse_canonical_catalyst_log(self, tmp_path: Path):
        path = tmp_path / "x.md"
        path.write_text(
            "# Title\n\n"
            "## Essence\n\nseed\n\n"
            "## Catalyst log\n\n"
            "- 2026-04-15 · *new* — first — [[n-1]]\n"
            "- 2026-04-22 · *extends 2026-04-15* — follow up — [[n-2]]\n"
        )
        hub = Hub.parse(path)
        assert len(hub.log) == 2
        assert hub.log[0].flag == "new"
        assert hub.log[1].flag == "extends"
        assert hub.log[1].ref == "2026-04-15"

    def test_parse_legacy_learning_log_heading(self, tmp_path: Path):
        """Legacy concept hubs used ``## Learning log``. Hub.parse must
        still extract entries before the migration runs."""
        path = tmp_path / "x.md"
        path.write_text(
            "# Title\n\n"
            "## Essence\n\n"
            "## Learning log\n\n"
            "- 2026-04-15 · *new* — legacy entry — [[n-1]]\n"
        )
        hub = Hub.parse(path)
        assert len(hub.log) == 1
        assert hub.log[0].text == "legacy entry"

    def test_parse_open_questions_section(self, tmp_path: Path):
        path = tmp_path / "x.md"
        path.write_text(
            "# Title\n\n"
            "## Essence\n\nseed\n\n"
            "## Catalyst log\n\n"
            "## Open questions\n\n"
            "What's next?\n"
        )
        hub = Hub.parse(path)
        assert "What's next?" in hub.open_questions


class TestHubRender:
    def test_render_skeleton_concept_style(self):
        hub = Hub(id="x", title="X", essence="seed", log=[])
        out = hub.render()
        assert "# X" in out
        assert "## Essence" in out
        assert "seed" in out
        assert "## Catalyst log" in out
        # No open-questions on concept-style render.
        assert "## Open questions" not in out

    def test_render_skeleton_theme_style_includes_open_questions(self):
        hub = Hub(id="thm-1", title="Theme", essence="thesis", open_questions="Q1?")
        out = hub.render(include_open_questions=True)
        assert "## Open questions" in out
        assert "Q1?" in out

    def test_render_empty_log_uses_placeholder(self):
        hub = Hub(id="x", title="X")
        out = hub.render()
        assert "*No entries yet.*" in out

    def test_render_empty_essence_uses_placeholder(self):
        hub = Hub(id="x", title="X")
        out = hub.render()
        assert "*No synthesis yet.*" in out


class TestHubAppend:
    def test_append_idempotent_on_citation(self):
        hub = Hub(id="x", title="X")
        e = HubLogEntry(date="2026-04-15", flag="new", text="foo", citation="n-1")
        assert hub.append(e) is True
        assert hub.append(e) is False
        assert len(hub.log) == 1

    def test_append_rejects_unknown_flag(self):
        hub = Hub(id="x", title="X")
        e = HubLogEntry(date="2026-04-15", flag="bogus", citation="n-1")
        assert hub.append(e) is False
        assert hub.log == []

    def test_cited_ids_view(self):
        hub = Hub(id="x", title="X")
        hub.append(HubLogEntry(date="2026-04-15", flag="new", citation="n-1"))
        hub.append(HubLogEntry(date="2026-04-16", flag="new", citation="n-2"))
        assert hub.cited_ids == {"n-1", "n-2"}


class TestHubRenderDag:
    def test_render_dag_empty_log(self):
        hub = Hub(id="x", title="X")
        assert hub.render_dag() == ""

    def test_render_dag_with_linked_entries(self):
        hub = Hub(
            id="x",
            title="X",
            log=[
                HubLogEntry(date="2026-01-01", flag="new", text="a", citation="n-1"),
                HubLogEntry(
                    date="2026-02-01",
                    flag="extends",
                    ref="2026-01-01",
                    text="b",
                    citation="n-2",
                ),
            ],
        )
        out = hub.render_dag()
        assert "graph LR" in out
        assert "extends" in out


# ---------------------------------------------------------------------------
# Section/log primitives
# ---------------------------------------------------------------------------


class TestExtractSection:
    def test_extracts_named_section(self):
        body = (
            "## Essence\n\nbody-A\n\n"
            "## Catalyst log\n\nbody-B\n"
        )
        assert "body-A" in extract_section(body, "## Essence")
        assert "body-B" not in extract_section(body, "## Essence")

    def test_missing_section_returns_empty(self):
        assert extract_section("# Just a title\n", "## Nope") == ""


class TestParseLogEntries:
    def test_parses_one_entry_with_citation(self):
        section = "- 2026-04-15 · *new* — claim text — [[n-abc]]\n"
        entries = parse_log_entries(section)
        assert len(entries) == 1
        assert entries[0].flag == "new"
        assert entries[0].citation == "n-abc"
        assert "claim text" in entries[0].text

    def test_drops_unknown_flags(self):
        section = "- 2026-04-15 · *bogus* — text — [[n-1]]\n"
        assert parse_log_entries(section) == []

    def test_all_allowed_flags_round_trip(self):
        section = "\n".join(
            f"- 2026-04-{15+i:02d} · *{flag}* — text-{i} — [[n-{i}]]"
            for i, flag in enumerate(sorted(ALLOWED_FLAGS))
        )
        # `extends` and `contradicts` need refs at parse time but the
        # parser is lax — that's what hub-link validation is for.
        entries = parse_log_entries(section)
        assert {e.flag for e in entries} == ALLOWED_FLAGS


# ---------------------------------------------------------------------------
# Migration: ## Learning log → ## Catalyst log
# ---------------------------------------------------------------------------


class TestMigrateHubLogHeading:
    def test_renames_legacy_heading(self, tmp_path: Path):
        path = tmp_path / "concept.md"
        path.write_text(
            "# x\n\n"
            "## Essence\nseed\n\n"
            "## Learning log\n\n"
            "- 2026-01-01 · *new* — entry — [[n-1]]\n"
        )
        assert migrate_hub_log_heading(path) is True
        text = path.read_text()
        assert CATALYST_LOG_HEADING in text
        assert LEGACY_LEARNING_LOG_HEADING not in text
        # Content preserved.
        assert "entry" in text

    def test_idempotent_second_run_is_noop(self, tmp_path: Path):
        path = tmp_path / "concept.md"
        path.write_text("# x\n\n## Essence\n\n## Learning log\n\n")
        first = migrate_hub_log_heading(path)
        second = migrate_hub_log_heading(path)
        assert first is True
        assert second is False
        # File contents stable on the second run.
        text_after_first = path.read_text()
        migrate_hub_log_heading(path)
        assert path.read_text() == text_after_first

    def test_no_op_when_already_canonical(self, tmp_path: Path):
        path = tmp_path / "concept.md"
        path.write_text("# x\n\n## Essence\n\n## Catalyst log\n\n")
        assert migrate_hub_log_heading(path) is False

    def test_no_op_when_neither_heading_present(self, tmp_path: Path):
        path = tmp_path / "concept.md"
        path.write_text("# x\n\n## Essence\n\nseed only.\n")
        assert migrate_hub_log_heading(path) is False

    def test_no_op_when_file_missing(self, tmp_path: Path):
        assert migrate_hub_log_heading(tmp_path / "nope.md") is False

    def test_does_not_touch_inline_mention_of_learning_log(self, tmp_path: Path):
        """The migration only rewrites the heading line, not in-prose
        mentions of the words 'learning log' elsewhere in the body."""
        path = tmp_path / "concept.md"
        path.write_text(
            "# x\n\n"
            "## Essence\nThe learning log is append-only.\n\n"
            "## Learning log\n\n"
            "- 2026-01-01 · *new* — body — [[n-1]]\n"
        )
        migrate_hub_log_heading(path)
        text = path.read_text()
        # In-prose mention preserved.
        assert "The learning log is append-only." in text
        # Heading rewritten.
        assert "\n## Catalyst log\n" in text
        assert "\n## Learning log\n" not in text

    def test_canonical_heading_alongside_legacy_is_left_alone(self, tmp_path: Path):
        """If both headings somehow coexist, we leave the legacy one for a
        human to resolve — the canonical heading is what readers see."""
        path = tmp_path / "concept.md"
        path.write_text(
            "# x\n\n"
            "## Catalyst log\n\n"
            "- 2026-01-01 · *new* — A — [[n-1]]\n\n"
            "## Learning log\n\n"
            "- 2026-01-02 · *new* — B — [[n-2]]\n"
        )
        assert migrate_hub_log_heading(path) is False
        text = path.read_text()
        # Both still present — no automatic merge.
        assert "## Catalyst log" in text
        assert "## Learning log" in text


# ---------------------------------------------------------------------------
# Cross-surface integration: deleting hub.py would break both surfaces.
# ---------------------------------------------------------------------------


class TestSpineSharedAcrossSurfaces:
    def test_concept_hub_module_re_exports_unified_types(self):
        """LogEntry on the concept-hub module is the unified ``HubLogEntry``."""
        from personal_mem.synthesis.concept_hub import LogEntry as CHLogEntry

        assert CHLogEntry is HubLogEntry

    def test_theme_hub_module_re_exports_unified_types(self):
        from personal_mem.synthesis.theme_hub import LogEntry as THLogEntry

        assert THLogEntry is HubLogEntry

    def test_concept_hub_uses_canonical_catalyst_log_heading(self):
        from personal_mem.synthesis import concept_hub as ch

        # The historical alias still resolves, but it points to the
        # canonical heading from hub.py.
        assert ch.LEARNING_LOG_HEADING == CATALYST_LOG_HEADING
