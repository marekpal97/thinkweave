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
  ``budget_tokens=SESSION_START_BUDGET_TOKENS`` — 4x that default — so without
  an explicit limit the headline acceptance criterion ("an interactive Codex session receives the
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
from thinkweave.core.harness import SESSION_START_BUDGET_TOKENS
from thinkweave.surfaces.hooks.install import install_hooks, uninstall_hooks

# The events whose thinkweave handler can return
# `hookSpecificOutput.additionalContext` (see surfaces/hooks/handler.py:
# _handle_session_start and _handle_user_prompt_submit). Independent of the
# installer's own constant — transcribed from the handler, which is what
# actually decides.
CONTEXT_EMITTING = {"SessionStart", "UserPromptSubmit"}

# `SESSION_START_BUDGET_TOKENS` (what `build_project_context` is asked for in
# _handle_session_start) is imported above rather than transcribed: the cap and
# the budget are one number now, so a raised budget must fail the limit
# assertion below rather than silently reintroduce the spill.


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
                        # Headroom, not parity: the budget is spent against a
                        # chars//4 estimate, and note-id-dense markdown
                        # tokenizes worse than that, so a narrow margin spills.
                        assert limit >= 2 * SESSION_START_BUDGET_TOKENS
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

    def test_uninstall_at_project_scope_is_not_refused(
        self, codex_home: Path, tmp_path: Path, capsys
    ):
        """`weave doctor --mcp` flags stray repo-local `.codex/hooks.json`
        entries; project-scope uninstall is the command that clears them, so
        the install-side refusal must not block it."""
        from thinkweave.surfaces.hooks.install import uninstall_hooks

        project = tmp_path / "project"
        (project / ".codex").mkdir(parents=True)
        (project / ".codex" / "hooks.json").write_text(
            json.dumps({
                "hooks": {
                    "Stop": [{
                        "matcher": "*",
                        "hooks": [{
                            "type": "command",
                            "command": "uv run -m thinkweave.surfaces.hooks.handler stop",
                        }],
                    }]
                }
            }),
            encoding="utf-8",
        )

        uninstall_hooks(scope="project", project_dir=str(project))

        left = json.loads(
            (project / ".codex" / "hooks.json").read_text(encoding="utf-8")
        )
        assert not left.get("hooks", {}).get("Stop")


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
            # A substring match read these as Codex's own AGENTS.md.
            "docs/subagents.md",
            "multi-agents.md",
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


# ---------------------------------------------------------------------------
# The serving surface: context_served.source
# ---------------------------------------------------------------------------


class TestServingSurfaceTagging:
    """Owner-agreed 2026-07-31: the Codex serving surface registers its own
    `context_served.source`. It is a materially different surface, not a
    relabel — Codex renders injected `additionalContext` as a *visible
    developer message* (openai/codex#16933), spills it above
    `additionalContextLimit`, and will not deliver it at all until the hook is
    trusted. Collapsing it into `startup` would make the RLVR export weigh
    two different delivery guarantees as one.

    The handler learns which harness fired it from its own argv, written by
    the installer — `harness.active()` is not usable here (see `_is_internal`).
    """

    def test_installer_stamps_the_harness_into_the_codex_command(
        self, codex_home: Path
    ):
        install_hooks(scope="user")
        doc = json.loads((codex_home / "hooks.json").read_text())
        for groups in doc["hooks"].values():
            for group in groups:
                for h in group["hooks"]:
                    assert h["command"].endswith(" --harness codex"), h["command"]

    def test_claude_code_command_carries_no_harness_flag(
        self, tmp_path: Path, use_profile
    ):
        """Explicit acceptance criterion: CC hook install output unchanged."""
        home = tmp_path / "cc-home"
        use_profile(user_settings=home / ".claude" / "settings.json")
        install_hooks(scope="user")
        doc = json.loads((home / ".claude" / "settings.json").read_text())
        for groups in doc["hooks"].values():
            for group in groups:
                for h in group["hooks"]:
                    assert "--harness" not in h["command"]

    def test_startup_event_is_stamped_with_the_surface(
        self, tmp_path: Path, monkeypatch
    ):
        cfg = Config(vault_root=tmp_path / "vault")
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        VaultManager(config=cfg).ensure_dirs()
        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context",
            lambda cfg, project, budget_tokens=10000: "- [[n-1|n-1]]\n",
        )
        monkeypatch.setattr(
            "sys.argv", ["weave-hook", "session_start", "--harness", "codex"]
        )

        handler_mod._handle_session_start(CODEX_SESSION_START)

        events = handler_mod._read_buffer(cfg.weave_dir, CODEX_SESSION_ID)
        startup = next(e for e in events if e["type"] == "startup")
        assert startup["surface"] == "codex"

    def test_claude_code_startup_event_is_unstamped(
        self, tmp_path: Path, monkeypatch
    ):
        cfg = Config(vault_root=tmp_path / "vault")
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        VaultManager(config=cfg).ensure_dirs()
        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context",
            lambda cfg, project, budget_tokens=10000: "- [[n-1|n-1]]\n",
        )
        monkeypatch.setattr("sys.argv", ["weave-hook", "session_start"])

        handler_mod._handle_session_start(
            {"session_id": "ses-cc", "cwd": str(tmp_path)}
        )

        events = handler_mod._read_buffer(cfg.weave_dir, "ses-cc")
        startup = next(e for e in events if e["type"] == "startup")
        assert "surface" not in startup


