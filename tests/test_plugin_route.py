"""Tests for ``core.plugin_route`` — plugin-route detection + skill-token
namespacing.

Claude Code registers plugin-shipped commands/agents namespaced
(``/thinkweave:dream``) with no bare-name aliasing (verified empirically
2026-06-12), so deterministic renderers (cron block, flow stages) rewrite
the skill token when — and only when — the plugin route is active.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thinkweave.core.plugin_route import namespace_prompt, plugin_namespace


# --------------------------------------------------------------------------- #
# namespace_prompt
# --------------------------------------------------------------------------- #


class TestNamespacePrompt:
    @pytest.mark.parametrize(
        ("arg", "expected"),
        [
            ("/dream", "/thinkweave:dream"),
            ("/dream --essence-cap 0", "/thinkweave:dream --essence-cap 0"),
            (
                "/discover --strategy rss_poll --source-type news",
                "/thinkweave:discover --strategy rss_poll --source-type news",
            ),
        ],
    )
    def test_bare_skill_tokens_get_prefixed(self, arg: str, expected: str):
        assert namespace_prompt(arg, "thinkweave") == expected

    @pytest.mark.parametrize(
        "arg",
        [
            "/thinkweave:dream",          # already namespaced
            "/home/user/notes.md",          # filesystem path, not a skill
            "summarize this please",        # plain prompt, no skill token
            "run /dream for me",            # skill token not in head position
            "/Dream",                       # not the kebab register
        ],
    )
    def test_non_bare_tokens_pass_through(self, arg: str):
        assert namespace_prompt(arg, "thinkweave") == arg

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
            {"thinkweave@marekpal97": [{"scope": "user"}]},
        )
        assert plugin_namespace(manifest=m) == "thinkweave"

    def test_detects_bare_key(self, tmp_path: Path):
        m = _write_manifest(
            tmp_path / "installed_plugins.json", {"thinkweave": []}
        )
        assert plugin_namespace(manifest=m) == "thinkweave"

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


class TestDevLinkRoute:
    """`weave dev-link` symlinks the checkout into ~/.claude/skills/thinkweave;
    Claude Code auto-loads it as the namespaced `thinkweave@skills-dir` plugin
    *without* an installed_plugins.json entry, so the symlink is the only
    signal."""

    def test_symlink_is_detected_without_manifest(self, tmp_path: Path):
        target = tmp_path / "checkout"
        target.mkdir()
        link = tmp_path / "thinkweave"
        link.symlink_to(target)
        assert (
            plugin_namespace(manifest=tmp_path / "absent.json", dev_link=link)
            == "thinkweave"
        )

    def test_no_symlink_no_manifest_is_bare(self, tmp_path: Path):
        assert (
            plugin_namespace(
                manifest=tmp_path / "absent.json",
                dev_link=tmp_path / "absent-link",
            )
            is None
        )

    def test_real_dir_is_not_a_dev_link(self, tmp_path: Path):
        # A real (non-symlink) directory at the skills path is some other
        # plugin's dir, not our dev-link — must not trigger namespacing.
        real = tmp_path / "thinkweave"
        real.mkdir()
        assert (
            plugin_namespace(manifest=tmp_path / "absent.json", dev_link=real)
            is None
        )
