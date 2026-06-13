"""Tests for ``thinkweave.acquisition.sources.intake`` — drop-folder enumerate +
archive helpers shared by ``/substack`` and future drop-folder importers.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from thinkweave.acquisition.sources.intake import (
    InboxEntry,
    archive_to_processed,
    enumerate_inbox,
)


# ---------------------------------------------------------------------------
# enumerate_inbox


def test_enumerate_empty_inbox_returns_empty_list(tmp_path: Path) -> None:
    assert enumerate_inbox(tmp_path) == []


def test_enumerate_missing_inbox_returns_empty_list(tmp_path: Path) -> None:
    # Mirrors the prior `ls ... 2>/dev/null` semantics — non-existent inbox
    # is not an error, just nothing to drain.
    missing = tmp_path / "does-not-exist"
    assert enumerate_inbox(missing) == []


def test_enumerate_flat_md_file(tmp_path: Path) -> None:
    md = tmp_path / "post.md"
    md.write_text("# hi\n")

    entries = enumerate_inbox(tmp_path)

    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, InboxEntry)
    assert entry.path == md.resolve()
    assert entry.kind == "flat"
    assert entry.companion_dir is None


def test_enumerate_flat_with_images_companion(tmp_path: Path) -> None:
    md = tmp_path / "post.md"
    md.write_text("# hi\n")
    companion = tmp_path / "post-images"
    companion.mkdir()
    (companion / "chart.png").write_bytes(b"\x89PNG")

    entries = enumerate_inbox(tmp_path)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "flat"
    assert entry.path == md.resolve()
    assert entry.companion_dir is not None
    assert entry.companion_dir.name == "post-images"


def test_enumerate_flat_with_assets_companion(tmp_path: Path) -> None:
    md = tmp_path / "post.md"
    md.write_text("# hi\n")
    companion = tmp_path / "post_assets"
    companion.mkdir()

    entries = enumerate_inbox(tmp_path)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.companion_dir is not None
    assert entry.companion_dir.name == "post_assets"


def test_enumerate_folder_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "post-folder"
    bundle.mkdir()
    (bundle / "index.md").write_text("# hi\n")
    (bundle / "chart.png").write_bytes(b"\x89PNG")

    entries = enumerate_inbox(tmp_path)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "folder"
    assert entry.path == bundle.resolve()
    assert entry.companion_dir is None


def test_enumerate_skips_archive_folder(tmp_path: Path) -> None:
    archive = tmp_path / "_processed"
    archive.mkdir()
    (archive / "old.md").write_text("# old\n")
    (tmp_path / "new.md").write_text("# new\n")

    entries = enumerate_inbox(tmp_path)

    names = [e.path.name for e in entries]
    assert names == ["new.md"]


def test_enumerate_custom_archive_name_skipped(tmp_path: Path) -> None:
    archive = tmp_path / "done"
    archive.mkdir()
    (archive / "old.md").write_text("# old\n")
    (tmp_path / "new.md").write_text("# new\n")

    entries = enumerate_inbox(tmp_path, archive_name="done")

    assert [e.path.name for e in entries] == ["new.md"]


def test_enumerate_skips_dotfiles_and_loose_non_md(tmp_path: Path) -> None:
    (tmp_path / ".DS_Store").write_text("")
    (tmp_path / ".git").mkdir()
    (tmp_path / "thumbnail.png").write_bytes(b"\x89PNG")
    (tmp_path / "post.md").write_text("# hi\n")

    entries = enumerate_inbox(tmp_path)

    assert [e.path.name for e in entries] == ["post.md"]


def test_enumerate_skips_folder_without_md(tmp_path: Path) -> None:
    images_only = tmp_path / "images-only"
    images_only.mkdir()
    (images_only / "a.png").write_bytes(b"\x89PNG")

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    (tmp_path / "real.md").write_text("# hi\n")

    entries = enumerate_inbox(tmp_path)

    assert [e.path.name for e in entries] == ["real.md"]


def test_enumerate_sorted_deterministic(tmp_path: Path) -> None:
    for name in ["c.md", "a.md", "b.md"]:
        (tmp_path / name).write_text("# hi\n")

    entries = enumerate_inbox(tmp_path)

    assert [e.path.name for e in entries] == ["a.md", "b.md", "c.md"]


def test_enumerate_does_not_double_list_companion_dir(tmp_path: Path) -> None:
    # A directory whose name matches the companion convention of a sibling
    # .md must not also be enumerated as a folder bundle in its own right.
    (tmp_path / "post.md").write_text("# hi\n")
    companion = tmp_path / "post-images"
    companion.mkdir()
    # Even if a stray .md slipped into the companion, the parent .md owns it.
    (companion / "stray.md").write_text("noise\n")

    entries = enumerate_inbox(tmp_path)

    assert [e.path.name for e in entries] == ["post.md"]
    assert entries[0].companion_dir is not None
    assert entries[0].companion_dir.name == "post-images"


# ---------------------------------------------------------------------------
# archive_to_processed


def test_archive_flat_file_moves_into_dated_folder(tmp_path: Path) -> None:
    inbox = tmp_path
    md = inbox / "post.md"
    md.write_text("# hi\n")

    final = archive_to_processed(md, inbox, today=date(2026, 1, 15))

    assert not md.exists()
    assert final == inbox / "_processed" / "2026-01-15" / "post.md"
    assert final.is_file()
    assert final.read_text() == "# hi\n"


def test_archive_flat_file_with_companion_moves_both(tmp_path: Path) -> None:
    inbox = tmp_path
    md = inbox / "post.md"
    md.write_text("# hi\n")
    companion = inbox / "post-images"
    companion.mkdir()
    (companion / "chart.png").write_bytes(b"\x89PNG")

    final = archive_to_processed(md, inbox, today=date(2026, 1, 15))

    dated = inbox / "_processed" / "2026-01-15"
    assert final == dated / "post.md"
    assert (dated / "post-images").is_dir()
    assert (dated / "post-images" / "chart.png").is_file()
    assert not md.exists()
    assert not companion.exists()


def test_archive_folder_bundle_moves_directory(tmp_path: Path) -> None:
    inbox = tmp_path
    bundle = inbox / "post-folder"
    bundle.mkdir()
    (bundle / "index.md").write_text("# hi\n")
    (bundle / "chart.png").write_bytes(b"\x89PNG")

    final = archive_to_processed(bundle, inbox, today=date(2026, 1, 15))

    dated = inbox / "_processed" / "2026-01-15"
    assert final == dated / "post-folder"
    assert (final / "index.md").is_file()
    assert (final / "chart.png").is_file()
    assert not bundle.exists()


def test_archive_creates_processed_root_if_missing(tmp_path: Path) -> None:
    inbox = tmp_path
    md = inbox / "post.md"
    md.write_text("# hi\n")
    assert not (inbox / "_processed").exists()

    final = archive_to_processed(md, inbox, today=date(2026, 1, 15))

    assert (inbox / "_processed").is_dir()
    assert final.exists()


def test_archive_idempotent_same_day_collision_appends_suffix(tmp_path: Path) -> None:
    inbox = tmp_path
    today = date(2026, 1, 15)

    first = inbox / "post.md"
    first.write_text("first\n")
    archive_to_processed(first, inbox, today=today)

    # A second entry with the same name (a fresh re-clip) appears in the
    # inbox; archiving it the same day must not overwrite the first.
    second = inbox / "post.md"
    second.write_text("second\n")

    final = archive_to_processed(second, inbox, today=today)

    dated = inbox / "_processed" / "2026-01-15"
    assert final == dated / "post-1.md"
    assert (dated / "post.md").read_text() == "first\n"
    assert (dated / "post-1.md").read_text() == "second\n"


def test_archive_idempotent_same_day_reuses_dated_folder(tmp_path: Path) -> None:
    inbox = tmp_path
    today = date(2026, 1, 15)

    (inbox / "a.md").write_text("a\n")
    (inbox / "b.md").write_text("b\n")

    archive_to_processed(inbox / "a.md", inbox, today=today)
    archive_to_processed(inbox / "b.md", inbox, today=today)

    dated = inbox / "_processed" / "2026-01-15"
    assert {p.name for p in dated.iterdir()} == {"a.md", "b.md"}


def test_archive_collision_keeps_companion_aligned(tmp_path: Path) -> None:
    inbox = tmp_path
    today = date(2026, 1, 15)

    # First archive — plain post.md + post-images/.
    (inbox / "post.md").write_text("first\n")
    (inbox / "post-images").mkdir()
    (inbox / "post-images" / "chart.png").write_bytes(b"\x89PNG")
    archive_to_processed(inbox / "post.md", inbox, today=today)

    # Second archive — re-clip with same names, new content.
    (inbox / "post.md").write_text("second\n")
    (inbox / "post-images").mkdir()
    (inbox / "post-images" / "chart.png").write_bytes(b"\xffNEW")

    final = archive_to_processed(inbox / "post.md", inbox, today=today)

    dated = inbox / "_processed" / "2026-01-15"
    assert final == dated / "post-1.md"
    # Companion suffix mirrors the primary's collision suffix.
    assert (dated / "post-images-1").is_dir()
    assert (dated / "post-images-1" / "chart.png").read_bytes() == b"\xffNEW"
    # Original archive untouched.
    assert (dated / "post.md").read_text() == "first\n"
    assert (dated / "post-images" / "chart.png").read_bytes() == b"\x89PNG"


def test_archive_raises_on_missing_entry(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        archive_to_processed(tmp_path / "nope.md", tmp_path, today=date(2026, 1, 15))


def test_archive_raises_when_entry_outside_inbox(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    stray = other / "post.md"
    stray.write_text("# hi\n")

    with pytest.raises(ValueError):
        archive_to_processed(stray, inbox, today=date(2026, 1, 15))


def test_archive_injectable_today(tmp_path: Path) -> None:
    md = tmp_path / "post.md"
    md.write_text("# hi\n")

    final = archive_to_processed(md, tmp_path, today=date(2099, 12, 31))

    assert final == tmp_path / "_processed" / "2099-12-31" / "post.md"
