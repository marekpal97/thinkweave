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


from thinkweave.operations.retrieval_log import (
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
    def test_paren_style_weave_search(self):
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
    def test_weave_search_keeps_whitelist(self):
        args = {
            "query": "foo", "mode": "fts", "project": "p",
            "secret": "should-not-be-logged",
        }
        out = summarize_args("mcp__thinkweave__weave_search", args)
        assert out == {"query": "foo", "mode": "fts", "project": "p"}
        assert "secret" not in out

    def test_drops_empty_values(self):
        args = {"query": "", "mode": "fts", "concepts": [], "project": "p"}
        out = summarize_args("mcp__thinkweave__weave_search", args)
        assert out == {"mode": "fts", "project": "p"}

    def test_unknown_tool_returns_empty(self):
        # Defensive — tool not in _KEEP_ARGS yields {} not crash.
        out = summarize_args("mcp__thinkweave__weave_unknown", {"foo": "bar"})
        assert out == {}


# ---------------------------------------------------------------------------
# build_retrieval_event — gate + dispatch
# ---------------------------------------------------------------------------


class TestBuildRetrievalEvent:
    def test_non_retrieval_tool_returns_none(self):
        assert build_retrieval_event("Bash", {"command": "ls"}, "", "ts") is None
        assert build_retrieval_event(
            "mcp__thinkweave__weave_create",
            {"title": "x"},
            "",
            "ts",
        ) is None

    def test_weave_search_builds_event(self):
        out = build_retrieval_event(
            "mcp__thinkweave__weave_search",
            {"query": "fts5", "mode": "fts"},
            "[note] FTS5 details (n-aaaabbbb)\n[decision] Use it (dec-ccccdddd)",
            "2026-05-13T22:00:00Z",
        )
        assert out is not None
        assert out["type"] == "retrieval"
        assert out["tool"] == "mcp__thinkweave__weave_search"
        assert out["args"] == {"query": "fts5", "mode": "fts"}
        assert out["returned_ids"] == ["n-aaaabbbb", "dec-ccccdddd"]
        assert out["ts"] == "2026-05-13T22:00:00Z"

    def test_weave_read_uses_arg_id_not_body(self):
        # The note body might or might not stamp its own id; the arg is canonical.
        out = build_retrieval_event(
            "mcp__thinkweave__weave_read",
            {"id": "n-1111aaaa"},
            "# Some title\n\nprose body, no id stamping.",
            "ts",
        )
        assert out is not None
        assert out["returned_ids"] == ["n-1111aaaa"]

    def test_weave_read_missing_id_yields_empty_list(self):
        out = build_retrieval_event(
            "mcp__thinkweave__weave_read",
            {},
            "Note not found.",
            "ts",
        )
        assert out is not None
        assert out["returned_ids"] == []

    def test_tool_output_non_string_is_handled(self):
        # If the harness ever passes a non-str (defensive), we don't crash.
        out = build_retrieval_event(
            "mcp__thinkweave__weave_search",
            {"query": "x"},
            None,  # type: ignore[arg-type]
            "ts",
        )
        assert out["returned_ids"] == []

    def test_tool_output_dict_shape_extracts_ids(self):
        # Claude Code's PostToolUse delivers Bash output as a dict with
        # stdout/stderr; non-hook callers (tests, headless catch-up flows)
        # sometimes hand that raw shape to build_retrieval_event without
        # going through the hook handler's _extract_tool_output_text
        # normaliser. Defensive in-function normalisation keeps the parse
        # working regardless.
        out = build_retrieval_event(
            "mcp__thinkweave__weave_search",
            {"query": "x"},
            {
                "stdout": "[note] Hit (n-abc123de) [tag]\n",
                "stderr": "",
                "interrupted": False,
            },
            "ts",
        )
        assert out["returned_ids"] == ["n-abc123de"]


# ---------------------------------------------------------------------------
# RETRIEVAL_TOOLS — names match the dash-form server name from install.py
# ---------------------------------------------------------------------------


class TestRetrievalToolNaming:
    def test_server_name_prefix(self):
        # The tool-name prefix must match install.py:SERVER_NAME ("thinkweave").
        # This is the most error-prone detail in the whole capture path, so we
        # pin it exactly. (Pre-rename, when the server was "personal-weave", this
        # also guarded a dash-vs-underscore footgun; the single-token name
        # "thinkweave" removes that hazard.)
        for name in RETRIEVAL_TOOLS:
            assert name.startswith("mcp__thinkweave__")

    def test_closed_set_excludes_mutation_tools(self):
        assert "mcp__thinkweave__weave_create" not in RETRIEVAL_TOOLS
        assert "mcp__thinkweave__weave_link" not in RETRIEVAL_TOOLS
        assert "mcp__thinkweave__weave_extract" not in RETRIEVAL_TOOLS


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
        monkeypatch.setenv("THINKWEAVE_VAULT", str(vault))
        monkeypatch.setenv("THINKWEAVE_PROJECT", "t")

        # Silence/short-circuit _ensure_session — it touches the indexer and
        # the vault root; for this test we only care that the buffer line gets
        # written, not that a full session note is materialised.
        from thinkweave.surfaces.hooks import handler as h

        monkeypatch.setattr(h, "_ensure_session", lambda *a, **kw: None)
        # And make _output a no-op so it doesn't print to stdout during pytest.
        monkeypatch.setattr(h, "_output", lambda *a, **kw: None)

        session_id = "ses-aaaa1111"
        hook_input = {
            "session_id": session_id,
            "tool_input": {"query": "fts5 details", "mode": "fts"},
            "tool_output": "[note] FTS5 details (n-bbbb2222) [refs]\n",
        }
        h._handle_post("mcp__thinkweave__weave_search", hook_input)

        # The buffer file should now exist and contain one retrieval event.
        from thinkweave.core.config import load_config

        cfg = load_config()
        buf_path = cfg.weave_dir / "buffer" / f"{session_id}.jsonl"
        assert buf_path.exists()
        lines = buf_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["type"] == "retrieval"
        assert event["tool"] == "mcp__thinkweave__weave_search"
        assert event["returned_ids"] == ["n-bbbb2222"]

    def test_handle_post_ignores_non_retrieval_mcp_tools(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv("THINKWEAVE_VAULT", str(tmp_path / "vault"))
        from thinkweave.surfaces.hooks import handler as h

        monkeypatch.setattr(h, "_ensure_session", lambda *a, **kw: None)
        monkeypatch.setattr(h, "_output", lambda *a, **kw: None)
        # If the gate fails, _buffer_event would still be called — guard:
        called = []
        monkeypatch.setattr(h, "_buffer_event", lambda *a, **kw: called.append(a))

        h._handle_post(
            "mcp__thinkweave__weave_create",
            {"session_id": "ses-yy", "tool_input": {"title": "x"}, "tool_output": ""},
        )
        assert called == []

    def test_handle_post_retrieval_defers_session_materialisation(
        self, tmp_path: Path, monkeypatch
    ):
        """Retrieval-tool PostToolUse must not invoke ``_ensure_session``.

        Regression-pin for the 2026-05-26 finding: on a populated vault,
        ``_ensure_session`` rgloss the entire vault to find the active
        session note. For retrieval calls (weave_search etc.) that scan
        blew Claude Code's 5s hook timeout, the hook was cancelled, and
        the buffer write never landed — empirical symptom on disk was
        zero ``retrieval_log.jsonl`` files and zero ``onthefly`` rows in
        ``context_served``.
        """
        vault = tmp_path / "vault"
        monkeypatch.setenv("THINKWEAVE_VAULT", str(vault))
        monkeypatch.setenv("THINKWEAVE_PROJECT", "t")

        from thinkweave.surfaces.hooks import handler as h

        ensure_calls = []
        monkeypatch.setattr(
            h,
            "_ensure_session",
            lambda *a, **kw: ensure_calls.append(a),
        )
        monkeypatch.setattr(h, "_output", lambda *a, **kw: None)

        h._handle_post(
            "mcp__thinkweave__weave_search",
            {
                "session_id": "ses-aaaa1111",
                "tool_input": {"query": "x"},
                "tool_output": "[note] hit (n-bbbbcccc)",
            },
        )
        # Buffer line still landed.
        from thinkweave.core.config import load_config

        cfg = load_config()
        buf_path = cfg.weave_dir / "buffer" / "ses-aaaa1111.jsonl"
        assert buf_path.exists()
        assert ensure_calls == []  # The hot-path skip we depend on.

    def test_handle_post_action_tool_still_materialises_session(
        self, tmp_path: Path, monkeypatch
    ):
        """Action-tool path (Write/Edit/Bash) keeps materialising the session.

        Symmetric counterpart of the retrieval-defer test — action tools
        still own session-note creation so MCP retrievals can discover
        the note mid-conversation.
        """
        vault = tmp_path / "vault"
        monkeypatch.setenv("THINKWEAVE_VAULT", str(vault))
        monkeypatch.setenv("THINKWEAVE_PROJECT", "t")

        from thinkweave.surfaces.hooks import handler as h

        ensure_calls = []
        monkeypatch.setattr(
            h, "_ensure_session", lambda *a, **kw: ensure_calls.append(a)
        )
        monkeypatch.setattr(h, "_output", lambda *a, **kw: None)

        h._handle_post(
            "Bash",
            {
                "session_id": "ses-cccc2222",
                "tool_input": {"command": "git commit -m 'x'"},
                "tool_output": "[main abcd123] x",
            },
        )
        assert len(ensure_calls) == 1

    def test_handle_post_accepts_tool_response_alias(
        self, tmp_path: Path, monkeypatch
    ):
        """Newer Claude Code payloads use ``tool_response`` not ``tool_output``.

        The handler must accept either so the capture path doesn't silently
        drop returned IDs across harness versions.
        """
        vault = tmp_path / "vault"
        monkeypatch.setenv("THINKWEAVE_VAULT", str(vault))
        monkeypatch.setenv("THINKWEAVE_PROJECT", "t")

        from thinkweave.surfaces.hooks import handler as h

        monkeypatch.setattr(h, "_ensure_session", lambda *a, **kw: None)
        monkeypatch.setattr(h, "_output", lambda *a, **kw: None)

        h._handle_post(
            "mcp__thinkweave__weave_search",
            {
                "session_id": "ses-dddd3333",
                "tool_input": {"query": "x"},
                # ``tool_response``, not ``tool_output`` — the new shape.
                "tool_response": "[note] hit (n-eeee4444)",
            },
        )
        from thinkweave.core.config import load_config

        cfg = load_config()
        buf_path = cfg.weave_dir / "buffer" / "ses-dddd3333.jsonl"
        assert buf_path.exists()
        event = json.loads(buf_path.read_text(encoding="utf-8").splitlines()[0])
        assert event["returned_ids"] == ["n-eeee4444"]


# ---------------------------------------------------------------------------
# Fast session-note lookup via SQLite index
# ---------------------------------------------------------------------------


class TestFindSessionNoteFast:
    """``_find_session_note`` must answer from SQLite, not an rglob scan.

    Cheapness here is load-bearing for the retrieval-capture pipeline —
    every PostToolUse hook walks this path, and a vault-wide rglob blows
    Claude Code's 5s hook timeout for retrieval calls.
    """

    def _build_vault_with_session(self, tmp_path: Path, source_session: str):
        from thinkweave.core.config import Config
        from thinkweave.core.indexer import Indexer
        from thinkweave.core.schemas import NoteType
        from thinkweave.core.vault import VaultManager

        vault = tmp_path / "vault"
        cfg = Config(vault_root=vault)
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        vm.create_note(
            NoteType.SESSION,
            "Test session",
            project="t",
            extra_frontmatter={"source_session": source_session},
        )
        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()
        return cfg, vm

    def test_indexed_session_found_via_sql(self, tmp_path: Path):
        from thinkweave.surfaces.hooks.handler import _find_session_note

        cfg, vm = self._build_vault_with_session(tmp_path, "ses-cc-abc123")
        found = _find_session_note(vm, "ses-cc-abc123")
        assert found is not None
        assert found.exists()

    def test_missing_session_returns_none(self, tmp_path: Path):
        from thinkweave.surfaces.hooks.handler import _find_session_note

        cfg, vm = self._build_vault_with_session(tmp_path, "ses-cc-abc123")
        assert _find_session_note(vm, "ses-cc-nope-nope") is None

    def test_empty_session_id_returns_none(self, tmp_path: Path):
        from thinkweave.surfaces.hooks.handler import _find_session_note

        cfg, vm = self._build_vault_with_session(tmp_path, "ses-cc-abc123")
        assert _find_session_note(vm, "") is None

    def test_falls_back_to_vault_scan_when_db_missing(
        self, tmp_path: Path, monkeypatch
    ):
        """If the index DB doesn't exist yet, the slow path still works.

        First-PostToolUse-of-a-fresh-vault edge case — the note exists on
        disk but hasn't been indexed yet. The vault-scan fallback covers it.
        """
        from thinkweave.core.config import Config
        from thinkweave.core.schemas import NoteType
        from thinkweave.core.vault import VaultManager
        from thinkweave.surfaces.hooks.handler import _find_session_note

        vault = tmp_path / "vault"
        cfg = Config(vault_root=vault)
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        vm.create_note(
            NoteType.SESSION,
            "Unindexed session",
            project="t",
            extra_frontmatter={"source_session": "ses-no-index"},
        )
        # Deliberately do NOT rebuild the index. ``index_db`` may not even exist.
        if cfg.index_db.exists():
            cfg.index_db.unlink()

        assert _find_session_note(vm, "ses-no-index") is not None


# ---------------------------------------------------------------------------
# End-to-end: retrieval event → events.jsonl partition → context_served
# ---------------------------------------------------------------------------


class TestRetrievalPipelineEndToEnd:
    """Walk the full chain on a tmp vault.

    Buffer write → ``archive_buffer`` partition → ``Indexer._rebuild_context_served``
    → ``context_served`` row with ``source='onthefly'``. Catches gaps in
    any of the three stages.
    """

    def test_retrieval_event_projects_onthefly_row(self, tmp_path: Path, monkeypatch):
        from thinkweave.core.buffer import archive_buffer
        from thinkweave.core.config import Config
        from thinkweave.core.indexer import Indexer
        from thinkweave.core.schemas import NoteType
        from thinkweave.core.vault import VaultManager

        vault = tmp_path / "vault"
        cfg = Config(vault_root=vault)
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        session_path = vm.create_note(
            NoteType.SESSION,
            "E2E session",
            project="t",
            extra_frontmatter={"source_session": "ses-e2e-aaaa"},
        )

        # Stage 1: write a retrieval event to the buffer (simulates the
        # PostToolUse hook's _buffer_event call).
        from thinkweave.operations.retrieval_log import (
            append_event,
            build_retrieval_event,
        )

        buf_path = cfg.weave_dir / "buffer" / "ses-e2e-aaaa.jsonl"
        ev = build_retrieval_event(
            "mcp__thinkweave__weave_search",
            {"query": "topic"},
            "[note] topic note (n-aaaaaaaa)",
            "2026-05-26T00:00:00+00:00",
        )
        append_event(buf_path, ev)

        # Stage 2: archive_buffer partitions retrieval events into the
        # sibling retrieval_log.jsonl.
        archive_buffer(cfg.weave_dir, "ses-e2e-aaaa", session_path.parent)
        retrieval_log = session_path.parent / "retrieval_log.jsonl"
        assert retrieval_log.exists()
        lines = retrieval_log.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["type"] == "retrieval"

        # Stage 3: indexer projects the retrieval log into context_served.
        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        rows = idx.db.execute(
            "SELECT note_id, source FROM context_served "
            "WHERE session_id = ? ORDER BY source, note_id",
            (idx.db.execute(
                "SELECT id FROM notes WHERE path = ?",
                (str(session_path.relative_to(vault)),),
            ).fetchone()[0],),
        ).fetchall()
        idx.close()

        # The note returned by the retrieval event lands as onthefly.
        sources = {(r[0], r[1]) for r in rows}
        assert ("n-aaaaaaaa", "onthefly") in sources

    def test_stop_hook_materialises_session_for_retrieval_only_buffer(
        self, tmp_path: Path, monkeypatch
    ):
        """Retrieval-only sessions still get a session note + archived log.

        Defends the Stop-hook fallback path: when no Write/Edit/Bash
        events fired, no session note was materialised mid-session
        (deferred by the retrieval-defer fix). Stop must notice the
        buffer and create one, otherwise the retrieval log is orphaned.
        """
        from thinkweave.core.config import Config
        from thinkweave.operations.retrieval_log import (
            append_event,
            build_retrieval_event,
        )
        from thinkweave.surfaces.hooks import handler as h

        vault = tmp_path / "vault"
        cfg = Config(vault_root=vault)
        # Drop a retrieval event into the buffer without ever creating a
        # session note (simulates a session that only ran weave_search).
        (cfg.weave_dir / "buffer").mkdir(parents=True, exist_ok=True)
        ev = build_retrieval_event(
            "mcp__thinkweave__weave_search",
            {"query": "x"},
            "(n-zzzz9999)",
            "2026-05-26T00:00:00+00:00",
        )
        append_event(cfg.weave_dir / "buffer" / "ses-retrieval-only.jsonl", ev)

        monkeypatch.setattr(
            "thinkweave.core.config.load_config", lambda: cfg
        )
        monkeypatch.setattr(h, "_output", lambda *a, **kw: None)
        monkeypatch.setattr(h, "_detect_project", lambda hi: "t")

        h._handle_stop({"session_id": "ses-retrieval-only", "cwd": str(vault)})

        # Stop created a session note + archived the retrieval log.
        from thinkweave.core.vault import VaultManager
        from thinkweave.core.schemas import NoteType

        vm = VaultManager(config=cfg)
        sessions = [
            n
            for n in vm.list_notes(note_type=NoteType.SESSION, limit=20)
            if n.frontmatter.get("source_session") == "ses-retrieval-only"
        ]
        assert len(sessions) == 1
        session_dir = (vault / sessions[0].path).parent
        assert (session_dir / "retrieval_log.jsonl").exists()
