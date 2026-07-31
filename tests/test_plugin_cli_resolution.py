"""Guard: plugin-route CLI resolution.

On the plugin route (marketplace or `weave dev-link`), the `weave` console
script is NOT on the user's PATH and `${CLAUDE_PLUGIN_ROOT}` is NOT exported to
a skill/agent's Bash calls. So skills and agents must invoke the CLI as bare
`weave …`, resolved via the plugin's `bin/weave` shim (Claude Code adds an
enabled plugin's `bin/` to the Bash tool's PATH). `uv run weave …` only resolves
from the repo cwd and breaks for every plugin user — these tests stop that
regression from coming back.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_no_skill_or_agent_uses_uv_run_weave():
    offenders: list[str] = []
    for sub in ("commands", "agents"):
        for md_file in (REPO_ROOT / sub).rglob("*.md"):
            if "uv run weave " in md_file.read_text(encoding="utf-8"):
                offenders.append(str(md_file.relative_to(REPO_ROOT)))
    assert not offenders, (
        "these skills/agents call `uv run weave` — it fails on the plugin route "
        f"(CLI not on PATH from a non-repo cwd). Use bare `weave`: {sorted(offenders)}"
    )


def test_bin_weave_shim_present_and_executable():
    shim = REPO_ROOT / "bin" / "weave"
    assert shim.exists(), (
        "bin/weave shim missing — plugin skills/agents can't resolve the CLI"
    )
    assert os.access(shim, os.X_OK), "bin/weave is present but not executable"


def test_bin_weave_shim_runs_via_plugin_root_not_cwd():
    """The shim must resolve its own location (so it works from any cwd), not
    assume the CLI is in PATH or cwd."""
    body = (REPO_ROOT / "bin" / "weave").read_text(encoding="utf-8")
    assert "BASH_SOURCE" in body or "$0" in body, (
        "bin/weave must resolve its own path to find the plugin root"
    )
    assert (
        'exec "$uv_bin" run --project "$root" --extra mcp weave "$@"' in body
    ), "bin/weave must run the bundled CLI via `uv run --project <root>`"


def test_bin_weave_shim_resolves_uv_like_the_mcp_launcher():
    """#47 rides on #52's resolution story: the shim must resolve uv via the
    same ladder as bin/weave-mcp-launch (PATH -> ~/.local/bin/uv ->
    $UV_INSTALL_DIR/uv) and fail loudly when uv is genuinely missing —
    a bare `uv` would silently fail on harness shells that lack
    ~/.local/bin on PATH, breaking /wrap's finalize step invisibly."""
    body = (REPO_ROOT / "bin" / "weave").read_text(encoding="utf-8")
    assert "$HOME/.local/bin/uv" in body, (
        "bin/weave must fall back to ~/.local/bin/uv when uv is off PATH"
    )
    assert "UV_INSTALL_DIR" in body, (
        "bin/weave must honour $UV_INSTALL_DIR as the last resolution rung"
    )
    assert "exit 127" in body, (
        "bin/weave must fail loudly (not silently) when uv cannot be resolved"
    )
