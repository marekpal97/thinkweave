"""The Codex hooks adapter (issue #107, W2b).

Sources of truth for every expected value here — all independent of the code
under test, all verified against codex-cli 0.146.0 on 2026-08-02:

* The **hook config format**. Codex loads lifecycle hooks from ``hooks.json``
  files *or* inline ``[hooks]`` TOML tables sitting next to an active config
  layer; the four useful locations are ``~/.codex/hooks.json``,
  ``~/.codex/config.toml``, ``<repo>/.codex/hooks.json``,
  ``<repo>/.codex/config.toml``. "If a single layer contains both
  ``hooks.json`` and inline ``[hooks]``, Codex loads both and warns. Prefer one
  representation per layer." Since #106 already owns ``~/.codex/config.toml``
  for the ``[mcp_servers]`` registration, hooks go in the *sibling*
  ``hooks.json`` — one representation per layer, no warning.
  (https://learn.chatgpt.com/docs/hooks, "Hooks" chapter of
  https://learn.chatgpt.com/docs/codex-manual.md)

* The **file shape** is the same ``{"hooks": {Event: [{matcher, hooks: [...]}]}}``
  object Claude Code nests inside ``settings.json`` — verbatim from the manual's
  ``hooks.json`` example, plus an optional top-level ``description``.

* The **event names** are identical to Claude Code's: ``PreToolUse``,
  ``PermissionRequest``, ``PostToolUse``, ``PreCompact``, ``PostCompact``,
  ``SessionStart``, ``SessionEnd``, ``SubagentStart``, ``SubagentStop``,
  ``UserPromptSubmit``, ``Stop``. Confirmed twice over: the manual's matcher
  table, and the ``hook_event_name`` const in the JSON schemas embedded in the
  0.146.0 binary (``session-start.command.input`` &c., extracted 2026-08-02).
  All four events thinkweave installs — SessionStart, UserPromptSubmit,
  PostToolUse, Stop — exist under those exact names, so the port renames
  nothing.

* The **matchers** need no translation either. The manual's own examples list
  ``mcp__filesystem__.*`` and ``Edit|Write``: Codex namespaces MCP tools as
  ``mcp__<server>__<tool>`` exactly like Claude Code, and "For ``apply_patch``,
  ``matcher`` values can also use ``Edit`` or ``Write``". So the canonical
  ``mcp__thinkweave__.*`` and ``Write|Edit|Bash`` matchers are already correct
  Codex regexes. (The issue text predicted a ``<server>:<tool>`` rename — that
  prediction is wrong for 0.146.0; see docs/HARNESSES.md.)

* ``additionalContextLimit``: "By default, Codex limits each model-visible
  hook-output message to roughly 2,500 tokens. If a hook returns more, Codex
  saves the full text under ``<temp_dir>/hook_outputs/…`` and gives the model a
  head-and-tail preview." Our SessionStart payload is built with
  ``budget_tokens=10000`` — 4x the default — so without an explicit limit the
  headline acceptance criterion ("an interactive Codex session receives the
  SessionStart context payload") would fail *silently*, spilling to a temp
  file. The handlers that can emit ``additionalContext`` therefore carry an
  explicit limit. Codex "ignores ``additionalContextLimit`` and reports a
  configuration warning" for events that cannot emit additional context, so it
  is written on those two events only.

Nothing here touches a real ``~/.codex``: every test aims the profile at a tmp
dir, and the suite-wide ``_sandbox_harness_home`` fixture is the backstop.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thinkweave.core import harness
from thinkweave.surfaces.hooks.install import install_hooks, uninstall_hooks

# The events whose thinkweave handler can return
# `hookSpecificOutput.additionalContext` (see surfaces/hooks/handler.py:
# _handle_session_start and _handle_user_prompt_submit). Independent of the
# installer's own constant — transcribed from the handler, which is what
# actually decides.
CONTEXT_EMITTING = {"SessionStart", "UserPromptSubmit"}

# What `build_project_context` is asked for in _handle_session_start.
SESSION_START_BUDGET_TOKENS = 10000


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Activate the codex profile against a throwaway $CODEX_HOME."""
    home = tmp_path / "codex-user"
    home.mkdir()
    monkeypatch.setattr(harness, "_OVERRIDE", harness.codex(home=home))
    return home / ".codex"


class TestProfile:
    def test_hooks_capability_is_on(self, codex_home: Path):
        """#106 left `hooks=False` for this issue to flip. Codex 0.146 ships
        the full lifecycle-hook surface, so the honest answer is now True."""
        assert harness.active().hooks is True

    def test_machine_scope_hook_file_is_hooks_json(self, codex_home: Path):
        """Not config.toml — that layer already carries [mcp_servers] from
        #106, and Codex warns when one layer holds both representations."""
        assert harness.active().user_settings == codex_home / "hooks.json"

    def test_project_scope_hook_file_is_repo_local_hooks_json(
        self, codex_home: Path
    ):
        assert harness.active().project_settings_relpath == Path(
            ".codex"
        ) / "hooks.json"


