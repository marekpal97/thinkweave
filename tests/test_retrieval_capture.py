"""Tests for the MCP retrieval-event capture pipeline (slice 1 of RLVR).

Pure-function coverage on ``operations/retrieval_log.py`` (no hook invocation),
plus a focused integration check that ``surfaces/hooks/handler._handle_post``
appends a retrieval event to the buffer when an MCP retrieval tool runs.

Slice 2 (buffer→retrieval_log.jsonl split at Stop time) and slice 3 (SQLite
projection) live in their own test files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_mem.operations.retrieval_log import (
    RETRIEVAL_TOOLS,
    append_event,
    build_retrieval_event,
    parse_returned_ids,
    summarize_args,
)


# ---------------------------------------------------------------------------
# parse_returned_ids — every stamping style currently in use
# ---------------------------------------------------------------------------


class TestParseReturnedIds:
    def test_paren_style_mem_search(self):
        text = (
            "[note] Title one (n-abc123def) [tag1, tag2]\n"
            "  snippet line\n"
            "[decision] Title two (dec-99aaee11)\n"
        )
        assert parse_returned_ids(text) == ["n-abc123def", "dec-99aaee11"]

    def test_bracket_style_source_lens(self):
        text = (
            "# Source lens for [src-1234abcd] Whatever\n"
            "## Decisions (2)\n"
            "- [dec-feedbe11] First _(cites, 2026-05-13)_\n"
            "- [dec-feedbe22] Second _(cites, 2026-05-12)_\n"
        )
        assert parse_returned_ids(text) == [
            "src-1234abcd", "dec-feedbe11", "dec-feedbe22"
        ]

    def test_backtick_style_project_snapshot(self):
        text = (
            "### Use SQLite (`dec-1111aaaa`)\n"
            "- `n-2222bbbb` Some title _(2026-05-13)_\n"
        )
        assert parse_returned_ids(text) == ["dec-1111aaaa", "n-2222bbbb"]

    def test_dedup_preserves_first_order(self):
        text = "(n-aaaabbbb) appears (dec-ccccdddd) then (n-aaaabbbb) again"
        assert parse_returned_ids(text) == ["n-aaaabbbb", "dec-ccccdddd"]

    def test_no_ids_returns_empty(self):
        assert parse_returned_ids("No results found.") == []

    def test_does_not_match_prose(self):
        # Prefix tokens are reserved — "dec-" without 6+ hex digits doesn't match.
        assert parse_returned_ids("nominal Dec-2026 release") == []

    def test_matches_theme_and_concept_prefixes(self):
        text = "[thm-aaaa1111] arc — [cand-bbbb2222] candidate — `cncpt-cccc3333`"
        assert parse_returned_ids(text) == [
            "thm-aaaa1111", "cand-bbbb2222", "cncpt-cccc3333"
        ]


# ---------------------------------------------------------------------------
# summarize_args — drops unknown keys, keeps the small subset
# ---------------------------------------------------------------------------


class TestSummarizeArgs:
    def test_mem_search_keeps_whitelist(self):
        args = {
            "query": "foo", "mode": "fts", "project": "p",
            "secret": "should-not-be-logged",
        }
        out = summarize_args("mcp__personal-mem__mem_search", args)
        assert out == {"query": "foo", "mode": "fts", "project": "p"}
        assert "secret" not in out

    def test_drops_empty_values(self):
        args = {"query": "", "mode": "fts", "concepts": [], "project": "p"}
        out = summarize_args("mcp__personal-mem__mem_search", args)
        assert out == {"mode": "fts", "project": "p"}

    def test_unknown_tool_returns_empty(self):
        # Defensive — tool not in _KEEP_ARGS yields {} not crash.
        out = summarize_args("mcp__personal-mem__mem_unknown", {"foo": "bar"})
        assert out == {}


# ---------------------------------------------------------------------------
# build_retrieval_event — gate + dispatch
# ---------------------------------------------------------------------------


class TestBuildRetrievalEvent:
    def test_non_retrieval_tool_returns_none(self):
        assert build_retrieval_event("Bash", {"command": "ls"}, "", "ts") is None
        assert build_retrieval_event(
            "mcp__personal-mem__mem_create",
            {"title": "x"},
            "",
            "ts",
        ) is None

    def test_mem_search_builds_event(self):
        out = build_retrieval_event(
            "mcp__personal-mem__mem_search",
            {"query": "fts5", "mode": "fts"},
            "[note] FTS5 details (n-aaaabbbb)\n[decision] Use it (dec-ccccdddd)",
            "2026-05-13T22:00:00Z",
        )
        assert out is not None
        assert out["type"] == "retrieval"
        assert out["tool"] == "mcp__personal-mem__mem_search"
        assert out["args"] == {"query": "fts5", "mode": "fts"}
        assert out["returned_ids"] == ["n-aaaabbbb", "dec-ccccdddd"]
        assert out["ts"] == "2026-05-13T22:00:00Z"

    def test_mem_read_uses_arg_id_not_body(self):
        # The note body might or might not stamp its own id; the arg is canonical.
        out = build_retrieval_event(
            "mcp__personal-mem__mem_read",
            {"id": "n-1111aaaa"},
            "# Some title\n\nprose body, no id stamping.",
            "ts",
        )
        assert out is not None
        assert out["returned_ids"] == ["n-1111aaaa"]

    def test_mem_read_missing_id_yields_empty_list(self):
        out = build_retrieval_event(
            "mcp__personal-mem__mem_read",
            {},
            "Note not found.",
            "ts",
        )
        assert out is not None
        assert out["returned_ids"] == []

    def test_tool_output_non_string_is_handled(self):
        # If the harness ever passes a non-str (defensive), we don't crash.
        out = build_retrieval_event(
            "mcp__personal-mem__mem_search",
            {"query": "x"},
            None,  # type: ignore[arg-type]
            "ts",
        )
        assert out["returned_ids"] == []


# ---------------------------------------------------------------------------
# RETRIEVAL_TOOLS — names match the dash-form server name from install.py
# ---------------------------------------------------------------------------


class TestRetrievalToolNaming:
    def test_dash_not_underscore_in_server_name(self):
        # personal-mem (dash) per install.py:SERVER_NAME — this is the most
        # error-prone detail in the whole capture path, so we pin it.
        for name in RETRIEVAL_TOOLS:
            assert name.startswith("mcp__personal-mem__")
            assert "mcp__personal_mem__" not in name

    def test_closed_set_excludes_mutation_tools(self):
        assert "mcp__personal-mem__mem_create" not in RETRIEVAL_TOOLS
        assert "mcp__personal-mem__mem_link" not in RETRIEVAL_TOOLS
        assert "mcp__personal-mem__mem_extract" not in RETRIEVAL_TOOLS


# ---------------------------------------------------------------------------
# append_event — JSONL roundtrip
# ---------------------------------------------------------------------------


class TestAppendEvent:
    def test_writes_one_line_per_call(self, tmp_path: Path):
        buf = tmp_path / "buffer" / "ses-xxx.jsonl"
        append_event(buf, {"a": 1})
        append_event(buf, {"b": 2})
        lines = buf.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"a": 1}
        assert json.loads(lines[1]) == {"b": 2}

    def test_creates_parent_dir(self, tmp_path: Path):
        buf = tmp_path / "deep" / "nested" / "ses.jsonl"
        append_event(buf, {"x": 1})
        assert buf.exists()


# ---------------------------------------------------------------------------
# Integration: _handle_post buffers retrieval events for MCP tools
# ---------------------------------------------------------------------------


class TestHandlerIntegration:
    def test_handle_post_buffers_retrieval_event(
        self, tmp_path: Path, monkeypatch
    ):
        # Point load_config at a fresh tmp vault.
        vault = tmp_path / "vault"
        monkeypatch.setenv("PERSONAL_MEM_VAULT", str(vault))
        monkeypatch.setenv("PERSONAL_MEM_PROJECT", "t")

        # Silence/short-circuit _ensure_session — it touches the indexer and
        # the vault root; for this test we only care that the buffer line gets
        # written, not that a full session note is materialised.
        from personal_mem.surfaces.hooks import handler as h

        monkeypatch.setattr(h, "_ensure_session", lambda *a, **kw: None)
        # And make _output a no-op so it doesn't print to stdout during pytest.
        monkeypatch.setattr(h, "_output", lambda *a, **kw: None)

        session_id = "ses-aaaa1111"
        hook_input = {
            "session_id": session_id,
            "tool_input": {"query": "fts5 details", "mode": "fts"},
            "tool_output": "[note] FTS5 details (n-bbbb2222) [refs]\n",
        }
        h._handle_post("mcp__personal-mem__mem_search", hook_input)

        # The buffer file should now exist and contain one retrieval event.
        from personal_mem.core.config import load_config

        cfg = load_config()
        buf_path = cfg.mem_dir / "buffer" / f"{session_id}.jsonl"
        assert buf_path.exists()
        lines = buf_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["type"] == "retrieval"
        assert event["tool"] == "mcp__personal-mem__mem_search"
        assert event["returned_ids"] == ["n-bbbb2222"]

    def test_handle_post_ignores_non_retrieval_mcp_tools(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv("PERSONAL_MEM_VAULT", str(tmp_path / "vault"))
        from personal_mem.surfaces.hooks import handler as h

        monkeypatch.setattr(h, "_ensure_session", lambda *a, **kw: None)
        monkeypatch.setattr(h, "_output", lambda *a, **kw: None)
        # If the gate fails, _buffer_event would still be called — guard:
        called = []
        monkeypatch.setattr(h, "_buffer_event", lambda *a, **kw: called.append(a))

        h._handle_post(
            "mcp__personal-mem__mem_create",
            {"session_id": "ses-yy", "tool_input": {"title": "x"}, "tool_output": ""},
        )
        assert called == []
