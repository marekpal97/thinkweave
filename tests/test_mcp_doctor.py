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
def _sandbox_home_plugin_dirs(tmp_path, monkeypatch):
    """Point the doctor's HOME-scoped plugin scan at empty dirs so it never
    reads the developer's real ~/.claude/plugins or ~/.claude/skills. Tests
    that want a plugin scope present re-point PLUGINS_CACHE/SKILLS_DIR
    themselves (their setattr runs after this fixture)."""
    monkeypatch.setattr(md, "PLUGINS_CACHE", tmp_path / "_home_plugins_cache")
    monkeypatch.setattr(md, "SKILLS_DIR", tmp_path / "_home_skills")


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


CANONICAL_ENTRY = {
    "type": "stdio",
    "command": "uv",
    "args": ["run", "--project", ".", "--extra", "mcp", "weave-mcp"],
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
    def test_empty_claude_json_reports_unregistered(self, tmp_path, monkeypatch):
        # No ~/.claude.json, no .mcp.json, no plugin manifests.
        monkeypatch.setattr(md, "CLAUDE_JSON", tmp_path / "claude.json")
        result = md.check_registration_scopes(tmp_path)
        assert not result.passed
        assert "not registered" in result.detail
        assert "weave install" in result.fix

    def test_machine_only_is_pass(self, tmp_path, monkeypatch):
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        monkeypatch.setattr(md, "CLAUDE_JSON", claude_json)
        result = md.check_registration_scopes(tmp_path)
        assert result.passed
        assert "1 scope" in result.detail

    def test_machine_plus_project_identical_is_pass(self, tmp_path, monkeypatch):
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        _write_mcp_json(tmp_path, CANONICAL_ENTRY)
        monkeypatch.setattr(md, "CLAUDE_JSON", claude_json)
        result = md.check_registration_scopes(tmp_path)
        assert result.passed, result.detail
        assert "identically" in result.detail

    def test_machine_plus_project_with_divergent_invocations_is_fail(
        self, tmp_path, monkeypatch
    ):
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        divergent = {
            "type": "stdio",
            "command": "weave-mcp",  # bare console-script — the legacy bug
            "args": [],
            "env": {},
        }
        _write_mcp_json(tmp_path, divergent)
        monkeypatch.setattr(md, "CLAUDE_JSON", claude_json)
        result = md.check_registration_scopes(tmp_path)
        assert not result.passed
        assert "DIFFERENT invocations" in result.detail

    def test_plugin_only_install_is_pass(self, tmp_path, monkeypatch):
        """A clean plugin-only install — manifest in the marketplace cache,
        no machine/project entry — must PASS. This is the false-negative a
        real plugin-route user hit: the doctor used to scan only cwd-relative
        dirs and report 'not registered'."""
        monkeypatch.setattr(md, "CLAUDE_JSON", tmp_path / "absent.json")
        cache = tmp_path / "cache"
        monkeypatch.setattr(md, "PLUGINS_CACHE", cache)
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

    def test_dev_link_install_is_pass(self, tmp_path, monkeypatch):
        """The dev-link (@skills-dir) equivalent: manifest under
        ~/.claude/skills/<name>/.claude-plugin/, no machine/project entry."""
        monkeypatch.setattr(md, "CLAUDE_JSON", tmp_path / "absent.json")
        skills = tmp_path / "skills"
        monkeypatch.setattr(md, "SKILLS_DIR", skills)
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
        self, tmp_path, monkeypatch
    ):
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
            "weave-mcp",
        ]
        _write_claude_json(claude_json, machine_entry)
        _write_mcp_json(tmp_path, CANONICAL_ENTRY)  # uses "."
        monkeypatch.setattr(md, "CLAUDE_JSON", claude_json)
        result = md.check_registration_scopes(tmp_path)
        assert result.passed, result.detail


    def test_machine_uv_plus_project_launcher_is_equivalent(
        self, tmp_path, monkeypatch
    ):
        """The portable launcher IS the uv-run invocation (#52): a machine
        scope written by `weave install` (uv run shape) plus the committed
        .mcp.json (launcher shape) must NOT read as conflicting scopes."""
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        _write_mcp_json(tmp_path, LAUNCHER_ENTRY)
        monkeypatch.setattr(md, "CLAUDE_JSON", claude_json)
        result = md.check_registration_scopes(tmp_path)
        assert result.passed, result.detail
        assert "identically" in result.detail


