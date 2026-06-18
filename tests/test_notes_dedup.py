"""Tests for the write-time source-dedup gate in :func:`operations.notes.create_note`.

The gate centralises a check that every prior importer / worker had to remember
individually (and several didn't — e.g. /research <url> direct paste, the
chatgpt importer, and the paper-batch importer all bypass the queue's
dedup_check). It runs only for ``note_type=NoteType.SOURCE`` and looks up the
existing-note index via the ``dedup_keys`` configured for the item's
``source_type`` in ``sources.yaml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.schemas import NoteType
from thinkweave.operations import notes as ops


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def cfg(vault_dir: Path) -> Config:
    return Config(vault_root=vault_dir)


# ---------------------------------------------------------------------------
# Happy-path source create
# ---------------------------------------------------------------------------


def _create_paper(cfg: Config, *, arxiv_id: str, url: str, title: str) -> ops.CreateResult:
    return ops.create_note(
        cfg,
        note_type=NoteType.SOURCE,
        title=title,
        body="brief body",
        extra_frontmatter={
            "source_type": "paper",
            "arxiv_id": arxiv_id,
            "url": url,
        },
    )


def test_first_create_writes_and_returns_existed_false(cfg: Config) -> None:
    result = _create_paper(
        cfg,
        arxiv_id="2507.22925",
        url="https://arxiv.org/abs/2507.22925",
        title="H-MEM: Hierarchical Memory",
    )
    assert result.existed is False
    assert result.note.type == NoteType.SOURCE
    assert result.note.id.startswith("src-")


def test_second_create_same_arxiv_id_returns_existed_true(cfg: Config) -> None:
    """The dedup gate matches on the configured arxiv_id key — even when the
    second attempt has a slightly different URL form, the arxiv_id collision
    short-circuits to the existing note."""
    first = _create_paper(
        cfg,
        arxiv_id="2507.22925",
        url="https://arxiv.org/abs/2507.22925",
        title="H-MEM: Hierarchical Memory",
    )
    second = _create_paper(
        cfg,
        arxiv_id="2507.22925",
        url="https://arxiv.org/pdf/2507.22925.pdf",  # different URL form
        title="H-MEM (PDF version)",
    )
    assert second.existed is True
    assert second.note.id == first.note.id


def test_second_create_same_title_returns_existed_true(cfg: Config) -> None:
    """Title alone is enough to dedup for papers — `title` is in paper's
    dedup_keys list (alongside arxiv_id, doi). This is the paper-pipeline
    equivalent of the URL-key case the article pipeline uses."""
    first = _create_paper(
        cfg,
        arxiv_id="2507.22925",
        url="https://arxiv.org/abs/2507.22925",
        title="H-MEM: Hierarchical Memory",
    )
    # Same title, no arxiv_id this time — title match suffices.
    second = ops.create_note(
        cfg,
        note_type=NoteType.SOURCE,
        title="H-MEM: Hierarchical Memory",
        body="x",
        extra_frontmatter={
            "source_type": "paper",
            "url": "https://example.com/different",
        },
    )
    assert second.existed is True
    assert second.note.id == first.note.id


def test_article_url_match_is_case_folded(cfg: Config) -> None:
    """Mirrors Queue._values_equal — strip + lower for string compare. Article
    pipeline carries `url` in its dedup_keys (papers don't; they use arxiv_id)."""
    first = ops.create_note(
        cfg,
        note_type=NoteType.SOURCE,
        title="Original article",
        body="x",
        extra_frontmatter={
            "source_type": "article",
            "url": "https://example.com/post-x",
        },
    )
    second = ops.create_note(
        cfg,
        note_type=NoteType.SOURCE,
        title="Re-paste of article",
        body="x",
        extra_frontmatter={
            "source_type": "article",
            "url": "  HTTPS://EXAMPLE.COM/post-x  ",  # whitespace + case
        },
    )
    assert second.existed is True
    assert second.note.id == first.note.id


def test_different_arxiv_ids_create_distinct_notes(cfg: Config) -> None:
    a = _create_paper(
        cfg,
        arxiv_id="2507.22925",
        url="https://arxiv.org/abs/2507.22925",
        title="H-MEM",
    )
    b = _create_paper(
        cfg,
        arxiv_id="2601.03192",
        url="https://arxiv.org/abs/2601.03192",
        title="MemRL",
    )
    assert a.existed is False
    assert b.existed is False
    assert a.note.id != b.note.id


# ---------------------------------------------------------------------------
# Skip conditions — must not false-positive
# ---------------------------------------------------------------------------


def test_skip_when_source_type_missing(cfg: Config) -> None:
    """Without source_type, the gate can't pick dedup_keys and short-circuits."""
    a = ops.create_note(
        cfg,
        note_type=NoteType.SOURCE,
        title="No source_type 1",
        body="x",
        extra_frontmatter={"url": "https://example.com/whatever"},
    )
    b = ops.create_note(
        cfg,
        note_type=NoteType.SOURCE,
        title="No source_type 2",
        body="x",
        extra_frontmatter={"url": "https://example.com/whatever"},
    )
    # Both got created — no dedup_keys to consult because source_type was absent.
    assert a.existed is False
    assert b.existed is False
    assert a.note.id != b.note.id


def test_skip_when_source_type_unknown_to_config(cfg: Config) -> None:
    """An unrecognised source_type has no dedup_keys → gate skips."""
    a = ops.create_note(
        cfg,
        note_type=NoteType.SOURCE,
        title="Unknown 1",
        body="x",
        extra_frontmatter={"source_type": "totally-unknown-type", "url": "https://x.test/1"},
    )
    b = ops.create_note(
        cfg,
        note_type=NoteType.SOURCE,
        title="Unknown 2",
        body="x",
        extra_frontmatter={"source_type": "totally-unknown-type", "url": "https://x.test/1"},
    )
    assert a.existed is False
    assert b.existed is False
    assert a.note.id != b.note.id


def test_skip_when_all_dedup_key_values_empty(cfg: Config) -> None:
    """Empty/missing dedup-key values must not match across notes (would
    false-positive every paper without an arxiv_id against every other)."""
    a = ops.create_note(
        cfg,
        note_type=NoteType.SOURCE,
        title="Paper without arxiv_id A",
        body="x",
        extra_frontmatter={
            "source_type": "paper",
            "arxiv_id": "",
            "doi": "",
            "url": "",
        },
    )
    b = ops.create_note(
        cfg,
        note_type=NoteType.SOURCE,
        title="Paper without arxiv_id B",
        body="x",
        extra_frontmatter={
            "source_type": "paper",
            "arxiv_id": "",
            "doi": "",
            "url": "",
        },
    )
    assert a.existed is False
    assert b.existed is False
    assert a.note.id != b.note.id


def test_non_source_note_types_skip_gate(cfg: Config) -> None:
    """Sessions/decisions/notes don't have source_type dedup_keys — the gate
    must not interfere with their creation even if they pass identical bodies."""
    a = ops.create_note(
        cfg, note_type=NoteType.NOTE, title="Note A", body="same body"
    )
    b = ops.create_note(
        cfg, note_type=NoteType.NOTE, title="Note B", body="same body"
    )
    assert a.existed is False
    assert b.existed is False
    assert a.note.id != b.note.id


# ---------------------------------------------------------------------------
# Cross-source-type isolation
# ---------------------------------------------------------------------------


def test_cross_source_type_url_collision_pins_to_first(cfg: Config) -> None:
    """The gate matches on the *incoming* item's dedup_keys against ALL source
    notes (not just same-source_type). Rationale: a single canonical URL
    should resolve to a single note regardless of which pipeline ingested it
    first. So if a paper at URL X exists, an article paste at URL X dedups
    against the paper. This is the right behavior for sources — they're
    unique by their identifying keys, not by the path they came in through.
    """
    paper = ops.create_note(
        cfg,
        note_type=NoteType.SOURCE,
        title="Paper at URL",
        body="x",
        extra_frontmatter={
            "source_type": "paper",
            "url": "https://example.com/shared",
        },
    )
    article = ops.create_note(
        cfg,
        note_type=NoteType.SOURCE,
        title="Article at URL",
        body="x",
        extra_frontmatter={
            "source_type": "article",
            "url": "https://example.com/shared",
        },
    )
    assert paper.existed is False
    assert article.existed is True
    assert article.note.id == paper.note.id


# ---------------------------------------------------------------------------
# find_existing_source_by_dedup_keys direct unit tests
# ---------------------------------------------------------------------------


def test_find_existing_returns_none_for_empty_source_type(cfg: Config) -> None:
    assert ops.find_existing_source_by_dedup_keys(cfg, "", {"url": "x"}) is None


def test_find_existing_returns_none_for_empty_frontmatter(cfg: Config) -> None:
    assert ops.find_existing_source_by_dedup_keys(cfg, "paper", {}) is None


def test_find_existing_returns_id_after_create(cfg: Config) -> None:
    result = _create_paper(
        cfg,
        arxiv_id="2507.22925",
        url="https://arxiv.org/abs/2507.22925",
        title="Test",
    )
    found = ops.find_existing_source_by_dedup_keys(
        cfg,
        "paper",
        {"arxiv_id": "2507.22925"},
    )
    assert found == result.note.id


def test_find_existing_unsafe_key_does_not_crash(cfg: Config) -> None:
    """A forged dedup_keys list with SQL-unsafe identifiers must be ignored
    (validator rejects keys that don't match _SAFE_FRONTMATTER_KEY)."""
    # We can't reach the validator with a bad key via real config; just
    # confirm the validator behaviour directly.
    assert ops._SAFE_FRONTMATTER_KEY.match("arxiv_id") is not None
    assert ops._SAFE_FRONTMATTER_KEY.match("video_id") is not None
    assert ops._SAFE_FRONTMATTER_KEY.match("url'); DROP TABLE notes;--") is None
    assert ops._SAFE_FRONTMATTER_KEY.match("$.url") is None
    assert ops._SAFE_FRONTMATTER_KEY.match("") is None
