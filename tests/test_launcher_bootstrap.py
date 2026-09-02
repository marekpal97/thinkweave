"""Bootstrap seam for the three POSIX launchers + the ``.cmd`` twins (#164).

Same seam as ``test_mcp_launcher.py``: the real script, a fake ``uv`` that
echoes its argv, a clone directory in tmp. The sentinel is
``site-packages/thinkweave-*.dist-info``, never console scripts; the sync's
output must land on stderr (for the MCP launcher, stdout is the JSON-RPC
stdio channel). Incident history and rationale: docs/HARNESSES.md.
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

# Both venv layouts the sentinel must recognise: POSIX and native Windows
# (the POSIX launchers also run under Git Bash against a Windows venv).
DIST_INFO_LAYOUTS = {
    "posix": Path("lib") / "python3.12" / "site-packages",
    "windows": Path("Lib") / "site-packages",
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


def _run(
    tmp_path: Path, script: Path, fake_uv_body: str | None = None
) -> subprocess.CompletedProcess:
    path_dir = tmp_path / "fakepath"
    path_dir.mkdir(exist_ok=True)
    fake = path_dir / "uv"
    fake.write_text(fake_uv_body or '#!/bin/sh\necho "uv $@"\n', encoding="utf-8")
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
    # A breadcrumb first, so a hook killed at its timeout mid-sync leaves a
    # diagnosable trace instead of dying silently.
    assert "bootstrap" in result.stderr
    # Bootstrap sync next — the sanctioned shape, on stderr (stdout is the
    # MCP stdio channel for weave-mcp-launch).
    assert f"uv sync --project {root} --extra all" in result.stderr
    # …then the unchanged --no-sync module exec on stdout.
    assert result.stdout.strip() == (
        f"uv run --no-sync --project {root} --extra mcp {LAUNCHERS[name]}"
    )


@pytest.mark.parametrize("layout", DIST_INFO_LAYOUTS)
@pytest.mark.parametrize("name", LAUNCHERS)
def test_installed_dist_skips_bootstrap(tmp_path, name, layout):
    """An installed distribution — and NOTHING else: no console scripts —
    must skip the bootstrap. This is exactly the half-shimmed venv the
    2026-08-03 incident produced (imports fine, shims deleted by an
    interrupted reinstall): survivable via ``python -m``, and syncing it
    while servers hold the shims is the os-error-32 failure."""
    clone, script = _clone_with_launcher(tmp_path, name)
    dist_info = clone / ".venv" / DIST_INFO_LAYOUTS[layout] / "thinkweave-0.2.0.dist-info"
    dist_info.mkdir(parents=True)

    result = _run(tmp_path, script)

    assert result.returncode == 0, result.stderr
    assert "sync --project" not in result.stderr
    assert result.stdout.strip() == (
        f"uv run --no-sync --project {_shell_path(clone)} --extra mcp "
        f"{LAUNCHERS[name]}"
    )


@pytest.mark.parametrize("name", LAUNCHERS)
def test_failed_bootstrap_aborts_before_run(tmp_path, name):
    """A failing sync must abort the launcher (set -eu), never fall through
    to a `uv run` that would fabricate an empty venv and die confusingly."""
    clone, script = _clone_with_launcher(tmp_path, name)
    failing_uv = (
        "#!/bin/sh\n"
        'if [ "$1" = "sync" ]; then echo "uv $@"; exit 7; fi\n'
        'echo "uv $@"\n'
    )
    result = _run(tmp_path, script, fake_uv_body=failing_uv)

    assert result.returncode != 0
    assert result.stdout.strip() == "", "launcher ran past a failed bootstrap"
    assert "uv sync" in result.stderr  # the failed attempt is visible


def test_cmd_launchers_pin_bootstrap_branch():
    """cmd.exe can't run here; pin the native-Windows twins by their
    non-comment invocation lines (the test_install.py pattern): sentinel,
    sanctioned sync shape on stderr, failure propagation, and the bootstrap
    ordered before the run line."""
    for name in ("weave-hook-launch.cmd", "weave-mcp-launch.cmd"):
        text = (REPO_ROOT / "bin" / name).read_text(encoding="utf-8")
        lines = [
            ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("rem ")
        ]
        sentinel = next(
            (i for i, ln in enumerate(lines) if ln.startswith(
                'if not exist "%root%\\.venv\\Lib\\site-packages\\thinkweave-*.dist-info"'
            )),
            None,
        )
        assert sentinel is not None, f"{name}: no dist-info bootstrap sentinel"
        sync = next(
            (i for i, ln in enumerate(lines)
             if 'sync --project "%root%" --extra all' in ln),
            None,
        )
        assert sync is not None, f"{name}: no sanctioned bootstrap sync line"
        assert "1>&2" in lines[sync], f"{name}: sync output not on stderr"
        assert "if errorlevel 1 exit /b 1" in lines, (
            f"{name}: failed bootstrap does not abort"
        )
        run = next(
            i for i, ln in enumerate(lines) if "run --no-sync" in ln
        )
        assert sentinel < sync < run, f"{name}: bootstrap not before the run line"
