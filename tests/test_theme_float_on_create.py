"""Tests for the event-grain post-create hook in
``VaultManager.create_note`` and the ``detect_signals`` surface that
``/dream`` reads.

Before 2026-05-25 the hook auto-wrote candidate stubs with mechanical
concept-pair slugs (e.g. ``geopolitics-thematic-investing``). That
naming doomed every cluster at /dream's disambiguation test — capability-
shaped names get archived. The new contract: the hook keeps the SQLite
index warm; ``detect_signals`` reads from the index and surfaces raw
clusters as JSON for /dream to name from the cluster + active themes.

These tests pin both halves: (1) creates still index the source so it's
visible to ``mem_search`` immediately, and (2) ``detect_signals``
surfaces the right clusters and skips the wrong ones.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager
from personal_mem.synthesis.theme_candidates import detect_signals


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


def test_third_event_grain_create_surfaces_signal(
    vault: VaultManager, config: Config
):
    """Three news sources sharing ≥2 concepts produce one ``detect_signals``
    cluster — and no stub on disk (the auto-write is gone)."""
    _make_news_source(vault, "Story A", concepts=["ai-policy", "regulation"])
    _make_news_source(vault, "Story B", concepts=["ai-policy", "regulation"])

    # Two creates: still below the cluster-size threshold.
    assert detect_signals(config) == []

    _make_news_source(vault, "Story C", concepts=["ai-policy", "regulation"])

    # The third create indexes the source; detect_signals surfaces one cluster.
    signals = detect_signals(config)
    assert len(signals) == 1
    s = signals[0]
    assert s.source_type == "news"
    assert set(s.shared_concepts) == {"ai-policy", "regulation"}
    assert len(s.cluster_source_ids) == 3


def test_signal_resurfaces_until_resolved(
    vault: VaultManager, config: Config
):
    """A cluster signal isn't a one-shot — ``detect_signals`` re-emits it
    on every scan until either a covering theme exists or a candidate
    stub exists. This is the property that lets /dream catch up
    asynchronously even if a particular cycle drops a signal."""
    for i in range(4):
        _make_news_source(
            vault, f"Story {i}", concepts=["ai-policy", "regulation"]
        )

    first = detect_signals(config)
    second = detect_signals(config)
    assert len(first) == 1
    assert len(second) == 1
    assert first[0].cluster_source_ids == second[0].cluster_source_ids


def test_concept_grain_source_does_not_surface(
    vault: VaultManager, config: Config
):
    """Papers are ``temporal_grain='concept'`` — three matching ones must
    NOT produce a signal, even though the cluster shape matches."""
    for i in range(3):
        vault.create_note(
            note_type=NoteType.SOURCE,
            title=f"Paper {i}",
            extra_frontmatter={
                "source_type": "paper",
                "concepts": ["transformer", "attention"],
            },
        )

    assert detect_signals(config) == []


def test_non_source_notes_skip_hook(vault: VaultManager, config: Config):
    """Note / decision creates must not affect the theme-signal surface —
    the hook short-circuits before the spec lookup. (Sanity check on
    the NoteType.SOURCE guard.)"""
    vault.create_note(
        note_type=NoteType.NOTE,
        title="Random observation",
        extra_frontmatter={"concepts": ["ai-policy", "regulation"]},
    )
    assert detect_signals(config) == []


def test_indexer_failure_does_not_block_create(
    vault: VaultManager, config: Config, monkeypatch
):
    """If the incremental indexer raises, the create itself must still
    succeed. The hook is opportunistic — write contract is sacred."""
    from personal_mem.core import indexer as indexer_module

    real_indexer = indexer_module.Indexer

    class _ExplodingIndexer(real_indexer):  # type: ignore[misc, valid-type]
        def index_file(self, *_args, **_kwargs):  # type: ignore[override]
            raise RuntimeError("simulated indexer failure")

    monkeypatch.setattr(indexer_module, "Indexer", _ExplodingIndexer)

    path = _make_news_source(
        vault, "Story X", concepts=["ai-policy", "regulation"]
    )
    assert path.exists()
    assert path.read_text(encoding="utf-8")  # non-empty


def test_post_create_latency_is_acceptable(
    vault: VaultManager, config: Config
):
    """The post-create hook should complete in well under a second on a
    tiny vault. Generous 2s budget to absorb CI flakiness; a regression
    past this deserves debouncing or async deferral."""
    _make_news_source(vault, "A", concepts=["ai-policy", "regulation"])
    _make_news_source(vault, "B", concepts=["ai-policy", "regulation"])

    t0 = time.monotonic()
    _make_news_source(vault, "C", concepts=["ai-policy", "regulation"])
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0, (
        f"post-create hook took {elapsed:.3f}s — investigate before "
        "shipping."
    )


class TestDetectionKnobsFromConfig:
    """Bucket-3 audit: detection thresholds default from config ``themes.*``."""

    def test_min_cluster_size_override_lowers_the_bar(
        self, vault: VaultManager, config: Config
    ):
        """Two sources don't cluster at the default (3) but do at 2."""
        _make_news_source(vault, "Story A", concepts=["ai-policy", "regulation"])
        _make_news_source(vault, "Story B", concepts=["ai-policy", "regulation"])

        assert detect_signals(config) == []

        config.theme_min_cluster_size = 2
        signals = detect_signals(config)
        assert len(signals) == 1
        assert len(signals[0].cluster_source_ids) == 2

    def test_min_shared_concepts_override_raises_the_bar(
        self, vault: VaultManager, config: Config
    ):
        """Three sources sharing exactly 2 concepts stop clustering at 3."""
        for i in range(3):
            _make_news_source(
                vault, f"Story {i}", concepts=["ai-policy", "regulation"]
            )

        assert len(detect_signals(config)) == 1

        config.theme_min_shared_concepts = 3
        assert detect_signals(config) == []

    def test_explicit_kwarg_overrides_config(
        self, vault: VaultManager, config: Config
    ):
        _make_news_source(vault, "Story A", concepts=["ai-policy", "regulation"])
        _make_news_source(vault, "Story B", concepts=["ai-policy", "regulation"])
        config.theme_min_cluster_size = 2

        assert detect_signals(config, min_cluster_size=3) == []
