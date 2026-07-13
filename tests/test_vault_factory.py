"""Tests for the shared ``vault_factory`` builder fixture (conftest.py).

These exercise the fixture's own affordances — the test-vault lifecycle it
owns on behalf of every other suite. If this suite is green, the builder
covers: bare construction (vault + Config + VaultManager + ensure_dirs),
seeded notes/themes, and the indexed state.
"""

from __future__ import annotations

import sqlite3

from thinkweave.core.schemas import NoteType


def test_bare_factory_builds_ready_vault(vault_factory):
    tv = vault_factory()
    # The ritual chain is hidden: ensure_dirs ran, config points at the vault.
    assert tv.config.vault_root.exists()
    assert (tv.config.vault_root / "sources").exists()
    assert (tv.config.vault_root / ".weave").exists()
    assert tv.vault.config is tv.config


def test_with_note_seeds_a_note(vault_factory):
    tv = vault_factory().with_note("Hello", extra_frontmatter={"concepts": ["python"]})
    notes = list(tv.config.vault_root.rglob("*.md"))
    assert any("Hello" in p.read_text(encoding="utf-8") for p in notes)


def test_notes_kwarg_seeds_and_chains(vault_factory):
    # A string is a title; a dict is create_note kwargs.
    tv = vault_factory(
        notes=[
            "Plain title",
            {"title": "Tagged", "tags": ["todo"]},
        ]
    )
    bodies = [p.read_text(encoding="utf-8") for p in tv.config.vault_root.rglob("*.md")]
    joined = "\n".join(bodies)
    assert "Plain title" in joined
    assert "Tagged" in joined


def test_with_theme_creates_theme_note(vault_factory):
    tv = vault_factory().with_theme("A Theme")
    theme_files = list((tv.config.vault_root / "themes").rglob("*.md"))
    assert theme_files, "expected a theme note under themes/"


def test_indexed_populates_sqlite(vault_factory):
    tv = vault_factory(notes=["Indexed note"]).indexed()
    assert tv.config.index_db.exists()
    db = sqlite3.connect(str(tv.config.index_db))
    try:
        (count,) = db.execute("SELECT COUNT(*) FROM notes").fetchone()
    finally:
        db.close()
    assert count >= 1


def test_indexed_kwarg_equivalent_to_method(vault_factory):
    tv = vault_factory(notes=["N"], indexed=True)
    assert tv.config.index_db.exists()


def test_config_kwargs_reach_config(vault_factory):
    # Escape hatch for suites that tweak config knobs.
    tv = vault_factory(default_project="proj_x")
    assert tv.config.default_project == "proj_x"


def test_derived_fixtures_share_one_vault(config, vault, indexer):
    # The conftest lifecycle fixtures (config/vault/indexer) all resolve to
    # the same underlying vault built by vault_factory.
    assert vault.config is config
    vault.create_note(note_type=NoteType.NOTE, title="Shared")
    indexer.rebuild()
    (count,) = indexer.db.execute("SELECT COUNT(*) FROM notes").fetchone()
    assert count == 1
