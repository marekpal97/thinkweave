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


# ---------------------------------------------------------------------------
# The envelope: driving the (harness-agnostic) handler with Codex payloads
# ---------------------------------------------------------------------------
#
# Ground truth for the envelopes below:
#
# * The SessionStart / UserPromptSubmit / SessionEnd objects are TRANSCRIBED
#   VERBATIM from a real codex-cli 0.146.0 run on 2026-08-02 — sentinel hooks
#   installed into a throwaway $CODEX_HOME, driven by
#   `codex exec --strict-config --dangerously-bypass-hook-trust "say hi"` with
#   no credentials, which dumped each hook's stdin before failing at auth.
# * The `apply_patch` PostToolUse shape comes from the manual ("`Bash` and
#   `apply_patch` use `tool_input.command`"; "Codex still reports
#   `tool_name: \"apply_patch\"`") plus the patch-envelope markers extracted
#   from the 0.146.0 binary: `*** Add File: `, `*** Update File:`,
#   `*** Delete File: `, `*** Move to: `, `*** End Patch`.

from thinkweave.core.config import Config  # noqa: E402
from thinkweave.core.schemas import NoteType  # noqa: E402
from thinkweave.core.vault import VaultManager  # noqa: E402
from thinkweave.surfaces.hooks import handler as handler_mod  # noqa: E402

# Verbatim from the 0.146.0 sentinel run (paths shortened, ids kept).
CODEX_SESSION_ID = "019fc43a-b029-7542-8626-884213ed5cee"

CODEX_SESSION_START = {
    "session_id": CODEX_SESSION_ID,
    "transcript_path": "/tmp/cx/sessions/rollout-2026-08-02T21-46-48.jsonl",
    "cwd": "/tmp/cx/work",
    "hook_event_name": "SessionStart",
    "model": "gpt-5.6-sol",
    "permission_mode": "bypassPermissions",
    "source": "startup",
}

CODEX_USER_PROMPT_SUBMIT = {
    "session_id": CODEX_SESSION_ID,
    "turn_id": "019fc43a-b08f-7083-b857-3b81968fbbf5",
    "transcript_path": "/tmp/cx/sessions/rollout-2026-08-02T21-46-48.jsonl",
    "cwd": "/tmp/cx/work",
    "hook_event_name": "UserPromptSubmit",
    "model": "gpt-5.6-sol",
    "permission_mode": "bypassPermissions",
    "prompt": "say hi",
}

# A two-file patch in Codex's apply_patch envelope.
CODEX_PATCH = """\
*** Begin Patch
*** Update File: src/thinkweave/core/harness.py
@@
-    hooks=False,
+    hooks=True,
*** Add File: docs/HARNESSES.md
+# Harnesses
*** End Patch
"""

CODEX_APPLY_PATCH = {
    "session_id": CODEX_SESSION_ID,
    "turn_id": "019fc43a-b08f-7083-b857-3b81968fbbf5",
    "cwd": "/tmp/cx/work",
    "hook_event_name": "PostToolUse",
    "model": "gpt-5.6-sol",
    "permission_mode": "bypassPermissions",
    "tool_name": "apply_patch",
    "tool_use_id": "call_abc123",
    "tool_input": {"command": CODEX_PATCH},
    "tool_response": {"output": "Success. Updated the following files:\nM src/thinkweave/core/harness.py\nA docs/HARNESSES.md"},
}


class TestIgnorePaths:
    """`_is_internal` filters the harness's own config out of files_touched.

    It knew only Claude Code's names, so a Codex session editing its own
    AGENTS.md or `.codex/` config logged them as project work.

    Deliberately a *union*, not profile data (which is what the issue text
    predicted): the installed hook command carries no `$THINKWEAVE_HARNESS`,
    so `harness.active()` inside a hook fired by Codex would report
    `claude-code` and pick the wrong list. The two vocabularies don't collide
    — Claude Code never writes `.codex/`, Codex never writes `.claude/` — so
    matching both is correct under either harness and needs no plumbing.
    """

    @pytest.mark.parametrize(
        "path",
        [
            ".codex/config.toml",
            "/home/u/.codex/hooks.json",
            "AGENTS.md",
            "/repo/agents.md",
            # Claude Code's own set must keep working.
            ".claude/settings.json",
            "CLAUDE.md",
        ],
    )
    def test_harness_config_is_internal(self, path: str):
        assert handler_mod._is_internal(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "src/thinkweave/core/harness.py",
            "docs/HARNESSES.md",
            "tests/test_codex_hooks.py",
        ],
    )
    def test_project_work_is_not_internal(self, path: str):
        assert handler_mod._is_internal(path) is False


