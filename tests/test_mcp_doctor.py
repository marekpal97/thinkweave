"""Tests for ``weave doctor --mcp`` (mcp_doctor module).

All tests monkeypatch the ``CLAUDE_JSON`` path to a tmp file so the
user's real ``~/.claude.json`` is never read or written.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from thinkweave.surfaces.cli import mcp_doctor as md


@pytest.fixture(autouse=True)
def _sandbox_home_plugin_dirs(tmp_path, use_profile):
    """Point the doctor's HOME-scoped plugin scan at empty dirs so it never
    reads the developer's real ~/.claude/plugins or ~/.claude/skills. Tests
    that want a plugin scope present re-point the profile themselves (their
    ``use_profile`` call runs after this fixture)."""
    use_profile(
        plugins_cache=tmp_path / "_home_plugins_cache",
        skills_dir=tmp_path / "_home_skills",
    )


# ---------- helpers ----------


def _write_claude_json(path: Path, entry: dict | None) -> None:
    body: dict = {"mcpServers": {}}
    if entry is not None:
        body["mcpServers"]["thinkweave"] = entry
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")


def _write_mcp_json(cwd: Path, entry: dict | None) -> None:
    body: dict = {"mcpServers": {}}
    if entry is not None:
        body["mcpServers"]["thinkweave"] = entry
    (cwd / ".mcp.json").write_text(json.dumps(body, indent=2), encoding="utf-8")


# The machine-scope shape `weave install` writes. Module execution, not the
# `weave-mcp` console script — a uv sync interrupted by a Windows file lock
# deletes the .exe shims before failing, which leaves a console-script entry
# permanently broken. `_key` must fingerprint this identically to the launcher.
CANONICAL_ENTRY = {
    "type": "stdio",
    "command": "uv",
    "args": [
        "run", "--project", ".", "--extra", "mcp",
        "python", "-m", "thinkweave.surfaces.mcp.server",
    ],
    "env": {},
}

# The portable-launcher shape the committed .mcp.json uses since #52.
LAUNCHER_ENTRY = {
    "type": "stdio",
    "command": "bin/weave-mcp-launch",
    "args": [],
    "env": {},
}


def _make_fake_launcher(root: Path) -> Path:
    """Executable stand-in for bin/weave-mcp-launch that exits 0 — the
    doctor treats a clean exit as a resolving launcher."""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "weave-mcp-launch"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    return fake


# ---------- scope-detection tests ----------


class TestRegistrationScopes:
    def test_empty_claude_json_reports_unregistered(self, tmp_path, use_profile):
        # No ~/.claude.json, no .mcp.json, no plugin manifests.
        use_profile(mcp_config=tmp_path / "claude.json")
        result = md.check_registration_scopes(tmp_path)
        assert not result.passed
        assert "not registered" in result.detail
        assert "weave install" in result.fix

    def test_machine_only_is_pass(self, tmp_path, use_profile):
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        use_profile(mcp_config=claude_json)
        result = md.check_registration_scopes(tmp_path)
        assert result.passed
        assert "1 scope" in result.detail

    def test_machine_plus_project_identical_is_pass(self, tmp_path, use_profile):
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        _write_mcp_json(tmp_path, CANONICAL_ENTRY)
        use_profile(mcp_config=claude_json)
        result = md.check_registration_scopes(tmp_path)
        assert result.passed, result.detail
        assert "identically" in result.detail

    def test_machine_plus_project_with_divergent_invocations_is_fail(
        self, tmp_path, use_profile):
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        divergent = {
            "type": "stdio",
            "command": "weave-mcp",  # bare console-script — the legacy bug
            "args": [],
            "env": {},
        }
        _write_mcp_json(tmp_path, divergent)
        use_profile(mcp_config=claude_json)
        result = md.check_registration_scopes(tmp_path)
        assert not result.passed
        assert "DIFFERENT invocations" in result.detail

    def test_plugin_only_install_is_pass(self, tmp_path, use_profile):
        """A clean plugin-only install — manifest in the marketplace cache,
        no machine/project entry — must PASS. This is the false-negative a
        real plugin-route user hit: the doctor used to scan only cwd-relative
        dirs and report 'not registered'."""
        cache = tmp_path / "cache"
        use_profile(mcp_config=tmp_path / "absent.json", plugins_cache=cache)
        manifest_dir = (
            cache / "thinkweave" / "thinkweave" / "0.1.0" / ".claude-plugin"
        )
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "thinkweave",
                    "mcpServers": {
                        "thinkweave": {
                            "type": "stdio",
                            "command": "uv",
                            "args": [
                                "run", "--project", "${CLAUDE_PLUGIN_ROOT}",
                                "--extra", "mcp", "weave-mcp",
                            ],
                            "env": {},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = md.check_registration_scopes(tmp_path)
        assert result.passed, result.detail
        assert "plugin" in result.detail

    def test_dev_link_install_is_pass(self, tmp_path, use_profile):
        """The dev-link (@skills-dir) equivalent: manifest under
        ~/.claude/skills/<name>/.claude-plugin/, no machine/project entry."""
        skills = tmp_path / "skills"
        use_profile(mcp_config=tmp_path / "absent.json", skills_dir=skills)
        manifest_dir = skills / "thinkweave" / ".claude-plugin"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "thinkweave",
                    "mcpServers": {
                        "thinkweave": {
                            "type": "stdio",
                            "command": "uv",
                            "args": ["run", "--project", "${CLAUDE_PLUGIN_ROOT}",
                                     "--extra", "mcp", "weave-mcp"],
                            "env": {},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = md.check_registration_scopes(tmp_path)
        assert result.passed, result.detail
        assert "plugin" in result.detail

    def test_project_path_variants_normalise_to_same_invocation(
        self, tmp_path, use_profile):
        """`.` vs absolute vs ${CLAUDE_PLUGIN_ROOT} for --project must
        be treated as the same invocation shape."""
        claude_json = tmp_path / "claude.json"
        machine_entry = dict(CANONICAL_ENTRY)
        machine_entry["args"] = [
            "run",
            "--project",
            "/abs/path",
            "--extra",
            "mcp",
            "python",
            "-m",
            "thinkweave.surfaces.mcp.server",
        ]
        _write_claude_json(claude_json, machine_entry)
        _write_mcp_json(tmp_path, CANONICAL_ENTRY)  # uses "."
        use_profile(mcp_config=claude_json)
        result = md.check_registration_scopes(tmp_path)
        assert result.passed, result.detail


    def test_machine_uv_plus_project_launcher_is_equivalent(
        self, tmp_path, use_profile):
        """The portable launcher IS the uv-run invocation (#52): a machine
        scope written by `weave install` (uv run shape) plus the committed
        .mcp.json (launcher shape) must NOT read as conflicting scopes."""
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        _write_mcp_json(tmp_path, LAUNCHER_ENTRY)
        use_profile(mcp_config=claude_json)
        result = md.check_registration_scopes(tmp_path)
        assert result.passed, result.detail
        assert "identically" in result.detail

    def test_string_valued_mcp_servers_manifest_is_skipped_not_fatal(
        self, tmp_path, use_profile):
        """A foreign plugin may point ``mcpServers`` at an external file rather
        than inlining it (Atlassian ships ``"./.mcp.json"``). The manifest scan
        used to call ``.get("thinkweave")`` straight on that string and die with
        AttributeError, taking the whole doctor down. Such a manifest declares
        nothing the doctor can verify, so it must be skipped silently while
        thinkweave's own plugin manifest is still found."""
        skills = tmp_path / "skills"
        use_profile(mcp_config=tmp_path / "absent.json", skills_dir=skills)

        foreign_dir = skills / "atlassian" / ".claude-plugin"
        foreign_dir.mkdir(parents=True)
        (foreign_dir / "plugin.json").write_text(
            json.dumps({"name": "atlassian", "mcpServers": "./.mcp.json"}),
            encoding="utf-8",
        )

        ours_dir = skills / "thinkweave" / ".claude-plugin"
        ours_dir.mkdir(parents=True)
        (ours_dir / "plugin.json").write_text(
            json.dumps({"name": "thinkweave", "mcpServers": {"thinkweave": CANONICAL_ENTRY}}),
            encoding="utf-8",
        )

        result = md.check_registration_scopes(tmp_path)
        assert result.passed, result.detail
        assert "atlassian" not in result.detail

    def test_string_valued_mcp_servers_alone_reads_as_unregistered(
        self, tmp_path, use_profile):
        """The external-file manifest on its own contributes no entry — the
        doctor reports the missing registration rather than inventing one."""
        skills = tmp_path / "skills"
        use_profile(mcp_config=tmp_path / "absent.json", skills_dir=skills)
        foreign_dir = skills / "atlassian" / ".claude-plugin"
        foreign_dir.mkdir(parents=True)
        (foreign_dir / "plugin.json").write_text(
            json.dumps({"name": "atlassian", "mcpServers": "./.mcp.json"}),
            encoding="utf-8",
        )
        result = md.check_registration_scopes(tmp_path)
        assert not result.passed


class TestMcpServersNarrowing:
    """``_mcp_servers`` narrows the manifest block to something dict-shaped."""

    def test_inline_dict_passes_through(self):
        block = {"thinkweave": {"command": "uv"}}
        assert md._mcp_servers({"mcpServers": block}) == block

    @pytest.mark.parametrize(
        "value",
        ["./.mcp.json", [], 7, None],
        ids=["external-file-string", "list", "int", "null"],
    )
    def test_non_dict_becomes_empty(self, value):
        assert md._mcp_servers({"mcpServers": value}) == {}

    def test_missing_key_becomes_empty(self):
        assert md._mcp_servers({"name": "x"}) == {}


# ---------- top-level driver tests ----------


class TestRunMcpDoctor:
    def test_passed_when_all_pass(self, tmp_path, monkeypatch, capsys, use_profile):
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        use_profile(mcp_config=claude_json)
        monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
        monkeypatch.delenv("MCP_DOCTOR_FAKE_VAULT", raising=False)

        # Replace the launcher subprocess with a stub that "times out"
        # (simulating an MCP server that started and idled on stdin).
        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="uv", timeout=5.0)

        monkeypatch.setattr(md.subprocess, "run", fake_run)
        # Hermetic: the venv-extras check must not depend on which extras the
        # suite's own venv happens to have synced (`--extra mcp` in CI).
        monkeypatch.setattr(
            md, "_EXTRA_MODULES", (("json", "stdlib", "always present"),)
        )

        result = md.run_mcp_doctor(cwd=tmp_path)
        assert result.passed
        out = capsys.readouterr().out
        assert "overall: PASS" in out

    def test_fails_when_vault_dir_missing(
        self, tmp_path, monkeypatch, capsys, use_profile
    ):
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        use_profile(mcp_config=claude_json)
        monkeypatch.setenv("MCP_DOCTOR_FAKE_VAULT", "/definitely/not/real")

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="uv", timeout=5.0)

        monkeypatch.setattr(md.subprocess, "run", fake_run)

        result = md.run_mcp_doctor(cwd=tmp_path)
        assert not result.passed
        names = [c.name for c in result.checks if not c.passed]
        assert "THINKWEAVE_VAULT" in names

    def test_fails_when_no_scope_registered(self, tmp_path, monkeypatch, capsys, use_profile):
        # ~/.claude.json doesn't exist, no .mcp.json, no plugins.
        use_profile(mcp_config=tmp_path / "absent.json")
        monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
        monkeypatch.delenv("MCP_DOCTOR_FAKE_VAULT", raising=False)
        result = md.run_mcp_doctor(cwd=tmp_path)
        assert not result.passed
        out = capsys.readouterr().out
        assert "overall: FAIL" in out

    def test_fails_when_scopes_conflict(self, tmp_path, monkeypatch, capsys, use_profile):
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        divergent = {
            "type": "stdio",
            "command": "weave-mcp",
            "args": [],
            "env": {},
        }
        _write_mcp_json(tmp_path, divergent)
        use_profile(mcp_config=claude_json)
        monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
        monkeypatch.delenv("MCP_DOCTOR_FAKE_VAULT", raising=False)

        result = md.run_mcp_doctor(cwd=tmp_path)
        assert not result.passed
        out = capsys.readouterr().out
        assert "overall: FAIL" in out


