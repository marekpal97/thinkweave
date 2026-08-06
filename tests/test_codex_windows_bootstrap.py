"""Acceptance tests for issue #164's native-Windows Codex bootstrap."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from thinkweave.core import harness
from thinkweave.surfaces.cli import install as install_mod
from thinkweave.surfaces.cli import mcp_doctor


def test_codex_plugin_manifest_exposes_bootstrap_skill() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    assert manifest["name"] == "thinkweave"
    assert manifest["skills"] == "./skills/"
    assert (root / "skills" / "thinkweave-bootstrap" / "SKILL.md").is_file()


def test_bootstrap_skill_metadata_matches_the_other_codex_skills() -> None:
    """Same shape every other `skills/*/agents/openai.yaml` carries — the
    projected-skill suite asserts this across the whole directory, so a
    bootstrap skill that skips it only breaks once the two land together."""
    metadata = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "skills"
            / "thinkweave-bootstrap"
            / "agents"
            / "openai.yaml"
        ).read_text(encoding="utf-8")
    )

    assert metadata["interface"]["default_prompt"].startswith("Use $thinkweave-")
    assert metadata["dependencies"]["tools"] == [
        {
            "type": "mcp",
            "value": "thinkweave",
            "description": "ThinkWeave durable-memory tools",
        }
    ]


def test_windows_launcher_bypasses_uv_and_editable_pth(tmp_path: Path) -> None:
    project = tmp_path / "thinkweave"
    purelib = project / ".venv" / "Lib" / "site-packages"
    base_python = tmp_path / "uv-python" / "python.exe"

    rendered = install_mod._render_codex_windows_cli_launcher(
        project_root=project,
        base_python=base_python,
        purelib=purelib,
    )

    assert "uv run" not in rendered
    assert str(project / ".venv" / "Scripts" / "python.exe") not in rendered
    assert str(base_python) in rendered
    assert str(project / "src") in rendered
    assert str(purelib) in rendered
    assert "PYTHONDONTWRITEBYTECODE=1" in rendered
    assert "-S -m thinkweave %*" in rendered


def test_windows_launcher_preserves_an_existing_pythonpath(tmp_path: Path) -> None:
    """Ours goes in front of the user's, and an unset PYTHONPATH never leaves
    the empty trailing entry that Python would read as the cwd."""
    rendered = install_mod._render_codex_windows_cli_launcher(
        project_root=tmp_path / "thinkweave",
        base_python=tmp_path / "python.exe",
        purelib=tmp_path / "site-packages",
    )

    ours = f"{tmp_path / 'thinkweave' / 'src'};{tmp_path / 'site-packages'}"
    assert f'if defined PYTHONPATH (set "PYTHONPATH={ours};%PYTHONPATH%")' in rendered
    assert f'else (set "PYTHONPATH={ours}")' in rendered


def test_windows_launcher_executes_the_cli_without_sync(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows-only: the launcher is a .cmd file")
    root = Path(__file__).resolve().parents[1]
    launcher = tmp_path / "weave.cmd"
    launcher.write_text(
        install_mod._render_codex_windows_cli_launcher(
            project_root=root,
            base_python=install_mod._base_python(),
            purelib=install_mod._venv_purelib(),
        ),
        encoding="utf-8",
        newline="\r\n",
    )

    result = subprocess.run(
        [str(launcher), "--help"], capture_output=True, text=True, timeout=10
    )

    assert result.returncode == 0, result.stderr
    assert "usage: weave" in result.stdout


def test_base_python_falls_back_when_the_private_attr_is_absent(monkeypatch) -> None:
    monkeypatch.delattr(sys, "_base_executable", raising=False)

    assert install_mod._base_python() == Path(sys.executable)


def test_codex_install_writes_launcher_and_prepends_user_path(
    tmp_path: Path, monkeypatch
) -> None:
    profile = harness.codex(home=tmp_path)
    project = tmp_path / "checkout"
    purelib = project / ".venv" / "Lib" / "site-packages"
    base_python = tmp_path / "uv-python" / "python.exe"
    prepended: list[Path] = []

    monkeypatch.setattr(install_mod, "_profile", lambda: profile)
    monkeypatch.setattr(install_mod, "_is_windows", lambda: True)
    monkeypatch.setattr(install_mod, "_base_python", lambda: base_python)
    monkeypatch.setattr(install_mod, "_venv_purelib", lambda: purelib)
    monkeypatch.setattr(
        install_mod, "_prepend_windows_user_path", lambda path: prepended.append(path)
    )

    install_mod._install_codex_windows_cli(project)

    launcher = tmp_path / ".codex" / "bin" / "weave.cmd"
    assert launcher.is_file()
    assert prepended == [launcher.parent]
    assert "-S -m thinkweave %*" in launcher.read_text(encoding="utf-8")


def test_posix_install_writes_no_launcher(tmp_path: Path, monkeypatch) -> None:
    """The gate itself: on POSIX the bootstrap is a no-op even under Codex —
    a .cmd launcher and an HKCU PATH edit mean nothing there."""
    monkeypatch.setattr(install_mod, "_profile", lambda: harness.codex(home=tmp_path))
    monkeypatch.setattr(install_mod, "_is_windows", lambda: False)

    assert install_mod._install_codex_windows_cli(tmp_path / "checkout") is None
    assert not (tmp_path / ".codex" / "bin").exists()


def test_claude_install_does_not_create_codex_launcher(
    tmp_path: Path, monkeypatch
) -> None:
    profile = harness.claude_code(home=tmp_path)
    monkeypatch.setattr(install_mod, "_profile", lambda: profile)
    monkeypatch.setattr(install_mod, "_is_windows", lambda: True)

    assert install_mod._install_codex_windows_cli(tmp_path / "checkout") is None
    assert not (tmp_path / ".codex").exists()


def test_codex_uninstall_removes_launcher_and_path_entry(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    profile = harness.codex(home=tmp_path)
    monkeypatch.setattr(install_mod, "_profile", lambda: profile)
    monkeypatch.setattr(install_mod, "_is_windows", lambda: True)
    removed: list[Path] = []
    monkeypatch.setattr(
        install_mod,
        "_remove_windows_user_path",
        lambda path: bool(removed.append(path)) or True,
    )
    launcher = tmp_path / ".codex" / "bin" / "weave.cmd"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("@echo off\n", encoding="utf-8")

    install_mod.cmd_uninstall(argparse.Namespace(yes=True))

    assert not launcher.exists()
    assert removed == [launcher.parent]
    assert str(launcher) in capsys.readouterr().out


class TestPathValueEditing:
    """The PATH string arithmetic, unit-tested off Windows — a repeat install
    that re-prepends the same directory is the bug these guard."""

    BIN = r"C:\Users\me\.codex\bin"

    def test_prepends_to_an_empty_value(self) -> None:
        assert install_mod._prepend_path_value("", self.BIN) == self.BIN

    def test_prepends_ahead_of_existing_entries(self) -> None:
        assert (
            install_mod._prepend_path_value(r"C:\Windows;C:\Windows\system32", self.BIN)
            == rf"{self.BIN};C:\Windows;C:\Windows\system32"
        )

    def test_already_present_is_a_no_op(self) -> None:
        current = rf"C:\Windows;{self.BIN}"
        assert install_mod._prepend_path_value(current, self.BIN) == current

    def test_case_difference_still_counts_as_present(self) -> None:
        current = r"c:\users\me\.CODEX\bin"
        assert install_mod._prepend_path_value(current, self.BIN) == current

    def test_trailing_separator_still_counts_as_present(self) -> None:
        for current in (f"{self.BIN}\\", f"{self.BIN}/"):
            assert install_mod._prepend_path_value(current, self.BIN) == current

    def test_expanded_variable_still_counts_as_present(self, monkeypatch) -> None:
        monkeypatch.setenv("TW_TEST_HOME", r"C:\Users\me")
        current = r"%TW_TEST_HOME%\.codex\bin"
        assert install_mod._prepend_path_value(current, self.BIN) == current

    def test_removal_drops_every_spelling_of_the_entry(self, monkeypatch) -> None:
        monkeypatch.setenv("TW_TEST_HOME", r"C:\Users\me")
        current = ";".join(
            [
                r"C:\Windows",
                self.BIN,
                f"{self.BIN}\\",
                r"%TW_TEST_HOME%\.codex\bin",
                r"C:\other",
            ]
        )
        assert (
            install_mod._remove_path_value(current, self.BIN)
            == r"C:\Windows;C:\other"
        )

    def test_removal_of_an_absent_entry_leaves_the_value_alone(self) -> None:
        assert install_mod._remove_path_value(r"C:\Windows", self.BIN) == r"C:\Windows"


def _completed(returncode: int, stderr: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["weave"], returncode, stdout=b"", stderr=stderr)


def test_cli_doctor_distinguishes_missing_from_import_blocked(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(mcp_doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(mcp_doctor.shutil, "which", lambda _name: None)
    missing = mcp_doctor.check_weave_cli()
    assert not missing.passed
    assert "not on PATH" in missing.detail

    resolved = tmp_path / "venv" / "Scripts" / "weave.exe"
    monkeypatch.setattr(mcp_doctor.shutil, "which", lambda _name: str(resolved))
    monkeypatch.setattr(
        mcp_doctor.subprocess,
        "run",
        lambda *a, **k: _completed(1, b"ModuleNotFoundError: thinkweave"),
    )
    blocked = mcp_doctor.check_weave_cli()
    assert not blocked.passed
    assert "resolves" in blocked.detail
    assert "cannot import" in blocked.detail
    assert "weave install --yes --harness codex" in blocked.fix


def test_cli_doctor_fix_does_not_require_weave_on_path(monkeypatch) -> None:
    """Telling a user whose `weave` is missing to run `weave` is no remedy."""
    monkeypatch.setattr(mcp_doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(mcp_doctor.shutil, "which", lambda _name: None)

    fix = mcp_doctor.check_weave_cli().fix

    assert fix.startswith("run `uv run --project ")
    assert not fix.startswith("run `weave")


def test_cli_doctor_is_informational_on_posix(monkeypatch) -> None:
    """The installer has no POSIX PATH remedy, so a missing `weave` must not
    redden a report the fix string cannot resolve."""
    monkeypatch.setattr(mcp_doctor, "_is_windows", lambda: False)
    monkeypatch.setattr(mcp_doctor.shutil, "which", lambda _name: None)

    result = mcp_doctor.check_weave_cli()

    assert result.passed
    assert "not on PATH" in result.detail
    assert "informational" in result.detail
    assert result.fix == ""


def test_cli_doctor_treats_a_slow_probe_as_syncing(tmp_path: Path, monkeypatch) -> None:
    """The plugin shim's bare `uv run` can cold-sync for far longer than the
    probe waits; slow is not the same failure as broken."""
    monkeypatch.setattr(mcp_doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(mcp_doctor.shutil, "which", lambda _name: str(tmp_path / "weave"))

    def _timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="weave", timeout=60)

    monkeypatch.setattr(mcp_doctor.subprocess, "run", _timeout)

    result = mcp_doctor.check_weave_cli()

    assert result.passed
    assert "syncing" in result.detail


def test_codex_install_calls_bootstrap_after_eager_sync(
    tmp_path: Path, monkeypatch, stub_install_validators
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        install_mod,
        "_check_scripts",
        lambda: install_mod.ScriptsCheck("ok", [], tmp_path / "Scripts"),
    )
    monkeypatch.setattr(install_mod, "_detect_project_root", lambda: tmp_path)
    monkeypatch.setattr(install_mod, "_write_mcp_entry", lambda *_: None)
    monkeypatch.setattr(install_mod, "_uv_sync", lambda _root: calls.append("sync"))
    monkeypatch.setattr(
        install_mod,
        "_install_codex_windows_cli",
        lambda _root: calls.append("bootstrap"),
    )
    monkeypatch.setattr(install_mod, "_print_next_steps", lambda: None)
    monkeypatch.setattr(install_mod, "_is_windows", lambda: True)
    monkeypatch.setattr(install_mod, "_profile", lambda: harness.codex(home=tmp_path))

    install_mod.cmd_install(argparse.Namespace(yes=True, vault=None, no_claude_md=True))

    assert calls == ["sync", "bootstrap"]
