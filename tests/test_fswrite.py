"""Contract tests for ``core.fswrite`` — the one write-safety module (#191 AC5).

Harness config files are a user's files; every writer that touches one goes
through these primitives. Expected behaviours are written from the guarantees
the callers rely on, not from the implementation.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from thinkweave.core import fswrite


class TestAtomicWriteText:
    def test_writes_and_creates_parents(self, tmp_path: Path):
        target = tmp_path / "a" / "b.json"
        fswrite.atomic_write_text(target, "x\n")
        assert target.read_text(encoding="utf-8") == "x\n"

    def test_no_tmp_droppings_left_behind(self, tmp_path: Path):
        target = tmp_path / "c.toml"
        fswrite.atomic_write_text(target, "x\n")
        assert [p.name for p in tmp_path.iterdir()] == ["c.toml"]

    def test_preserves_the_original_mode(self, tmp_path: Path):
        # Codex creates config.toml 0600 and it can carry env secrets;
        # falling back to the umask default would quietly widen it.
        target = tmp_path / "config.toml"
        target.write_text("old\n", encoding="utf-8")
        os.chmod(target, 0o600)
        fswrite.atomic_write_text(target, "new\n")
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_backup_keeps_the_previous_bytes(self, tmp_path: Path):
        target = tmp_path / "settings.json"
        target.write_text("old\n", encoding="utf-8")
        fswrite.atomic_write_text(target, "new\n", backup=True)
        assert target.with_suffix(".json.bak").read_text(encoding="utf-8") == "old\n"
        assert target.read_text(encoding="utf-8") == "new\n"

    def test_backup_of_a_fresh_file_writes_no_bak(self, tmp_path: Path):
        target = tmp_path / "fresh.json"
        fswrite.atomic_write_text(target, "new\n", backup=True)
        assert not target.with_suffix(".json.bak").exists()


class TestReplaceBetween:
    S, E = "<!-- s -->", "<!-- e -->"

    def test_replaces_only_the_sentinel_span(self):
        doc = f"head\n{self.S}\nold\n{self.E}\ntail\n"
        out = fswrite.replace_between(doc, self.S, self.E, f"{self.S}\nnew\n{self.E}")
        assert out == f"head\n{self.S}\nnew\n{self.E}\ntail\n"

    def test_append_mode_adds_a_fresh_block(self):
        out = fswrite.replace_between(
            "body\n", self.S, self.E, f"{self.S}x{self.E}", on_missing="append"
        )
        assert out.startswith("body\n") and out.rstrip().endswith(self.E)

    def test_error_mode_refuses_an_unmarked_file(self):
        # Generator-fingerprint gate: a generated block only ever overwrites
        # a span its own sentinels mark — never someone's unmarked prose.
        with pytest.raises(ValueError):
            fswrite.replace_between("prose\n", self.S, self.E, "x")
