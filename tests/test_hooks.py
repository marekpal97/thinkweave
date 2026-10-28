"""Tests for Claude Code hook handler and installer."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import write_transcript
from thinkweave.core.config import Config, detect_project
# The auto-summary shape has one producer (core.events, via
# extract_deterministic); the Stop hook's duplicate of it is gone.
from thinkweave.core.events import _build_summary as _build_auto_summary
from thinkweave.surfaces.hooks.handler import (
    _buffer_event,
    _build_event,
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

    def test_chained_commit_is_significant(self):
        # The head segment is `git add`; the commit rides the chain.
        assert _is_significant_command('git add calc.py && git commit -m "x"')
        assert _is_significant_command("cd repo; uv run pytest -q")
        assert not _is_significant_command("ls && cat file.txt")

    def test_detect_project(self, tmp_path: Path):
        # Env var takes priority
        with patch.dict("os.environ", {"THINKWEAVE_PROJECT": "from-env"}):
            assert detect_project("/anywhere") == "from_env"  # normalized like every vault write

        # Git repo detection: walk up to .git
        repo = tmp_path / "my_project" / "src" / "pkg"
        repo.mkdir(parents=True)
        (tmp_path / "my_project" / ".git").mkdir()
        assert detect_project(str(repo)) == "my_project"

        # Fallback to cwd directory name
        no_git = tmp_path / "random_dir"
        no_git.mkdir()
        assert detect_project(str(no_git)) == "random_dir"

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
        assert "Skill|Write|Edit|Bash" in matchers
        assert "mcp__(plugin_thinkweave_)?thinkweave__.*" in matchers
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
        assert weave_matchers == {"Skill|Write|Edit|Bash", "mcp__(plugin_thinkweave_)?thinkweave__.*"}

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
                        "matcher": "Skill|Write|Edit|Bash",
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
        action_entry = next(e for e in post_entries if e["matcher"] == "Skill|Write|Edit|Bash")
        mcp_entry = next(e for e in post_entries if e["matcher"] == "mcp__(plugin_thinkweave_)?thinkweave__.*")
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
        assert post_matchers == {"Skill|Write|Edit|Bash", "mcp__(plugin_thinkweave_)?thinkweave__.*"}
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
        ``Skill|Write|Edit|Bash``. Without a second matcher targeting
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
            (e for e in post_entries if e["matcher"] == "mcp__(plugin_thinkweave_)?thinkweave__.*"),
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
            if e["matcher"] == "mcp__(plugin_thinkweave_)?thinkweave__.*"
        ]
        assert len(mcp_entries) == 1

    def test_reinstall_migrates_the_previous_mcp_matcher(self, tmp_path: Path):
        """An install written before the plugin-route matcher is rewritten in
        place (matcher snapped), not joined by a second MCP entry."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))
        path = project_dir / ".claude" / "settings.local.json"
        settings = json.loads(path.read_text())
        for e in settings["hooks"]["PostToolUse"]:
            if e["matcher"].startswith("mcp__"):
                e["matcher"] = "mcp__thinkweave__.*"
        path.write_text(json.dumps(settings))
        install_hooks(project_dir=str(project_dir))
        matchers = [e["matcher"] for e in json.loads(path.read_text())["hooks"]["PostToolUse"]]
        assert matchers.count("mcp__(plugin_thinkweave_)?thinkweave__.*") == 1
        assert "mcp__thinkweave__.*" not in matchers

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
    def test_a_skill_tool_call_is_one_skill_row(self):
        event = _build_event("Skill", {"skill": "wrap", "args": "--force"}, "", "14:00")
        assert event == {"ts": "14:00", "tool": "Skill", "skill": "wrap", "declared": "tool"}
        assert _build_event("Skill", {"args": "x"}, "", "14:00") is None

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
        assert _is_git_commit("git add -A && git commit -m 'fix'")
        assert not _is_git_commit("git commit --amend")
        assert not _is_git_commit("git log")
        assert not _is_git_commit("echo git commit")
        assert _is_git_commit("git add a.py && git commit -m 'fix' && git status")
        assert not _is_git_commit("git add a.py && git commit --amend")

    def test_is_git_commit_respects_quoting(self):
        assert _is_git_commit("cd repo; git commit")
        assert _is_git_commit('git commit -m "a; b && c"')
        assert not _is_git_commit('echo "git commit"')
        assert not _is_git_commit('echo "a && git commit"')
        assert not _is_git_commit("echo 'a; git commit'")

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
        assert not _is_test_command('echo "done && pytest"')
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
            "thinkweave.surfaces.hooks.handler.detect_project",
            lambda cwd: project,
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
        assert "## Available MCP Tools" not in ctx
        assert "## Retrieval Hints" not in ctx

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
        # The prompt, then the manifest the first prompt stamps.
        assert len(lines) == 2
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

    def _run_prompt(self, cfg, monkeypatch, session_id: str, prompt: str, cwd: str = "/p"):
        """Drive _handle_user_prompt_submit with session-note creation stubbed."""
        from thinkweave.surfaces.hooks import handler as handler_mod

        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        monkeypatch.setattr(
            "thinkweave.surfaces.hooks.handler._ensure_session",
            lambda *a, **k: None,
        )
        handler_mod._handle_user_prompt_submit(
            {"session_id": session_id, "prompt": prompt, "cwd": cwd}
        )
        buf_file = cfg.weave_dir / "buffer" / f"{session_id}.jsonl"
        if not buf_file.exists():
            return []
        return [json.loads(ln) for ln in buf_file.read_text().splitlines() if ln.strip()]

    def test_harness_injected_blocks_are_not_user_prompts(self, tmp_path: Path, monkeypatch):
        from thinkweave.core.config import Config

        cfg = Config(vault_root=tmp_path / "vault")
        notice = ("<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n"
                  "</task-notification>")
        assert self._run_prompt(cfg, monkeypatch, "ses-inj-1", notice) == []
        assert self._run_prompt(
            cfg, monkeypatch, "ses-inj-2",
            f"<system-reminder>\nnote\n</system-reminder>\n{notice}\n",
        ) == []
        typed = self._run_prompt(cfg, monkeypatch, "ses-inj-3", f"{notice}\nwhy did it fail?")
        assert [r["type"] for r in typed] == ["prompt", "manifest"]

    def test_the_first_prompt_records_the_session_s_base_sha(
        self, tmp_path: Path, monkeypatch
    ):
        """The session's base commit: the repo HEAD its first prompt saw,
        recorded once per buffer. Every later prompt already has one."""
        from thinkweave.core.config import Config
        from tests.conftest import run_git, scratch_repo

        repo = scratch_repo(tmp_path, monkeypatch)
        head = run_git("rev-parse", "HEAD", cwd=repo)
        cfg = Config(vault_root=tmp_path / "vault")
        rows = self._run_prompt(
            cfg, monkeypatch, "ses-base-1", "do the thing", cwd=str(repo)
        )
        assert [r["type"] for r in rows] == ["prompt", "base", "manifest"]
        assert rows[1]["hash"] == head
        assert rows[1]["session_id"] == "ses-base-1"
        assert rows[1]["ts"] == rows[0]["ts"]

        again = self._run_prompt(
            cfg, monkeypatch, "ses-base-1", "and now?", cwd=str(repo)
        )
        assert [r["type"] for r in again if r["type"] == "base"] == ["base"]
        assert [r["type"] for r in again] == ["prompt", "base", "manifest", "prompt"]

    def test_a_session_outside_any_repo_records_no_base_sha(
        self, tmp_path: Path, monkeypatch
    ):
        """No repo at the prompt's cwd: nothing is recorded, and the prompt
        still lands — a round's ``base`` is optional."""
        from thinkweave.core.config import Config

        plain = tmp_path / "plain"
        plain.mkdir()
        cfg = Config(vault_root=tmp_path / "vault")
        rows = self._run_prompt(
            cfg, monkeypatch, "ses-base-2", "do the thing", cwd=str(plain)
        )
        assert [r["type"] for r in rows] == ["prompt", "manifest"]

    def test_the_first_prompt_stamps_the_manifest_beside_the_base(
        self, tmp_path: Path, monkeypatch
    ):
        """The policy the session runs under: the rsi-rail commit and dirty
        flag, the firing harness, the reader versions — once per buffer."""
        from thinkweave.core.config import Config
        from tests.conftest import init_repo, run_git, scratch_repo

        repo = scratch_repo(tmp_path, monkeypatch)
        rail = init_repo(tmp_path / "rail")
        cfg = Config(vault_root=tmp_path / "vault", rsi_rail=rail)
        rows = self._run_prompt(cfg, monkeypatch, "ses-man-1", "go", cwd=str(repo))
        stamp = rows[2]
        assert stamp["type"] == "manifest" and stamp["ts"] == rows[0]["ts"]
        assert stamp["manifest"]["harness"] == "claude-code"
        assert stamp["manifest"]["rsi_rail"] == {
            "repo": str(rail), "commit": run_git("rev-parse", "HEAD", cwd=rail), "dirty": False,
        }
        assert stamp["manifest"]["readers"]["thinkweave"]
        again = self._run_prompt(cfg, monkeypatch, "ses-man-1", "more", cwd=str(repo))
        assert [r["type"] for r in again].count("manifest") == 1

    def test_a_leading_skill_token_records_a_prompt_declared_skill_row(
        self, tmp_path: Path, monkeypatch
    ):
        """What the user invoked by its slash form, in the Skill tool's row
        shape, tagged as declared on the prompt."""
        from thinkweave.core.config import Config

        cfg = Config(vault_root=tmp_path / "vault")
        rows = self._run_prompt(cfg, monkeypatch, "ses-sk-1", "/wrap")
        assert [r["type"] for r in rows if "type" in r] == ["prompt", "manifest"]
        (skill,) = [r for r in rows if r.get("tool") == "Skill"]
        assert skill == {
            "ts": rows[0]["ts"], "tool": "Skill", "skill": "wrap",
            "declared": "prompt", "harness": "claude-code",
        }
        rows = self._run_prompt(cfg, monkeypatch, "ses-sk-2", "/drain --source-type news")
        assert [r["skill"] for r in rows if r.get("tool") == "Skill"] == ["drain"]
        rows = self._run_prompt(cfg, monkeypatch, "ses-sk-3", "/thinkweave:wrap now")
        assert [r["skill"] for r in rows if r.get("tool") == "Skill"] == ["thinkweave:wrap"]
        rows = self._run_prompt(cfg, monkeypatch, "ses-sk-4", "look under /etc/hosts")
        assert [r for r in rows if r.get("tool") == "Skill"] == []

    def test_a_skill_mention_follows_the_firing_harness_s_prefix(
        self, tmp_path: Path, monkeypatch
    ):
        from thinkweave.core.config import Config

        cfg = Config(vault_root=tmp_path / "vault")
        monkeypatch.setattr("sys.argv", ["weave-hook", "user_prompt_submit", "--harness", "pi"])
        rows = self._run_prompt(cfg, monkeypatch, "ses-pi-1", "/skill:wrap")
        assert [(r["skill"], r["harness"]) for r in rows if r.get("tool") == "Skill"] == [("wrap", "pi")]
        rows = self._run_prompt(cfg, monkeypatch, "ses-pi-2", "/wrap")
        assert [r for r in rows if r.get("tool") == "Skill"] == []
        monkeypatch.setattr("sys.argv", ["weave-hook", "user_prompt_submit", "--harness", "codex"])
        rows = self._run_prompt(cfg, monkeypatch, "ses-cx-1", "$thinkweave-wrap")
        assert [(r["skill"], r["harness"]) for r in rows if r.get("tool") == "Skill"] == [
            ("thinkweave-wrap", "codex")
        ]
        rows = self._run_prompt(cfg, monkeypatch, "ses-cx-2", "/wrap")
        assert [r for r in rows if r.get("tool") == "Skill"] == []

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
        # The prompt is captured, and with it the base commit and manifest
        # the first prompt saw; the disabled enrichment writes nothing back.
        assert [r["type"] for r in lines] == ["prompt", "base", "manifest"]

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

    def test_resume_and_compact_skip_by_policy_not_by_accident(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """Serve-once (#175 rework, PR #203 review): the initial context is
        served exactly once — on ``startup`` — and a re-entered session gets
        NOTHING, by policy: resume replays the transcript, compaction is
        followed by on-demand ``weave_*`` retrieval. The distinction from
        #161's silent-dedup blocker is that every skipped delivery leaves a
        visible ``skipped_lifecycle`` telemetry event, not a silent hole.
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

        assert "ses-1" in served[0]
        assert served[1] == "" and served[2] == ""
        starts = [e for e in self._buffered(cfg) if e.get("type") == "startup"]
        assert [e.get("disposition") for e in starts] == [
            "served_full", "skipped_lifecycle", "skipped_lifecycle",
        ]

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
        return write_transcript(tmp_path, rows)

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

    def test_bridge_row_near_transcript_end_is_found(self, tmp_path: Path):
        # Fix round 2 minor: ~18% of real bridged transcripts carry the
        # bridge row only near the END (measured ~1MB in) — the scan reads
        # a bounded tail chunk as well as the head.
        from thinkweave.surfaces.hooks.handler import _logical_session_key

        f = tmp_path / "transcript.jsonl"
        filler = json.dumps({"type": "user", "text": "x" * 200}) + "\n"
        f.write_text(
            filler * 500
            + json.dumps({
                "type": "bridge-session", "bridgeSessionId": "cse_TAIL",
            })
            + "\n",
            encoding="utf-8",
        )
        assert f.stat().st_size > 65536
        assert _logical_session_key({"transcript_path": str(f)}) == "cse_TAIL"

    def test_stop_backfills_missing_logical_session(
        self, tmp_path: Path, monkeypatch
    ):
        # Fix round 2 minor: the creation-time stamp misses a bridge row
        # that only exists later in the transcript — the Stop hook (which
        # rewrites the note frontmatter anyway) backfills it.
        from thinkweave.core.schemas import NoteType
        from thinkweave.core.vault import VaultManager
        from thinkweave.surfaces.hooks import handler as handler_mod

        cfg = Config(vault_root=tmp_path / "vault")
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        monkeypatch.setenv("THINKWEAVE_PROJECT", "t")

        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        vm.create_note(
            NoteType.SESSION, "S", project="t",
            extra_frontmatter={"source_session": "seg-uuid-late"},
        )
        buf_dir = cfg.weave_dir / "buffer"
        buf_dir.mkdir(parents=True, exist_ok=True)
        (buf_dir / "seg-uuid-late.jsonl").write_text(
            json.dumps({
                "ts": "2026-08-30T10:00:00+00:00", "type": "prompt",
                "text": "hello", "session_id": "seg-uuid-late",
            }) + "\n",
            encoding="utf-8",
        )
        t = self._transcript(tmp_path, [
            {"type": "bridge-session", "bridgeSessionId": "cse_LATE"},
        ])
        handler_mod._handle_stop({
            "session_id": "seg-uuid-late", "transcript_path": str(t),
            "cwd": str(tmp_path),
        })
        path = handler_mod._find_session_note(vm, "seg-uuid-late")
        assert path is not None
        note = vm.read_note(path)
        assert note.frontmatter.get("logical_session") == "cse_LATE"
        assert note.frontmatter.get("processed") is True

    def test_invalid_utf8_transcript_never_escapes(
        self, tmp_path: Path, monkeypatch
    ):
        # Fix round 1 blocker: invalid UTF-8 in the transcript raised
        # UnicodeDecodeError (a ValueError, not OSError) out of the head
        # read, unwinding through _ensure_session into the prompt/post
        # handlers — no session note, failure banner on every prompt.
        from thinkweave.core.vault import VaultManager
        from thinkweave.surfaces.hooks import handler as handler_mod

        t = tmp_path / "transcript.jsonl"
        t.write_bytes(
            b'\xff\xfe{"type":"bridge-session","bridgeSessionId":"cse_X"}\n'
        )
        assert handler_mod._logical_session_key(
            {"transcript_path": str(t)}
        ) == ""

        cfg = Config(vault_root=tmp_path / "vault")
        monkeypatch.setenv("THINKWEAVE_PROJECT", "t")
        handler_mod._ensure_session(cfg, "seg-uuid-bad", {
            "session_id": "seg-uuid-bad",
            "transcript_path": str(t),
            "cwd": str(tmp_path),
        })
        vm = VaultManager(config=cfg)
        assert handler_mod._find_session_note(vm, "seg-uuid-bad") is not None

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


TRANSCRIPTS = Path(__file__).parent / "fixtures" / "harness_transcripts"

# One recorded session per harness: its transcript fixture, the model its hook
# envelopes report (the Codex and Pi transcripts name it too; the Claude Code
# one names none), and the argv flag `weave hooks install` writes for it.
HARNESS_SESSIONS = {
    "claude-code": (
        TRANSCRIPTS / "claude-code" / "2f6b9c1e-0000-4000-8000-000000000001.jsonl",
        "claude-opus-4-7",
        [],
    ),
    "codex": (
        TRANSCRIPTS
        / "codex"
        / "rollout-2026-10-03T10-00-00-01a10380-0000-7000-8000-000000000002.jsonl",
        "gpt-5.6-terra",
        ["--harness", "codex"],
    ),
    "pi": (
        TRANSCRIPTS / "pi" / "2026-10-04T15-46-02-748Z_0199cccc-0000-7000-8000-000000000002.jsonl",
        "moonshotai/kimi-k2.6",
        ["--harness", "pi"],
    ),
}


class TestSessionHarnessAndModel:
    """Session notes and buffered events record the harness and model.

    Envelopes follow each harness's wire: Claude Code reports ``model`` only
    on SessionStart, Codex on every hook, and the Pi shim on every call.
    """

    @pytest.fixture
    def vault(self, tmp_path: Path, monkeypatch):
        cfg = Config(vault_root=tmp_path / "vault")
        (cfg.vault_root / "config").mkdir(parents=True)
        (cfg.vault_root / "config" / "sources.yaml").write_text("{}\n")
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        monkeypatch.setenv("THINKWEAVE_PROJECT", "proj")
        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context",
            lambda *a, **k: "",
        )
        return cfg

    @staticmethod
    def _fire(monkeypatch, capsys, harness_id: str, hook: str, envelope: dict):
        from thinkweave.surfaces.hooks import handler as handler_mod

        flags = HARNESS_SESSIONS[harness_id][2]
        monkeypatch.setattr("sys.argv", ["weave-hook", hook, *flags])
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(envelope)))
        handler_mod.main()
        out = json.loads(capsys.readouterr().out or "{}")
        assert "systemMessage" not in out, out

    @staticmethod
    def _envelope(harness_id: str, session_id: str, event: str, **extra) -> dict:
        transcript, model, _ = HARNESS_SESSIONS[harness_id]
        env = {
            "session_id": session_id,
            "transcript_path": str(transcript),
            "cwd": "/home/u/proj",
            "hook_event_name": event,
            **extra,
        }
        if harness_id != "claude-code" or event == "SessionStart":
            env.setdefault("model", model)
        return env

    def _start_and_prompt(self, monkeypatch, capsys, harness_id, session_id):
        self._fire(
            monkeypatch, capsys, harness_id, "session_start",
            self._envelope(harness_id, session_id, "SessionStart", source="startup"),
        )
        self._fire(
            monkeypatch, capsys, harness_id, "user_prompt_submit",
            self._envelope(harness_id, session_id, "UserPromptSubmit", prompt="go"),
        )

    @pytest.mark.parametrize("harness_id", sorted(HARNESS_SESSIONS))
    def test_session_note_records_harness_and_model(
        self, vault, monkeypatch, capsys, harness_id
    ):
        from thinkweave.core.vault import VaultManager
        from thinkweave.surfaces.hooks import handler as handler_mod

        session_id = f"ses-{harness_id}"
        self._start_and_prompt(monkeypatch, capsys, harness_id, session_id)

        vm = VaultManager(config=vault)
        path = handler_mod._find_session_note(vm, session_id)
        assert path is not None
        fm = vm.read_note(path).frontmatter
        assert fm["harness"] == harness_id
        assert fm["model"] == HARNESS_SESSIONS[harness_id][1]

    @pytest.mark.parametrize("harness_id", sorted(HARNESS_SESSIONS))
    def test_every_buffered_event_carries_the_harness(
        self, vault, monkeypatch, capsys, harness_id
    ):
        session_id = f"ses-ev-{harness_id}"
        self._start_and_prompt(monkeypatch, capsys, harness_id, session_id)
        self._fire(
            monkeypatch, capsys, harness_id, "post_tool_use",
            self._envelope(
                harness_id, session_id, "PostToolUse",
                tool_name="Write", tool_use_id="call-w1",
                tool_input={"file_path": "/home/u/proj/a.py", "content": "x = 1\n"},
                tool_response={"success": True},
            ),
        )
        self._fire(
            monkeypatch, capsys, harness_id, "post_tool_use",
            self._envelope(
                harness_id, session_id, "PostToolUse",
                tool_name="mcp__thinkweave__weave_search", tool_use_id="call-r1",
                tool_input={"query": "x"}, tool_response="no results",
            ),
        )

        events = _read_buffer(vault.weave_dir, session_id)
        assert {e.get("type", e.get("tool")) for e in events} >= {
            "startup", "prompt", "Write", "retrieval",
        }
        assert {e["harness"] for e in events} == {harness_id}

    def test_model_change_is_recorded_as_an_event(self, vault, monkeypatch, capsys):
        session_id = "ses-pi-switch"
        self._start_and_prompt(monkeypatch, capsys, "pi", session_id)
        # The first Stop archives the buffer; the switch lands a turn later.
        self._fire(
            monkeypatch, capsys, "pi", "stop",
            self._envelope("pi", session_id, "Stop"),
        )
        self._fire(
            monkeypatch, capsys, "pi", "user_prompt_submit",
            self._envelope(
                "pi", session_id, "UserPromptSubmit",
                prompt="again", model="deepseek/deepseek-v4",
            ),
        )

        changes = [
            e for e in _read_buffer(vault.weave_dir, session_id)
            if e.get("type") == "model_change"
        ]
        assert [(e["from"], e["to"], e["harness"]) for e in changes] == [
            ("moonshotai/kimi-k2.6", "deepseek/deepseek-v4", "pi"),
        ]

    def test_unchanged_model_records_no_change(self, vault, monkeypatch, capsys):
        session_id = "ses-codex-steady"
        self._start_and_prompt(monkeypatch, capsys, "codex", session_id)
        self._fire(
            monkeypatch, capsys, "codex", "user_prompt_submit",
            self._envelope("codex", session_id, "UserPromptSubmit", prompt="more"),
        )
        assert not [
            e for e in _read_buffer(vault.weave_dir, session_id)
            if e.get("type") == "model_change"
        ]

    def test_model_tracking_failure_is_surfaced_and_capture_continues(
        self, vault, monkeypatch, capsys
    ):
        from thinkweave.surfaces.hooks import handler as handler_mod

        def broken(*a, **k):
            raise OSError("model store unwritable")

        monkeypatch.setattr(handler_mod, "record_model", broken)
        session_id = "ses-codex-broken"
        monkeypatch.setattr("sys.argv", ["weave-hook", "user_prompt_submit", "--harness", "codex"])
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(json.dumps(
                self._envelope("codex", session_id, "UserPromptSubmit", prompt="go")
            )),
        )
        handler_mod.main()

        out = json.loads(capsys.readouterr().out)
        assert "model store unwritable" in out["systemMessage"]
        assert [e["type"] for e in _read_buffer(vault.weave_dir, session_id)] == ["prompt", "manifest"]


class TestCaptureGaps:
    """Commit, `gh pr` and plugin-route retrieval capture (2026-10-12)."""

    def test_push_line_is_not_a_commit_message(self):
        from thinkweave.operations.hook_events import parse_commit_from_output

        out = " * [new branch]      feat/x -> feat/x\n"
        assert parse_commit_from_output("git commit -q -F - && git push", out) is None

    def test_quiet_commit_falls_back_to_fresh_head(self, tmp_path: Path, monkeypatch):
        import subprocess

        from thinkweave.surfaces.hooks.handler import _build_events

        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run([*git, "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "root"], check=True)
        (tmp_path / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "a.py"], check=True)
        subprocess.run([*git, "-C", str(tmp_path), "commit", "-q", "-m", "quiet one"], check=True)
        monkeypatch.chdir(tmp_path)
        [event] = _build_events("Bash", {"command": "git commit -q -F msg.txt"}, "", "now")
        assert event["commit"]["message"] == "quiet one"
        assert len(event["commit"]["hash"]) >= 7
        assert event["commit"]["files"] == ["a.py"]

    def test_stale_head_is_not_claimed(self, tmp_path: Path, monkeypatch):
        from thinkweave.surfaces.hooks import handler

        monkeypatch.chdir(tmp_path)  # not a repo: no HEAD at all
        assert handler._fresh_head_commit() == {}

    def test_gh_pr_create_and_merge(self):
        from thinkweave.surfaces.hooks.handler import _is_significant_command, _parse_gh_pr

        url = "https://github.com/o/r/pull/303"
        assert _parse_gh_pr("gh pr create --title x --body-file -", url + "\n") == {
            "action": "create", "url": url, "number": 303,
        }
        assert _parse_gh_pr("cd repo && gh pr merge 303 --merge", "") == {
            "action": "merge", "number": 303,
        }
        assert _parse_gh_pr("gh pr view 303", "") is None
        assert _is_significant_command("gh pr merge 303 --merge")
        assert not _is_significant_command("gh pr checks 303")

    def test_pull_requests_reach_the_extract_result(self):
        from thinkweave.core.events import extract_deterministic

        result = extract_deterministic([
            {"tool": "Bash", "command": "gh pr create", "pull_request": {"action": "create", "number": 7}},
        ])
        assert result.pull_requests == [{"action": "create", "number": 7}]

    def test_plugin_route_tool_name_counts_as_retrieval(self, monkeypatch):
        from thinkweave.surfaces.hooks import handler

        seen = {}
        monkeypatch.setattr(handler, "_handle_post", lambda name, hi: seen.update(name=name))
        monkeypatch.setattr(handler, "_track_model", lambda hi: None)
        monkeypatch.setattr(handler, "_read_stdin", lambda: {
            "tool_name": "mcp__plugin_thinkweave_thinkweave__weave_search", "session_id": "s",
        })
        monkeypatch.setattr("sys.argv", ["handler", "post_tool_use"])
        monkeypatch.setattr("thinkweave.core.config.is_vault_initialized", lambda cfg: True)
        handler.main()
        assert seen["name"] == "mcp__thinkweave__weave_search"


def _hold_index_write_lock(db_path: Path, ready, release, seconds=None) -> None:
    """Take the index db's write lock.

    It is handed back after ``seconds`` when given, otherwise when ``release``
    is set.
    """
    import sqlite3
    import time

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("BEGIN IMMEDIATE")
    ready.set()
    if seconds is None:
        release.wait(30)
    else:
        time.sleep(seconds)
    conn.execute("ROLLBACK")
    conn.close()


class TestIndexWriteLockRetry:
    """The hook's index write retries a locked db instead of losing the note.

    Two harness sessions writing one vault used to fail the index step with
    ``sqlite3.OperationalError: database is locked``: the session note reached
    disk but never entered the index until a manual ``weave index --only-new``.
    """

    @staticmethod
    def _warmed_vault(tmp_path: Path, monkeypatch):
        """A vault whose index db exists before anyone takes a lock."""
        from thinkweave.core.indexer import Indexer
        from thinkweave.core.vault import VaultManager

        cfg = Config(vault_root=tmp_path / "vault")
        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        monkeypatch.setenv("THINKWEAVE_PROJECT", "t")
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        idx = Indexer(config=cfg)
        idx.rebuild()
        idx.close()
        return cfg, vm

    @staticmethod
    def _indexed_ids(cfg: Config, note_id: str) -> int:
        from thinkweave.core.indexer import Indexer

        idx = Indexer(config=cfg)
        try:
            return idx.db.execute(
                "SELECT COUNT(*) FROM notes WHERE id = ?", (note_id,)
            ).fetchone()[0]
        finally:
            idx.close()

    def test_index_retry_clears_a_one_second_write_lock(
        self, tmp_path: Path, monkeypatch
    ):
        import threading

        from thinkweave.surfaces.hooks import handler as handler_mod

        cfg, vm = self._warmed_vault(tmp_path, monkeypatch)
        ready, release = threading.Event(), threading.Event()
        holder = threading.Thread(
            target=_hold_index_write_lock,
            args=(cfg.index_db, ready, release, 1.0),
            daemon=True,
        )
        holder.start()
        assert ready.wait(10)
        try:
            handler_mod._ensure_session(
                cfg, "lock-seg-a", {"session_id": "lock-seg-a", "cwd": str(tmp_path)}
            )
        finally:
            release.set()
            holder.join(10)

        path = handler_mod._find_session_note(vm, "lock-seg-a")
        assert path is not None
        note_id = vm.read_note(path).frontmatter["id"]
        assert self._indexed_ids(cfg, note_id) == 1

        log_text = (cfg.weave_dir / "hooks.log").read_text(encoding="utf-8")
        assert "locked" in log_text.lower()
        assert "retrying" in log_text.lower()

    def test_index_retry_budget_leaves_the_note_on_disk(
        self, tmp_path: Path, monkeypatch
    ):
        import threading

        from thinkweave.surfaces.hooks import handler as handler_mod

        cfg, vm = self._warmed_vault(tmp_path, monkeypatch)
        # Scale the real ladder down; the attempt count and the per-attempt
        # wait stay production's.
        monkeypatch.setattr(
            handler_mod, "_INDEX_RETRY_DELAYS_S",
            tuple(d / 50 for d in handler_mod._INDEX_RETRY_DELAYS_S),
        )
        ready, release = threading.Event(), threading.Event()
        holder = threading.Thread(
            target=_hold_index_write_lock,
            args=(cfg.index_db, ready, release),
            daemon=True,
        )
        holder.start()
        assert ready.wait(10)
        try:
            handler_mod._ensure_session(
                cfg, "lock-seg-b", {"session_id": "lock-seg-b", "cwd": str(tmp_path)}
            )
        finally:
            release.set()
            holder.join(10)

        path = handler_mod._find_session_note(vm, "lock-seg-b")
        assert path is not None and path.exists()
        note_id = vm.read_note(path).frontmatter["id"]
        assert self._indexed_ids(cfg, note_id) == 0

        log_text = (cfg.weave_dir / "hooks.log").read_text(encoding="utf-8")
        assert "database is locked" in log_text

    def test_index_retry_does_not_swallow_an_ordinary_index_failure(
        self, tmp_path: Path, monkeypatch
    ):
        from thinkweave.core.indexer import Indexer
        from thinkweave.surfaces.hooks import handler as handler_mod

        cfg, _ = self._warmed_vault(tmp_path, monkeypatch)

        def boom(self, path):
            raise ValueError("not a lock")

        monkeypatch.setattr(Indexer, "index_file", boom)
        with pytest.raises(ValueError, match="not a lock"):
            handler_mod._index_with_retry(cfg, tmp_path / "note.md", "stop/index")



# ---------------------------------------------------------------------------
# UserPromptSubmit — the harness's hook budget bounds prompt-time retrieval
# ---------------------------------------------------------------------------


class TestUserPromptSubmitHookBudget:
    def _run(self, cfg, monkeypatch, harness_id: str, captured: dict):
        from thinkweave.surfaces.hooks import handler as handler_mod

        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
        monkeypatch.setattr(handler_mod, "_ensure_session", lambda *a, **k: None)
        monkeypatch.setattr(handler_mod, "_hook_harness", lambda: harness_id)
        monkeypatch.setattr(
            "thinkweave.operations.prompt_time_retrieval.build_enrichment",
            lambda cfg, sid, text, *, deadline=None: captured.__setitem__("deadline", deadline)
            or (None, [], False),
        )
        handler_mod._handle_user_prompt_submit(
            {"session_id": "ses-budget", "prompt": "please explain how the indexer resolves a session note", "cwd": "/p"}
        )

    def test_pi_budget_reaches_the_enrichment_less_the_reply_margin(self, tmp_path: Path, monkeypatch):
        from thinkweave.core.config import Config

        captured: dict = {}
        self._run(Config(vault_root=tmp_path / "vault"), monkeypatch, "pi", captured)
        # Pi's shim waits 2.5 s; the handler keeps 0.3 s back for the reply.
        assert 0 < captured["deadline"] <= 2.2

    def test_a_harness_without_a_budget_leaves_the_configured_deadline_alone(self, tmp_path: Path, monkeypatch):
        from thinkweave.core.config import Config

        captured: dict = {}
        self._run(Config(vault_root=tmp_path / "vault"), monkeypatch, "codex", captured)
        assert captured["deadline"] is None

    def test_remaining_budget_counts_from_the_handler_start(self, monkeypatch):
        import time

        from thinkweave.surfaces.hooks import handler as handler_mod

        monkeypatch.setattr(handler_mod, "_hook_harness", lambda: "pi")
        fresh = handler_mod._hook_budget_remaining("UserPromptSubmit", time.monotonic())
        assert 2.1 < fresh <= 2.2
        spent = handler_mod._hook_budget_remaining("UserPromptSubmit", time.monotonic() - 10)
        assert spent < 0
        assert handler_mod._hook_budget_remaining("SubagentStart", time.monotonic()) is None


# ---------------------------------------------------------------------------
# rsi-rail fan-out: skills and the rules file linked into every profile
# ---------------------------------------------------------------------------

# The link each registered profile should end up with, by the paths the
# harness docs declare (not derived from PROFILES).
_RAIL_SKILL_LINKS = (
    ".claude/skills/{name}",
    ".codex/skills/{name}",
    ".pi/agent/skills/{name}",
    ".config/opencode/commands/{name}",
)
_RAIL_INSTRUCTIONS = (
    ".claude/CLAUDE.md",
    ".codex/AGENTS.md",
    ".pi/agent/AGENTS.md",
    ".config/opencode/AGENTS.md",
)


class TestRailLinks:
    """``weave hooks install`` links the rsi-rail repo's ``skills/`` and
    ``claude/CLAUDE.md`` into every profile, idempotently."""

    def test_first_run_links_every_profile(self, rail_home, capsys):
        repo, home = rail_home

        install_hooks(scope="user")

        for name in ("tdd", "retro"):
            for rel in _RAIL_SKILL_LINKS:
                link = home / rel.format(name=name)
                assert link.is_symlink(), link
                assert link.resolve() == repo / "skills" / name
        for rel in _RAIL_INSTRUCTIONS:
            link = home / rel
            assert link.is_symlink(), link
            assert link.resolve() == repo / "claude" / "CLAUDE.md"
        # A file beside the skill folders is not a skill.
        assert not (home / ".claude" / "skills" / "README.md").exists()
        out = capsys.readouterr().out
        assert str(home / ".pi" / "agent" / "skills" / "retro") in out
        assert "12 changed" in out

    def test_second_run_changes_nothing(self, rail_home, capsys):
        repo, home = rail_home
        install_hooks(scope="user")
        before = {p: p.readlink() for p in home.rglob("*") if p.is_symlink()}
        capsys.readouterr()

        install_hooks(scope="user")

        assert {p: p.readlink() for p in home.rglob("*") if p.is_symlink()} == before
        out = capsys.readouterr().out
        assert "0 changed, 12 already current" in out

    def test_links_that_already_resolve_into_the_repo_are_kept(self, rail_home, capsys):
        """A hand-made link through an intermediate dir is already correct."""
        repo, home = rail_home
        shared = home / ".agents" / "skills"
        shared.parent.mkdir(parents=True)
        shared.symlink_to(repo / "skills")
        link = home / ".claude" / "skills" / "tdd"
        link.parent.mkdir(parents=True)
        link.symlink_to(shared / "tdd")

        install_hooks(scope="user")

        assert link.readlink() == shared / "tdd"
        assert "11 changed, 1 already current" in capsys.readouterr().out

    def test_rules_are_spliced_into_an_instructions_file_with_other_content(
        self, rail_home, capsys
    ):
        repo, home = rail_home
        agents = home / ".codex" / "AGENTS.md"
        agents.parent.mkdir(parents=True)
        own = "# mine\n\n<!-- thinkweave:start -->\nnudge\n<!-- thinkweave:end -->\n"
        agents.write_text(own, encoding="utf-8")

        install_hooks(scope="user")

        assert not agents.is_symlink()
        text = agents.read_text(encoding="utf-8")
        assert text.startswith(own)
        assert "<!-- rsi-rail:start -->" in text and "<!-- rsi-rail:end -->" in text
        assert "- rule one" in text
        assert str(repo / "claude" / "CLAUDE.md") in text
        assert "splice" in capsys.readouterr().out

        # Idempotent: a second run leaves the bytes alone.
        install_hooks(scope="user")
        assert agents.read_text(encoding="utf-8") == text
        assert "0 changed" in capsys.readouterr().out

        # The repo moved on: the block is refreshed in place, nothing else moves.
        (repo / "claude" / "CLAUDE.md").write_text("# Global rules\n\n- rule two\n")
        install_hooks(scope="user")
        text = agents.read_text(encoding="utf-8")
        assert text.startswith(own)
        assert "- rule two" in text and "- rule one" not in text
        assert text.count("<!-- rsi-rail:start -->") == 1

    def test_foreign_entries_are_left_alone_and_dangling_links_repointed(
        self, rail_home, capsys
    ):
        repo, home = rail_home
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        (skills / "tdd").mkdir()  # the user's own tdd skill
        elsewhere = home / "my-retro"
        elsewhere.mkdir()
        (skills / "retro").symlink_to(elsewhere)  # their own link, resolves
        dangling = home / ".codex" / "skills" / "tdd"
        dangling.parent.mkdir(parents=True)
        dangling.symlink_to(home / "gone")

        install_hooks(scope="user")

        assert (skills / "tdd").is_dir() and not (skills / "tdd").is_symlink()
        assert (skills / "retro").readlink() == elsewhere
        assert dangling.resolve() == repo / "skills" / "tdd"
        out = capsys.readouterr().out
        assert f"skipped {skills / 'tdd'}" in out
        assert f"skipped {skills / 'retro'}" in out
        assert f"relink {dangling}" in out

    def test_nudge_block_writer_keeps_the_rail_link(self, rail_home, capsys):
        """`weave install`'s block writer, run after the rail install, must
        not turn the instructions link into a copy: the rules file is the
        rail's and shared by every harness, so the per-harness block is
        refused there and the link survives."""
        from thinkweave.surfaces.cli import mcp_doctor
        from thinkweave.surfaces.cli.install import _install_claude_md_block

        repo, home = rail_home
        rules = repo / "claude" / "CLAUDE.md"
        rules_before = rules.read_bytes()
        install_hooks(scope="user")
        capsys.readouterr()

        _install_claude_md_block(yes=True)

        claude_md = home / ".claude" / "CLAUDE.md"
        assert claude_md.is_symlink() and claude_md.resolve() == rules
        assert rules.read_bytes() == rules_before
        out = capsys.readouterr().out
        assert str(rules) in out and "not written" in out
        assert mcp_doctor.check_rail_links().passed
        install_hooks(scope="user")
        assert "0 changed, 12 already current" in capsys.readouterr().out
        assert claude_md.read_text(encoding="utf-8").count("- rule one") == 1

    def test_dry_run_writes_nothing(self, rail_home, capsys):
        repo, home = rail_home

        install_hooks(scope="user", dry_run=True)

        assert not (home / ".codex").exists()
        assert not (home / ".claude" / "CLAUDE.md").exists()
        out = capsys.readouterr().out
        assert f"Would link {home / '.claude' / 'CLAUDE.md'}" in out

    def test_no_repo_configured_says_so(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.setattr(
            "thinkweave.core.config.load_config",
            lambda: Config(vault_root=tmp_path / "vault"),
        )

        install_hooks(scope="user")

        assert "No rsi-rail repo configured" in capsys.readouterr().out
        assert not (home / ".codex").exists()
