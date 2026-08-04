"""Acceptance tests for issue #164's native-Windows Codex bootstrap."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

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


def test_codex_install_writes_launcher_and_prepends_user_path(
    tmp_path: Path, monkeypatch
) -> None:
    profile = harness.codex(home=tmp_path)
    project = tmp_path / "checkout"
    purelib = project / ".venv" / "Lib" / "site-packages"
    base_python = tmp_path / "uv-python" / "python.exe"
    prepended: list[Path] = []

    monkeypatch.setattr(install_mod, "_profile", lambda: profile)
    monkeypatch.setattr(install_mod.os, "name", "nt")
    monkeypatch.setattr(install_mod.sys, "_base_executable", str(base_python))
    monkeypatch.setattr(install_mod, "_venv_purelib", lambda: purelib)
    monkeypatch.setattr(
        install_mod, "_prepend_windows_user_path", lambda path: prepended.append(path)
    )

    install_mod._install_codex_windows_cli(project)

    launcher = tmp_path / ".codex" / "bin" / "weave.cmd"
    assert launcher.is_file()
    assert prepended == [launcher.parent]
    assert "-S -m thinkweave %*" in launcher.read_text(encoding="utf-8")


def test_claude_install_does_not_create_codex_launcher(tmp_path: Path, monkeypatch) -> None:
    profile = harness.claude_code(home=tmp_path)
    monkeypatch.setattr(install_mod, "_profile", lambda: profile)
    monkeypatch.setattr(install_mod.os, "name", "nt")

    assert install_mod._install_codex_windows_cli(tmp_path / "checkout") is None
    assert not (tmp_path / ".codex").exists()


def test_cli_doctor_distinguishes_missing_from_import_blocked(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(mcp_doctor.shutil, "which", lambda _name: None)
    missing = mcp_doctor.check_weave_cli()
    assert not missing.passed
    assert "not on PATH" in missing.detail

    resolved = tmp_path / "venv" / "Scripts" / "weave.exe"
    monkeypatch.setattr(mcp_doctor.shutil, "which", lambda _name: str(resolved))
    monkeypatch.setattr(
        mcp_doctor.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout=b"", stderr=b"ModuleNotFoundError: thinkweave"
        ),
    )
    blocked = mcp_doctor.check_weave_cli()
    assert not blocked.passed
    assert "resolves" in blocked.detail
    assert "cannot import" in blocked.detail
    assert "weave install --yes --harness codex" in blocked.fix


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
    monkeypatch.setattr(install_mod.os, "name", "nt")
    monkeypatch.setattr(
        install_mod, "_profile", lambda: harness.codex(home=tmp_path)
    )

    install_mod.cmd_install(
        argparse.Namespace(yes=True, vault=None, no_claude_md=True)
    )

    assert calls == ["sync", "bootstrap"]
