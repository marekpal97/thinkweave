"""Suite-wide fixtures.

The plugin-route detector (``core.plugin_route.plugin_namespace``) reads real
machine state — ``~/.claude/plugins/installed_plugins.json`` (marketplace) and
the ``~/.claude/skills/thinkweave`` symlink (dev-link). Point both probes at
nonexistent paths for every test so rendered commands (cron lines, flow
invocations) don't depend on whether the dev box happens to have the plugin
installed or dev-linked. Tests that exercise the plugin route override
explicitly via the ``manifest=`` / ``dev_link=`` kwargs or by patching
``plugin_namespace`` at the import site.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thinkweave.core import plugin_route


@pytest.fixture(autouse=True)
def _no_plugin_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        plugin_route, "_INSTALLED_PLUGINS", tmp_path / "absent-installed_plugins.json"
    )
    monkeypatch.setattr(
        plugin_route, "_DEV_LINK", tmp_path / "absent-skills-thinkweave"
    )
