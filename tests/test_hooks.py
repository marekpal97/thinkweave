"""Tests for Claude Code hook handler and installer."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from thinkweave.core.config import Config
from thinkweave.surfaces.hooks.handler import (
    _buffer_event,
    _build_auto_summary,
    _build_event,
    _detect_project,
    _diff_context,
    _extract_insight_blocks,
    _extract_tool_output_text,
    _first_meaningful_line,
    _get_commit_files,
    _is_git_commit,
    _is_internal,
    _is_significant_command,
    _is_test_command,
    _log_error,
    _parse_commit_from_output,
    _parse_push_branch,
    _parse_test_result,
    _read_buffer,
    _summarize_events,
    archive_buffer,
    cleanup_buffer,
)
from thinkweave.surfaces.hooks.install import install_hooks, uninstall_hooks


@pytest.fixture(autouse=True)
def _isolated_vault(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every ``load_config()`` call in this module resolves under ``tmp_path``.

    Without this, a test that only stubs a downstream function (e.g. patching
    ``build_project_context`` to raise) and lets the exception propagate to
    ``_log_error`` falls through to the REAL ``load_config()`` — which
    resolves the user's actual vault — and writes a synthetic-failure
    traceback into their real ``hooks.log``. That happened here: production
    ``.weave/hooks.log`` accumulated "synthetic failure" tracebacks from this
    file's ``test_failure_in_payload_does_not_block``. Tests that explicitly
    stub ``load_config`` with their own tmp-path ``Config`` (the majority,
    via ``monkeypatch.setattr`` or ``unittest.mock.patch``) simply override
    this default afterward — no behavior change for them.
    """
    default_cfg = Config(vault_root=tmp_path / "isolated-vault")
    monkeypatch.setattr(
        "thinkweave.core.config.load_config", lambda: default_cfg
    )


class TestHookHelpers:
    def test_is_internal(self):
        assert _is_internal("/home/user/.claude/settings.json")
        assert _is_internal("/project/CLAUDE.md")
        assert _is_internal("/project/.weave/index.db")
        assert not _is_internal("/project/src/main.py")
        assert not _is_internal("/project/README.md")

    def test_is_significant_command(self):
        assert _is_significant_command("git commit -m 'fix'")
        assert _is_significant_command("pytest tests/")
        assert _is_significant_command("python3 script.py")
        assert _is_significant_command("uv run pytest")
        assert not _is_significant_command("ls -la")
        assert not _is_significant_command("cat file.txt")
        assert not _is_significant_command("echo hello")

    def test_detect_project(self, tmp_path: Path):
        # Env var takes priority
        with patch.dict("os.environ", {"THINKWEAVE_PROJECT": "from-env"}):
            assert _detect_project({"cwd": "/anywhere"}) == "from-env"

        # Git repo detection: walk up to .git
        repo = tmp_path / "my_project" / "src" / "pkg"
        repo.mkdir(parents=True)
        (tmp_path / "my_project" / ".git").mkdir()
        assert _detect_project({"cwd": str(repo)}) == "my_project"

        # Fallback to cwd directory name
        no_git = tmp_path / "random_dir"
        no_git.mkdir()
        assert _detect_project({"cwd": str(no_git)}) == "random_dir"

    def test_extract_insight_blocks(self):
        text = """Some text before.
★ Insight ─────────────────────────────────────
Key point 1: FTS5 is fast.
Key point 2: WAL enables concurrency.
─────────────────────────────────────────────────
Some text after."""
        insights = _extract_insight_blocks(text)
        assert len(insights) == 1
        assert "FTS5 is fast" in insights[0]
        assert "WAL enables concurrency" in insights[0]

    def test_extract_no_insights(self):
        assert _extract_insight_blocks("No insights here.") == []


REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_HOOKS = REPO_ROOT / "hooks" / "hooks.json"


class TestInstallCanonicalAgreement:
    """``weave hooks install`` derives from ``hooks/hooks.json`` (#50).

    The installer used to carry a hand-maintained duplicate of the hook
    definitions, which diverged from the canonical file: stale 5s/10s
    timeouts (hooks.json says 60s/30s since PR #10), an install-time
    absolute-path snapshot of ``weave-hook`` (repointing to whatever venv
    was live at install time — the stale-skills-venv bug), and an event/
    matcher set that could silently drop anything newly authored in
    hooks.json. These tests load the canonical file INDEPENDENTLY and pin
    the installer's public output to it.
    """

    @staticmethod
    def _canonical() -> dict:
        return json.loads(CANONICAL_HOOKS.read_text(encoding="utf-8"))["hooks"]

    @staticmethod
    def _install(tmp_path: Path, **kwargs) -> dict:
        project_dir = tmp_path / "project"
        project_dir.mkdir(exist_ok=True)
        install_hooks(project_dir=str(project_dir), **kwargs)
        return json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )

    def test_output_matches_canonical_for_every_event(self, tmp_path: Path):
        """AC3: for EVERY event in hooks.json, the installed entry agrees on
        matcher, command (modulo the explicit ${CLAUDE_PLUGIN_ROOT} → repo
        root transformation), and timeout. A future event added to
        hooks.json can never be silently dropped by the installer again."""
        canonical = self._canonical()
        settings = self._install(tmp_path)

        assert canonical, "canonical hooks.json is empty — test is vacuous"
        for event, entries in canonical.items():
            assert event in settings["hooks"], f"installer dropped {event}"
            for c_entry in entries:
                matches = [
                    e for e in settings["hooks"][event]
                    if e["matcher"] == c_entry["matcher"]
                ]
                assert len(matches) == 1, (
                    f"{event}: expected exactly one installed entry with "
                    f"matcher {c_entry['matcher']!r}, got {len(matches)}"
                )
                c_hook = c_entry["hooks"][0]
                i_hook = matches[0]["hooks"][0]
                expected_cmd = c_hook["command"].replace(
                    "${CLAUDE_PLUGIN_ROOT}", str(REPO_ROOT)
                )
                assert i_hook["command"] == expected_cmd, (
                    f"{event}/{c_entry['matcher']!r}: command diverges from "
                    f"canonical\n  installed: {i_hook['command']!r}\n"
                    f"  expected:  {expected_cmd!r}"
                )
                assert i_hook.get("timeout") == c_hook.get("timeout"), (
                    f"{event}: timeout {i_hook.get('timeout')!r} diverges "
                    f"from canonical {c_hook.get('timeout')!r}"
                )

    def test_no_stale_timeouts_or_venv_snapshot(self, tmp_path: Path):
        """AC1 regression pin on the two symptoms #50 names: no 5s/10s
        timeouts anywhere, and no install-time interpreter/venv path baked
        into any command — resolution happens at fire time via the committed
        ``bin/weave-hook-launch`` shim (the #47/#52 launcher story: the shim
        runs the uv ladder, so the harness's stripped PATH can't silently
        kill the hook)."""
        settings = self._install(tmp_path)
        for event, entries in settings["hooks"].items():
            for entry in entries:
                for hook in entry["hooks"]:
                    if "weave-hook" not in hook["command"]:
                        continue  # foreign hook — not ours to pin
                    assert hook.get("timeout") not in (5, 10), (
                        f"{event}: stale hand-maintained timeout "
                        f"{hook.get('timeout')} survived"
                    )
                    # Fire-time resolution lives in the committed launcher,
                    # not a snapshotted venv path: the command points at the
                    # repo's bin/weave-hook-launch and nowhere else.
                    assert "bin/weave-hook-launch" in hook["command"], (
                        f"{event}: command does not route through the launcher "
                        f"shim: {hook['command']!r}"
                    )
                    assert str(REPO_ROOT) in hook["command"], (
                        f"{event}: launcher path is not absolute to this repo: "
                        f"{hook['command']!r}"
                    )

    def test_installer_follows_mutated_canonical(self, tmp_path: Path):
        """AC2 contract test: the installer READS the canonical file rather
        than carrying a duplicate. Mutate a timeout and add a brand-new
        event in a temp copy — the installer output must follow both."""
        mutated = json.loads(CANONICAL_HOOKS.read_text(encoding="utf-8"))
        mutated["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = 61
        mutated["hooks"]["PreCompact"] = [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            '"${CLAUDE_PLUGIN_ROOT}/bin/weave-hook-launch" '
                            "pre_compact"
                        ),
                        "timeout": 42,
                    }
                ],
            }
        ]
        alt = tmp_path / "hooks.json"
        alt.write_text(json.dumps(mutated), encoding="utf-8")

        settings = self._install(tmp_path, hooks_json=str(alt))

        ss_hook = settings["hooks"]["SessionStart"][0]["hooks"][0]
        assert ss_hook["timeout"] == 61, (
            "mutated canonical timeout did not flow through — the installer "
            "still carries a hand-maintained duplicate"
        )
        assert "PreCompact" in settings["hooks"], (
            "event added to the canonical file was silently dropped"
        )
        pc_hook = settings["hooks"]["PreCompact"][0]["hooks"][0]
        assert pc_hook["timeout"] == 42
        assert pc_hook["command"].endswith(" pre_compact")
        assert str(REPO_ROOT) in pc_hook["command"]

        # And uninstall round-trips the novel event too.
        uninstall_hooks(project_dir=str(tmp_path / "project"))
        settings = json.loads(
            (tmp_path / "project" / ".claude" / "settings.local.json").read_text()
        )
        assert "PreCompact" not in settings.get("hooks", {})


