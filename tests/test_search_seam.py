"""Characterization tests for the search surfaces (issue #23, C1).

These pin the *observable* behavior of the CLI ``cmd_search`` (stdout shape)
and the MCP ``handle_search`` (returned ``TextContent`` shape) across the
fts / similar / hybrid modes plus the empty-result path. They guard the
refactor that reroutes both surfaces through ``operations.search.query_*``:
they must pass against the pre-refactor code and stay green after it.

Seams under test: the two surface entry points. No test reaches into the
``retrieval.search.Search`` internals — only what a caller of the surface
observes.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager
from thinkweave.surfaces.cli import notes as cli_notes
from thinkweave.surfaces.mcp.tools import search as mcp_search


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(vault_root=tmp_path / "vault")


@pytest.fixture
def populated(config: Config) -> Config:
    vm = VaultManager(config=config)
    vm.ensure_dirs()
    vm.create_note(
        NoteType.NOTE,
        "Alpha SQLite note",
        body="sqlite full text search is fast",
        project="p1",
        extra_frontmatter={"concepts": ["sqlite"]},
    )
    vm.create_note(
        NoteType.NOTE,
        "Beta markdown note",
        body="markdown parsing details",
        project="p1",
        extra_frontmatter={"concepts": ["markdown"]},
    )
    idx = Indexer(config=config)
    idx.rebuild(full=True)
    idx.close()
    return config


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        query="",
        mode="fts",
        semantic=False,
        type="",
        project="",
        tags="",
        limit=10,
        concept="",
        match_mode="any",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# CLI — cmd_search (stdout shape)
# ---------------------------------------------------------------------------


class TestCliSearchShape:
    def test_fts_hit_shape(self, populated, monkeypatch, capsys):
        monkeypatch.setattr(cli_notes, "load_config", lambda: populated)
        cli_notes.cmd_search(_args(query="sqlite", project="p1"))
        out = capsys.readouterr().out
        assert "[note]" in out
        assert "Alpha SQLite note" in out
        assert "Beta markdown note" not in out

    def test_hybrid_falls_back_to_fts(self, populated, monkeypatch, capsys):
        # No embeddings db in a scratch vault → hybrid returns FTS-only.
        monkeypatch.setattr(cli_notes, "load_config", lambda: populated)
        cli_notes.cmd_search(_args(query="sqlite", project="p1", mode="hybrid"))
        out = capsys.readouterr().out
        assert "Alpha SQLite note" in out

    def test_similar_without_embeddings_message(self, populated, monkeypatch, capsys):
        monkeypatch.setattr(cli_notes, "load_config", lambda: populated)
        cli_notes.cmd_search(_args(query="sqlite", project="p1", mode="similar"))
        out = capsys.readouterr().out
        assert "No semantic results" in out

    def test_semantic_flag_aliases_similar(self, populated, monkeypatch, capsys):
        monkeypatch.setattr(cli_notes, "load_config", lambda: populated)
        cli_notes.cmd_search(_args(query="sqlite", project="p1", semantic=True))
        out = capsys.readouterr().out
        assert "No semantic results" in out

    def test_no_results_message(self, populated, monkeypatch, capsys):
        monkeypatch.setattr(cli_notes, "load_config", lambda: populated)
        cli_notes.cmd_search(_args(query="zzznomatchzzz", project="p1"))
        out = capsys.readouterr().out
        assert "No results found." in out


# ---------------------------------------------------------------------------
# MCP — handle_search (TextContent shape)
# ---------------------------------------------------------------------------


class TestMcpSearchShape:
    def test_fts_hit_shape(self, populated):
        res = mcp_search.handle_search(populated, {"query": "sqlite", "project": "p1"})
        assert len(res) == 1
        text = res[0].text
        assert "[note] Alpha SQLite note (" in text
        assert "Beta markdown note" not in text

    def test_hybrid_falls_back_to_fts(self, populated):
        res = mcp_search.handle_search(
            populated, {"query": "sqlite", "project": "p1", "mode": "hybrid"}
        )
        assert "Alpha SQLite note" in res[0].text

    def test_similar_without_embeddings_message(self, populated):
        res = mcp_search.handle_search(
            populated, {"query": "sqlite", "project": "p1", "mode": "similar"}
        )
        assert res[0].text.startswith("No semantic results")

    def test_no_results_message(self, populated):
        res = mcp_search.handle_search(
            populated, {"query": "zzznomatchzzz", "project": "p1"}
        )
        assert res[0].text == "No results found."


def _ctx_args(**overrides) -> argparse.Namespace:
    base = dict(query="", project="", tags="", concepts="", limit=5)
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# CLI — cmd_context / MCP — handle_context (query_context consumers)
# ---------------------------------------------------------------------------


class TestContextShape:
    def test_cli_context_hit_shape(self, populated, monkeypatch, capsys):
        monkeypatch.setattr(cli_notes, "load_config", lambda: populated)
        cli_notes.cmd_context(_ctx_args(query="sqlite", project="p1"))
        out = capsys.readouterr().out
        assert "[note]" in out
        assert "Alpha SQLite note" in out

    def test_cli_context_empty_message(self, config, monkeypatch, capsys):
        # Empty (but indexed) vault → "No context available."
        VaultManager(config=config).ensure_dirs()
        idx = Indexer(config=config)
        idx.rebuild(full=True)
        idx.close()
        monkeypatch.setattr(cli_notes, "load_config", lambda: config)
        cli_notes.cmd_context(_ctx_args(query="nothingatall", project="p1"))
        out = capsys.readouterr().out
        assert "No context available." in out

    def test_mcp_context_hit_shape(self, populated):
        res = mcp_search.handle_context(
            populated, {"query": "sqlite", "project": "p1"}
        )
        assert "[note] Alpha SQLite note (" in res[0].text

    def test_mcp_context_empty_message(self, config):
        VaultManager(config=config).ensure_dirs()
        idx = Indexer(config=config)
        idx.rebuild(full=True)
        idx.close()
        res = mcp_search.handle_context(
            config, {"query": "nothingatall", "project": "p1"}
        )
        assert res[0].text == "No context available."
