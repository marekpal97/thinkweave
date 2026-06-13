"""Surface-contract consistency tests (pre-ship audit, Bucket 4b).

The MCP <-> CLI boundary is principled — MCP = agent operations; CLI =
admin / cron / orchestration plus exactly four narrow agent-Bash entries
(`mem wrap-finalize`, `mem hubs apply-linkage`, `mem landing --doc`,
`mem judge --rejudge/--drain`) — but recent contract breaks (rejudge-queue
prose vs behavior, digest path drift) were only caught by manual audit.
These tests pin the contract mechanically:

- every MCP tool the server registers has a resolvable handler;
- every `mem <subcommand>` referenced in skill/agent markdown exists in
  the CLI dispatch table;
- every `mcp__personal-mem__mem_*` name in worker tool allowlists exists
  on the MCP server;
- both surfaces' name sets are pinned against their documented inventory
  (ARCHITECTURE.md "Invocation surface" / CLAUDE.md §7).

Dependency-light by design: no vault, no network, no fixtures — pure
imports plus a conservative regex sweep over `commands/` and `agents/`.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from personal_mem.surfaces.cli import _DISPATCH, build_parser
from personal_mem.surfaces.mcp.tools import DISPATCH as MCP_DISPATCH
from personal_mem.surfaces.mcp.tools import all_schemas

REPO_ROOT = Path(__file__).resolve().parent.parent

# The 18 MCP tools documented in CLAUDE.md §7 + ARCHITECTURE.md
# ("Invocation surface"). If you add/remove a tool, update BOTH docs and
# this pin in the same change — that's the point of the pin.
DOCUMENTED_MCP_TOOLS = {
    "mem_search",
    "mem_create",
    "mem_read",
    "mem_update",
    "mem_link",
    "mem_unlink",
    "mem_context",
    "mem_graph",
    "mem_concepts",
    "mem_extract",
    "mem_judge",
    "mem_landing",
    "mem_enrich",
    "mem_timeline",
    "mem_project_snapshot",
    "mem_queue",
    "mem_sources_config",
    "mem_prompts",
}


class TestMcpSurface:
    def test_every_registered_schema_has_a_handler(self):
        # all_schemas() is what list_tools serves; MCP_DISPATCH is what
        # call_tool resolves against. A tool in one but not the other is
        # either invisible or uncallable.
        schema_names = {tool.name for tool in all_schemas()}
        assert schema_names == set(MCP_DISPATCH), (
            "MCP schema registration and dispatch table drifted: "
            f"schema-only={schema_names - set(MCP_DISPATCH)}, "
            f"dispatch-only={set(MCP_DISPATCH) - schema_names}"
        )

    def test_every_handler_resolves_to_tools_package(self):
        # Handlers are thin wrappers living under surfaces/mcp/tools/*,
        # which in turn delegate to operations/. Pin the first hop: each
        # dispatch value is a real callable from the tools package (a
        # stale import or a renamed handler fails here, not at call_tool
        # time).
        for name, handler in MCP_DISPATCH.items():
            assert callable(handler), f"{name} handler is not callable"
            assert handler.__module__.startswith(
                "personal_mem.surfaces.mcp.tools."
            ), f"{name} handler lives in unexpected module {handler.__module__}"

    def test_tool_inventory_pinned_to_docs(self):
        # CLAUDE.md §7 says "The MCP server exposes 18 tools" and names
        # them; ARCHITECTURE.md's "Invocation surface" table repeats the
        # list. Catch additions/removals that skip the docs.
        assert {tool.name for tool in all_schemas()} == DOCUMENTED_MCP_TOOLS


class TestCliSurface:
    def test_subcommand_count_pinned(self):
        # Moved here from tests/test_rlvr_cli.py (pre-ship audit 4b) —
        # the count pin is a surface-contract concern, not an RLVR one.
        # History of the number:
        # P1-4 dropped ``mem connect`` (deprecation alias): 34 → 33.
        # `mem dream` (vault-hygiene cycle) and `mem news-stats`
        # (per-outlet drain stats) added later: 33 → 35.
        # Phase-3 prediction-judge rework adds `mem judge`: 35 → 36.
        # C24 CLI parity (Slice 4) adds unlink, timeline,
        # project-snapshot, prompts: 36 → 40.
        # `mem pause` / `mem resume` (hook pause toggle) + `mem themes`
        # (themes registry rebuild) added later: 40 → 43.
        # `mem schedule` (cross-platform scheduler — crontab / Task
        # Scheduler) added: 43 → 44.
        # Cost-tracking (`mem spend`) shipped 2026-06-01 and was removed
        # 2026-06-10 — net zero on the count.
        # `mem news-stats` removed in the 2026-06-13 pre-ship dead-code
        # sweep (zero callers in skills/docs/crontab): 44 → 43.
        # CLAUDE.md §7 reflects the same count; if either slips, the
        # other catches doc drift.
        assert len(_DISPATCH) == 43

    def test_dispatch_handlers_resolve(self):
        for name, handler in _DISPATCH.items():
            assert callable(handler), f"mem {name} handler is not callable"

    def test_parser_and_dispatch_agree(self):
        # A subcommand registered in parser.py but missing from _DISPATCH
        # raises KeyError at runtime; one in _DISPATCH but not the parser
        # is unreachable. Both are contract breaks.
        parser = build_parser()
        sub = next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        )
        parser_cmds = set(sub.choices)
        assert parser_cmds == set(_DISPATCH), (
            "argparse subcommands and _DISPATCH drifted: "
            f"parser-only={parser_cmds - set(_DISPATCH)}, "
            f"dispatch-only={set(_DISPATCH) - parser_cmds}"
        )


def _skill_and_agent_files() -> list[Path]:
    files = sorted((REPO_ROOT / "commands").glob("**/*.md"))
    files += sorted((REPO_ROOT / "agents").glob("*.md"))
    assert files, "no commands/*.md or agents/*.md found — repo layout moved?"
    return files


class TestDocReferences:
    # Conservative patterns: only count a `mem <word>` as a CLI invocation
    # when it is backticked (`mem foo ...`) or sits at the start of a line
    # inside a fenced block (optionally prefixed by `uv run`). Prose like
    # "the mem CLI" or placeholders like `mem <command>` never match.
    _BACKTICK = re.compile(r"`(?:uv run )?mem ([a-z][a-z0-9-]*)")
    _LINE_START = re.compile(r"^\s*(?:uv run )?mem ([a-z][a-z0-9-]*)", re.M)
    _MCP_TOOL = re.compile(r"mcp__personal-mem__(mem_[a-z_]+)")

    def test_doc_cli_invocations_exist_in_dispatch(self):
        # Only the first token after `mem` is checked — multi-word forms
        # (`mem hubs apply-linkage`, `mem dream scan`) are sub-actions of
        # their first token, which is the dispatch key.
        unknown: dict[str, set[str]] = {}
        for md in _skill_and_agent_files():
            text = md.read_text(encoding="utf-8")
            tokens = set(self._BACKTICK.findall(text))
            tokens |= set(self._LINE_START.findall(text))
            for token in tokens - set(_DISPATCH):
                unknown.setdefault(token, set()).add(
                    str(md.relative_to(REPO_ROOT))
                )
        assert not unknown, (
            "skill/agent markdown references `mem` subcommands that are "
            f"not in _DISPATCH: {unknown}"
        )

    def test_agent_allowlisted_mcp_tools_exist(self):
        # Worker frontmatter allowlists tools as
        # `mcp__personal-mem__mem_*`; every such name must be a tool the
        # server actually registers, or the worker silently loses it.
        unknown: dict[str, set[str]] = {}
        for md in _skill_and_agent_files():
            text = md.read_text(encoding="utf-8")
            for tool in set(self._MCP_TOOL.findall(text)) - set(MCP_DISPATCH):
                unknown.setdefault(tool, set()).add(
                    str(md.relative_to(REPO_ROOT))
                )
        assert not unknown, (
            "skill/agent markdown allowlists MCP tools the server does "
            f"not register: {unknown}"
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
