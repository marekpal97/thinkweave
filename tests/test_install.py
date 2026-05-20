"""Regression tests for ``mem install`` and the project-scope ``.mcp.json``.

The three MCP-registration paths — project-scope ``.mcp.json``, machine-
scope ``~/.claude.json`` (written by ``mem install``), and the plugin
manifests under ``.claude-plugin/`` — must all produce equivalent server
entries so that Claude Code's MCP launcher sees the same invocation
regardless of how the user installed the package.

"Equivalent" here means: same ``args`` list, same ``env`` keys, and
command resolves to ``uv`` (the absolute path baked into
``~/.claude.json`` and the bare ``"uv"`` used in checked-in manifests
are both legitimate forms).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_mem.surfaces.cli.install import (
    _build_server_entry,
    _detect_project_root,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT_MCP_JSON = REPO_ROOT / ".mcp.json"
PLUGIN_MANIFEST_ROOT = REPO_ROOT / ".claude-plugin" / "plugin.json"
PLUGIN_MANIFEST_NESTED = (
    REPO_ROOT / ".claude" / "plugins" / "personal-mem" / ".claude-plugin" / "plugin.json"
)


def _command_basename(cmd: str) -> str:
    return Path(cmd).name


def _normalise_args_for_compare(args: list[str], placeholder_for_project: str) -> list[str]:
    """Replace the ``--project <value>`` slot with a sentinel so absolute,
    relative, and ``${CLAUDE_PLUGIN_ROOT}`` forms compare equal.
    """
    out: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--project" and i + 1 < len(args):
            out.extend(["--project", placeholder_for_project])
            i += 2
            continue
        out.append(args[i])
        i += 1
    return out


def _entry_from_install() -> dict:
    project_root = _detect_project_root()
    return _build_server_entry(project_root, vault_root=None)


def _entry_from_mcp_json() -> dict:
    data = json.loads(PROJECT_MCP_JSON.read_text(encoding="utf-8"))
    return data["mcpServers"]["personal-mem"]


def _entry_from_plugin_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "mcpServers" in data, f"{path} must declare mcpServers inline"
    return data["mcpServers"]["personal-mem"]


class TestMcpInvocationConsistency:
    """All three scopes resolve to the same launcher + args shape."""

    def test_mcp_json_uses_uv_run_shape(self):
        entry = _entry_from_mcp_json()
        assert _command_basename(entry["command"]) == "uv"
        assert entry["args"][:2] == ["run", "--project"]
        assert "--extra" in entry["args"]
        assert "mcp" in entry["args"]
        assert entry["args"][-1] == "mem-mcp"

    def test_mem_install_uses_uv_run_shape(self):
        entry = _entry_from_install()
        assert _command_basename(entry["command"]) == "uv"
        assert entry["args"][:2] == ["run", "--project"]
        assert "--extra" in entry["args"]
        assert "mcp" in entry["args"]
        assert entry["args"][-1] == "mem-mcp"

    def test_plugin_manifest_root_uses_uv_run_shape(self):
        entry = _entry_from_plugin_manifest(PLUGIN_MANIFEST_ROOT)
        assert _command_basename(entry["command"]) == "uv"
        assert entry["args"][:2] == ["run", "--project"]
        assert "--extra" in entry["args"]
        assert "mcp" in entry["args"]
        assert entry["args"][-1] == "mem-mcp"

    def test_plugin_manifest_nested_uses_uv_run_shape(self):
        entry = _entry_from_plugin_manifest(PLUGIN_MANIFEST_NESTED)
        assert _command_basename(entry["command"]) == "uv"
        assert entry["args"][:2] == ["run", "--project"]
        assert "--extra" in entry["args"]
        assert "mcp" in entry["args"]
        assert entry["args"][-1] == "mem-mcp"

    def test_all_scopes_normalise_to_same_args_shape(self):
        """Once the per-scope project path is replaced with a sentinel,
        every config produces exactly the same args list and env keys."""
        sentinel = "<PROJECT_PATH>"
        install_entry = _entry_from_install()
        mcp_entry = _entry_from_mcp_json()
        plugin_root_entry = _entry_from_plugin_manifest(PLUGIN_MANIFEST_ROOT)
        plugin_nested_entry = _entry_from_plugin_manifest(PLUGIN_MANIFEST_NESTED)

        norm_install = _normalise_args_for_compare(install_entry["args"], sentinel)
        norm_mcp = _normalise_args_for_compare(mcp_entry["args"], sentinel)
        norm_root = _normalise_args_for_compare(plugin_root_entry["args"], sentinel)
        norm_nested = _normalise_args_for_compare(plugin_nested_entry["args"], sentinel)

        assert norm_install == norm_mcp == norm_root == norm_nested, (
            f"args shape diverged:\n"
            f"  install={norm_install}\n"
            f"  mcp.json={norm_mcp}\n"
            f"  plugin/root={norm_root}\n"
            f"  plugin/nested={norm_nested}"
        )

        # env keys (not values — install may inject PERSONAL_MEM_VAULT)
        for entry in (mcp_entry, plugin_root_entry, plugin_nested_entry):
            assert entry.get("env", {}) == {}, (
                f"checked-in manifest must not bake env vars: {entry}"
            )
        # install with vault_root=None matches
        assert install_entry.get("env", {}) == {}


class TestMcpJsonSyntax:
    """Basic sanity: every checked-in MCP-bearing JSON file is valid JSON
    and parses to a dict with the expected top-level shape."""

    @pytest.mark.parametrize(
        "path",
        [PROJECT_MCP_JSON, PLUGIN_MANIFEST_ROOT, PLUGIN_MANIFEST_NESTED],
        ids=lambda p: str(p.relative_to(REPO_ROOT)),
    )
    def test_file_is_valid_json(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "mcpServers" in data
        assert "personal-mem" in data["mcpServers"]
