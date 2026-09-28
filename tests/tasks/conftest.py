"""Shared temp-vault fixture and stdin-driven hook runner for the seam tests."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from thinkweave.core.config import Config


@pytest.fixture()
def cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Config:
    """An initialized temp vault every ``load_config()`` call resolves to."""
    config = Config(vault_root=tmp_path / "vault")
    (config.vault_root / "config").mkdir(parents=True)
    (config.vault_root / "config" / "sources.yaml").write_text(
        "sources: []\n", encoding="utf-8"
    )
    monkeypatch.setattr("thinkweave.core.config.load_config", lambda: config)
    # The suite may itself run under a harness that exports a session id;
    # these tests exercise the explicit-key and no-session paths.
    for env in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "PI_SESSION_ID"):
        monkeypatch.delenv(env, raising=False)
    return config


def run_hook(monkeypatch: pytest.MonkeyPatch, phase: str, payload: dict) -> dict:
    """Drive ``handler.main()`` exactly as the harness does: phase on argv,
    the synthetic payload on stdin; returns the parsed hook reply."""
    from thinkweave.surfaces.hooks import handler

    monkeypatch.setattr("sys.argv", ["weave-hook", phase])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    handler.main()
    return json.loads(out.getvalue()) if out.getvalue().strip() else {}