class TestApplyPatchCapture:
    """Codex reports every file edit as one `apply_patch` call whose
    `tool_input.command` is the patch envelope — there is no `file_path`.
    Claude Code's per-file `Write`/`Edit` events have no counterpart, so
    without a reader for that envelope a Codex session's `files_touched` is
    always empty."""

    def test_patch_yields_one_event_per_file(self):
        events = handler_mod._build_events(
            "apply_patch", CODEX_APPLY_PATCH["tool_input"], "", "2026-08-02T00:00:00Z"
        )
        assert [e["file"] for e in events] == [
            "src/thinkweave/core/harness.py",
            "docs/HARNESSES.md",
        ]

    def test_operations_map_onto_the_buffer_vocabulary(self):
        """Downstream consumers (`core/events.py`, `_summarize_events`) match
        on `tool in ("Edit", "Write")`. Codex documents `Edit` and `Write` as
        matcher aliases for `apply_patch`, so normalising here — at the one
        wire boundary — keeps every consumer harness-agnostic instead of
        teaching each one a second vocabulary."""
        events = handler_mod._build_events(
            "apply_patch", CODEX_APPLY_PATCH["tool_input"], "", "2026-08-02T00:00:00Z"
        )
        assert [e["tool"] for e in events] == ["Edit", "Write"]

    def test_all_four_patch_operations_are_read(self):
        patch = (
            "*** Begin Patch\n"
            "*** Update File: a.py\n"
            "*** Add File: b.py\n"
            "*** Delete File: c.py\n"
            "*** Update File: d.py\n"
            "*** Move to: e.py\n"
            "*** End Patch\n"
        )
        events = handler_mod._build_events(
            "apply_patch", {"command": patch}, "", "2026-08-02T00:00:00Z"
        )
        assert [e["file"] for e in events] == ["a.py", "b.py", "c.py", "d.py", "e.py"]

    def test_internal_paths_are_filtered_out_of_a_patch(self):
        patch = (
            "*** Begin Patch\n"
            "*** Update File: .codex/config.toml\n"
            "*** Update File: src/real.py\n"
            "*** End Patch\n"
        )
        events = handler_mod._build_events(
            "apply_patch", {"command": patch}, "", "2026-08-02T00:00:00Z"
        )
        assert [e["file"] for e in events] == ["src/real.py"]

    def test_bash_still_builds_exactly_one_event(self):
        """Claude Code's path must be byte-identical through this change."""
        events = handler_mod._build_events(
            "Bash",
            {"command": "git commit -m 'x'"},
            "[main abc1234] x\n 1 file changed\n",
            "2026-08-02T00:00:00Z",
        )
        assert len(events) == 1
        assert events[0]["tool"] == "Bash"
        assert events[0]["commit"]["hash"] == "abc1234"


class TestCodexSessionEndToEnd:
    """The headline acceptance criterion, verified at the strongest seam
    reachable without an authenticated Codex session: drive the real handler
    with the real Codex envelopes and require a session note written AND
    indexed into a tmp vault."""

    def test_session_start_prompt_edit_stop(self, tmp_path: Path, monkeypatch):
        cfg = Config(vault_root=tmp_path / "vault")
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()

        # 1. SessionStart — emits the context payload as additionalContext.
        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context",
            lambda cfg, project, budget_tokens=10000: "## Recent\n- [[n-1|n-1]]\n",
        )
        handler_mod._handle_session_start(CODEX_SESSION_START)

        # 2. UserPromptSubmit — captures the prompt.
        handler_mod._handle_user_prompt_submit(CODEX_USER_PROMPT_SUBMIT)

        # 3. PostToolUse(apply_patch) — captures the file edits.
        handler_mod._handle_post("apply_patch", CODEX_APPLY_PATCH)

        # 4. Stop — materialises + indexes the session note.
        handler_mod._handle_stop(
            {
                "session_id": CODEX_SESSION_ID,
                "hook_event_name": "Stop",
                "cwd": "/tmp/cx/work",
                "stop_hook_active": False,
                "last_assistant_message": None,
            }
        )

        note = next(
            n
            for n in vm.list_notes(note_type=NoteType.SESSION, limit=10)
            if n.frontmatter.get("source_session") == CODEX_SESSION_ID
        )
        assert note.frontmatter.get("processed") is True
        assert note.frontmatter.get("files_touched") == [
            "src/thinkweave/core/harness.py",
            "docs/HARNESSES.md",
        ]

        # Indexed, not merely written — `weave_search` must find it.
        import sqlite3

        with sqlite3.connect(cfg.index_db) as db:
            rows = db.execute(
                "SELECT 1 FROM notes WHERE type='session' AND frontmatter LIKE ?",
                (f'%"source_session": "{CODEX_SESSION_ID}"%',),
            ).fetchall()
        assert rows, "Stop must index the session note"

    def test_session_start_emits_codex_shaped_output(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """Codex's session-start.command.output schema requires
        `hookSpecificOutput.hookEventName == "SessionStart"` alongside
        `additionalContext` — the same object Claude Code consumes."""
        cfg = Config(vault_root=tmp_path / "vault")
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        VaultManager(config=cfg).ensure_dirs()
        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context",
            lambda cfg, project, budget_tokens=10000: "PAYLOAD",
        )

        handler_mod._handle_session_start(CODEX_SESSION_START)

        emitted = json.loads(capsys.readouterr().out)
        assert emitted["hookSpecificOutput"] == {
            "hookEventName": "SessionStart",
            "additionalContext": "PAYLOAD",
        }