class TestContextServedProjection:
    """The indexer projects a stamped startup event to its own source value.
    Mirrors how `retrieval` events already fan out by their `tool` sentinel."""

    def _project(self, tmp_path: Path, event: dict) -> list[tuple]:
        from thinkweave.core.indexer import Indexer

        cfg = Config(vault_root=tmp_path / "vault")
        VaultManager(config=cfg).ensure_dirs()
        log = tmp_path / "retrieval_log.jsonl"
        log.write_text(json.dumps(event) + "\n", encoding="utf-8")

        idx = Indexer(config=cfg)
        try:
            idx._project_session_retrieval_log("ses-x", log)
            idx.db.commit()
            return [
                tuple(r)
                for r in idx.db.execute(
                    "SELECT note_id, source FROM context_served ORDER BY note_id"
                )
            ]
        finally:
            idx.close()

    def test_codex_startup_gets_its_own_source(self, tmp_path: Path):
        rows = self._project(
            tmp_path,
            {
                "type": "startup",
                "surface": "codex",
                "returned_ids": ["n-1"],
                "ts": "2026-08-02T00:00:00Z",
            },
        )
        assert rows == [("n-1", "codex-startup")]

    def test_unstamped_startup_stays_startup(self, tmp_path: Path):
        rows = self._project(
            tmp_path,
            {
                "type": "startup",
                "returned_ids": ["n-1"],
                "ts": "2026-08-02T00:00:00Z",
            },
        )
        assert rows == [("n-1", "startup")]

    def test_check_constraint_admits_the_new_source(self, tmp_path: Path):
        """The CHECK is a closed set — a source it does not list is rejected
        at INSERT, which is the failure this migration exists to prevent."""
        from thinkweave.core.indexer import Indexer

        cfg = Config(vault_root=tmp_path / "vault")
        VaultManager(config=cfg).ensure_dirs()
        idx = Indexer(config=cfg)
        try:
            sql = idx.db.execute(
                "SELECT sql FROM sqlite_master WHERE name='context_served'"
            ).fetchone()[0]
            assert "codex-startup" in sql
        finally:
            idx.close()

    def test_pre_codex_table_is_dropped_and_recreated(self, tmp_path: Path):
        """A vault indexed before this issue carries the narrower CHECK, which
        rejects every codex-startup INSERT. SQLite cannot ALTER a CHECK, and
        the table is 100% derived from retrieval_log.jsonl — so the established
        remedy is drop + recreate (the same one 'prompttime' and 'loop-prime'
        used). Runs against a fixture DB only."""
        from thinkweave.core.indexer import Indexer

        cfg = Config(vault_root=tmp_path / "vault")
        VaultManager(config=cfg).ensure_dirs()

        # Build the pre-#107 table by hand — independent of SCHEMA_SQL.
        idx = Indexer(config=cfg)
        idx.db.execute("DROP TABLE IF EXISTS context_served")
        idx.db.execute(
            "CREATE TABLE context_served ("
            " session_id TEXT NOT NULL,"
            " note_id    TEXT NOT NULL,"
            " source     TEXT NOT NULL CHECK(source IN "
            "  ('startup', 'onthefly', 'prompttime', 'loop-prime')),"
            " ts         TEXT,"
            " PRIMARY KEY (session_id, note_id, source))"
        )
        idx.db.execute(
            "INSERT INTO context_served VALUES ('ses-old', 'n-old', 'startup', '')"
        )
        idx.db.commit()
        idx.close()

        # Reopening runs the migration.
        idx = Indexer(config=cfg)
        try:
            idx._init_schema()
            sql = idx.db.execute(
                "SELECT sql FROM sqlite_master WHERE name='context_served'"
            ).fetchone()[0]
            assert "codex-startup" in sql
            idx.db.execute(
                "INSERT INTO context_served VALUES "
                "('ses-x', 'n-1', 'codex-startup', '')"
            )
        finally:
            idx.close()

    def test_dropped_table_is_repopulated_without_a_full_rebuild(
        self, tmp_path: Path
    ):
        """The drop empties a table the co_served edges project from, and only
        `rebuild(full=True)` re-walks every session — so deferring the refill
        to the caller leaves an ordinary post-upgrade open serving nothing
        until the nightly full pass."""
        from thinkweave.core.indexer import Indexer
        from thinkweave.core.schemas import NoteType

        cfg = Config(vault_root=tmp_path / "vault")
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        sess_path = vm.create_note(
            NoteType.SESSION, "S", body="## Summary\nseed\n", project="p"
        )
        (sess_path.parent / "retrieval_log.jsonl").write_text(
            json.dumps({
                "ts": "2026-08-02T00:00:00Z",
                "type": "startup",
                "returned_ids": ["n-seeded"],
            }) + "\n",
            encoding="utf-8",
        )

        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        # Hand-downgrade to the pre-#107 CHECK, rows and all.
        idx.db.execute("DROP TABLE context_served")
        idx.db.execute(
            "CREATE TABLE context_served ("
            " session_id TEXT NOT NULL, note_id TEXT NOT NULL,"
            " source TEXT NOT NULL CHECK(source IN ('startup', 'onthefly')),"
            " ts TEXT, PRIMARY KEY (session_id, note_id, source))"
        )
        idx.db.commit()
        idx.close()

        idx = Indexer(config=cfg)
        try:
            idx._init_schema()
            rows = [
                r["note_id"]
                for r in idx.db.execute("SELECT note_id FROM context_served")
            ]
            assert rows == ["n-seeded"]
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# `weave doctor --mcp` — the repo-local hook warning
# ---------------------------------------------------------------------------


