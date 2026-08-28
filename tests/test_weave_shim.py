"""Behavioral test for ``bin/weave`` — the plugin CLI shim on every skill's PATH.

Until 2026-08-23 this shim ran ``uv run --project <root> --extra mcp weave``
WITHOUT ``--no-sync``. ``uv run`` syncs the venv to exactly the named extras,
so every bare ``weave …`` call from a skill or subagent silently uninstalled
the news / embeddings / gemini / youtube extras — the news pull died for 11
days (``feedparser_missing``) and the embed + dream crons were one ``/wrap``
away from the same fate. The hook and MCP launchers had already moved to
``--no-sync`` + module entry (#156); ``bin/weave`` was the straggler (#164).

Same seam as ``test_hook_launcher.py``: fake ``uv`` echoes its argv.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIM = REPO_ROOT / "bin" / "weave"


def _shell_path(path: Path) -> str:
    resolved = Path(os.path.realpath(path)).as_posix()
    if os.name == "nt":
        return f"/{resolved[0].lower()}{resolved[2:]}"
    return resolved


PROJECT_ROOT_PHYSICAL = _shell_path(REPO_ROOT)

EXPECTED_UV_ARGV = (
    f"run --no-sync --project {PROJECT_ROOT_PHYSICAL} --extra mcp "
    "python -m thinkweave queue list"
)


def _shim_command(*args: str) -> list[str]:
    if os.name != "nt":
        return [str(SHIM), *args]
    git = shutil.which("git")
    assert git, "Git Bash is required to exercise POSIX launchers on Windows"
    bash = Path(git).resolve().parents[1] / "bin" / "bash.exe"
    return [str(bash), _shell_path(SHIM), *args]


def test_shim_runs_cli_module_with_no_sync_and_forwards_args(tmp_path):
    path_dir = tmp_path / "fakepath"
    path_dir.mkdir()
    fake = path_dir / "uv"
    fake.write_text('#!/bin/sh\necho "$0 $@"\n', encoding="utf-8")
    fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()

    result = subprocess.run(
        _shim_command("queue", "list"),
        env={"PATH": str(path_dir), "HOME": str(home)},
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{_shell_path(fake)} {EXPECTED_UV_ARGV}"
    assert "--no-sync" in result.stdout  # the regression guard, spelled out
