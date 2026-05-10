"""Integration tests for concept hub primitives.

These are the shared core both hub-execution paths rely on:

- ``/drain --target hubs --via inline`` + ``/update-hubs`` skills — inline Claude Code path
- ``mem drain --target hubs --via batch`` — OpenAI Batches API path (gpt-5-mini)

Both paths call ``append_log_entries`` / ``parse_concept_hub`` /
``unprocessed_notes_for_concept`` after producing LLM output, so a
regression in any of these primitives breaks **both** paths. Tests here
lock in the contract without invoking an LLM.

Regression guard for probe n-277818a0 (architecture × concept-hubs
blend) — confirms the dual execution plumbing converges at the same
writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.synthesis.concept_hub import (
    LogEntry,
    _strip_inline_wikilinks,
    append_log_entries,
    concept_hub_path,
    ensure_concept_hub_skeleton,
    parse_concept_hub,
    parse_llm_response,
    unprocessed_notes_for_concept,
)
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager


@pytest.fixture
def vault_setup(tmp_path: Path):
    cfg = Config(vault_root=tmp_path / "vault")
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    idx = Indexer(config=cfg)
    yield cfg, vm, idx
    idx.close()


def _make_note_with_concept(vm, idx, title: str, concept: str) -> str:
    path = vm.create_note(
        note_type=NoteType.NOTE,
        title=title,
        body=f"Content for {title}",
        project="test",
        extra_frontmatter={"concepts": [concept]},
    )
    idx.index_file(path)
    return vm.read_note(path).id


class TestHubDiffContract:
    def test_unprocessed_notes_returns_all_when_hub_empty(self, vault_setup):
        cfg, vm, idx = vault_setup
        ids = {
            _make_note_with_concept(vm, idx, f"note-{i}", "test-concept")
            for i in range(3)
        }
        ensure_concept_hub_skeleton(cfg, "test-concept")
        unprocessed = unprocessed_notes_for_concept(cfg, "test-concept")
        assert {n.id for n in unprocessed} == ids

    def test_citation_removes_note_from_unprocessed(self, vault_setup):
        cfg, vm, idx = vault_setup
        ids = [
            _make_note_with_concept(vm, idx, f"note-{i}", "test-concept")
            for i in range(3)
        ]
        ensure_concept_hub_skeleton(cfg, "test-concept")

        append_log_entries(
            cfg,
            "test-concept",
            [LogEntry(
                date="2026-04-21",
                flag="new",
                text="first artifact",
                citation=ids[0],
            )],
        )

        unprocessed = unprocessed_notes_for_concept(cfg, "test-concept")
        assert ids[0] not in {n.id for n in unprocessed}
        assert {ids[1], ids[2]} == {n.id for n in unprocessed}

    def test_append_is_idempotent_for_same_citation(self, vault_setup):
        cfg, vm, idx = vault_setup
        nid = _make_note_with_concept(vm, idx, "only-note", "test-concept")

        entry = LogEntry(
            date="2026-04-21", flag="new", text="artifact", citation=nid
        )
        append_log_entries(cfg, "test-concept", [entry])
        append_log_entries(cfg, "test-concept", [entry])

        hub = parse_concept_hub(concept_hub_path(cfg, "test-concept"))
        assert len(hub.log_entries) == 1

    def test_invalid_flag_silently_skipped(self, vault_setup):
        cfg, vm, idx = vault_setup
        nid = _make_note_with_concept(vm, idx, "only-note", "test-concept")

        append_log_entries(
            cfg,
            "test-concept",
            [LogEntry(
                date="2026-04-21",
                flag="garbage-flag",
                text="should not persist",
                citation=nid,
            )],
        )
        hub = parse_concept_hub(concept_hub_path(cfg, "test-concept"))
        assert len(hub.log_entries) == 0

    def test_parse_round_trip_preserves_entries(self, vault_setup):
        cfg, vm, idx = vault_setup
        nid = _make_note_with_concept(vm, idx, "only-note", "test-concept")

        append_log_entries(
            cfg,
            "test-concept",
            [
                LogEntry(date="2026-04-21", flag="new", text="first", citation=nid),
                LogEntry(
                    date="2026-04-22",
                    flag="extends",
                    ref="2026-04-21",
                    text="follow-up detail",
                    citation=nid + "x",
                ),
            ],
        )

        hub = parse_concept_hub(concept_hub_path(cfg, "test-concept"))
        dates = [e.date for e in hub.log_entries]
        flags = [e.flag for e in hub.log_entries]
        assert dates == ["2026-04-21", "2026-04-22"]
        assert flags == ["new", "extends"]
        assert hub.log_entries[1].ref == "2026-04-21"

    def test_hub_skeleton_does_not_clobber_existing(self, vault_setup):
        cfg, vm, idx = vault_setup
        nid = _make_note_with_concept(vm, idx, "only-note", "test-concept")

        append_log_entries(
            cfg,
            "test-concept",
            [LogEntry(date="2026-04-21", flag="new", text="artifact", citation=nid)],
        )

        ensure_concept_hub_skeleton(cfg, "test-concept")

        hub = parse_concept_hub(concept_hub_path(cfg, "test-concept"))
        assert len(hub.log_entries) == 1
        assert hub.log_entries[0].citation == nid


class TestStripInlineWikilinks:
    """Part 2 regression: the LLM sometimes embeds the citation in the
    artifact text AND the render path appends the citation separately,
    producing duplicated `[[id]] — [[id]]` tails. Stripping happens at
    parse time so every downstream consumer sees clean text.
    """

    def test_strips_trailing_wikilink(self):
        out = _strip_inline_wikilinks("Some fact text [[n-abc123]]")
        assert out == "Some fact text"

    def test_strips_mid_sentence_wikilink(self):
        out = _strip_inline_wikilinks("Use git blame [[dec-xyz]] for attribution")
        assert "[[" not in out
        assert "git blame" in out and "attribution" in out

    def test_strips_multiple_wikilinks(self):
        out = _strip_inline_wikilinks("A [[n-1]] B [[n-2]] C")
        assert "[[" not in out
        for piece in ("A", "B", "C"):
            assert piece in out

    def test_preserves_text_with_no_wikilinks(self):
        assert _strip_inline_wikilinks("plain text") == "plain text"

    def test_handles_piped_wikilink_form(self):
        out = _strip_inline_wikilinks("Pattern [[target|display]] here")
        assert "[[" not in out
        assert "Pattern" in out and "here" in out

    def test_strips_parenthesized_wikilink(self):
        out = _strip_inline_wikilinks("technique A ([[n-1]]) applied at scale")
        assert "(" not in out and ")" not in out
        assert "technique A" in out and "applied at scale" in out

    def test_strips_parenthesized_wikilink_before_period(self):
        out = _strip_inline_wikilinks("favoring protection over bundling ([[dec-e57b9776]]).")
        assert "(" not in out and ")" not in out
        # Final period survives.
        assert out.endswith(".")

    def test_strips_empty_parens_leftover(self):
        # Simulates what the old regex would leave behind.
        out = _strip_inline_wikilinks("Use worktrees ( ).")
        assert "(" not in out and ")" not in out
        assert out.endswith(".")

    def test_removes_trailing_dangling_in_fragment(self):
        out = _strip_inline_wikilinks("Blocks direct pushes to protected branches; implemented in [[n-0389b20b]].")
        # "implemented in ." should not survive — the trailing preposition
        # gets dropped, leaving "implemented." which is grammatical.
        assert "in ." not in out
        assert out.endswith("implemented.")


class TestParseLLMResponseUsesNoteDate:
    """Part 1 regression: parse_llm_response stamps entries with the date
    the caller passes. Callers should pass the source note's date so the
    log carries real temporal structure. This test locks the contract
    that whatever is passed as run_date wins.
    """

    def test_entry_date_matches_supplied_run_date(self):
        raw = '{"entries": [{"flag": "new", "text": "artifact text"}]}'
        entries, _ = parse_llm_response(raw, note_id="n-abc", run_date="2025-09-12")
        assert len(entries) == 1
        assert entries[0].date == "2025-09-12"

    def test_inline_wikilink_stripped_from_text(self):
        raw = '{"entries": [{"flag": "new", "text": "use git blame [[dec-xyz]]"}]}'
        entries, _ = parse_llm_response(raw, note_id="n-abc", run_date="2025-09-12")
        assert len(entries) == 1
        assert "[[" not in entries[0].text

    def test_entry_all_wikilink_is_dropped(self):
        raw = '{"entries": [{"flag": "new", "text": "[[dec-xyz]]"}]}'
        entries, _ = parse_llm_response(raw, note_id="n-abc", run_date="2025-09-12")
        # Entry text was nothing but a wikilink — after stripping, nothing useful.
        assert entries == []


class TestLinkageHelpers:
    """Part 3 regression for `mem hubs link`: the pure helpers that build
    per-hub prompts and parse the LLM's linkage revisions. Exercised
    without any LLM call — contract is (input shape) → (output shape).
    """

    def _build_prompt(self, concept, essence, entries):
        from personal_mem.surfaces.cli import _build_linkage_user_prompt
        return _build_linkage_user_prompt(concept, essence, entries)

    def _parse(self, raw):
        from personal_mem.surfaces.cli import _parse_linkage_response
        return _parse_linkage_response(raw)

    def test_prompt_preserves_chronological_order(self):
        entries = [
            LogEntry(date="2025-09-12", flag="new", text="use git blame", citation="n-a"),
            LogEntry(date="2026-02-03", flag="new", text="map hash to files", citation="n-b"),
            LogEntry(date="2026-03-17", flag="new", text="blame_lines flips verdict", citation="n-c"),
        ]
        prompt = self._build_prompt("git", "", entries)
        # All three artifact bodies appear in order, each on its own numbered line.
        assert prompt.index("use git blame") < prompt.index("map hash to files") < prompt.index("blame_lines flips verdict")
        assert "1. 2025-09-12" in prompt
        assert "2. 2026-02-03" in prompt
        assert "3. 2026-03-17" in prompt

    def test_prompt_includes_essence_when_present(self):
        entries = [LogEntry(date="2025-01-01", flag="new", text="x", citation="n-1")]
        prompt = self._build_prompt("c", "The essence here.", entries)
        assert "The essence here." in prompt

    def test_prompt_uses_placeholder_when_essence_empty(self):
        entries = [LogEntry(date="2025-01-01", flag="new", text="x", citation="n-1")]
        prompt = self._build_prompt("c", "", entries)
        assert "*No synthesis yet.*" in prompt

    def test_parse_accepts_plain_json(self):
        raw = '{"entries": [{"flag": "new", "ref": ""}, {"flag": "extends", "ref": "2025-01-01"}]}'
        out = self._parse(raw)
        assert len(out) == 2
        assert out[1]["flag"] == "extends"
        assert out[1]["ref"] == "2025-01-01"

    def test_parse_strips_code_fences(self):
        raw = '```json\n{"entries": [{"flag": "new", "ref": ""}]}\n```'
        out = self._parse(raw)
        assert len(out) == 1
        assert out[0]["flag"] == "new"

    def test_parse_returns_empty_on_malformed(self):
        assert self._parse("not json") == []
        assert self._parse('{"wrong_key": []}') == []
        assert self._parse('{"entries": "not a list"}') == []

    def test_parse_filters_non_dict_entries(self):
        raw = '{"entries": [{"flag": "new"}, "garbage", 42, {"flag": "extends", "ref": "2025-01-01"}]}'
        out = self._parse(raw)
        # Only the two dicts survive.
        assert len(out) == 2
        assert out[0]["flag"] == "new"
        assert out[1]["flag"] == "extends"


class TestValidateLinkageRevision:
    """Regression for n-c9614ce7: parser-side validation is the
    load-bearing rule that prevents subject/object inversion from the
    LLM contaminating hub data. The prompt is a hint; the validator is
    the contract.
    """

    def _validate(self, entry_date, flag, ref):
        from personal_mem.surfaces.cli import _validate_linkage_revision
        return _validate_linkage_revision(entry_date, flag, ref)

    def test_unknown_flag_returns_none(self):
        flag, ref, quote = self._validate("2026-03-01", "weird", "")
        assert flag is None
        assert ref == ""
        assert quote == ""

    def test_new_clears_any_ref(self):
        flag, ref, quote = self._validate("2026-03-01", "new", "2026-01-01")
        assert flag == "new"
        assert ref == ""
        assert quote == ""

    def test_valid_extends_with_earlier_ref_passes_through(self):
        flag, ref, _ = self._validate("2026-03-01", "extends", "2026-01-15")
        # No by_date_texts passed → quote validation skipped, ref accepted.
        assert flag == "extends"
        assert ref == "2026-01-15"

    def test_agrees_with_empty_ref_now_downgrades(self):
        # Strict ref-required policy: an agrees claim without a specific
        # predecessor is structurally indistinguishable from `new` —
        # demote so the DAG only carries verifiable edges.
        flag, ref, _ = self._validate("2026-03-01", "agrees", "")
        assert flag == "new"
        assert ref == ""

    def test_extends_with_future_ref_downgrades_to_new(self):
        # The exact inversion the LLM produced 51 times: entry-X cites
        # entry-Y where Y is later. Parser-side: invalid ref → drop ref.
        # Since extends REQUIRES a ref, the flag downgrades to new.
        flag, ref, _ = self._validate("2026-01-15", "extends", "2026-03-01")
        assert flag == "new"
        assert ref == ""

    def test_contradicts_with_future_ref_downgrades_to_new(self):
        flag, ref, _ = self._validate("2026-01-15", "contradicts", "2026-03-01")
        assert flag == "new"
        assert ref == ""

    def test_extends_with_same_day_ref_downgrades_to_new(self):
        # ref must be STRICTLY earlier — same-day is not a temporal edge.
        flag, ref, _ = self._validate("2026-03-01", "extends", "2026-03-01")
        assert flag == "new"
        assert ref == ""

    def test_agrees_with_future_ref_downgrades_to_new(self):
        # Stricter than before: future ref → ref dropped → since `agrees`
        # now requires a ref, the flag downgrades.
        flag, ref, _ = self._validate("2026-01-15", "agrees", "2026-03-01")
        assert flag == "new"
        assert ref == ""

    def test_malformed_ref_string_is_dropped(self):
        flag, ref, _ = self._validate("2026-03-01", "extends", "yesterday")
        # Invalid format → ref dropped → required-ref flag downgrades.
        assert flag == "new"
        assert ref == ""

    def test_empty_ref_with_required_flag_downgrades(self):
        flag, ref, _ = self._validate("2026-03-01", "extends", "")
        assert flag == "new"
        assert ref == ""

    def test_quote_validation_passes_when_substring_matches(self):
        from personal_mem.surfaces.cli import _validate_linkage_revision
        flag, ref, quote = _validate_linkage_revision(
            "2026-03-01", "extends", "2026-01-15",
            ref_quote="pytest-bdd lets you write Gherkin scenarios",
            by_date_texts={"2026-01-15": [
                "pytest-bdd lets you write Gherkin scenarios as tests"
            ]},
        )
        assert flag == "extends"
        assert ref == "2026-01-15"
        assert "pytest-bdd" in quote

    def test_quote_validation_downgrades_when_quote_absent(self):
        from personal_mem.surfaces.cli import _validate_linkage_revision
        flag, ref, _ = _validate_linkage_revision(
            "2026-03-01", "extends", "2026-01-15",
            ref_quote="something the model invented out of thin air",
            by_date_texts={"2026-01-15": [
                "an unrelated artifact about a different topic entirely"
            ]},
        )
        assert flag == "new"
        assert ref == ""

    def test_quote_validation_downgrades_when_quote_too_short(self):
        from personal_mem.surfaces.cli import _validate_linkage_revision
        flag, ref, _ = _validate_linkage_revision(
            "2026-03-01", "extends", "2026-01-15",
            ref_quote="pytest",  # < 20 chars, untrustworthy
            by_date_texts={"2026-01-15": ["pytest is fine"]},
        )
        assert flag == "new"
        assert ref == ""