class TestDoctorHookScope:
    """Silently-inert config is this repo's canonical failure class, and a
    repo-local Codex hook entry is exactly that: the parser accepts it, then
    nothing fires (openai/codex#17532, and the manual additionally gates
    project `.codex/` layers on the project being trusted). Verified on
    0.146.0 — a hooks.json in an untrusted/repo-local position produced no
    hook invocations at all, with no message.

    Codex reads hooks from BOTH `.codex/hooks.json` and an inline `[hooks]`
    table in `.codex/config.toml`, so the doctor has to look in both or it
    leaves half the failure undiagnosed.
    """

    def _run(self, cwd: Path):
        from thinkweave.surfaces.cli import mcp_doctor

        return mcp_doctor.check_hook_scope(cwd)

    def test_clean_repo_passes(self, codex_home: Path, tmp_path: Path):
        project = tmp_path / "clean"
        project.mkdir()
        assert self._run(project).passed is True

    def test_repo_local_hooks_json_is_flagged(
        self, codex_home: Path, tmp_path: Path
    ):
        project = tmp_path / "repo"
        (project / ".codex").mkdir(parents=True)
        (project / ".codex" / "hooks.json").write_text(
            json.dumps({"hooks": {"SessionStart": [{"hooks": []}]}})
        )
        check = self._run(project)
        assert check.passed is False
        assert ".codex/hooks.json" in check.detail.replace("\\", "/")
        assert "--scope user" in check.fix

    def test_repo_local_inline_toml_hooks_are_flagged(
        self, codex_home: Path, tmp_path: Path
    ):
        project = tmp_path / "repo"
        (project / ".codex").mkdir(parents=True)
        (project / ".codex" / "config.toml").write_text(
            '[[hooks.SessionStart]]\nmatcher = ""\n'
        )
        check = self._run(project)
        assert check.passed is False
        assert "config.toml" in check.detail

    def test_mcp_only_project_config_is_not_flagged(
        self, codex_home: Path, tmp_path: Path
    ):
        """#106 writes `[mcp_servers]` into a project config.toml. That is a
        different concern and must not trip the hook warning."""
        project = tmp_path / "repo"
        (project / ".codex").mkdir(parents=True)
        (project / ".codex" / "config.toml").write_text(
            '[mcp_servers.thinkweave]\ncommand = "uv"\n'
        )
        assert self._run(project).passed is True

    def test_check_runs_only_for_global_only_harnesses(self, tmp_path: Path):
        """Claude Code honours project-scope hooks, so its doctor report is
        unchanged — the row is not emitted at all."""
        from thinkweave.surfaces.cli import mcp_doctor

        project = tmp_path / "cc"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "settings.local.json").write_text(
            json.dumps({"hooks": {"SessionStart": []}})
        )
        result = mcp_doctor.run_mcp_doctor(project)
        assert not any(c.name == "hook scope" for c in result.checks)


