"""Tests for the note-format mechanic — per-source brief skeletons as vault data.

Design (2026-06-13): the brief skeleton each research writer follows is a plain
markdown file. Shipped defaults live in ``vault_templates/note_formats/`` and are
seeded into ``vault/config/note_formats/`` at ``weave init``; the writers ``Read``
that vault copy directly and the user edits it in place. No CLI, no resolver —
just a file in the vault.
"""

from __future__ import annotations

from pathlib import Path

from thinkweave.surfaces.cli.util import _seed_vault_templates

EXPECTED_KEYS = {"paper", "repo", "article", "news", "newsletter", "youtube", "podcast"}


def _shipped_dir() -> Path:
    import thinkweave

    return Path(thinkweave.__file__).resolve().parent / "vault_templates" / "note_formats"


def test_every_writer_family_ships_a_default():
    shipped = {p.stem for p in _shipped_dir().glob("*.md")}
    assert shipped == EXPECTED_KEYS


def test_shipped_defaults_are_nonempty_markdown():
    for key in EXPECTED_KEYS:
        text = (_shipped_dir() / f"{key}.md").read_text(encoding="utf-8")
        assert "## " in text, f"{key} default has no markdown sections"
        # No dangling reference to the removed CLI.
        assert "weave note-format" not in text, f"{key} still mentions the dropped CLI"


def test_seed_copies_note_formats_into_vault(tmp_path: Path):
    _seed_vault_templates(tmp_path)
    nf_dir = tmp_path / "config" / "note_formats"
    assert nf_dir.is_dir()
    seeded = {p.stem for p in nf_dir.glob("*.md")}
    assert seeded == EXPECTED_KEYS
    # Writers Read this exact path.
    assert (nf_dir / "paper.md").read_text(encoding="utf-8").count("## ") >= 3


def test_seed_is_idempotent_and_preserves_user_edits(tmp_path: Path):
    nf_dir = tmp_path / "config" / "note_formats"
    nf_dir.mkdir(parents=True)
    custom = nf_dir / "paper.md"
    custom.write_text("## My Custom Shape\nedited by the user\n", encoding="utf-8")

    _seed_vault_templates(tmp_path)

    # Existing user file is NOT clobbered; the rest are still seeded.
    assert custom.read_text(encoding="utf-8") == "## My Custom Shape\nedited by the user\n"
    assert (nf_dir / "news.md").exists()