class TestHookInstaller:
    def test_plugin_route_is_the_only_claude_hook_registration_owner(
        self, tmp_path: Path, use_profile, plugin_route_active, capsys
    ):
        settings = tmp_path / ".claude" / "settings.json"
        manifest = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        use_profile(user_settings=settings, installed_plugins=manifest)
        plugin_route_active()

        install_hooks(scope="user")

        assert not settings.exists()
        out = capsys.readouterr().out.lower()
        assert "plugin" in out
        assert "owns" in out

    def test_plugin_owner_converges_a_stale_manual_registration(
        self, tmp_path: Path, use_profile, plugin_route_active
    ):
        settings = tmp_path / ".claude" / "settings.json"
        manifest = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        use_profile(user_settings=settings, installed_plugins=manifest)
        settings.parent.mkdir(parents=True)
        settings.write_text(
            json.dumps({
                "hooks": {
                    "Stop": [
                        {"matcher": "", "hooks": [{"command": "weave-hook stop"}]},
                        {"matcher": "", "hooks": [{"command": "foreign-hook"}]},
                    ]
                }
            }),
            encoding="utf-8",
        )
        plugin_route_active()

        install_hooks(scope="user")

        saved = json.loads(settings.read_text(encoding="utf-8"))
        commands = [
            hook["command"]
            for entry in saved["hooks"]["Stop"]
            for hook in entry["hooks"]
        ]
        assert commands == ["foreign-hook"]

    # --- plugin-owner sweep across scopes (#161 review) -------------------

    STALE = {
        "hooks": {
            "Stop": [
                {"matcher": "", "hooks": [{"command": "weave-hook stop"}]},
                {"matcher": "", "hooks": [{"command": "foreign-hook"}]},
            ]
        }
    }

    def _both_scopes(self, tmp_path: Path, use_profile) -> tuple[Path, Path, Path]:
        """Point the profile at a tmp machine scope; return (user, project_dir, project)."""
        user_settings = tmp_path / "home" / ".claude" / "settings.json"
        manifest = tmp_path / "home" / ".claude" / "plugins" / "installed_plugins.json"
        use_profile(user_settings=user_settings, installed_plugins=manifest)
        project_dir = tmp_path / "project"
        project_settings = project_dir / ".claude" / "settings.local.json"
        return user_settings, project_dir, project_settings

    def _write_stale(self, *paths: Path) -> None:
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.STALE), encoding="utf-8")

    def _stop_commands(self, path: Path) -> list[str]:
        saved = json.loads(path.read_text(encoding="utf-8"))
        return [
            hook["command"]
            for entry in saved.get("hooks", {}).get("Stop", [])
            for hook in entry["hooks"]
        ]

    def test_plugin_owner_sweeps_every_scope_not_just_the_named_one(
        self, tmp_path: Path, use_profile, plugin_route_active, capsys
    ):
        """The reported machine had a machine-scope AND a project-local
        registration; cleaning only the named scope left the other firing."""
        user_settings, project_dir, project_settings = self._both_scopes(
            tmp_path, use_profile
        )
        self._write_stale(user_settings, project_settings)
        plugin_route_active()

        install_hooks(project_dir=str(project_dir), scope="project")

        assert self._stop_commands(user_settings) == ["foreign-hook"]
        assert self._stop_commands(project_settings) == ["foreign-hook"]
        out = capsys.readouterr().out
        assert str(user_settings) in out
        assert str(project_settings) in out

    def test_plugin_owner_reports_nothing_when_nothing_was_stale(
        self, tmp_path: Path, use_profile, plugin_route_active, capsys
    ):
        user_settings, project_dir, _ = self._both_scopes(tmp_path, use_profile)
        plugin_route_active()

        install_hooks(project_dir=str(project_dir), scope="project")

        out = capsys.readouterr().out
        assert "No stale manual hooks found" in out
        assert "Removed" not in out
        assert not user_settings.exists()

    def test_plugin_owner_dry_run_writes_nothing(
        self, tmp_path: Path, use_profile, plugin_route_active, capsys
    ):
        user_settings, project_dir, project_settings = self._both_scopes(
            tmp_path, use_profile
        )
        self._write_stale(user_settings, project_settings)
        plugin_route_active()

        install_hooks(project_dir=str(project_dir), scope="project", dry_run=True)

        assert self._stop_commands(user_settings) == [
            "weave-hook stop",
            "foreign-hook",
        ]
        assert self._stop_commands(project_settings) == [
            "weave-hook stop",
            "foreign-hook",
        ]
        out = capsys.readouterr().out
        assert out.count("Would remove stale manual hooks from") == 2
        assert "Dry run — nothing was written." in out
        assert "Removed stale manual hooks" not in out

    def test_install_fresh(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))

        settings_path = project_dir / ".claude" / "settings.local.json"
        assert settings_path.exists()

        settings = json.loads(settings_path.read_text())
        assert "hooks" in settings
        # PreToolUse retired — fresh install must not register it.
        assert "PreToolUse" not in settings["hooks"]
        assert "PostToolUse" in settings["hooks"]

        # Two PostToolUse entries: action-tool gate + MCP-tool gate.
        matchers = {e["matcher"] for e in settings["hooks"]["PostToolUse"]}
        assert "Write|Edit|Bash" in matchers
        assert "mcp__thinkweave__.*" in matchers
        # Timeouts come from the canonical hooks.json (30s for PostToolUse
        # since PR #10), not the retired hand-maintained 5s default.
        for entry in settings["hooks"]["PostToolUse"]:
            assert entry["hooks"][0]["timeout"] == 30

    def test_install_preserves_existing(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)

        # Pre-existing settings: a foreign PreToolUse entry plus a foreign
        # PostToolUse entry. The installer must leave both untouched while
        # appending its own PostToolUse hook.
        existing = {
            "permissions": {"allow": ["Bash(ls)"]},
            "hooks": {
                "PreToolUse": [
                    {"matcher": "SomeOther", "hooks": [{"type": "command", "command": "echo hi"}]}
                ],
                "PostToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo post"}]}
                ],
            },
        }
        (claude_dir / "settings.local.json").write_text(json.dumps(existing))

        install_hooks(project_dir=str(project_dir))

        settings = json.loads((claude_dir / "settings.local.json").read_text())
        # Existing permission preserved
        assert "Bash(ls)" in settings["permissions"]["allow"]
        # Foreign PreToolUse hook left intact, no thinkweave entry added
        assert len(settings["hooks"]["PreToolUse"]) == 1
        assert not any("weave-hook" in str(entry) for entry in settings["hooks"]["PreToolUse"])
        # Foreign PostToolUse preserved + two thinkweave PostToolUse
        # entries appended (action gate + MCP gate).
        assert len(settings["hooks"]["PostToolUse"]) == 3
        weave_entries = [
            e for e in settings["hooks"]["PostToolUse"]
            if "weave-hook" in str(e)
        ]
        assert len(weave_entries) == 2
        weave_matchers = {e["matcher"] for e in weave_entries}
        assert weave_matchers == {"Write|Edit|Bash", "mcp__thinkweave__.*"}

    def test_install_migrates_legacy_shell_wrapper_command(self, tmp_path: Path):
        """Every historical hook form (run_hook.sh, `python -m`, bare
        weave-hook, install-time absolute-path snapshots, the interim bare
        `uv run --project` form) gets rewritten to the current canonical
        `bin/weave-hook-launch` shim form for retained phases. The retired
        PreToolUse phase is stripped instead."""
        project_dir = tmp_path / "project"
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)

        legacy = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Write|Edit",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/abs/path/to/run_hook.sh pre_tool_use",
                                "timeout": 5,
                            }
                        ],
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": "Write|Edit|Bash",
                        "hooks": [
                            {
                                "type": "command",
                                # Bare `weave-hook` form from the brief pre-
                                # absolute-path iteration of install.py.
                                "command": "weave-hook post_tool_use",
                                "timeout": 5,
                            }
                        ],
                    }
                ],
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python3 -m thinkweave.surfaces.hooks.handler stop",
                                "timeout": 5,
                            }
                        ],
                    }
                ],
            }
        }
        (claude_dir / "settings.local.json").write_text(json.dumps(legacy))

        install_hooks(project_dir=str(project_dir))

        settings = json.loads(
            (claude_dir / "settings.local.json").read_text()
        )
        # PreToolUse: retired phase stripped entirely (no foreign hooks
        # were present to retain, so the key disappears).
        assert "PreToolUse" not in settings["hooks"]
        # Retained phases rewritten in place — the legacy single-matcher
        # PostToolUse entry stays at one and gets rewritten to the
        # canonical form; the second MCP-matcher entry is appended
        # on top. Stop has no slot fan-out, so it stays at exactly one.
        assert len(settings["hooks"]["PostToolUse"]) == 2
        assert len(settings["hooks"]["Stop"]) == 1

        # Identify the action vs MCP PostToolUse entry by matcher.
        post_entries = settings["hooks"]["PostToolUse"]
        action_entry = next(e for e in post_entries if e["matcher"] == "Write|Edit|Bash")
        mcp_entry = next(e for e in post_entries if e["matcher"] == "mcp__thinkweave__.*")
        action_cmd = action_entry["hooks"][0]["command"]
        mcp_cmd = mcp_entry["hooks"][0]["command"]
        stop_cmd = settings["hooks"]["Stop"][0]["hooks"][0]["command"]

        # New form: fire-time resolution via the committed
        # `bin/weave-hook-launch` shim, ending in the phase arg — no
        # snapshotted script path.
        for cmd, phase in [
            (action_cmd, "post_tool_use"),
            (mcp_cmd, "post_tool_use"),
            (stop_cmd, "stop"),
        ]:
            assert cmd.endswith(f"/bin/weave-hook-launch\" {phase}")
            assert str(REPO_ROOT) in cmd

        # Legacy fragments fully replaced.
        assert "python3" not in stop_cmd
        assert "run_hook.sh" not in json.dumps(settings)

    def test_install_strips_retired_pretooluse_but_keeps_foreign(self, tmp_path: Path):
        """Re-running install must remove a stale thinkweave PreToolUse
        entry without disturbing PreToolUse hooks owned by other tools."""
        project_dir = tmp_path / "project"
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)

        legacy = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Write|Edit",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/abs/path/to/weave-hook pre_tool_use",
                                "timeout": 5,
                            }
                        ],
                    },
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "echo other-tool"}
                        ],
                    },
                ]
            }
        }
        (claude_dir / "settings.local.json").write_text(json.dumps(legacy))

        install_hooks(project_dir=str(project_dir))

        settings = json.loads(
            (claude_dir / "settings.local.json").read_text()
        )
        pre = settings["hooks"]["PreToolUse"]
        assert len(pre) == 1
        # The thinkweave entry is gone; the foreign one is intact.
        assert pre[0]["matcher"] == "Bash"
        assert "weave-hook" not in str(pre)

    def test_install_writes_absolute_project_root(self, tmp_path: Path):
        """Fresh install writes an absolute path to the committed
        `bin/weave-hook-launch` shim, which resolves `weave-hook` from the
        project venv AT FIRE TIME regardless of the caller's cwd — never an
        install-time snapshot of a venv script path, which repointed hooks at
        whatever (possibly stale) venv was live when `weave hooks install`
        ran (#50). The shim itself runs the uv ladder so a stripped harness
        PATH can't silently kill the hook (#47/#52)."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))

        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        # PreToolUse retired — never installed.
        assert "PreToolUse" not in settings["hooks"]
        for hook_type in ("SessionStart", "PostToolUse", "Stop"):
            for entry in settings["hooks"][hook_type]:
                cmd = entry["hooks"][0]["command"]
                # The embedded launcher path is absolute to this repo and
                # points at the committed shim (fire-time resolution lives
                # there, not in a snapshotted venv path).
                assert f'"{REPO_ROOT}/bin/weave-hook-launch"' in cmd, (
                    f"{hook_type}: command does not route through the "
                    f"absolute launcher path: {cmd!r}"
                )
                assert (REPO_ROOT / "bin" / "weave-hook-launch").exists()
                assert (REPO_ROOT / "pyproject.toml").exists()

    def test_install_idempotent(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))
        install_hooks(project_dir=str(project_dir))

        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        # PreToolUse retired — never installed; idempotent reinstall must
        # not resurrect it.
        assert "PreToolUse" not in settings["hooks"]
        # PostToolUse owns two slots (action + MCP) — must not duplicate
        # within slot. Single-matcher phases stay at exactly one.
        assert len(settings["hooks"]["PostToolUse"]) == 2
        post_matchers = {e["matcher"] for e in settings["hooks"]["PostToolUse"]}
        assert post_matchers == {"Write|Edit|Bash", "mcp__thinkweave__.*"}
        assert len(settings["hooks"]["SessionStart"]) == 1
        assert len(settings["hooks"]["Stop"]) == 1

    def test_install_includes_stop_hook(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))

        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        assert "Stop" in settings["hooks"]
        stop = settings["hooks"]["Stop"][0]
        assert "stop" in stop["hooks"][0]["command"]

    def test_install_includes_session_start_hook(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))

        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        assert "SessionStart" in settings["hooks"]
        ss = settings["hooks"]["SessionStart"][0]
        assert "session_start" in ss["hooks"][0]["command"]
        # No matcher is used for SessionStart
        assert ss["matcher"] == ""

    def test_install_registers_mcp_post_tool_use_matcher(self, tmp_path: Path):
        """RLVR context-served substrate depends on this matcher.

        Claude Code matches PostToolUse entries against the dispatched
        tool's ``tool_name`` string. MCP calls arrive with names like
        ``mcp__thinkweave__weave_search`` — they don't match
        ``Write|Edit|Bash``. Without a second matcher targeting
        ``mcp__thinkweave__.*``, ``_handle_post``'s retrieval branch
        (see operations/retrieval_log.RETRIEVAL_TOOLS) never fires and
        ``retrieval_log.jsonl`` only contains the SessionStart entry.

        Regression for the audit finding that the matcher was
        action-tool-only — pinning the installed shape rather than just
        the in-handler gate.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))

        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        post_entries = settings["hooks"]["PostToolUse"]
        # The MCP-matcher entry must be present.
        mcp_entry = next(
            (e for e in post_entries if e["matcher"] == "mcp__thinkweave__.*"),
            None,
        )
        assert mcp_entry is not None, (
            f"no MCP-matcher PostToolUse entry installed; got matchers "
            f"{[e['matcher'] for e in post_entries]!r}"
        )
        # It must dispatch to the same handler entry point as the action gate.
        assert "weave-hook" in mcp_entry["hooks"][0]["command"]
        assert mcp_entry["hooks"][0]["command"].endswith(" post_tool_use")

    def test_install_idempotent_for_mcp_matcher(self, tmp_path: Path):
        """Re-installing must rewrite the MCP entry in place, not duplicate."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))
        install_hooks(project_dir=str(project_dir))

        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        mcp_entries = [
            e for e in settings["hooks"]["PostToolUse"]
            if e["matcher"] == "mcp__thinkweave__.*"
        ]
        assert len(mcp_entries) == 1

    def test_uninstall(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))
        uninstall_hooks(project_dir=str(project_dir))

        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        assert "hooks" not in settings

    def test_install_user_scope_writes_to_home(
        self, tmp_path: Path, use_profile, capsys
    ):
        """``scope='user'`` targets ``~/.claude/settings.json`` (note: NOT the
        ``.local`` variant — the per-user file). Aim the harness profile at a
        tmp home so the test never touches the real home directory.
        """
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        use_profile(user_settings=fake_home / ".claude" / "settings.json")

        from thinkweave.surfaces.hooks.install import install_hooks

        install_hooks(scope="user", project_dir="")

        target = fake_home / ".claude" / "settings.json"
        assert target.exists(), f"expected user-scope file at {target}"
        # The legacy .local.json variant must NOT have been written.
        assert not (fake_home / ".claude" / "settings.local.json").exists()

        settings = json.loads(target.read_text())
        assert "hooks" in settings
        assert "SessionStart" in settings["hooks"]

        out = capsys.readouterr().out
        assert "scope=user" in out

    def test_install_project_scope_unchanged(self, tmp_path: Path):
        """Default ``scope='project'`` keeps the historical settings.local.json
        target (backwards compat for scripts calling ``weave hooks install``)."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        from thinkweave.surfaces.hooks.install import install_hooks

        install_hooks(scope="project", project_dir=str(project_dir))

        target = project_dir / ".claude" / "settings.local.json"
        assert target.exists()
        # Confirm the non-local file was NOT touched.
        assert not (project_dir / ".claude" / "settings.json").exists()

    def test_install_dry_run_prints_diff_and_does_not_write(
        self, tmp_path: Path, capsys
    ):
        """``dry_run=True`` prints a unified diff and writes nothing."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        from thinkweave.surfaces.hooks.install import install_hooks

        install_hooks(scope="project", project_dir=str(project_dir), dry_run=True)

        target = project_dir / ".claude" / "settings.local.json"
        # Critical: no file written, no parent .claude/ mkdir.
        assert not target.exists()
        assert not (project_dir / ".claude").exists()

        out = capsys.readouterr().out
        # Mentions the target path so the user knows which file applies.
        assert str(target) in out
        # Looks like a unified diff against the (empty) starting state.
        assert "+++" in out and "---" in out
        # The diff should include at least one of the hook keys being added.
        assert "SessionStart" in out or "PostToolUse" in out

    def test_install_invalid_scope_raises(self, tmp_path: Path):
        """Unknown scope value must fail loud at the helper boundary."""
        from thinkweave.surfaces.hooks.install import install_hooks

        with pytest.raises(ValueError, match="unknown scope"):
            install_hooks(scope="garbage", project_dir=str(tmp_path))

    def test_install_user_scope_idempotent(self, tmp_path: Path, use_profile):
        """Running ``install_hooks(scope='user')`` twice converges — second
        call produces no net change. Pins the same idempotency contract
        as the project-scope path."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        use_profile(user_settings=fake_home / ".claude" / "settings.json")

        from thinkweave.surfaces.hooks.install import install_hooks

        install_hooks(scope="user", project_dir="")
        target = fake_home / ".claude" / "settings.json"
        first = json.loads(target.read_text())

        install_hooks(scope="user", project_dir="")
        second = json.loads(target.read_text())

        assert first == second, "second install must be a no-op"
        # Single entry per single-matcher phase, two for PostToolUse.
        assert len(second["hooks"]["SessionStart"]) == 1
        assert len(second["hooks"]["PostToolUse"]) == 2

    def test_cli_smoke_install_scope_user_dry_run(self, tmp_path: Path, monkeypatch):
        """``weave hooks install --scope user --dry-run`` reaches ``cmd_hooks``
        with both flags set on the argparse Namespace."""
        # Redirect home so even if the dry-run did write (it shouldn't),
        # it wouldn't hit the real machine.
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        from thinkweave.surfaces.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(
            ["hooks", "install", "--scope", "user", "--dry-run"]
        )
        assert args.hooks_action == "install"
        assert args.scope == "user"
        assert args.dry_run is True

        # cmd_hooks should accept these flags and run without writing.
        from thinkweave.surfaces.cli.hooks import cmd_hooks

        cmd_hooks(args)
        # No file written.
        assert not (fake_home / ".claude" / "settings.json").exists()

    def test_uninstall_removes_session_start(self, tmp_path: Path):
        """SessionStart must be round-trippable like every other hook type."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))

        # Sanity — installed
        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        assert "SessionStart" in settings["hooks"]

        uninstall_hooks(project_dir=str(project_dir))
        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        # Either the whole hooks dict is gone or SessionStart specifically is empty/missing
        assert "hooks" not in settings or "SessionStart" not in settings.get("hooks", {})


class TestBuildEventEnrichment:
    """Tests for _build_event metadata enrichment (commit, test, insights)."""

    def test_bash_with_commit(self):
        output = "[main abc1234] Fix bug\n 2 files changed\n"
        event = _build_event("Bash", {"command": "git commit -m 'Fix bug'"}, output, "14:00")
        assert event is not None
        assert "commit" in event
        assert event["commit"]["hash"] == "abc1234"

    def test_bash_with_test_result(self):
        output = "====== 12 passed in 1.5s ======"
        event = _build_event("Bash", {"command": "uv run pytest"}, output, "14:00")
        assert event is not None
        assert "test_run" in event
        assert event["test_run"]["passed"] == 12

    def test_bash_with_git_push(self):
        event = _build_event("Bash", {"command": "git push origin feature/x"}, "", "14:00")
        assert event is not None
        assert event["git_branch"] == "feature/x"

    def test_insight_captured_in_event(self):
        output = "Some text\n★ Insight ─────────────────────────────────────\nKey point here.\n─────────────────────────────────────────────────\n"
        event = _build_event("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"}, output, "14:00")
        assert event is not None
        assert "insights" in event
        assert "Key point here." in event["insights"][0]

    def test_no_insight_no_field(self):
        event = _build_event("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"}, "", "14:00")
        assert event is not None
        assert "insights" not in event


class TestExtractToolOutputText:
    """Audit item A1 — Claude Code's PostToolUse payload uses ``tool_response``,
    not ``tool_output``, and for the Bash tool that key is an *object* with
    ``stdout`` / ``stderr`` / ``interrupted`` / ``isImage`` fields. The
    handler used to read ``tool_output`` directly and got ``""`` for every
    real Bash invocation — which silently dropped commit / test / insight
    capture across the whole hook pipeline. This test class pins the
    normalisation contract.
    """

    def test_reads_tool_response_dict_for_bash(self):
        """The current Claude Code shape — ``tool_response`` is a dict."""
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m 'fix'"},
            "tool_response": {
                "stdout": "[main abc1234] fix\n 2 files changed\n",
                "stderr": "",
                "interrupted": False,
                "isImage": False,
            },
        }
        assert "[main abc1234] fix" in _extract_tool_output_text(payload)

    def test_concatenates_stdout_and_stderr(self):
        """``pytest`` writes warnings to stderr alongside the summary line."""
        payload = {
            "tool_response": {"stdout": "12 passed\n", "stderr": "warn: x\n"},
        }
        out = _extract_tool_output_text(payload)
        assert "12 passed" in out
        assert "warn: x" in out

    def test_falls_back_to_stderr_when_stdout_empty(self):
        payload = {"tool_response": {"stdout": "", "stderr": "boom\n"}}
        assert _extract_tool_output_text(payload) == "boom\n"

    def test_legacy_tool_output_string_still_works(self):
        """Pre-A1 fixtures used ``tool_output`` as a string. Keep them green."""
        payload = {"tool_output": "[main abc1234] msg\n 1 file changed\n"}
        assert "[main abc1234]" in _extract_tool_output_text(payload)

    def test_tool_response_takes_precedence_over_tool_output(self):
        """When Claude Code sends both, prefer the canonical key."""
        payload = {
            "tool_response": {"stdout": "from-response"},
            "tool_output": "from-output",
        }
        assert _extract_tool_output_text(payload) == "from-response"

    def test_tool_response_string_form(self):
        """Some tools send a bare string under ``tool_response`` — use as-is."""
        assert _extract_tool_output_text({"tool_response": "plain text"}) == "plain text"

    def test_missing_both_yields_empty(self):
        assert _extract_tool_output_text({}) == ""

    def test_dict_with_no_text_yields_empty(self):
        assert _extract_tool_output_text(
            {"tool_response": {"interrupted": False, "isImage": False}}
        ) == ""

    @pytest.mark.parametrize(
        "tool_response",
        [
            # Write: `content` is the file just written.
            {
                "type": "create",
                "filePath": "/repo/notes.py",
                "content": "# ★ Insight ─────\n# Key point here.\n",
                "structuredPatch": [],
                "userModified": False,
            },
            # Edit: `originalFile` is the whole file, pre-edit.
            {
                "filePath": "/repo/a.py",
                "oldString": "x",
                "newString": "y",
                "originalFile": "# ★ Insight ─────\n# Key point here.\n",
                "structuredPatch": [{"lines": ["-x", "+y"]}],
                "userModified": False,
            },
        ],
        ids=["write", "edit"],
    )
    def test_file_echo_response_is_not_text(self, tool_response: dict):
        """Claude Code's Write/Edit ``tool_response`` echoes the file it just
        wrote. It is not tool *output* — treating it as such feeds the whole
        file back to ``_extract_insight_blocks``, so any ★ Insight block living
        in the source re-captures on every single touch (#107).
        """
        assert _extract_tool_output_text({"tool_response": tool_response}) == ""


class TestHandlePostCommitCapture:
    """End-to-end regression for the A1 fix.

    Drives ``_handle_post`` with the exact Claude Code PostToolUse shape
    (``tool_response`` as a dict carrying ``stdout``) and verifies that
    the resulting buffer line carries the ``commit`` subfield. Mirrors
    the failure mode the audit caught empirically: 0/405 native sessions
    had ``commits[]`` because the handler was reading ``tool_output``.
    """

    def test_bash_commit_lands_in_buffer(self, tmp_path: Path, monkeypatch):
        from thinkweave.core.config import Config
        from thinkweave.surfaces.hooks import handler as handler_mod

        vault = tmp_path / "vault"
        cfg = Config(vault_root=vault)
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)

        # Avoid the eager session-note creation path; A1 lives in the buffer
        # write, not in the session note materialisation.
        monkeypatch.setattr(
            "thinkweave.surfaces.hooks.handler._ensure_session",
            lambda *a, **k: None,
        )
        # Don't shell out to git for the file list — return a stable fixture.
        monkeypatch.setattr(
            "thinkweave.surfaces.hooks.handler._get_commit_files",
            lambda h: ["src/a.py", "src/b.py"],
        )

        payload = {
            "session_id": "ses-cc-a1",
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m 'A1 fix'"},
            # Real Claude Code shape — dict, not string, under tool_response.
            "tool_response": {
                "stdout": "[podcasts abc1234] A1 fix\n 2 files changed, 10 insertions(+)\n",
                "stderr": "",
                "interrupted": False,
                "isImage": False,
            },
        }
        handler_mod._handle_post("Bash", payload)

        buf_file = cfg.weave_dir / "buffer" / "ses-cc-a1.jsonl"
        assert buf_file.exists(), "expected a buffer line for the Bash event"
        rows = [json.loads(l) for l in buf_file.read_text().splitlines() if l.strip()]
        assert len(rows) == 1
        ev = rows[0]
        assert "commit" in ev, (
            "PostToolUse hook must carry commit subfield (A1 regression)"
        )
        assert ev["commit"]["hash"] == "abc1234"
        assert ev["commit"]["message"] == "A1 fix"
        assert ev["commit"]["files"] == ["src/a.py", "src/b.py"]

    def test_session_archive_lands_commits_in_frontmatter(
        self, tmp_path: Path, monkeypatch
    ):
        """End-to-end: PostToolUse → buffer → Stop hook → ``fm['commits']``.

        Pins the full pipeline from the audit's empirical finding all the
        way to the session note frontmatter.
        """
        from thinkweave.core.config import Config
        from thinkweave.core.schemas import NoteType
        from thinkweave.core.vault import VaultManager
        from thinkweave.surfaces.hooks import handler as handler_mod

        vault = tmp_path / "vault"
        cfg = Config(vault_root=vault)
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        monkeypatch.setattr(
            "thinkweave.surfaces.hooks.handler._get_commit_files",
            lambda h: ["src/a.py", "src/b.py", "src/c.py"],
        )

        # Pre-create the session note so _ensure_session is a no-op (the
        # find function just needs to locate it by source_session).
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        vm.create_note(
            NoteType.SESSION,
            "Session A1",
            project="alpha",
            extra_frontmatter={"source_session": "ses-cc-a1-e2e"},
        )

        # Drive a PostToolUse with a real Bash commit shape.
        handler_mod._handle_post(
            "Bash",
            {
                "session_id": "ses-cc-a1-e2e",
                "tool_name": "Bash",
                "tool_input": {"command": "git commit -m 'E2E commit'"},
                "tool_response": {
                    "stdout": "[main def5678] E2E commit\n 3 files changed\n",
                    "stderr": "",
                    "interrupted": False,
                    "isImage": False,
                },
            },
        )

        # Then drive Stop to archive the buffer into the session note.
        handler_mod._handle_stop({"session_id": "ses-cc-a1-e2e"})

        # The session note must now carry commits[] in its frontmatter.
        from thinkweave.core.schemas import NoteType as _NT

        ses = next(
            n for n in vm.list_notes(note_type=_NT.SESSION, limit=10)
            if n.frontmatter.get("source_session") == "ses-cc-a1-e2e"
        )
        assert ses.frontmatter.get("processed") is True
        commits = ses.frontmatter.get("commits") or []
        assert commits, "Stop hook must write commits[] to session frontmatter"
        assert commits[0]["hash"] == "def5678"
        assert commits[0]["message"] == "E2E commit"


class TestStopHookNoEmbed:
    """A1 (2026-06-06): the Stop hook no longer fires opportunistic
    embeddings. The cron path (``weave index --embed --only-new``) is the
    sole driver. This guard test asserts the regression — if anyone
    re-adds a ``compute_all`` call to ``_handle_stop``, the test trips.
    """

    def test_stop_hook_does_not_call_compute_all(self, tmp_path: Path, monkeypatch):
        from thinkweave.core.config import Config
        from thinkweave.core.schemas import NoteType
        from thinkweave.core.vault import VaultManager
        from thinkweave.surfaces.hooks import handler as handler_mod

        vault = tmp_path / "vault"
        cfg = Config(vault_root=vault)
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        vm.create_note(
            NoteType.SESSION,
            "Session embed-test",
            project="alpha",
            extra_frontmatter={"source_session": "ses-embed-test"},
        )
        buf = cfg.weave_dir / "buffer" / "ses-embed-test.jsonl"
        buf.parent.mkdir(parents=True, exist_ok=True)
        buf.write_text(
            json.dumps({
                "ts": "2026-05-29T00:00:00Z",
                "tool": "Bash",
                "command": "ls",
                "session_id": "ses-embed-test",
            }) + "\n",
            encoding="utf-8",
        )

        # Seed an API key — would have triggered the embed under the old code.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

        calls: list = []

        class _Tripwire:
            def __init__(self, config=None):
                pass

            def compute_all(self, **kw):
                calls.append(kw)
                return {}

        monkeypatch.setattr("thinkweave.core.embeddings.EmbeddingSearch", _Tripwire)

        handler_mod._handle_stop({"session_id": "ses-embed-test"})

        assert calls == [], (
            "Stop hook must NOT call EmbeddingSearch.compute_all — "
            "embeddings are cron-driven only (plan A1, 2026-06-06)."
        )


class TestDiffContext:
    def test_edit_context(self):
        ctx = _diff_context("Edit", {"old_string": "foo = 1", "new_string": "foo = 2"})
        assert "foo = 1" in ctx
        assert "foo = 2" in ctx
        assert "→" in ctx

    def test_edit_truncates_long_strings(self):
        ctx = _diff_context("Edit", {"old_string": "x" * 200, "new_string": "y" * 200})
        # Should truncate to ~80 chars each
        assert len(ctx) < 250

    def test_write_context(self):
        ctx = _diff_context("Write", {"content": "def main():\n    pass\n"})
        assert "def main():" in ctx

    def test_write_skips_comments(self):
        ctx = _diff_context("Write", {"content": "# comment\n# another\ndef real():\n"})
        assert "def real():" in ctx

    def test_empty_input(self):
        assert _diff_context("Edit", {}) == ""
        assert _diff_context("Write", {}) == ""


class TestFirstMeaningfulLine:
    def test_skips_blanks_and_comments(self):
        assert _first_meaningful_line("\n\n# comment\n//js comment\nactual code") == "actual code"

    def test_empty(self):
        assert _first_meaningful_line("") == ""
        assert _first_meaningful_line("\n\n\n") == ""


class TestGitCommitDetection:
    def test_is_git_commit(self):
        assert _is_git_commit("git commit -m 'fix'")
        assert _is_git_commit("git commit -am 'fix'")
        assert not _is_git_commit("git commit --amend")
        assert not _is_git_commit("git log")
        assert not _is_git_commit("echo git commit")

    def test_parse_commit_output(self):
        output = '[main abc1234] Fix parser bug\n 2 files changed, 15 insertions(+), 3 deletions(-)\n'
        result = _parse_commit_from_output("git commit -m 'Fix parser bug'", output)
        assert result is not None
        assert result["hash"] == "abc1234"
        assert result["message"] == "Fix parser bug"
        assert result["files_changed"] == 2

    def test_parse_commit_from_output_line(self):
        output = '[feature/x 1a2b3c4] Refactor module\n 1 file changed, 5 insertions(+)\n'
        result = _parse_commit_from_output("git commit", output)
        assert result is not None
        assert result["hash"] == "1a2b3c4"
        assert "Refactor module" in result["message"]

    def test_parse_commit_empty_output(self):
        assert _parse_commit_from_output("git commit", "") is None


class TestGetCommitFiles:
    @patch("thinkweave.surfaces.hooks.handler.subprocess.run")
    def test_returns_file_list(self, mock_run):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "src/a.py\nsrc/b.py\n"
        result = _get_commit_files("abc1234")
        assert result == ["src/a.py", "src/b.py"]
        mock_run.assert_called_once()

    @patch("thinkweave.surfaces.hooks.handler.subprocess.run")
    def test_returns_empty_on_error(self, mock_run):
        mock_run.side_effect = FileNotFoundError("git not found")
        assert _get_commit_files("abc1234") == []

    @patch("thinkweave.surfaces.hooks.handler.subprocess.run")
    def test_returns_empty_on_timeout(self, mock_run):
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired("git", 5)
        assert _get_commit_files("abc1234") == []

    @patch("thinkweave.surfaces.hooks.handler.subprocess.run")
    def test_returns_empty_on_nonzero_exit(self, mock_run):
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        assert _get_commit_files("abc1234") == []

    @patch("thinkweave.surfaces.hooks.handler.subprocess.run")
    def test_filters_blank_lines(self, mock_run):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "src/a.py\n\n  \nsrc/b.py\n"
        result = _get_commit_files("abc1234")
        assert result == ["src/a.py", "src/b.py"]


class TestBuildEventCommitFiles:
    @patch("thinkweave.surfaces.hooks.handler._get_commit_files", return_value=["src/a.py", "src/b.py"])
    def test_commit_event_includes_files(self, mock_files):
        output = "[main abc1234] Fix bug\n 2 files changed\n"
        event = _build_event("Bash", {"command": "git commit -m 'Fix bug'"}, output, "14:00")
        assert event["commit"]["files"] == ["src/a.py", "src/b.py"]

    @patch("thinkweave.surfaces.hooks.handler._get_commit_files", return_value=[])
    def test_commit_event_no_files_key_when_empty(self, mock_files):
        output = "[main abc1234] Fix bug\n 2 files changed\n"
        event = _build_event("Bash", {"command": "git commit -m 'Fix bug'"}, output, "14:00")
        assert "files" not in event["commit"]


class TestTestDetection:
    def test_is_test_command(self):
        assert _is_test_command("pytest tests/")
        assert _is_test_command("uv run pytest -x")
        assert _is_test_command("python -m pytest")
        assert not _is_test_command("python script.py")
        assert not _is_test_command("echo pytest")

    def test_is_test_command_chained(self):
        """Chained commands like 'cd foo && uv run pytest' should be detected."""
        assert _is_test_command("cd src && uv run pytest -x")
        assert _is_test_command("export FOO=1 && pytest tests/")
        assert _is_test_command("cd /tmp; pytest")
        assert _is_test_command("echo setup || uv run pytest --tb=short")
        assert not _is_test_command("cd src && echo pytest")

    def test_parse_test_result_passed(self):
        output = "====== 12 passed in 1.5s ======"
        result = _parse_test_result("uv run pytest", output)
        assert result is not None
        assert result["passed"] == 12
        assert "failed" not in result

    def test_parse_test_result_mixed(self):
        output = "====== 10 passed, 2 failed, 1 error in 3.2s ======"
        result = _parse_test_result("pytest", output)
        assert result is not None
        assert result["passed"] == 10
        assert result["failed"] == 2
        assert result["errors"] == 1

    def test_parse_test_result_empty(self):
        assert _parse_test_result("pytest", "") is None


class TestPushBranch:
    def test_git_push_with_remote_and_branch(self):
        assert _parse_push_branch("git push origin feature/x") == "feature/x"

    def test_git_push_with_remote_only(self):
        assert _parse_push_branch("git push origin") == "origin"

    def test_git_push_with_flags(self):
        assert _parse_push_branch("git push -u origin main") == "main"

    def test_not_a_push(self):
        assert _parse_push_branch("git pull origin main") is None


class TestEventBuffer:
    def test_buffer_event_creates_file(self, tmp_path):
        _buffer_event(tmp_path, "ses-test", {"ts": "14:00", "tool": "Edit", "file": "a.py"})
        buf_file = tmp_path / "buffer" / "ses-test.jsonl"
        assert buf_file.exists()
        lines = buf_file.read_text().splitlines()
        assert len(lines) == 1
        assert '"Edit"' in lines[0]

    def test_buffer_appends(self, tmp_path):
        _buffer_event(tmp_path, "ses-test", {"ts": "14:00", "tool": "Edit", "file": "a.py"})
        _buffer_event(tmp_path, "ses-test", {"ts": "14:05", "tool": "Write", "file": "b.py"})
        lines = (tmp_path / "buffer" / "ses-test.jsonl").read_text().splitlines()
        assert len(lines) == 2

    def test_build_event_edit(self):
        event = _build_event("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"}, "", "14:00")
        assert event is not None
        assert event["tool"] == "Edit"
        assert event["file"] == "a.py"

    def test_build_event_skips_internal(self):
        event = _build_event("Edit", {"file_path": ".claude/settings.json"}, "", "14:00")
        assert event is None

    def test_build_event_bash_significant(self):
        event = _build_event("Bash", {"command": "git commit -m 'fix'"}, "", "14:00")
        assert event is not None
        assert event["tool"] == "Bash"

    def test_build_event_bash_insignificant(self):
        event = _build_event("Bash", {"command": "ls -la"}, "", "14:00")
        assert event is None

    def test_cleanup_buffer(self, tmp_path):
        buf_dir = tmp_path / "buffer"
        buf_dir.mkdir()
        buf_file = buf_dir / "ses-test.jsonl"
        buf_file.write_text('{"ts":"14:00"}\n')
        cleanup_buffer(tmp_path, "ses-test")
        assert not buf_file.exists()

    def test_cleanup_buffer_missing_file(self, tmp_path):
        # Should not raise
        cleanup_buffer(tmp_path, "nonexistent")

    def test_archive_buffer(self, tmp_path):
        buf_dir = tmp_path / "buffer"
        buf_dir.mkdir()
        buf_file = buf_dir / "ses-test.jsonl"
        buf_file.write_text('{"ts":"14:00"}\n')

        session_dir = tmp_path / "session_dir"
        session_dir.mkdir()

        archive_buffer(tmp_path, "ses-test", session_dir)

        assert not buf_file.exists()
        assert (session_dir / "events.jsonl").exists()
        assert '{"ts":"14:00"}' in (session_dir / "events.jsonl").read_text()

    def test_archive_buffer_missing_file(self, tmp_path):
        session_dir = tmp_path / "session_dir"
        session_dir.mkdir()
        # Should not raise
        archive_buffer(tmp_path, "nonexistent", session_dir)


class TestReadBuffer:
    def test_reads_events(self, tmp_path):
        buf_dir = tmp_path / "buffer"
        buf_dir.mkdir()
        (buf_dir / "ses-test.jsonl").write_text(
            '{"ts":"14:00","tool":"Edit","file":"a.py"}\n'
            '{"ts":"14:05","tool":"Bash","command":"pytest"}\n'
        )
        events = _read_buffer(tmp_path, "ses-test")
        assert len(events) == 2
        assert events[0]["tool"] == "Edit"
        assert events[1]["tool"] == "Bash"

    def test_missing_buffer(self, tmp_path):
        assert _read_buffer(tmp_path, "nonexistent") == []

    def test_skips_bad_json(self, tmp_path):
        buf_dir = tmp_path / "buffer"
        buf_dir.mkdir()
        (buf_dir / "ses-test.jsonl").write_text(
            '{"ts":"14:00","tool":"Edit"}\n'
            'not valid json\n'
            '{"ts":"14:05","tool":"Bash"}\n'
        )
        events = _read_buffer(tmp_path, "ses-test")
        assert len(events) == 2


class TestSummarizeEvents:
    def test_extracts_files(self):
        events = [
            {"ts": "14:00", "tool": "Edit", "file": "a.py"},
            {"ts": "14:05", "tool": "Edit", "file": "b.py"},
            {"ts": "14:10", "tool": "Edit", "file": "a.py"},  # duplicate
        ]
        meta = _summarize_events(events)
        assert meta["files_touched"] == ["a.py", "b.py"]  # deduped, order preserved

    def test_extracts_commits(self):
        events = [
            {"ts": "14:00", "tool": "Bash", "command": "git commit", "commit": {"hash": "abc", "message": "fix"}},
        ]
        meta = _summarize_events(events)
        assert len(meta["commits"]) == 1
        assert meta["commits"][0]["hash"] == "abc"

    def test_extracts_test_runs(self):
        events = [
            {"ts": "14:00", "tool": "Bash", "command": "pytest", "test_run": {"passed": 10, "failed": 0}},
        ]
        meta = _summarize_events(events)
        assert meta["test_runs"][0]["passed"] == 10

    def test_extracts_insights(self):
        events = [
            {"ts": "14:00", "tool": "Edit", "file": "a.py", "insights": ["Insight A"]},
            {"ts": "14:05", "tool": "Edit", "file": "b.py", "insights": ["Insight B", "Insight C"]},
        ]
        meta = _summarize_events(events)
        assert meta["insights"] == ["Insight A", "Insight B", "Insight C"]

    def test_extracts_git_branch(self):
        events = [
            {"ts": "14:00", "tool": "Bash", "command": "git push", "git_branch": "feature/x"},
        ]
        meta = _summarize_events(events)
        assert meta["git_branch"] == "feature/x"

    def test_empty_events(self):
        meta = _summarize_events([])
        assert meta["files_touched"] == []
        assert meta["commits"] == []
        assert meta["insights"] == []


class TestHookErrorLogging:
    def test_initialization_access_failure_is_visible(
        self, monkeypatch, capsys
    ):
        from thinkweave.surfaces.hooks import handler as handler_mod

        monkeypatch.setattr(
            "thinkweave.core.config.is_vault_initialized",
            lambda cfg: (_ for _ in ()).throw(PermissionError("vault denied")),
        )
        monkeypatch.setattr("sys.argv", ["weave-hook", "session_start"])
        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))

        handler_mod.main()

        emitted = json.loads(capsys.readouterr().out)
        assert "not persisted" in emitted["systemMessage"]
        assert "PermissionError" in emitted["systemMessage"]

    def test_log_error_creates_file(self, tmp_path):
        cfg = Config(vault_root=tmp_path / "vault")
        with patch("thinkweave.core.config.load_config", return_value=cfg):
            _log_error("test_hook", ValueError("test error"))

        log_path = cfg.weave_dir / "hooks.log"
        assert log_path.exists()
        content = log_path.read_text()
        assert "test_hook" in content
        assert "test error" in content

    def test_log_error_appends(self, tmp_path):
        cfg = Config(vault_root=tmp_path / "vault")
        with patch("thinkweave.core.config.load_config", return_value=cfg):
            _log_error("hook1", ValueError("error1"))
            _log_error("hook2", RuntimeError("error2"))

        content = (cfg.weave_dir / "hooks.log").read_text()
        assert "error1" in content
        assert "error2" in content

    def test_log_error_never_raises(self):
        # Even if everything fails inside, _log_error should not raise
        # Force a failure by making the import path invalid
        with patch.dict("sys.modules", {"thinkweave.core.config": None}):
            _log_error("test", ValueError("err"))  # Should not raise

    def test_prompt_persistence_failure_is_visible_to_the_harness(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        from thinkweave.surfaces.hooks import handler as handler_mod

        monkeypatch.setattr(
            handler_mod,
            "_buffer_event",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                PermissionError("vault is outside the writable sandbox")
            ),
        )

        handler_mod._handle_user_prompt_submit(
            {"session_id": "ses-denied", "prompt": "persist me", "cwd": str(tmp_path)}
        )

        emitted = json.loads(capsys.readouterr().out)
        message = emitted["systemMessage"]
        assert "ThinkWeave" in message
        assert "not persisted" in message
        assert "PermissionError" in message

    def test_archive_failure_is_visible_and_stop_remains_retriable(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        from thinkweave.core.schemas import NoteType
        from thinkweave.core.vault import VaultManager
        from thinkweave.surfaces.hooks import handler as handler_mod

        cfg = Config(vault_root=tmp_path / "vault")
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        path = vm.create_note(
            NoteType.SESSION,
            "Retryable Stop",
            project="alpha",
            extra_frontmatter={"source_session": "ses-stop-denied"},
        )
        _buffer_event(
            cfg.weave_dir,
            "ses-stop-denied",
            {"type": "prompt", "text": "keep", "ts": "now"},
        )
        monkeypatch.setattr(
            handler_mod,
            "archive_buffer",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                PermissionError("archive denied")
            ),
        )

        handler_mod._handle_stop({"session_id": "ses-stop-denied"})

        emitted = json.loads(capsys.readouterr().out)
        assert "not persisted" in emitted["systemMessage"]
        assert vm.read_note(path).frontmatter.get("processed") is not True
        assert (cfg.weave_dir / "buffer" / "ses-stop-denied.jsonl").exists()

    def test_session_start_capture_failure_keeps_context_and_adds_diagnostic(
        self, monkeypatch, capsys
    ):
        from thinkweave.surfaces.hooks import handler as handler_mod

        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context",
            lambda *args, **kwargs: "STARTUP CONTEXT",
        )
        monkeypatch.setattr(
            handler_mod,
            "_buffer_event",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                PermissionError("vault write denied")
            ),
        )

        handler_mod._handle_session_start(
            {"session_id": "ses-denied-start", "cwd": "/repo"}
        )

        emitted = json.loads(capsys.readouterr().out)
        assert emitted["hookSpecificOutput"]["additionalContext"] == "STARTUP CONTEXT"
        assert "not persisted" in emitted["systemMessage"]


class TestBuildAutoSummary:
    def test_with_files(self):
        s = _build_auto_summary(["a.py", "b.py"], [], [], 5)
        assert "Edited 2 files" in s
        assert "a.py" in s

    def test_with_commits(self):
        s = _build_auto_summary([], [{"message": "Fix bug"}], [], 1)
        assert "Commits:" in s
        assert "Fix bug" in s

    def test_with_tests(self):
        s = _build_auto_summary([], [], [{"passed": 12, "failed": 1}], 1)
        assert "12 passed" in s
        assert "1 failed" in s

    def test_fallback_event_count(self):
        s = _build_auto_summary([], [], [], 7)
        assert "7 tool events recorded" in s

    def test_many_files_truncated(self):
        files = [f"file{i}.py" for i in range(10)]
        s = _build_auto_summary(files, [], [], 10)
        assert "+5 more" in s


class TestSessionStartHandler:
    """End-to-end: run _handle_session_start with a fresh vault and inspect stdout."""

    def _run(self, vault_dir: Path, project: str, monkeypatch) -> dict:
        """Invoke _handle_session_start with a stubbed config + project, capture stdout."""
        import io

        from thinkweave.core.config import Config
        from thinkweave.surfaces.hooks import handler as handler_mod

        cfg = Config(vault_root=vault_dir)
        # Force our config through load_config so the handler uses the tmp vault
        monkeypatch.setattr(
            "thinkweave.core.config.load_config", lambda: cfg
        )
        monkeypatch.setattr(
            "thinkweave.surfaces.hooks.handler._detect_project",
            lambda hook_input: project,
        )

        buf = io.StringIO()
        monkeypatch.setattr("sys.stdout", buf)
        handler_mod._handle_session_start({"session_id": "cc-test", "cwd": str(vault_dir)})
        return json.loads(buf.getvalue() or "{}")

    def test_empty_vault_emits_valid_response(self, tmp_path: Path, monkeypatch):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        (vault_dir / ".weave").mkdir()

        result = self._run(vault_dir, "ghost", monkeypatch)
        # Either empty (no payload) or contains hookSpecificOutput
        if result:
            assert "hookSpecificOutput" in result
            assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
            assert "additionalContext" in result["hookSpecificOutput"]

    def test_populated_vault_emits_additional_context(
        self, tmp_path: Path, monkeypatch
    ):
        from thinkweave.core.indexer import Indexer
        from thinkweave.core.schemas import NoteType
        from thinkweave.core.vault import VaultManager
        from thinkweave.core.config import Config

        vault_dir = tmp_path / "vault"
        cfg = Config(vault_root=vault_dir)
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()

        vm.create_note(
            NoteType.SESSION,
            "Populated session",
            body=(
                "## Summary\n"
                "We built SessionStart.\n"
                "\n## Candidate Insights\n"
                "\n- **An insight title** body\n"
            ),
            project="alpha",
            extra_frontmatter={
                "processed": True,
                "processed_at": "2026-04-07",
                "source_session": "cc-alpha",
            },
        )
        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()

        result = self._run(vault_dir, "alpha", monkeypatch)
        assert "hookSpecificOutput" in result
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Populated session" in ctx
        assert "An insight title" in ctx
        assert "## Header" in ctx
        assert "## Available MCP Tools" in ctx

    def test_failure_in_payload_does_not_block(self, tmp_path: Path, monkeypatch):
        """Hook must always exit cleanly, even if the payload builder raises."""
        import io

        from thinkweave.surfaces.hooks import handler as handler_mod

        def boom(*args, **kwargs):
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context", boom
        )
        buf = io.StringIO()
        monkeypatch.setattr("sys.stdout", buf)

        # Should not raise
        handler_mod._handle_session_start(
            {"session_id": "cc-test", "cwd": str(tmp_path)}
        )
        # Stdout should still be valid JSON (empty dict)
        result = json.loads(buf.getvalue() or "{}")
        assert isinstance(result, dict)


class TestUserPromptSubmitHook:
    """Phase 4 E1 — UserPromptSubmit captures every prompt to the JSONL buffer."""

    def test_appends_prompt_event(self, tmp_path: Path, monkeypatch):
        from thinkweave.core.config import Config
        from thinkweave.surfaces.hooks import handler as handler_mod

        vault = tmp_path / "vault"
        cfg = Config(vault_root=vault)
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)

        # Avoid the eager session-note creation path (it indexes/writes to
        # the vault) — for this unit test we only care about the JSONL line.
        monkeypatch.setattr(
            "thinkweave.surfaces.hooks.handler._ensure_session",
            lambda *a, **k: None,
        )

        handler_mod._handle_user_prompt_submit(
            {
                "session_id": "ses-cc-1",
                "prompt": "What does the indexer skip?",
                "cwd": "/some/where",
            }
        )

        buf_file = cfg.weave_dir / "buffer" / "ses-cc-1.jsonl"
        assert buf_file.exists()
        lines = buf_file.read_text().splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["type"] == "prompt"
        assert row["text"] == "What does the indexer skip?"
        assert row["session_id"] == "ses-cc-1"
        assert row["cwd"] == "/some/where"
        assert row["ts"]  # populated

    def test_missing_text_skipped(self, tmp_path: Path, monkeypatch):
        from thinkweave.core.config import Config
        from thinkweave.surfaces.hooks import handler as handler_mod

        vault = tmp_path / "vault"
        cfg = Config(vault_root=vault)
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        monkeypatch.setattr(
            "thinkweave.surfaces.hooks.handler._ensure_session",
            lambda *a, **k: None,
        )

        handler_mod._handle_user_prompt_submit(
            {"session_id": "ses-cc-2", "prompt": ""}
        )

        # No buffer should have been created
        assert not (cfg.weave_dir / "buffer" / "ses-cc-2.jsonl").exists()

    def _run_prompt(self, cfg, monkeypatch, session_id: str, prompt: str):
        """Drive _handle_user_prompt_submit with session-note creation stubbed."""
        from thinkweave.surfaces.hooks import handler as handler_mod

        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        monkeypatch.setattr(
            "thinkweave.surfaces.hooks.handler._ensure_session",
            lambda *a, **k: None,
        )
        handler_mod._handle_user_prompt_submit(
            {"session_id": session_id, "prompt": prompt, "cwd": "/p"}
        )
        buf_file = cfg.weave_dir / "buffer" / f"{session_id}.jsonl"
        if not buf_file.exists():
            return []
        return [json.loads(ln) for ln in buf_file.read_text().splitlines() if ln.strip()]

    def test_hook_never_writes_feedback_events(self, tmp_path: Path, monkeypatch):
        # #101 — hooks capture, never judge. Even an unambiguous correction
        # produces only the raw prompt event; feedback labeling happens
        # asynchronously via `weave wrap-finalize --feedback`.
        from thinkweave.core.config import Config

        cfg = Config(vault_root=tmp_path / "vault")
        for sid, prompt in (
            ("ses-fb-1", "no, that's wrong — use a dict instead"),
            ("ses-fb-2", "yes, exactly — perfect"),
            ("ses-fb-3", "Add a feedback register to the hook"),
        ):
            rows = self._run_prompt(cfg, monkeypatch, sid, prompt)
            assert [r for r in rows if r.get("type") == "feedback"] == []
            assert [r for r in rows if r.get("type") == "prompt"]

    def test_install_registers_user_prompt_submit(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))

        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        assert "UserPromptSubmit" in settings["hooks"]
        ups = settings["hooks"]["UserPromptSubmit"][0]
        assert "user_prompt_submit" in ups["hooks"][0]["command"]
        assert "weave-hook" in ups["hooks"][0]["command"]

    def test_install_idempotent_with_user_prompt_submit(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))
        install_hooks(project_dir=str(project_dir))

        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        assert len(settings["hooks"]["UserPromptSubmit"]) == 1

    def test_uninstall_removes_user_prompt_submit(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))
        uninstall_hooks(project_dir=str(project_dir))

        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        assert "hooks" not in settings or "UserPromptSubmit" not in settings.get(
            "hooks", {}
        )

    def test_install_user_prompt_submit_timeout_raised(self, tmp_path: Path):
        # R2 runs in this hook; its timeout is raised above the harness
        # default to cover the bounded embedding deadline + render +
        # write-back. The value is authored in hooks/hooks.json (30s since
        # PR #10) — the installer derives it, never hand-maintains it.
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))
        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        ups = settings["hooks"]["UserPromptSubmit"][0]
        assert ups["hooks"][0]["timeout"] == 30


class TestPromptTimeEnrichment:
    """R2 — UserPromptSubmit prepends a bounded, deduped vault block."""

    def _cfg(self, tmp_path: Path, monkeypatch):
        from thinkweave.core.config import Config

        cfg = Config(vault_root=tmp_path / "vault")
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        monkeypatch.setattr(
            "thinkweave.surfaces.hooks.handler._ensure_session",
            lambda *a, **k: None,
        )
        return cfg

    def test_emits_additional_context_and_writes_back(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        from thinkweave.surfaces.hooks import handler as handler_mod

        cfg = self._cfg(tmp_path, monkeypatch)
        block = "📎 Possibly relevant from your vault (optional):\n- [[n-aaaaaa01]] (note) — X"
        monkeypatch.setattr(
            "thinkweave.operations.prompt_time_retrieval.build_enrichment",
            lambda *a, **k: (block, ["n-aaaaaa01"], False),
        )

        handler_mod._handle_user_prompt_submit(
            {"session_id": "ses-r2", "prompt": "a real substantive question here"}
        )

        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert out["hookSpecificOutput"]["additionalContext"] == block

        # Buffer carries the prompt event AND the prompt-time write-back.
        lines = [
            json.loads(x)
            for x in (cfg.weave_dir / "buffer" / "ses-r2.jsonl")
            .read_text().splitlines()
            if x.strip()
        ]
        assert lines[0]["type"] == "prompt"
        wb = lines[-1]
        assert wb["type"] == "retrieval"
        assert wb["tool"] == "prompt_time_retrieval"
        assert wb["returned_ids"] == ["n-aaaaaa01"]
        assert wb["chars"] == len(block)

    def test_deadline_miss_writes_distinct_telemetry_event(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """A deadline miss must land as its own event — not a firing, not a
        ``retrieval`` event — so it's invisible to both the firings ledger
        and the indexer's context_served projection (see the module
        docstring in operations/prompt_time_retrieval.py)."""
        from thinkweave.surfaces.hooks import handler as handler_mod

        cfg = self._cfg(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "thinkweave.operations.prompt_time_retrieval.build_enrichment",
            lambda *a, **k: (None, [], True),
        )

        handler_mod._handle_user_prompt_submit(
            {"session_id": "ses-r2miss", "prompt": "a real substantive question here"}
        )

        # No injection this turn — plain response.
        assert json.loads(capsys.readouterr().out) == {}

        lines = [
            json.loads(x)
            for x in (cfg.weave_dir / "buffer" / "ses-r2miss.jsonl")
            .read_text().splitlines()
            if x.strip()
        ]
        assert lines[0]["type"] == "prompt"
        miss = lines[-1]
        assert miss["type"] == "prompt_time_miss"
        assert miss["session_id"] == "ses-r2miss"
        # Must never carry the firing tag or the "retrieval" type — those
        # are what the firings ledger and the context_served projection key
        # off of, respectively.
        assert miss.get("tool") != "prompt_time_retrieval"
        assert miss["type"] != "retrieval"

    def test_noop_emits_plain_and_no_writeback(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        from thinkweave.surfaces.hooks import handler as handler_mod

        cfg = self._cfg(tmp_path, monkeypatch)
        # Real module path, but disabled → guaranteed no-op without any search.
        cfg.retrieval_prompt_time.enabled = False

        handler_mod._handle_user_prompt_submit(
            {"session_id": "ses-r2b", "prompt": "a real substantive question here"}
        )

        assert json.loads(capsys.readouterr().out) == {}
        lines = [
            json.loads(x)
            for x in (cfg.weave_dir / "buffer" / "ses-r2b.jsonl")
            .read_text().splitlines()
            if x.strip()
        ]
        assert len(lines) == 1 and lines[0]["type"] == "prompt"

    def test_enrichment_failure_never_breaks_turn(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        from thinkweave.surfaces.hooks import handler as handler_mod

        cfg = self._cfg(tmp_path, monkeypatch)

        def _boom(*a, **k):
            raise RuntimeError("search exploded")

        monkeypatch.setattr(
            "thinkweave.operations.prompt_time_retrieval.build_enrichment", _boom
        )

        # Must not raise; emits a plain response; prompt event still captured.
        handler_mod._handle_user_prompt_submit(
            {"session_id": "ses-r2c", "prompt": "a real substantive question here"}
        )
        assert json.loads(capsys.readouterr().out) == {}
        assert (cfg.weave_dir / "buffer" / "ses-r2c.jsonl").exists()


# Realistic Claude Code hook envelopes. The distinguishing feature versus the
# Codex fixtures in tests/test_codex_hooks.py is what is MISSING: Claude Code
# stamps no per-delivery id on SessionStart or UserPromptSubmit, which is
# exactly the case the #161 dedup has to leave alone.
CC_SESSION_ID = "9d1c8a02-4b77-4a1e-8f0d-2c6a5b3e9f11"
CC_TRANSCRIPT = f"/home/u/.claude/projects/-home-u-proj/{CC_SESSION_ID}.jsonl"


def _cc_session_start(source: str) -> dict:
    return {
        "session_id": CC_SESSION_ID,
        "transcript_path": CC_TRANSCRIPT,
        "cwd": "/home/u/proj",
        "hook_event_name": "SessionStart",
        "source": source,
    }


def _cc_prompt(prompt: str) -> dict:
    return {
        "session_id": CC_SESSION_ID,
        "transcript_path": CC_TRANSCRIPT,
        "cwd": "/home/u/proj",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    }


def _cc_write(tool_use_id: str) -> dict:
    return {
        "session_id": CC_SESSION_ID,
        "transcript_path": CC_TRANSCRIPT,
        "cwd": "/home/u/proj",
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_use_id": tool_use_id,
        "tool_input": {
            "file_path": "/home/u/proj/src/app.py",
            "content": "def go():\n    return 1\n",
        },
        "tool_response": {"filePath": "/home/u/proj/src/app.py", "success": True},
    }


class TestDuplicateDeliveryPolicy:
    """#161: collapse replayed deliveries, never collapse genuine repeats.

    The receipt is keyed on the harness's own per-delivery id
    (``tool_use_id`` / ``turn_id``). Events that carry none are written
    unconditionally — the earlier content-hash surrogate got this wrong in
    both directions, and the cure for id-less duplicates is single-owner
    registration (``TestHookInstaller`` below), not a guess here.
    """

    def _cfg(self, tmp_path: Path, monkeypatch) -> Config:
        cfg = Config(vault_root=tmp_path / "vault")
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        monkeypatch.setattr(
            "thinkweave.surfaces.hooks.handler._ensure_session",
            lambda *a, **k: None,
        )
        return cfg

    def _buffered(self, cfg: Config) -> list[dict]:
        return _read_buffer(cfg.weave_dir, CC_SESSION_ID)

    def test_replayed_post_tool_use_with_same_tool_use_id_persists_once(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        from thinkweave.surfaces.hooks import handler as handler_mod

        cfg = self._cfg(tmp_path, monkeypatch)
        payload = _cc_write("toolu_01H9kQ7mVn3xJc2pR8sT4bYd")

        handler_mod._handle_post("Write", payload)
        handler_mod._handle_post("Write", payload)
        capsys.readouterr()

        edits = [e for e in self._buffered(cfg) if e.get("file")]
        assert len(edits) == 1, "a replayed tool delivery was persisted twice"

    def test_distinct_tool_use_ids_persist_separately(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        from thinkweave.surfaces.hooks import handler as handler_mod

        cfg = self._cfg(tmp_path, monkeypatch)
        handler_mod._handle_post("Write", _cc_write("toolu_01AAAAAAAAAAAAAAAAAAAAAA"))
        handler_mod._handle_post("Write", _cc_write("toolu_01BBBBBBBBBBBBBBBBBBBBBB"))
        capsys.readouterr()

        assert len([e for e in self._buffered(cfg) if e.get("file")]) == 2

    def test_repeated_identical_prompt_is_captured_twice(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """Sending `continue` twice is two prompts, not one delivered twice.

        Claude Code's UserPromptSubmit has no wire id, so there is nothing to
        tell those apart — and the vault must keep the user's real behaviour.
        """
        from thinkweave.surfaces.hooks import handler as handler_mod

        cfg = self._cfg(tmp_path, monkeypatch)
        handler_mod._handle_user_prompt_submit(_cc_prompt("continue"))
        handler_mod._handle_user_prompt_submit(_cc_prompt("continue"))
        capsys.readouterr()

        prompts = [e for e in self._buffered(cfg) if e.get("type") == "prompt"]
        assert [e["text"] for e in prompts] == ["continue", "continue"]

    def test_resume_and_compact_still_inject_context(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """The blocker: SessionStart dedup made resume a context-less session.

        Receipts are never reopened, so a session id whose ``startup`` was
        already recorded would have gone permanently un-oriented on every
        later re-entry.
        """
        from thinkweave.surfaces.hooks import handler as handler_mod

        cfg = self._cfg(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context",
            lambda *a, **k: "## Recent\n- [[sessions/s|ses-1]]\n",
        )

        served = []
        for source in ("startup", "resume", "compact"):
            handler_mod._handle_session_start(_cc_session_start(source))
            emitted = json.loads(capsys.readouterr().out or "{}")
            served.append(
                emitted.get("hookSpecificOutput", {}).get("additionalContext", "")
            )

        assert all("ses-1" in ctx for ctx in served), (
            "a re-entered session was left without project context"
        )
        starts = [e for e in self._buffered(cfg) if e.get("type") == "startup"]
        assert len(starts) == 3

    def test_receipts_are_cleared_with_the_buffer(self, tmp_path: Path):
        from thinkweave.core.buffer import session_state_dir

        weave_dir = tmp_path / ".weave"
        _buffer_event(
            weave_dir,
            "ses-arch",
            {"type": "prompt", "text": "hi", "delivery_id": "user:ses-arch:t1"},
        )
        assert any(session_state_dir(weave_dir, "ses-arch").iterdir())

        archive_buffer(weave_dir, "ses-arch", tmp_path / "session")
        assert not session_state_dir(weave_dir, "ses-arch").exists()

    def test_receipts_are_cleared_by_cleanup(self, tmp_path: Path):
        from thinkweave.core.buffer import session_state_dir

        weave_dir = tmp_path / ".weave"
        _buffer_event(
            weave_dir,
            "ses-clean",
            {"type": "prompt", "text": "hi", "delivery_id": "user:ses-clean:t1"},
        )
        cleanup_buffer(weave_dir, "ses-clean")
        assert not session_state_dir(weave_dir, "ses-clean").exists()


class TestFailureDiagnosticRateLimit:
    """A persistent vault fault must not narrate itself on every delivery."""

    def test_repeat_failures_message_once_per_session(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        from thinkweave.surfaces.hooks import handler as handler_mod

        cfg = Config(vault_root=tmp_path / "vault")
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        monkeypatch.setattr(
            handler_mod,
            "_buffer_event",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")),
        )

        messages = []
        for _ in range(3):
            handler_mod._handle_user_prompt_submit(_cc_prompt("do the thing"))
            emitted = json.loads(capsys.readouterr().out or "{}")
            messages.append(emitted.get("systemMessage", ""))

        assert "not persisted" in messages[0]
        assert messages[1:] == ["", ""], (
            "every failed delivery re-announced the same vault problem"
        )
        # Still fully recorded where diagnosis actually happens.
        log = (cfg.weave_dir / "hooks.log").read_text(encoding="utf-8")
        assert log.count("PermissionError") >= 3

    def test_a_different_hook_type_still_reports(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        from thinkweave.surfaces.hooks import handler as handler_mod

        cfg = Config(vault_root=tmp_path / "vault")
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        monkeypatch.setattr(
            handler_mod,
            "_buffer_event",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")),
        )
        monkeypatch.setattr(
            handler_mod, "_ensure_session", lambda *a, **k: None
        )

        handler_mod._handle_user_prompt_submit(_cc_prompt("do the thing"))
        capsys.readouterr()
        handler_mod._handle_post("Write", _cc_write("toolu_01CCCCCCCCCCCCCCCCCCCCCC"))

        emitted = json.loads(capsys.readouterr().out or "{}")
        assert "not persisted" in emitted.get("systemMessage", "")


class TestLogicalSessionKey:
    """#180 — the cross-segment chain key.

    claude.ai/code splits one logical session across several harness
    session UUIDs (one per context compaction). The only durable link the
    harness provides (#180 investigation, 2026-08-30) is the
    ``bridge-session`` transcript row near the top of EVERY segment's
    transcript: its ``bridgeSessionId`` (``cse_…``) is the cloud session id
    all segments share. The hooks lift it into each segment note's
    frontmatter as ``logical_session`` so the wrap verdict join can
    resolve the chain.
    """

    def _transcript(self, tmp_path: Path, rows: list[dict]) -> Path:
        f = tmp_path / "transcript.jsonl"
        f.write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
        return f

    def test_key_read_from_bridge_session_row(self, tmp_path: Path):
        from thinkweave.surfaces.hooks.handler import _logical_session_key

        t = self._transcript(tmp_path, [
            {"type": "mode", "sessionId": "seg-1"},
            {"type": "bridge-session", "sessionId": "seg-1",
             "bridgeSessionId": "cse_ABC123"},
        ])
        assert (
            _logical_session_key({"transcript_path": str(t)}) == "cse_ABC123"
        )

    def test_no_bridge_row_or_transcript_is_empty(self, tmp_path: Path):
        # Interactive CLI sessions, Codex, and missing/garbled transcripts
        # all degrade to "" — the stamp is best-effort, never blocking.
        from thinkweave.surfaces.hooks.handler import _logical_session_key

        t = self._transcript(
            tmp_path, [{"type": "mode", "sessionId": "seg-1"}]
        )
        (tmp_path / "garbled.jsonl").write_text(
            "not json\n", encoding="utf-8"
        )
        assert _logical_session_key({"transcript_path": str(t)}) == ""
        assert _logical_session_key(
            {"transcript_path": str(tmp_path / "missing.jsonl")}
        ) == ""
        assert _logical_session_key(
            {"transcript_path": str(tmp_path / "garbled.jsonl")}
        ) == ""
        assert _logical_session_key({}) == ""

    def test_ensure_session_stamps_logical_session(
        self, tmp_path: Path, monkeypatch
    ):
        from thinkweave.core.vault import VaultManager
        from thinkweave.surfaces.hooks import handler as handler_mod

        cfg = Config(vault_root=tmp_path / "vault")
        monkeypatch.setenv("THINKWEAVE_PROJECT", "t")
        t = self._transcript(tmp_path, [
            {"type": "bridge-session", "sessionId": "seg-uuid-1",
             "bridgeSessionId": "cse_stamp1"},
        ])
        handler_mod._ensure_session(cfg, "seg-uuid-1", {
            "session_id": "seg-uuid-1",
            "transcript_path": str(t),
            "cwd": str(tmp_path),
        })
        vm = VaultManager(config=cfg)
        path = handler_mod._find_session_note(vm, "seg-uuid-1")
        assert path is not None
        note = vm.read_note(path)
        assert note.frontmatter["logical_session"] == "cse_stamp1"