class TestDoctorHarnessAwareMessages:
    """Slice-1/2 follow-ups: two doctor strings assumed Claude Code."""

    def test_absent_registration_fix_names_the_harness(
        self, codex_home: Path, tmp_path: Path
    ):
        from thinkweave.surfaces.cli import mcp_doctor

        check = mcp_doctor.check_registration_scopes(tmp_path / "nowhere")
        assert check.passed is False
        assert "--harness codex" in check.fix

    def test_claude_code_fix_stays_unqualified(self, tmp_path: Path):
        from thinkweave.surfaces.cli import mcp_doctor

        check = mcp_doctor.check_registration_scopes(tmp_path / "nowhere")
        assert "--harness" not in check.fix

    def test_divergent_scope_message_names_the_active_harness(
        self, codex_home: Path, tmp_path: Path, monkeypatch
    ):
        from thinkweave.surfaces.cli import mcp_doctor

        monkeypatch.setattr(
            mcp_doctor,
            "_entry_from_claude_json",
            lambda: (Path("/m"), {"command": "uv", "args": ["a"]}),
        )
        monkeypatch.setattr(
            mcp_doctor,
            "_entry_from_project_mcp_json",
            lambda cwd: (Path("/p"), {"command": "uv", "args": ["b"]}),
        )
        check = mcp_doctor.check_registration_scopes(tmp_path)
        assert check.passed is False
        assert "Claude Code" not in check.detail
        assert "codex" in check.detail


class TestHeadlessHookTrust:
    """Spike Q1's consequence. Hooks DO fire under `codex exec` — verified on
    0.146.0 by a credential-less run whose sentinel hooks recorded
    SessionStart, UserPromptSubmit and SessionEnd before the 401. But the
    same run with the trust flag removed fired *nothing, silently*: no
    warning, no row, no output. An unattended cron cannot answer a `/hooks`
    trust prompt, so without this flag every Codex cron would quietly lose
    passive capture — the exact silently-inert failure the doctor check
    above exists to catch, in the one place a doctor never runs.
    """

    def test_unattended_codex_run_bypasses_hook_trust(self, codex_home: Path):
        argv = harness.active().headless_argv("/dream", bypass=True)
        assert argv[:2] == ["codex", "exec"]
        assert "--dangerously-bypass-hook-trust" in argv

    def test_attended_run_does_not(self, codex_home: Path):
        """An interactive user can trust via `/hooks`; nothing should hand
        out a dangerous flag it did not need."""
        argv = harness.active().headless_argv("/dream")
        assert "--dangerously-bypass-hook-trust" not in argv

    def test_claude_code_argv_unchanged(self, tmp_path: Path):
        argv = harness.active().headless_argv("/dream", bypass=True)
        assert argv == ["claude", "-p", "/dream", "--dangerously-skip-permissions"]