class TestInstalledArtifact:
    """The bytes `weave hooks install --scope user --harness codex` leaves."""

    def test_writes_codex_shaped_hooks_json(self, codex_home: Path):
        install_hooks(scope="user")

        target = codex_home / "hooks.json"
        assert target.exists(), f"expected hook config at {target}"
        doc = json.loads(target.read_text(encoding="utf-8"))

        # Top-level shape from the manual's hooks.json example.
        assert set(doc["hooks"]) == {
            "SessionStart",
            "UserPromptSubmit",
            "PostToolUse",
            "Stop",
        }
        for event, groups in doc["hooks"].items():
            for group in groups:
                assert set(group) <= {"matcher", "hooks"}, event
                for handler in group["hooks"]:
                    assert handler["type"] == "command"
                    assert handler["command"]

    def test_matchers_are_carried_over_verbatim(self, codex_home: Path):
        """Codex's regex vocabulary already covers both canonical matchers:
        `Edit`/`Write` match apply_patch, `Bash` matches shell, and MCP tools
        are `mcp__<server>__<tool>` just like Claude Code."""
        install_hooks(scope="user")
        doc = json.loads((codex_home / "hooks.json").read_text())

        matchers = {g["matcher"] for g in doc["hooks"]["PostToolUse"]}
        assert matchers == {"Write|Edit|Bash", "mcp__thinkweave__.*"}

    def test_context_emitting_handlers_carry_an_explicit_limit(
        self, codex_home: Path
    ):
        """Without this the ~10k-token SessionStart payload silently spills to
        a temp file at Codex's 2500-token default and the model sees a preview
        instead of the context. The limit must cover the payload's own budget.
        """
        install_hooks(scope="user")
        doc = json.loads((codex_home / "hooks.json").read_text())

        for event, groups in doc["hooks"].items():
            for group in groups:
                for handler in group["hooks"]:
                    limit = handler.get("additionalContextLimit")
                    if event in CONTEXT_EMITTING:
                        assert limit is not None, (
                            f"{event} can emit additionalContext and needs an "
                            "explicit limit"
                        )
                        assert limit >= SESSION_START_BUDGET_TOKENS
                    else:
                        # Codex reports a configuration warning when the key
                        # rides an event that cannot emit additionalContext.
                        assert limit is None, event

    def test_install_names_the_trust_gate(self, codex_home: Path, capsys):
        """"Before a non-managed command hook can run, Codex requires you to
        review and trust the exact hook definition." A freshly written
        hooks.json is inert until then — silently-inert config is this repo's
        canonical failure class, so the installer has to say so."""
        install_hooks(scope="user")
        out = capsys.readouterr().out
        assert "/hooks" in out
        assert "trust" in out.lower()

    def test_reinstall_is_idempotent(self, codex_home: Path):
        install_hooks(scope="user")
        first = json.loads((codex_home / "hooks.json").read_text())
        install_hooks(scope="user")
        second = json.loads((codex_home / "hooks.json").read_text())
        assert first == second

    def test_uninstall_round_trips(self, codex_home: Path):
        install_hooks(scope="user")
        uninstall_hooks(scope="user")
        doc = json.loads((codex_home / "hooks.json").read_text())
        assert "hooks" not in doc


class TestClaudeCodeUnchanged:
    """Explicit acceptance criterion: "CC hook install output unchanged"."""

    def test_no_additional_context_limit_leaks_into_claude_code(
        self, tmp_path: Path, use_profile
    ):
        home = tmp_path / "cc-home"
        use_profile(user_settings=home / ".claude" / "settings.json")
        install_hooks(scope="user")

        doc = json.loads((home / ".claude" / "settings.json").read_text())
        for groups in doc["hooks"].values():
            for group in groups:
                for handler in group["hooks"]:
                    assert "additionalContextLimit" not in handler

    def test_claude_code_still_installs_project_scope(self, tmp_path: Path):
        """Codex is global-scope-only (below); Claude Code must keep its
        historical project-scope default."""
        project = tmp_path / "project"
        project.mkdir()
        install_hooks(scope="project", project_dir=str(project))
        assert (project / ".claude" / "settings.local.json").exists()


class TestGlobalScopeOnly:
    """Amendment (2026-08-01): hooks declared in a repo-local `.codex/` layer
    do not fire in interactive sessions (openai/codex#17532), and the manual
    additionally gates them on the project being *trusted*. Two ways to be
    silently inert — so the Codex profile refuses project scope outright."""

    def test_project_scope_is_refused(self, codex_home: Path, tmp_path: Path):
        project = tmp_path / "project"
        project.mkdir()
        with pytest.raises(SystemExit):
            install_hooks(scope="project", project_dir=str(project))
        assert not (project / ".codex").exists()

    def test_refusal_points_at_user_scope(
        self, codex_home: Path, tmp_path: Path, capsys
    ):
        project = tmp_path / "project"
        project.mkdir()
        with pytest.raises(SystemExit):
            install_hooks(scope="project", project_dir=str(project))
        err = capsys.readouterr().err
        assert "--scope user" in err
