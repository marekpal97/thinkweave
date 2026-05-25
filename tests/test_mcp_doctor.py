"""Tests for ``mem doctor --mcp`` (mcp_doctor module).

All tests monkeypatch the ``CLAUDE_JSON`` path to a tmp file so the
user's real ``~/.claude.json`` is never read or written.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from personal_mem.surfaces.cli import mcp_doctor as md


# ---------- helpers ----------


def _write_claude_json(path: Path, entry: dict | None) -> None:
    body: dict = {"mcpServers": {}}
    if entry is not None:
        body["mcpServers"]["personal-mem"] = entry
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")


def _write_mcp_json(cwd: Path, entry: dict | None) -> None:
    body: dict = {"mcpServers": {}}
    if entry is not None:
        body["mcpServers"]["personal-mem"] = entry
    (cwd / ".mcp.json").write_text(json.dumps(body, indent=2), encoding="utf-8")


CANONICAL_ENTRY = {
    "type": "stdio",
    "command": "uv",
    "args": ["run", "--project", ".", "--extra", "mcp", "mem-mcp"],
    "env": {},
}


# ---------- scope-detection tests ----------


class TestRegistrationScopes:
    def test_empty_claude_json_reports_unregistered(self, tmp_path, monkeypatch):
        # No ~/.claude.json, no .mcp.json, no plugin manifests.
        monkeypatch.setattr(md, "CLAUDE_JSON", tmp_path / "claude.json")
        result = md.check_registration_scopes(tmp_path)
        assert not result.passed
        assert "not registered" in result.detail
        assert "mem install" in result.fix

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
            "command": "mem-mcp",  # bare console-script — the legacy bug
            "args": [],
            "env": {},
        }
        _write_mcp_json(tmp_path, divergent)
        monkeypatch.setattr(md, "CLAUDE_JSON", claude_json)
        result = md.check_registration_scopes(tmp_path)
        assert not result.passed
        assert "DIFFERENT invocations" in result.detail

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
            "mem-mcp",
        ]
        _write_claude_json(claude_json, machine_entry)
        _write_mcp_json(tmp_path, CANONICAL_ENTRY)  # uses "."
        monkeypatch.setattr(md, "CLAUDE_JSON", claude_json)
        result = md.check_registration_scopes(tmp_path)
        assert result.passed, result.detail


# ---------- top-level driver tests ----------


class TestRunMcpDoctor:
    def test_passed_when_all_pass(self, tmp_path, monkeypatch, capsys):
        claude_json = tmp_path / "claude.json"
        _write_claude_json(claude_json, CANONICAL_ENTRY)
        monkeypatch.setattr(md, "CLAUDE_JSON", claude_json)
        monkeypatch.delenv("PERSONAL_MEM_VAULT", raising=False)
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
        assert "PERSONAL_MEM_VAULT" in names

    def test_fails_when_no_scope_registered(self, tmp_path, monkeypatch, capsys):
        # ~/.claude.json doesn't exist, no .mcp.json, no plugins.
        monkeypatch.setattr(md, "CLAUDE_JSON", tmp_path / "absent.json")
        monkeypatch.delenv("PERSONAL_MEM_VAULT", raising=False)
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
            "command": "mem-mcp",
            "args": [],
            "env": {},
        }
        _write_mcp_json(tmp_path, divergent)
        monkeypatch.setattr(md, "CLAUDE_JSON", claude_json)
        monkeypatch.delenv("PERSONAL_MEM_VAULT", raising=False)
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


# ---------- env-var check ----------


class TestVaultEnvCheck:
    def test_unset_is_pass(self, monkeypatch):
        monkeypatch.delenv("PERSONAL_MEM_VAULT", raising=False)
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
