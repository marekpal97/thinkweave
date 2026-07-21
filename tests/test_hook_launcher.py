"""Behavioral tests for ``bin/weave-hook-launch`` — the portable hook launcher.

The canonical ``hooks/hooks.json`` used to fire each hook as a bare ``uv run
… weave-hook <phase>`` command. The Claude Code harness fires hooks with the
same stripped, non-login PATH that made the MCP server silently fail to launch
(#52) — so a bare ``uv`` there died invisibly, taking the SessionStart context
payload and the RLVR context-served substrate with it. This launcher resolves
uv robustly (PATH → ``~/.local/bin/uv`` → ``$UV_INSTALL_DIR/uv``), passes the
hook phase through as ``"$@"``, and fails LOUDLY when no uv exists.

Test seam mirrors ``test_mcp_launcher.py``: the script invoked as a subprocess
with a fully controlled environment and a cwd that is NOT the repo root (the
launcher must self-locate). A fake ``uv`` echoes its own path and argv so tests
assert both which binary resolved and exactly what it was asked to run —
including that the phase argument survives.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "bin" / "weave-hook-launch"

# The launcher resolves its project root physically (`cd -P`), so symlinked
# checkouts (dev-link route) compare against the realpath.
PROJECT_ROOT_PHYSICAL = Path(os.path.realpath(REPO_ROOT))

# The phase argument a real hooks.json command passes; the launcher must
# forward it verbatim to `weave-hook`.
PHASE = "session_start"

# What the launcher must hand to uv — the fire-time-resolution shape, with the
# root made absolute and the phase passed through.
EXPECTED_UV_ARGV = (
    f"run --project {PROJECT_ROOT_PHYSICAL} --extra mcp weave-hook {PHASE}"
)

# The actionable one-line failure contract.
EXPECTED_ERROR_LINE = (
    "weave-hook-launch: uv not found (checked PATH, ~/.local/bin/uv, "
    "$UV_INSTALL_DIR/uv); install uv from "
    "https://docs.astral.sh/uv/getting-started/installation/ "
    "or add it to PATH"
)

# Fake uv: prints "<its own path> <argv>" on one line so tests can assert
# BOTH which binary was resolved and what it was asked to run.
FAKE_UV_BODY = '#!/bin/sh\necho "$0 $@"\n'


def _make_fake_uv(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fake = directory / "uv"
    fake.write_text(FAKE_UV_BODY, encoding="utf-8")
    fake.chmod(0o755)
    return fake


def _run_launcher(
    env: dict[str, str], cwd: Path, *args: str
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(LAUNCHER), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestUvResolution:
    def test_uv_on_path_execs_uv_run_with_phase_passed_through(self, tmp_path):
        path_dir = tmp_path / "fakepath"
        fake = _make_fake_uv(path_dir)
        home = tmp_path / "home"
        home.mkdir()
        cwd = tmp_path / "elsewhere"
        cwd.mkdir()

        result = _run_launcher(
            {"PATH": str(path_dir), "HOME": str(home)}, cwd, PHASE
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"{fake} {EXPECTED_UV_ARGV}"

    def test_uv_resolved_from_home_local_bin_when_not_on_path(self, tmp_path):
        empty_path_dir = tmp_path / "emptypath"
        empty_path_dir.mkdir()
        home = tmp_path / "home"
        fake = _make_fake_uv(home / ".local" / "bin")
        cwd = tmp_path / "elsewhere"
        cwd.mkdir()

        result = _run_launcher(
            {"PATH": str(empty_path_dir), "HOME": str(home)}, cwd, PHASE
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"{fake} {EXPECTED_UV_ARGV}"

    def test_uv_resolved_from_uv_install_dir_as_last_resort(self, tmp_path):
        empty_path_dir = tmp_path / "emptypath"
        empty_path_dir.mkdir()
        home = tmp_path / "home"
        home.mkdir()  # no ~/.local/bin/uv
        install_dir = tmp_path / "custom-uv-install"
        fake = _make_fake_uv(install_dir)
        cwd = tmp_path / "elsewhere"
        cwd.mkdir()

        result = _run_launcher(
            {
                "PATH": str(empty_path_dir),
                "HOME": str(home),
                "UV_INSTALL_DIR": str(install_dir),
            },
            cwd,
            PHASE,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"{fake} {EXPECTED_UV_ARGV}"

    def test_path_wins_over_home_local_bin(self, tmp_path):
        path_dir = tmp_path / "fakepath"
        path_fake = _make_fake_uv(path_dir)
        home = tmp_path / "home"
        _make_fake_uv(home / ".local" / "bin")
        cwd = tmp_path / "elsewhere"
        cwd.mkdir()

        result = _run_launcher(
            {"PATH": str(path_dir), "HOME": str(home)}, cwd, PHASE
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"{path_fake} {EXPECTED_UV_ARGV}"


class TestLoudFailure:
    def test_missing_uv_exits_nonzero_with_actionable_one_line_stderr(
        self, tmp_path
    ):
        empty_path_dir = tmp_path / "emptypath"
        empty_path_dir.mkdir()
        home = tmp_path / "home"
        home.mkdir()  # no ~/.local/bin/uv, no UV_INSTALL_DIR
        cwd = tmp_path / "elsewhere"
        cwd.mkdir()

        result = _run_launcher(
            {"PATH": str(empty_path_dir), "HOME": str(home)}, cwd, PHASE
        )

        assert result.returncode == 127
        assert result.stdout == ""
        assert result.stderr.strip().splitlines() == [EXPECTED_ERROR_LINE]


class TestScriptHygiene:
    def test_launcher_is_committed_and_executable(self):
        assert LAUNCHER.exists(), "bin/weave-hook-launch must be committed"
        assert os.access(LAUNCHER, os.X_OK), "launcher must carry the exec bit"

    def test_launcher_is_posix_sh(self):
        first_line = LAUNCHER.read_text(encoding="utf-8").splitlines()[0]
        assert first_line == "#!/bin/sh"

    def test_launcher_matches_mcp_launcher_resolution_ladder(self):
        """The hook and MCP launchers must resolve uv identically — one
        resolution story for every launch surface (#47/#50/#52). Pin the
        shared ladder so the two can't silently drift apart."""
        hook_src = LAUNCHER.read_text(encoding="utf-8")
        mcp_src = (REPO_ROOT / "bin" / "weave-mcp-launch").read_text(
            encoding="utf-8"
        )
        for probe in (
            'command -v uv >/dev/null 2>&1',
            '[ -x "${HOME:-}/.local/bin/uv" ]',
            '[ -n "${UV_INSTALL_DIR:-}" ] && [ -x "$UV_INSTALL_DIR/uv" ]',
            'root=$(CDPATH=\'\' cd -P -- "$script_dir/.." && pwd)',
        ):
            assert probe in hook_src, f"hook launcher missing ladder line: {probe}"
            assert probe in mcp_src, f"mcp launcher missing ladder line: {probe}"