# ---------- top-level driver tests ----------


class TestRunMcpDoctor:
    def test_passed_when_all_pass(self, tmp_path, monkeypatch, capsys):
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        monkeypatch.setattr(md, "CLAUDE_JSON", claude_json)
        monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
        monkeypatch.delenv("MCP_DOCTOR_FAKE_VAULT", raising=False)

        # Replace the launcher subprocess with a stub that "times out"
        # (simulating an MCP server that started and idled on stdin).
        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="uv", timeout=5.0)

        monkeypatch.setattr(md.subprocess, "run", fake_run)

        result = md.run_mcp_doctor(cwd=tmp_path)
        assert result.passed
        out = capsys.readouterr().out
        assert "overall: PASS" in out

    def test_fails_when_vault_dir_missing(self, tmp_path, monkeypatch, capsys):
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        monkeypatch.setattr(md, "CLAUDE_JSON", claude_json)
        monkeypatch.setenv("MCP_DOCTOR_FAKE_VAULT", "/definitely/not/real")

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="uv", timeout=5.0)

        monkeypatch.setattr(md.subprocess, "run", fake_run)

        result = md.run_mcp_doctor(cwd=tmp_path)
        assert not result.passed
        names = [c.name for c in result.checks if not c.passed]
        assert "THINKWEAVE_VAULT" in names

    def test_fails_when_no_scope_registered(self, tmp_path, monkeypatch, capsys):
        # ~/.claude.json doesn't exist, no .mcp.json, no plugins.
        monkeypatch.setattr(md, "CLAUDE_JSON", tmp_path / "absent.json")
        monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
        monkeypatch.delenv("MCP_DOCTOR_FAKE_VAULT", raising=False)
        result = md.run_mcp_doctor(cwd=tmp_path)
        assert not result.passed
        out = capsys.readouterr().out
        assert "overall: FAIL" in out

    def test_fails_when_scopes_conflict(self, tmp_path, monkeypatch, capsys):
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        divergent = {
            "type": "stdio",
            "command": "weave-mcp",
            "args": [],
            "env": {},
        }
        _write_mcp_json(tmp_path, divergent)
        monkeypatch.setattr(md, "CLAUDE_JSON", claude_json)
        monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
        monkeypatch.delenv("MCP_DOCTOR_FAKE_VAULT", raising=False)

        result = md.run_mcp_doctor(cwd=tmp_path)
        assert not result.passed
        out = capsys.readouterr().out
        assert "overall: FAIL" in out


# ---------- launcher-probe tests ----------


class TestLauncherResolves:
    def test_succeeds_on_timeout(self, tmp_path, monkeypatch):
        """An MCP server idling on stdin reads as ``TimeoutExpired`` —
        the doctor treats that as success ("process is up")."""
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        monkeypatch.setattr(md, "CLAUDE_JSON", claude_json)

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="uv", timeout=5.0)

        monkeypatch.setattr(md.subprocess, "run", fake_run)
        result = md.check_launcher_resolves(tmp_path, timeout_s=0.1)
        assert result.passed
        assert "spawned a process" in result.detail

    def test_fails_on_nonzero_exit(self, tmp_path, monkeypatch):
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        monkeypatch.setattr(md, "CLAUDE_JSON", claude_json)

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
        self, tmp_path, monkeypatch
    ):
        """.mcp.json's `bin/weave-mcp-launch` is relative to the PROJECT
        dir (Claude Code spawns project-scope servers with cwd = project),
        not to wherever the doctor process happens to run."""
        monkeypatch.setattr(md, "CLAUDE_JSON", tmp_path / "absent.json")
        _write_mcp_json(tmp_path, LAUNCHER_ENTRY)
        _make_fake_launcher(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        result = md.check_launcher_resolves(tmp_path, timeout_s=5.0)
        assert result.passed, result.detail
        assert "exited 0" in result.detail

    def test_plugin_launcher_command_expands_claude_plugin_root(
        self, tmp_path, monkeypatch
    ):
        """The plugin manifest's command embeds ${CLAUDE_PLUGIN_ROOT};
        the probe must expand it to the manifest's own plugin root."""
        monkeypatch.setattr(md, "CLAUDE_JSON", tmp_path / "absent.json")
        skills = tmp_path / "skills"
        monkeypatch.setattr(md, "SKILLS_DIR", skills)
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
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(md, "CLAUDE_JSON", tmp_path / "absent.json")
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
