"""Unit tests for the shared importer indexing policy.

Locks the contract of :func:`index_imported_notes`: end-of-run bulk indexing
of the exact set of written paths, and a no-op for an empty path list (a fully
idempotent re-run).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from thinkweave.acquisition.importers.common import index_imported_notes
from thinkweave.core.config import Config
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager


def _make_config(tmp_path: Path) -> Config:
    return Config(vault_root=tmp_path / "vault")


def test_indexes_written_note_into_the_index(tmp_path: Path):
    config = _make_config(tmp_path)
    vm = VaultManager(config=config)
    vm.ensure_dirs()

    path = vm.create_note(
        NoteType.SOURCE,
        title="Attention Is All You Need",
        body="A landmark transformer paper.",
        extra_frontmatter={"source_type": "paper"},
    )
    note_id = vm.read_note(path).id

    stats = index_imported_notes(config, [path])
    assert stats["indexed"] == 1

    conn = sqlite3.connect(str(config.index_db))
    try:
        row = conn.execute(
            "SELECT title FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "Attention Is All You Need"


def test_empty_paths_is_a_noop(tmp_path: Path):
    config = _make_config(tmp_path)
    vm = VaultManager(config=config)
    vm.ensure_dirs()

    stats = index_imported_notes(config, [])
    assert stats == {"indexed": 0, "skipped": 0, "removed": 0, "edges": 0}