class TestRetrievalGateUnderCodex:
    """Explicit acceptance criterion: "the RLVR retrieval-served PostToolUse
    gate fires on `weave_*` tool calls or its absence is documented as a known
    gap". It fires — Codex namespaces MCP tools `mcp__<server>__<tool>`, the
    same strings `RETRIEVAL_TOOLS` already holds, so the closed set needs no
    Codex entries. Driven here with a Codex-shaped envelope rather than left
    as an inference from the naming convention.
    """

    def test_weave_search_lands_in_the_buffer(self, tmp_path: Path, monkeypatch):
        cfg = Config(vault_root=tmp_path / "vault")
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        VaultManager(config=cfg).ensure_dirs()

        handler_mod._handle_post(
            "mcp__thinkweave__weave_search",
            {
                "session_id": CODEX_SESSION_ID,
                "turn_id": "019fc43a-b08f-7083-b857-3b81968fbbf5",
                "cwd": "/tmp/cx/work",
                "hook_event_name": "PostToolUse",
                "tool_name": "mcp__thinkweave__weave_search",
                "tool_use_id": "call_r1",
                "tool_input": {"query": "codex hooks"},
                "tool_response": {"output": "- [[notes/n-abc123|n-abc123]] Hooks"},
            },
        )

        events = handler_mod._read_buffer(cfg.weave_dir, CODEX_SESSION_ID)
        retrieval = [e for e in events if e.get("type") == "retrieval"]
        assert retrieval, "the retrieval gate must capture a Codex weave_* call"
        assert retrieval[0]["tool"] == "mcp__thinkweave__weave_search"
        assert "n-abc123" in retrieval[0]["returned_ids"]

    def test_non_retrieval_mcp_tool_is_still_ignored(
        self, tmp_path: Path, monkeypatch
    ):
        """`RETRIEVAL_TOOLS` is a closed set on purpose — mutation tools must
        not pollute the log just because they share the namespace."""
        cfg = Config(vault_root=tmp_path / "vault")
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        VaultManager(config=cfg).ensure_dirs()

        handler_mod._handle_post(
            "mcp__thinkweave__weave_create",
            {
                "session_id": CODEX_SESSION_ID,
                "tool_name": "mcp__thinkweave__weave_create",
                "tool_input": {"title": "x"},
                "tool_response": {"output": "created"},
            },
        )
        assert handler_mod._read_buffer(cfg.weave_dir, CODEX_SESSION_ID) == []

    def test_nested_mcp_content_shape_still_yields_ids(self, tmp_path: Path, monkeypatch):
        """`tool_response` is typed "any JSON" in the 0.146.0 schema, so there
        is no key list to hardcode. A nested MCP-style payload must still
        expose its note ids rather than logging an empty retrieval.

        Driven through `_handle_post` rather than a helper: the recovery is
        scoped to the retrieval path precisely so the action path keeps
        ignoring shapes it doesn't recognise.
        """
        cfg = Config(vault_root=tmp_path / "vault")
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        VaultManager(config=cfg).ensure_dirs()

        handler_mod._handle_post(
            "mcp__thinkweave__weave_search",
            {
                "session_id": CODEX_SESSION_ID,
                "tool_name": "mcp__thinkweave__weave_search",
                "tool_input": {"query": "x"},
                "tool_response": {"content": [{"type": "text", "text": "[[n-abc123]]"}]},
            },
        )

        events = handler_mod._read_buffer(cfg.weave_dir, CODEX_SESSION_ID)
        assert events[0]["returned_ids"] == ["n-abc123"]

    def test_flag_only_dict_still_yields_empty(self):
        """Claude Code's Bash response with no output is genuinely textless —
        that contract must not drift into emitting `{"interrupted": false}`."""
        assert handler_mod._extract_tool_output_text(
            {"tool_response": {"interrupted": False, "isImage": False}}
        ) == ""


class TestHooksCliTakesHarness:
    """`weave hooks install` became harness-relevant only with this issue —
    before it, Claude Code was the sole harness with hooks, so `hooks` was
    left off the `--harness` list. The doctor's own fix hint now tells users
    to run `weave hooks install --scope user --harness codex`, so the flag has
    to exist or the remedy is broken advice."""

    @pytest.mark.parametrize("action", ["install", "uninstall"])
    def test_flag_parses(self, action: str):
        from thinkweave.surfaces.cli.parser import build_parser

        args = build_parser().parse_args(
            ["hooks", action, "--scope", "user", "--harness", "codex"]
        )
        assert args.harness == "codex"

    def test_install_targets_the_named_harness(self, tmp_path: Path, capsys):
        """End-to-end through the CLI entry point: the flag must actually pin
        the profile, not merely parse."""
        from thinkweave.surfaces.cli import main

        home = tmp_path / "cx"
        home.mkdir()
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("CODEX_HOME", str(home / ".codex"))
            mp.setattr(harness, "_OVERRIDE", None)
            mp.setattr(
                "sys.argv",
                ["weave", "hooks", "install", "--scope", "user",
                 "--harness", "codex"],
            )
            main()

        assert (home / ".codex" / "hooks.json").exists()
        assert "trust" in capsys.readouterr().out.lower()
