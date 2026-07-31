"""Behavioral tests for ``bin/weave-mcp-launch`` — the portable MCP launcher.

The committed ``.mcp.json`` and ``.claude-plugin/plugin.json`` used to launch
the MCP server as a bare ``uv`` command. When the Claude Code harness PATH
omits ``~/.local/bin`` (common for non-login shells), the server silently
failed to launch — no error, the ``weave_*`` tools just never appeared
(issue #52). The launcher resolves uv robustly (PATH → ``~/.local/bin/uv`` →
``$UV_INSTALL_DIR/uv``) and fails LOUDLY when none exists.

Test seam: the script itself, invoked as a subprocess with a fully
controlled environment (PATH / HOME / UV_INSTALL_DIR) and a cwd that is
NOT the repo root (the launcher must self-locate, never rely on cwd).
A fake ``uv`` executable — a tiny sh script that echoes its own path and
argv — observes what the launcher execs. No shell mocking, no real server.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "bin" / "weave-mcp-launch"

# The launcher resolves its project root physically (`cd -P`), so symlinked
# checkouts (dev-link route) compare against the realpath.
PROJECT_ROOT_PHYSICAL = Path(os.path.realpath(REPO_ROOT))

# What the launcher must hand to uv — the same invocation shape as the old
# `.mcp.json` entry and `weave install`, with the root made absolute.
EXPECTED_UV_ARGV = f"run --project {PROJECT_ROOT_PHYSICAL} --extra mcp weave-mcp"

# The actionable one-line failure contract (acceptance criterion 1).
EXPECTED_ERROR_LINE = (
    "weave-mcp-launch: uv not found (checked PATH, ~/.local/bin/uv, "
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


def _run_launcher(env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(LAUNCHER)],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestUvResolution:
    def test_uv_on_path_execs_uv_run_with_project_args(self, tmp_path):
        path_dir = tmp_path / "fakepath"
        fake = _make_fake_uv(path_dir)
        home = tmp_path / "home"
        home.mkdir()
        cwd = tmp_path / "elsewhere"
        cwd.mkdir()

        result = _run_launcher(
            env={"PATH": str(path_dir), "HOME": str(home)}, cwd=cwd
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
            env={"PATH": str(empty_path_dir), "HOME": str(home)}, cwd=cwd
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
            env={
                "PATH": str(empty_path_dir),
                "HOME": str(home),
                "UV_INSTALL_DIR": str(install_dir),
            },
            cwd=cwd,
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
            env={"PATH": str(path_dir), "HOME": str(home)}, cwd=cwd
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
            env={"PATH": str(empty_path_dir), "HOME": str(home)}, cwd=cwd
        )

        assert result.returncode == 127
        assert result.stdout == ""
        assert result.stderr.strip().splitlines() == [EXPECTED_ERROR_LINE]


class TestScriptHygiene:
    def test_launcher_is_committed_and_executable(self):
        assert LAUNCHER.exists(), "bin/weave-mcp-launch must be committed"
        assert os.access(LAUNCHER, os.X_OK), "launcher must carry the exec bit"

    def test_launcher_is_posix_sh(self):
        first_line = LAUNCHER.read_text(encoding="utf-8").splitlines()[0]
        assert first_line == "#!/bin/sh"
