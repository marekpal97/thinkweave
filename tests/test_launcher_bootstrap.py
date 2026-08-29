"""Bootstrap seam for the three POSIX launchers + the ``.cmd`` twins (#164).

Pre-#156 the launchers' implicit ``uv run`` sync populated the plugin
(marketplace) clone's venv — the route's ONLY dependency bootstrap. The move
to ``--no-sync`` (#156/#164) removed it: ``uv run --no-sync`` on a venv-less
clone fabricates an EMPTY venv and dies with ModuleNotFoundError, with no
actionable message. The launchers now branch: when the venv lacks the
project's own console script, they run the ONE sanctioned sync
(dec-3d4f8ce9) — ``uv sync --extra all`` — before exec'ing the module.
A synced venv (every dev checkout, this suite's included) never triggers it,
so a live session still never re-syncs.

Same seam as ``test_mcp_launcher.py``: the real script, a fake ``uv`` that
echoes its argv, a clone directory in tmp. The sync's output must land on
stderr — for the MCP launcher, stdout is the JSON-RPC stdio channel.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# launcher name → the module the exec'd `uv run` line must invoke.
LAUNCHERS = {
    "weave": "python -m thinkweave",
    "weave-hook-launch": "python -m thinkweave.surfaces.hooks.handler",
    "weave-mcp-launch": "python -m thinkweave.surfaces.mcp.server",
}


def _shell_path(path: Path) -> str:
    resolved = Path(os.path.realpath(path)).as_posix()
    if os.name == "nt":
        return f"/{resolved[0].lower()}{resolved[2:]}"
    return resolved


def _launcher_command(script: Path) -> list[str]:
    if os.name != "nt":
        return [str(script)]
    git = shutil.which("git")
    assert git, "Git Bash is required to exercise POSIX launchers on Windows"
    bash = Path(git).resolve().parents[1] / "bin" / "bash.exe"
    return [str(bash), _shell_path(script)]


def _clone_with_launcher(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """A bare 'plugin clone': just bin/<launcher>, no venv."""
    clone = tmp_path / "clone"
    (clone / "bin").mkdir(parents=True)
    script = clone / "bin" / name
    shutil.copy(REPO_ROOT / "bin" / name, script)
    script.chmod(0o755)
    return clone, script


def _run(tmp_path: Path, script: Path) -> subprocess.CompletedProcess:
    path_dir = tmp_path / "fakepath"
    path_dir.mkdir(exist_ok=True)
    fake = path_dir / "uv"
    fake.write_text('#!/bin/sh\necho "uv $@"\n', encoding="utf-8")
    fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    cwd = tmp_path / "elsewhere"
    cwd.mkdir(exist_ok=True)
    return subprocess.run(
        _launcher_command(script),
        env={"PATH": str(path_dir), "HOME": str(home)},
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("name", LAUNCHERS)
def test_venv_less_clone_syncs_extra_all_then_runs(tmp_path, name):
    clone, script = _clone_with_launcher(tmp_path, name)
    result = _run(tmp_path, script)

    assert result.returncode == 0, result.stderr
    root = _shell_path(clone)
    # Bootstrap sync first — the sanctioned shape, on stderr (stdout is the
    # MCP stdio channel for weave-mcp-launch).
    assert f"uv sync --project {root} --extra all" in result.stderr
    # …then the unchanged --no-sync module exec on stdout.
    assert result.stdout.strip() == (
        f"uv run --no-sync --project {root} --extra mcp {LAUNCHERS[name]}"
    )


@pytest.mark.parametrize("name", LAUNCHERS)
def test_synced_venv_skips_bootstrap(tmp_path, name):
    clone, script = _clone_with_launcher(tmp_path, name)
    venv_bin = clone / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    marker = venv_bin / "weave"
    marker.write_text("#!/bin/sh\n", encoding="utf-8")
    marker.chmod(0o755)

    result = _run(tmp_path, script)

    assert result.returncode == 0, result.stderr
    assert "sync --project" not in result.stderr
    assert result.stdout.strip() == (
        f"uv run --no-sync --project {_shell_path(clone)} --extra mcp "
        f"{LAUNCHERS[name]}"
    )


def test_cmd_launchers_pin_bootstrap_branch():
    """cmd.exe can't run here; pin the native-Windows twins by content."""
    for name in ("weave-hook-launch.cmd", "weave-mcp-launch.cmd"):
        text = (REPO_ROOT / "bin" / name).read_text(encoding="utf-8")
        assert 'if not exist "%root%\\.venv\\Scripts\\weave.exe"' in text, name
        assert 'sync --project "%root%" --extra all' in text, name
