"""Surface-contract consistency tests (pre-ship audit, Bucket 4b).

The MCP <-> CLI boundary is principled — MCP = agent operations; CLI =
admin / cron / orchestration plus exactly four narrow agent-Bash entries
(`weave wrap-finalize`, `weave hubs apply-linkage`, `weave landing --doc`,
`weave judge --rejudge/--drain`) — but recent contract breaks (rejudge-queue
prose vs behavior, digest path drift) were only caught by manual audit.
These tests pin the contract mechanically:

- every MCP tool the server registers has a resolvable handler;
- every `weave <subcommand>` referenced in skill/agent markdown exists in
  the CLI dispatch table;
- every `mcp__thinkweave__weave_*` name in worker tool allowlists exists
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

from thinkweave.surfaces.cli import _DISPATCH, build_parser
from thinkweave.surfaces.mcp.tools import DISPATCH as MCP_DISPATCH
from thinkweave.surfaces.mcp.tools import all_schemas

REPO_ROOT = Path(__file__).resolve().parent.parent

# The 17 MCP tools documented in CLAUDE.md §7 + ARCHITECTURE.md
# ("Invocation surface"). If you add/remove a tool, update BOTH docs and
# this pin in the same change — that's the point of the pin.
DOCUMENTED_MCP_TOOLS = {
    "weave_search",
    "weave_create",
    "weave_read",
    "weave_update",
    "weave_link",
    "weave_unlink",
    "weave_context",
    "weave_graph",
    "weave_concepts",
    "weave_extract",
    "weave_judge",
    "weave_landing",
    "weave_timeline",
    "weave_project_snapshot",
    "weave_queue",
    "weave_sources_config",
    "weave_prompts",
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
                "thinkweave.surfaces.mcp.tools."
            ), f"{name} handler lives in unexpected module {handler.__module__}"

    def test_tool_inventory_pinned_to_docs(self):
        # CLAUDE.md §7 says "The MCP server exposes 17 tools" and names
        # them; ARCHITECTURE.md's "Invocation surface" table repeats the
        # list. Catch additions/removals that skip the docs.
        assert {tool.name for tool in all_schemas()} == DOCUMENTED_MCP_TOOLS


class TestCliSurface:
    def test_subcommand_count_pinned(self):
        # Moved here from tests/test_rlvr_cli.py (pre-ship audit 4b) —
        # the count pin is a surface-contract concern, not an RLVR one.
        # History of the number:
        # P1-4 dropped ``weave connect`` (deprecation alias): 34 → 33.
        # `weave dream` (vault-hygiene cycle) and `weave news-stats`
        # (per-outlet drain stats) added later: 33 → 35.
        # Phase-3 prediction-judge rework adds `weave judge`: 35 → 36.
        # C24 CLI parity (Slice 4) adds unlink, timeline,
        # project-snapshot, prompts: 36 → 40.
        # `weave pause` / `weave resume` (hook pause toggle) + `weave themes`
        # (themes registry rebuild) added later: 40 → 43.
        # `weave schedule` (cross-platform scheduler — crontab / Task
        # Scheduler) added: 43 → 44.
        # Cost-tracking (`weave spend`) shipped 2026-06-01 and was removed
        # 2026-06-10 — net zero on the count.
        # `weave news-stats` removed in the 2026-06-13 pre-ship dead-code
        # sweep (zero callers in skills/docs/crontab): 44 → 43.
        # `weave seam` (CC-auto-memory↔vault reconciliation — the
        # dream-seam-worker's surface/commit hands) added 2026-06-13:
        # 43 → 44.
        # (A `weave note-format` subcommand was briefly added then dropped
        # 2026-06-13 — note-format skeletons are seeded into the vault at
        # init and the writers Read them directly, so no CLI is needed.)
        # `weave dev-link` / `dev-unlink` (clone-dev flagless plugin loading
        # via a ~/.claude/skills/ symlink — the @skills-dir mechanism) added
        # 2026-06-14: 44 → 46.
        # `weave enrich` (deferred concept-tagging backfill) removed
        # 2026-06-16 — concepts are now proposed inline at note creation, so
        # the standalone deferred pass is gone: 46 → 45.
        # `weave config` (show + set-vault — a platform-resolved user-config
        # surface so /onboard never shells `uv run python -c` or hardcodes the
        # XDG path, which is wrong on Windows) added 2026-06-21: 45 → 46.
        # `weave trajectory` (judge — the deterministic issue-loop trajectory
        # outcome judge / phase-2 dream-outcome-worker rail, issue #60) added
        # 2026-07-18: 46 → 47.
        # `weave steering` (evidence / gate — the evidence-gated steering + weekly
        # budget the slow self-improvement loop #61 calls before filing proposals,
        # issue #62) added 2026-07-18: 47 → 48.
        # docs/CLI-AND-MCP.md ("The CLI exposes N subcommands" + the "N CLI
        # subcommands × 17 MCP tools" inventory header) reflects the same count;
        # if either slips, the other catches doc drift.
        assert len(_DISPATCH) == 48

    def test_dispatch_handlers_resolve(self):
        for name, handler in _DISPATCH.items():
            assert callable(handler), f"weave {name} handler is not callable"

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
    # Conservative patterns: only count a `weave <word>` as a CLI invocation
    # when it is backticked (`weave foo ...`) or sits at the start of a line
    # inside a fenced block (optionally prefixed by `uv run`). Prose like
    # "the weave CLI" or placeholders like `weave <command>` never match.
    _BACKTICK = re.compile(r"`(?:uv run )?weave ([a-z][a-z0-9-]*)")
    _LINE_START = re.compile(r"^\s*(?:uv run )?weave ([a-z][a-z0-9-]*)", re.M)
    _MCP_TOOL = re.compile(r"mcp__thinkweave__(weave_[a-z_]+)")

    def test_doc_cli_invocations_exist_in_dispatch(self):
        # Only the first token after `weave` is checked — multi-word forms
        # (`weave hubs apply-linkage`, `weave dream scan`) are sub-actions of
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
            "skill/agent markdown references `weave` subcommands that are "
            f"not in _DISPATCH: {unknown}"
        )

    def test_every_dispatch_subcommand_documented_in_cli_reference(self):
        # The reverse of test_doc_cli_invocations_exist_in_dispatch: every
        # `weave <subcommand>` in _DISPATCH must appear in the CLI reference
        # (docs/CLI-AND-MCP.md), so a new subcommand that skips the doc is
        # caught here — the exact drift that shipped `weave trajectory` /
        # `weave steering` while the doc still said 46/47.
        doc = (REPO_ROOT / "docs" / "CLI-AND-MCP.md").read_text(encoding="utf-8")
        documented = set(self._BACKTICK.findall(doc))
        documented |= set(self._LINE_START.findall(doc))
        undocumented = set(_DISPATCH) - documented
        assert not undocumented, (
            "weave subcommands in _DISPATCH but absent from docs/CLI-AND-MCP.md: "
            f"{sorted(undocumented)}"
        )

    def test_agent_allowlisted_mcp_tools_exist(self):
        # Worker frontmatter allowlists tools as
        # `mcp__thinkweave__weave_*`; every such name must be a tool the
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
