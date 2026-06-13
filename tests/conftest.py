"""Suite-wide fixtures.

The plugin-route detector (``core.plugin_route.plugin_namespace``) reads
``~/.claude/plugins/installed_plugins.json`` — real machine state. Point it
at a nonexistent path for every test so rendered commands (cron lines, flow
invocations) don't depend on whether the dev box happens to have the plugin
installed. Tests that exercise the plugin route override explicitly via the
``manifest=`` kwarg or by patching ``plugin_namespace`` at the import site.
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