# ---------- launcher-probe tests ----------


class TestLauncherResolves:
    def test_succeeds_on_timeout(self, tmp_path, monkeypatch, use_profile):
        """An MCP server idling on stdin reads as ``TimeoutExpired`` —
        the doctor treats that as success ("process is up")."""
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        use_profile(mcp_config=claude_json)

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="uv", timeout=5.0)

        monkeypatch.setattr(md.subprocess, "run", fake_run)
        result = md.check_launcher_resolves(tmp_path, timeout_s=0.1)
        assert result.passed
        assert "spawned a process" in result.detail

    def test_fails_on_nonzero_exit(self, tmp_path, monkeypatch, use_profile):
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        use_profile(mcp_config=claude_json)

        class FakeProc:
            returncode = 2
            stderr = b"command not found: foobarbaz"

        monkeypatch.setattr(
            md.subprocess, "run", lambda *a, **kw: FakeProc()
        )
        result = md.check_launcher_resolves(tmp_path, timeout_s=0.1)
        assert not result.passed
        assert "exited 2" in result.detail


    def test_relative_launcher_command_resolves_against_project_dir(
        self, tmp_path, monkeypatch, use_profile):
        """.mcp.json's `bin/weave-mcp-launch` is relative to the PROJECT
        dir (Claude Code spawns project-scope servers with cwd = project),
        not to wherever the doctor process happens to run."""
        use_profile(mcp_config=tmp_path / "absent.json")
        _write_mcp_json(tmp_path, LAUNCHER_ENTRY)
        _make_fake_launcher(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        result = md.check_launcher_resolves(tmp_path, timeout_s=5.0)
        assert result.passed, result.detail
        assert "exited 0" in result.detail

    def test_plugin_launcher_command_expands_claude_plugin_root(
        self, tmp_path, monkeypatch, use_profile):
        """The plugin manifest's command embeds ${CLAUDE_PLUGIN_ROOT};
        the probe must expand it to the manifest's own plugin root."""
        skills = tmp_path / "skills"
        use_profile(mcp_config=tmp_path / "absent.json", skills_dir=skills)
        plugin_root = skills / "thinkweave"
        manifest_dir = plugin_root / ".claude-plugin"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "thinkweave",
                    "mcpServers": {
                        "thinkweave": {
                            "type": "stdio",
                            "command": (
                                "${CLAUDE_PLUGIN_ROOT}/bin/weave-mcp-launch"
                            ),
                            "args": [],
                            "env": {},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        _make_fake_launcher(plugin_root)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        result = md.check_launcher_resolves(tmp_path, timeout_s=5.0)
        assert result.passed, result.detail

    def test_missing_relative_launcher_fails_with_resolved_path(
        self, tmp_path, monkeypatch, use_profile):
        use_profile(mcp_config=tmp_path / "absent.json")
        _write_mcp_json(tmp_path, LAUNCHER_ENTRY)  # no launcher on disk
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        result = md.check_launcher_resolves(tmp_path, timeout_s=5.0)
        assert not result.passed
        assert "bin/weave-mcp-launch" in result.detail

# ---------- env-var check ----------


class TestVaultEnvCheck:
    def test_unset_is_pass(self, monkeypatch):
        monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
        monkeypatch.delenv("MCP_DOCTOR_FAKE_VAULT", raising=False)
        result = md.check_vault_env()
        assert result.passed
        assert "not set" in result.detail

    def test_missing_dir_fails(self, monkeypatch):
        monkeypatch.setenv("MCP_DOCTOR_FAKE_VAULT", "/this/does/not/exist")
        result = md.check_vault_env()
        assert not result.passed
        assert "does not exist" in result.detail

    def test_existing_dir_passes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_DOCTOR_FAKE_VAULT", str(tmp_path))
        result = md.check_vault_env()
        assert result.passed


# ---------- machine-local slash-command symlinks (#172) ----------


def _commands_dir(root: Path) -> Path:
    d = root / ".claude" / "commands"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestCommandSymlinks:
    """A machine-local `.claude/commands/` symlink that stops resolving takes
    the slash command off the menu with no error anywhere (#172: the
    issue-loop repoint that never happened, dangling 08-03 → 08-10)."""

    def test_dangling_symlink_fails_and_names_link_and_target(
        self, tmp_path, requires_symlinks
    ):
        link = _commands_dir(tmp_path) / "issue-loop.md"
        link.symlink_to("../../../funloops/packages/devloop/docs/agents/issue-loop.command.md")

        result = md.check_command_symlinks(tmp_path)

        assert not result.passed
        assert "issue-loop.md" in result.detail
        assert "../../../funloops/packages/devloop/docs/agents/issue-loop.command.md" in result.detail
        # Remediation names the funloops sibling checkout (CLAUDE.md convention).
        assert "funloops/packages/devloop/docs/agents/issue-loop.command.md" in result.fix

    def test_resolving_symlink_passes(self, tmp_path, requires_symlinks):
        target = tmp_path / "wrap.source.md"
        target.write_text("body\n", encoding="utf-8")
        (_commands_dir(tmp_path) / "wrap.md").symlink_to(target)

        assert md.check_command_symlinks(tmp_path).passed

    def test_regular_file_passes(self, tmp_path):
        (_commands_dir(tmp_path) / "wrap.md").write_text("body\n", encoding="utf-8")

        assert md.check_command_symlinks(tmp_path).passed

    def test_absent_commands_dir_passes(self, tmp_path):
        assert md.check_command_symlinks(tmp_path).passed

    def test_dangling_symlink_fails_the_lane(
        self, tmp_path, monkeypatch, capsys, use_profile, requires_symlinks
    ):
        """The whole `--mcp` lane goes red — broken dev tooling is a failing
        check, and cmd_doctor exits non-zero off `result.passed`."""
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        use_profile(mcp_config=claude_json)
        monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
        monkeypatch.delenv("MCP_DOCTOR_FAKE_VAULT", raising=False)
        monkeypatch.setattr(
            md.subprocess,
            "run",
            lambda *a, **k: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(cmd="uv", timeout=5.0)
            ),
        )
        (_commands_dir(tmp_path) / "issue-loop.md").symlink_to(tmp_path / "gone.md")

        result = md.run_mcp_doctor(cwd=tmp_path)

        assert not result.passed
        assert "command symlinks" in [c.name for c in result.checks if not c.passed]
        assert "overall: FAIL" in capsys.readouterr().out


# ---------- venv extras check ----------


class TestVenvExtrasCheck:
    """A pruned venv (``uv sync --extra <one>``) must be a loud FAIL with the
    ``--extra all`` remedy, never a silent cron death."""

    def test_all_importable_passes(self, monkeypatch):
        monkeypatch.setattr(
            md, "_EXTRA_MODULES", (("json", "stdlib", "always present"),)
        )
        result = md.check_venv_extras()
        assert result.passed

    def test_missing_module_fails_with_extra_all_fix(self, monkeypatch):
        monkeypatch.setattr(
            md,
            "_EXTRA_MODULES",
            (("no_such_module_xyz", "news", "news rss_poll"),),
        )
        result = md.check_venv_extras()
        assert not result.passed
        assert "no_such_module_xyz [news]" in result.detail
        assert "--extra all" in result.fix

    def test_dotted_module_with_absent_parent_reports_missing(self, monkeypatch):
        """``find_spec("google.genai")`` RAISES ModuleNotFoundError when the
        parent package is absent — it only returns None when the parent
        exists. The check must report the extra missing, never crash on the
        very condition it exists to detect (#164)."""
        monkeypatch.setattr(
            md,
            "_EXTRA_MODULES",
            (("no_such_parent_xyz.child", "gemini", "podcast transcription"),),
        )
        result = md.check_venv_extras()
        assert not result.passed
        assert "no_such_parent_xyz.child [gemini]" in result.detail
        assert "--extra all" in result.fix

    def test_broken_module_reports_missing_not_crash(self, monkeypatch):
        """``find_spec`` can raise beyond ModuleNotFoundError: it imports a
        dotted name's parent (whose ``__init__`` may raise anything), and a
        sys.modules entry with ``__spec__ = None`` raises ValueError. A
        present-but-broken extra must report as missing — the doctor's job
        is diagnosing a damaged venv, never crashing on one."""
        import sys
        import types

        broken = types.ModuleType("broken_extra_xyz")
        broken.__spec__ = None
        monkeypatch.setitem(sys.modules, "broken_extra_xyz", broken)
        monkeypatch.setattr(
            md, "_EXTRA_MODULES", (("broken_extra_xyz", "news", "news rss_poll"),)
        )
        result = md.check_venv_extras()
        assert not result.passed
        assert "broken_extra_xyz [news]" in result.detail
