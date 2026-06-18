"""Tests for the vault/config/ canonical-location loader.

Verifies ``resolve_config_file`` prefers the canonical location, raises
:class:`LegacyConfigLocationError` when a config still sits at the
deprecated ``vault/.weave/<filename>`` path (the legacy fallback was
retired in Phase 3.1B, 2026-06-05), and returns the canonical path when
neither location exists (so writes commit forward).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thinkweave.core.config import (
    LegacyConfigLocationError,
    resolve_config_file,
)


def test_prefers_canonical_when_both_exist(tmp_path: Path):
    canonical = tmp_path / "config" / "sources.yaml"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("canonical: true\n", encoding="utf-8")
    legacy = tmp_path / ".weave" / "sources.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy: true\n", encoding="utf-8")

    resolved = resolve_config_file(tmp_path, "sources.yaml")
    assert resolved == canonical


def test_raises_when_only_legacy_exists(tmp_path: Path):
    legacy = tmp_path / ".weave" / "sources.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy: true\n", encoding="utf-8")

    with pytest.raises(LegacyConfigLocationError) as exc:
        resolve_config_file(tmp_path, "sources.yaml")
    msg = str(exc.value)
    assert "sources.yaml" in msg
    assert "vault/config/sources.yaml" in msg


def test_returns_canonical_when_neither_exists(tmp_path: Path):
    """Writes commit forward: callers can mkdir+write at the returned path."""
    resolved = resolve_config_file(tmp_path, "ontology.yaml")
    assert resolved == tmp_path / "config" / "ontology.yaml"
    assert not resolved.exists()  # neither file exists yet
