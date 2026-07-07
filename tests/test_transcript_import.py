"""Characterization + adoption tests for the transcript importer.

Pins the on-disk frontmatter shape (source_type / title / url / authors) and
the derived-title behaviour so the swap to ``build_source_frontmatter`` and
the shared indexing policy stays behaviour-preserving. Also asserts the note
is searchable after import (the imported note lands in the index).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from thinkweave.acquisition.importers.transcript import import_transcript
from thinkweave.core.config import Config
from thinkweave.core.vault import VaultManager, parse_frontmatter


def _make_config(tmp_path: Path) -> Config:
    return Config(vault_root=tmp_path / "vault")


def _write_text(tmp_path: Path, text: str, name: str = "doc.txt") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_explicit_metadata_frontmatter(tmp_path: Path):
    config = _make_config(tmp_path)
    VaultManager(config=config).ensure_dirs()
    src = _write_text(tmp_path, "Body of the article goes here.")

    path = import_transcript(
        config=config,
        file_path=src,
        source_type="article",
        title="A Great Article",
        url="https://example.com/a",
        authors=["Ada Lovelace"],
    )
    fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["source_type"] == "article"
    assert fm["title"] == "A Great Article"
    assert fm["url"] == "https://example.com/a"
    assert fm["authors"] == ["Ada Lovelace"]
    assert "Body of the article" in body


def test_authors_default_empty_list_when_absent(tmp_path: Path):
    config = _make_config(tmp_path)
    VaultManager(config=config).ensure_dirs()
    src = _write_text(tmp_path, "No author here.")

    path = import_transcript(
        config=config, file_path=src, source_type="article", title="Untitled"
    )
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    # Empty list is kept as `authors: []`; empty-string url is dropped by
    # render_frontmatter (both hold identically before and after the swap).
    assert fm["authors"] == []
    assert "url" not in fm


def test_title_derived_from_markdown_heading(tmp_path: Path):
    config = _make_config(tmp_path)
    VaultManager(config=config).ensure_dirs()
    src = _write_text(tmp_path, "# Derived Heading Title\n\nSome body text.")

    path = import_transcript(config=config, file_path=src, source_type="article")
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["title"] == "Derived Heading Title"


def test_title_derived_from_first_line_truncated(tmp_path: Path):
    config = _make_config(tmp_path)
    VaultManager(config=config).ensure_dirs()
    long_first = "x" * 80
    src = _write_text(tmp_path, f"{long_first}\n\nrest")

    path = import_transcript(config=config, file_path=src, source_type="article")
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["title"] == "x" * 60 + "..."


def test_imported_note_is_indexed(tmp_path: Path):
    config = _make_config(tmp_path)
    VaultManager(config=config).ensure_dirs()
    src = _write_text(tmp_path, "Searchable transcript content about widgets.")

    path = import_transcript(
        config=config, file_path=src, source_type="article", title="Widgets"
    )
    note_id = VaultManager(config=config).read_note(path).id

    conn = sqlite3.connect(str(config.index_db))
    try:
        row = conn.execute("SELECT title FROM notes WHERE id = ?", (note_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "Widgets"
