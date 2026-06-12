"""Tests for ``core.plugin_route`` — plugin-route detection + skill-token
namespacing.

Claude Code registers plugin-shipped commands/agents namespaced
(``/personal-mem:dream``) with no bare-name aliasing (verified empirically
2026-06-12), so deterministic renderers (cron block, flow stages) rewrite
the skill token when — and only when — the plugin route is active.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_mem.core.plugin_route import namespace_prompt, plugin_namespace


# --------------------------------------------------------------------------- #
# namespace_prompt
# --------------------------------------------------------------------------- #


class TestNamespacePrompt:
    @pytest.mark.parametrize(
        ("arg", "expected"),
        [
            ("/dream", "/personal-mem:dream"),
            ("/dream --essence-cap 0", "/personal-mem:dream --essence-cap 0"),
            (
                "/discover --strategy rss_poll --source-type news",
                "/personal-mem:discover --strategy rss_poll --source-type news",
            ),
        ],
    )
    def test_bare_skill_tokens_get_prefixed(self, arg: str, expected: str):
        assert namespace_prompt(arg, "personal-mem") == expected

    @pytest.mark.parametrize(
        "arg",
        [
            "/personal-mem:dream",          # already namespaced
            "/home/user/notes.md",          # filesystem path, not a skill
            "summarize this please",        # plain prompt, no skill token
            "run /dream for me",            # skill token not in head position
            "/Dream",                       # not the kebab register
        ],
    )
    def test_non_bare_tokens_pass_through(self, arg: str):
        assert namespace_prompt(arg, "personal-mem") == arg

    def test_none_namespace_is_noop(self):
        assert namespace_prompt("/dream", None) == "/dream"


# --------------------------------------------------------------------------- #
# plugin_namespace
# --------------------------------------------------------------------------- #


def _write_manifest(path: Path, plugins: dict) -> Path:
    path.write_text(
        json.dumps({"version": 2, "plugins": plugins}), encoding="utf-8"
    )
    return path


class TestPluginNamespace:
    def test_detects_marketplace_key(self, tmp_path: Path):
        m = _write_manifest(
            tmp_path / "installed_plugins.json",
            {"personal-mem@marekpal97": [{"scope": "user"}]},
        )
        assert plugin_namespace(manifest=m) == "personal-mem"

    def test_detects_bare_key(self, tmp_path: Path):
        m = _write_manifest(
            tmp_path / "installed_plugins.json", {"personal-mem": []}
        )
        assert plugin_namespace(manifest=m) == "personal-mem"

    def test_other_plugins_do_not_match(self, tmp_path: Path):
        m = _write_manifest(
            tmp_path / "installed_plugins.json",
            {"claude-mem@thedotmack": [], "linear@official": []},
        )
        assert plugin_namespace(manifest=m) is None

    def test_missing_file_means_not_plugin_route(self, tmp_path: Path):
        assert plugin_namespace(manifest=tmp_path / "absent.json") is None

    def test_corrupt_file_means_not_plugin_route(self, tmp_path: Path):
        bad = tmp_path / "installed_plugins.json"
        bad.write_text("{not json", encoding="utf-8")
        assert plugin_namespace(manifest=bad) is None
