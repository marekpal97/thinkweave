"""Tests for the auto-fire theme-candidate floater hook in
``VaultManager.create_note``.

Closes the CLAUDE.md contract gap where event-grain sources only
produced theme candidates when ``/drain`` ran the ``theme_scan``
post-batch hook. Direct ``mem_create``, ``/news <url>``, ``/capture``,
or any other path that calls ``VaultManager.create_note`` now triggers
the floater unconditionally for ``temporal_grain='event'`` source types.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager, parse_frontmatter
from personal_mem.synthesis.theme_candidates import (
    CANDIDATES_DIR_NAME,
)


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def config(vault_dir: Path) -> Config:
    return Config(vault_root=vault_dir)


@pytest.fixture
def vault(config: Config) -> VaultManager:
    vm = VaultManager(config=config)
    vm.ensure_dirs()
    return vm


def _cand_dir(config: Config) -> Path:
    return config.vault_root / "themes" / CANDIDATES_DIR_NAME


def _make_news_source(
    vault: VaultManager, title: str, *, concepts: list[str]
) -> Path:
    """Create a source note with source_type=news (event-grain)."""
    return vault.create_note(
        note_type=NoteType.SOURCE,
        title=title,
        extra_frontmatter={
            "source_type": "news",
            "concepts": concepts,
        },
    )


def test_third_event_grain_create_floats_candidate(
    vault: VaultManager, config: Config
):
    """Three news sources sharing ≥2 concepts: the auto-fire from the
    third create writes a candidate stub. No explicit scan call needed."""
    _make_news_source(vault, "Story A", concepts=["ai-policy", "regulation"])
    _make_news_source(vault, "Story B", concepts=["ai-policy", "regulation"])

    # Two creates: still below the cluster-size threshold.
    cand_dir = _cand_dir(config)
    assert not cand_dir.exists() or list(cand_dir.glob("cand-*.md")) == []

    _make_news_source(vault, "Story C", concepts=["ai-policy", "regulation"])

    # The third create's auto-fire writes the candidate.
    cand_files = list(_cand_dir(config).glob("cand-*.md"))
    assert len(cand_files) == 1
    fm, _ = parse_frontmatter(cand_files[0].read_text(encoding="utf-8"))
    assert fm["source_type"] == "news"
    assert fm["status"] == "candidate"
    assert "ai-policy" in fm.get("cluster_concepts", [])
    assert "regulation" in fm.get("cluster_concepts", [])


def test_auto_fire_idempotent_no_duplicate_candidates(
    vault: VaultManager, config: Config
):
    """Adding a 4th matching source must not produce a second candidate
    stub — the existing-candidate dedup in scan_candidates catches it."""
    for i in range(4):
        _make_news_source(
            vault, f"Story {i}", concepts=["ai-policy", "regulation"]
        )

    cand_files = list(_cand_dir(config).glob("cand-*.md"))
    assert len(cand_files) == 1, (
        f"Expected exactly one candidate after 4 creates; got "
        f"{[p.name for p in cand_files]}"
    )


def test_concept_grain_source_does_not_float(
    vault: VaultManager, config: Config
):
    """Papers are temporal_grain='concept' — three matching ones must
    NOT produce a candidate, even though the cluster shape matches."""
    for i in range(3):
        vault.create_note(
            note_type=NoteType.SOURCE,
            title=f"Paper {i}",
            extra_frontmatter={
                "source_type": "paper",
                "concepts": ["transformer", "attention"],
            },
        )

    assert not _cand_dir(config).exists() or list(
        _cand_dir(config).glob("cand-*.md")
    ) == []


def test_non_source_notes_skip_floater(vault: VaultManager, config: Config):
    """Note / decision creates must not trigger the floater — the hook
    short-circuits before the spec lookup. (Sanity check on the
    NoteType.SOURCE guard.)"""
    vault.create_note(
        note_type=NoteType.NOTE,
        title="Random observation",
        extra_frontmatter={"concepts": ["ai-policy", "regulation"]},
    )
    assert not _cand_dir(config).exists() or list(
        _cand_dir(config).glob("cand-*.md")
    ) == []


def test_failure_does_not_block_create(
    vault: VaultManager, config: Config, monkeypatch
):
    """If scan_candidates raises, the create itself must still succeed.
    The floater is opportunistic — write contract is sacred."""
    from personal_mem.synthesis import theme_candidates

    def _explode(*_args, **_kwargs):
        raise RuntimeError("simulated indexer failure")

    monkeypatch.setattr(theme_candidates, "scan_candidates", _explode)

    # The create must still produce a file at the expected path.
    path = _make_news_source(vault, "Story X", concepts=["ai-policy", "regulation"])
    assert path.exists()
    assert path.read_text(encoding="utf-8")  # non-empty


def test_auto_fire_latency_is_acceptable(
    vault: VaultManager, config: Config
):
    """Profile sanity: the auto-fire path should complete in well under
    a second on a tiny vault. Latency budget here is 2s (generous to
    avoid CI flakiness); a regression that pushes this above the budget
    deserves attention via debouncing or async deferral."""
    _make_news_source(vault, "A", concepts=["ai-policy", "regulation"])
    _make_news_source(vault, "B", concepts=["ai-policy", "regulation"])

    t0 = time.monotonic()
    _make_news_source(vault, "C", concepts=["ai-policy", "regulation"])
    elapsed = time.monotonic() - t0

    # Generous budget — the test vault has 3 notes; <500ms is realistic.
    assert elapsed < 2.0, (
        f"auto-fire create took {elapsed:.3f}s — investigate before "
        "shipping (CLAUDE.md flags >500ms as the debounce trigger)."
    )
