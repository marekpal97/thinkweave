"""Tests for ``synthesis/theme_candidates.py`` — enriched theme cluster
detection (``detect_signals``), direct minting (``mint_theme_from_signal``),
and extension of existing themes (``extend_theme_with_sources``).

Post-2026-05-30 teardown: no candidate stubs, no proposed_theme vote
winner, no dormant/resolved lifecycle. detect_signals surfaces every
qualifying cluster (covered or not) with the raw ``proposed_names`` tally
and any overlapping ``covering_themes`` so ``/dream`` can mint or extend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager, parse_frontmatter
from personal_mem.synthesis.theme_candidates import (
    ThemeClusterSignal,
    detect_signals,
    extend_theme_with_sources,
    mint_theme_from_signal,
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


@pytest.fixture
def indexer(config: Config):
    idx = Indexer(config=config)
    yield idx
    idx.close()


def _make_source(
    vault: VaultManager,
    title: str,
    *,
    concepts: list[str],
    source_type: str = "substack",
    proposed_theme: str = "",
) -> Path:
    extra: dict = {"source_type": source_type, "concepts": concepts}
    if proposed_theme:
        extra["proposed_theme"] = proposed_theme
    return vault.create_note(
        note_type=NoteType.SOURCE,
        title=title,
        body=f"# {title}\n",
        extra_frontmatter=extra,
    )


def _src_ids(paths: list[Path]) -> list[str]:
    out = []
    for p in paths:
        fm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
        out.append(fm["id"])
    return out


class TestDetectSignalsClustering:
    def test_cluster_of_three_surfaces(self, vault, indexer, config):
        for i in range(3):
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"])
        indexer.rebuild()
        signals = detect_signals(config)
        assert len(signals) == 1
        s = signals[0]
        assert isinstance(s, ThemeClusterSignal)
        assert s.source_type == "substack"
        assert set(s.shared_concepts) == {"ai-capex", "hyperscaler"}
        assert len(s.cluster_source_ids) == 3

    def test_two_sources_below_cluster_size(self, vault, indexer, config):
        for t in ("A", "B"):
            _make_source(vault, t, concepts=["ai-capex", "hyperscaler"])
        indexer.rebuild()
        assert detect_signals(config) == []

    def test_single_shared_concept_below_threshold(self, vault, indexer, config):
        for t in ("A", "B", "C"):
            _make_source(vault, t, concepts=["ai-capex"])
        indexer.rebuild()
        assert detect_signals(config) == []

    def test_concept_grain_source_skipped(self, vault, indexer, config):
        for i in range(3):
            _make_source(
                vault, f"P{i}", concepts=["transformer", "attention"],
                source_type="paper",
            )
        indexer.rebuild()
        assert detect_signals(config) == []

    def test_signal_resurfaces_each_scan(self, vault, indexer, config):
        for i in range(4):
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"])
        indexer.rebuild()
        first = detect_signals(config)
        second = detect_signals(config)
        assert len(first) == len(second) == 1
        assert first[0].cluster_source_ids == second[0].cluster_source_ids


class TestProposedNamesTally:
    def test_two_same_one_missing(self, vault, indexer, config):
        _make_source(vault, "S0", concepts=["ai-capex", "hyperscaler"],
                     proposed_theme="ai-capex-unwind")
        _make_source(vault, "S1", concepts=["ai-capex", "hyperscaler"],
                     proposed_theme="ai-capex-unwind")
        _make_source(vault, "S2", concepts=["ai-capex", "hyperscaler"])
        indexer.rebuild()
        s = detect_signals(config)[0]
        assert s.proposed_names == {"ai-capex-unwind": 2}

    def test_variant_slugs_fold_into_one_family(self, vault, indexer, config):
        # Name-primary clustering: token-Jaccard merges near-variant
        # slugs into one arc, label = most-supported variant, the rest
        # ride along in related_names. Distinct-source counts (D1).
        for i, slug in enumerate(["iran-war", "iran-war", "iran-war-escalation"]):
            _make_source(vault, f"S{i}", concepts=["geopolitics", "oil"],
                         proposed_theme=slug)
        indexer.rebuild()
        sigs = detect_signals(config)
        assert len(sigs) == 1
        s = sigs[0]
        assert s.cluster_kind == "name"
        assert s.label == "iran-war"
        assert s.proposed_names == {"iran-war": 2, "iran-war-escalation": 1}
        assert s.related_names == {"iran-war-escalation": 1}

    def test_divergent_slugs_do_not_cluster(self, vault, indexer, config):
        # No shared significant tokens (arc/story/play are stopwords) and
        # each slug is a singleton → no name cluster. Stamped sources do
        # not fall through to the concept-fallback path, so: nothing.
        for i, slug in enumerate(["alpha-arc", "beta-story", "gamma-play"]):
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"],
                         proposed_theme=slug)
        indexer.rebuild()
        assert detect_signals(config) == []

    def test_empty_when_no_stamps(self, vault, indexer, config):
        for i in range(3):
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"])
        indexer.rebuild()
        s = detect_signals(config)[0]
        assert s.proposed_names == {}

    def test_stamp_rides_on_source_dicts(self, vault, indexer, config):
        for i in range(3):
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"],
                         proposed_theme="ai-capex-unwind")
        indexer.rebuild()
        s = detect_signals(config)[0]
        assert all(d["proposed_theme"] == "ai-capex-unwind" for d in s.sources)
        assert all("title" in d and "id" in d for d in s.sources)


class TestCoveringThemes:
    def test_covered_cluster_surfaces_with_theme(self, vault, indexer, config):
        for i in range(3):
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"])
        vault.create_note(
            note_type=NoteType.THEME,
            title="AI capex unwind",
            extra_frontmatter={
                "concepts": ["ai-capex", "hyperscaler"],
                "status": "active",
            },
        )
        indexer.rebuild()
        signals = detect_signals(config)
        ai = [s for s in signals if "ai-capex" in s.shared_concepts]
        assert len(ai) == 1
        cov = ai[0].covering_themes
        assert cov and cov[0]["overlap"] == 2
        assert cov[0]["theme_id"].startswith("thm-")

    def test_no_covering_theme_when_disjoint(self, vault, indexer, config):
        for i in range(3):
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"])
        vault.create_note(
            note_type=NoteType.THEME,
            title="Unrelated",
            extra_frontmatter={"concepts": ["biotech", "fda"], "status": "active"},
        )
        indexer.rebuild()
        s = [x for x in detect_signals(config) if "ai-capex" in x.shared_concepts][0]
        assert s.covering_themes == []


class TestMintThemeFromSignal:
    def test_mints_and_backfills(self, vault, indexer, config):
        paths = [
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"])
            for i in range(3)
        ]
        indexer.rebuild()
        ids = _src_ids(paths)

        theme_path = mint_theme_from_signal(
            config,
            slug="ai-capex-unwind",
            essence="AI capex unwind: hyperscaler spend reversal.",
            cluster_source_ids=ids,
            cluster_concepts=["ai-capex", "hyperscaler"],
        )
        assert theme_path.exists()
        fm, body = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
        assert fm["type"] == "theme"
        assert fm["status"] == "active"
        assert fm["id"].startswith("thm-")
        assert set(fm["cites"]) == set(ids)
        assert "## Essence" in body and "## Catalyst log" in body
        thm_id = fm["id"]

        for p in paths:
            sfm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
            assert thm_id in (sfm.get("relates_to") or [])

    def test_quote_bearing_title_roundtrips(self, vault, indexer, config):
        # Defect 4 regression: the mint emitter used to build frontmatter
        # via f-strings (`title: "{display_title}"` unescaped), so a
        # news-derived title containing double quotes broke the minted
        # file's YAML. Mint with such a title, then load the file back
        # through the vault reader and assert nothing was mangled.
        paths = [
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"])
            for i in range(3)
        ]
        indexer.rebuild()
        ids = _src_ids(paths)
        title = 'He said "no" — Q1 "pivot"'

        theme_path = mint_theme_from_signal(
            config,
            slug="q1-pivot",
            essence="Quote-bearing title regression.",
            cluster_source_ids=ids,
            cluster_concepts=["ai-capex", "hyperscaler"],
            project="alpha",
            parent="thm-aaaa1111",
            title=title,
        )
        fm, body = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
        assert fm["title"] == title
        # the rest of the frontmatter survives the quoted title intact
        assert fm["type"] == "theme"
        assert fm["status"] == "active"
        assert fm["id"].startswith("thm-")
        assert set(fm["cites"]) == set(ids)
        assert set(fm["concepts"]) == {"ai-capex", "hyperscaler"}
        assert fm["project"] == "alpha"
        assert fm["parent"] == "thm-aaaa1111"
        assert fm["aliases"] == [fm["id"]]
        assert "## Essence" in body and "## Catalyst log" in body

    def test_parent_written(self, vault, indexer, config):
        paths = [
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"])
            for i in range(3)
        ]
        indexer.rebuild()
        theme_path = mint_theme_from_signal(
            config,
            slug="child-arc",
            essence="x",
            cluster_source_ids=_src_ids(paths),
            cluster_concepts=["ai-capex"],
            parent="thm-aaaa1111",
        )
        fm, _ = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
        assert fm["parent"] == "thm-aaaa1111"


class TestExtendThemeWithSources:
    def test_links_new_sources(self, vault, indexer, config):
        theme_path = vault.create_note(
            note_type=NoteType.THEME,
            title="AI capex",
            body="## Essence\n\nx\n\n## Catalyst log\n\n## Open questions\n",
            extra_frontmatter={"concepts": ["ai-capex"], "status": "active"},
        )
        tfm, _ = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
        theme_id = tfm["id"]
        paths = [
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"])
            for i in range(3)
        ]
        indexer.rebuild()
        ids = _src_ids(paths)

        n = extend_theme_with_sources(config, theme_id=theme_id, source_ids=ids)
        assert n == 3
        # relates_to backfilled on sources
        for p in paths:
            sfm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
            assert theme_id in (sfm.get("relates_to") or [])
        # theme cites + catalyst lines updated
        fm, body = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
        assert set(ids).issubset(set(fm.get("cites") or []))
        assert body.count("extend —") == 3

    def test_already_cited_skipped(self, vault, indexer, config):
        paths = [
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"])
            for i in range(3)
        ]
        indexer.rebuild()
        ids = _src_ids(paths)
        theme_path = mint_theme_from_signal(
            config, slug="ai-capex", essence="x",
            cluster_source_ids=ids, cluster_concepts=["ai-capex"],
        )
        fm, _ = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
        theme_id = fm["id"]
        indexer.rebuild()
        # all three already cited → nothing new to link
        n = extend_theme_with_sources(config, theme_id=theme_id, source_ids=ids)
        assert n == 0

    def test_missing_theme_raises(self, vault, indexer, config):
        indexer.rebuild()
        with pytest.raises(FileNotFoundError):
            extend_theme_with_sources(
                config, theme_id="thm-nope", source_ids=["src-x"]
            )


class TestCatalystDistillation:
    """S1 — worker-composed catalyst texts flow into mint/extend log lines."""

    def _theme(self, vault) -> tuple[Path, str]:
        theme_path = vault.create_note(
            note_type=NoteType.THEME,
            title="AI capex",
            body="## Essence\n\nx\n\n## Catalyst log\n\n## Open questions\n",
            extra_frontmatter={"concepts": ["ai-capex"], "status": "active"},
        )
        tfm, _ = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
        return theme_path, tfm["id"]

    def test_extend_writes_provided_text_and_flag(
        self, vault, indexer, config
    ):
        theme_path, theme_id = self._theme(vault)
        paths = [
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"])
            for i in range(2)
        ]
        indexer.rebuild()
        ids = _src_ids(paths)

        n = extend_theme_with_sources(
            config,
            theme_id=theme_id,
            source_ids=ids,
            catalysts=[
                {"source_id": ids[0], "text": "Capex guide cut 18%", "flag": "contradicts"},
                # ids[1] deliberately absent → falls back to generic line
            ],
        )
        assert n == 2
        _, body = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
        assert "Capex guide cut 18%" in body
        assert "*contradicts*" in body
        assert body.count("extend —") == 1  # only the fallback source

    def test_extend_invalid_flag_falls_back_to_new(
        self, vault, indexer, config
    ):
        theme_path, theme_id = self._theme(vault)
        paths = [_make_source(vault, "S0", concepts=["ai-capex", "x"])]
        indexer.rebuild()
        ids = _src_ids(paths)

        extend_theme_with_sources(
            config,
            theme_id=theme_id,
            source_ids=ids,
            catalysts=[{"source_id": ids[0], "text": "artifact", "flag": "bogus"}],
        )
        _, body = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
        assert "*new* — artifact" in body

    def test_extend_clips_overlong_text(self, vault, indexer, config):
        theme_path, theme_id = self._theme(vault)
        paths = [_make_source(vault, "S0", concepts=["ai-capex", "x"])]
        indexer.rebuild()
        ids = _src_ids(paths)

        extend_theme_with_sources(
            config,
            theme_id=theme_id,
            source_ids=ids,
            catalysts=[{"source_id": ids[0], "text": "word " * 200}],
        )
        _, body = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
        line = next(ln for ln in body.splitlines() if "word" in ln)
        assert len(line) < 420  # ~300-char clip + link/date decoration
        assert "…" in line

    def test_mint_seed_entries_use_catalysts_and_title(
        self, vault, indexer, config
    ):
        paths = [
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"])
            for i in range(2)
        ]
        indexer.rebuild()
        ids = _src_ids(paths)
        theme_path = mint_theme_from_signal(
            config,
            slug="ai-capex-unwind",
            title="AI capex unwind",
            essence="Hyperscaler capex pulls back through 2026.",
            cluster_source_ids=ids,
            cluster_concepts=["ai-capex"],
            catalysts=[
                {"source_id": ids[0], "text": "First guide-down of the arc", "flag": "new"},
            ],
        )
        fm, body = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
        assert fm["title"] == "AI capex unwind"
        assert "# AI capex unwind" in body
        # Real essence ⇒ essence_updated stamped at mint.
        assert str(fm.get("essence_updated") or "")
        assert "First guide-down of the arc" in body
        assert body.count("cluster seed") == 1  # the un-distilled source
        # Filename stays slug-shaped, not title-shaped.
        assert theme_path.stem == "ai-capex-unwind"

    def test_mint_placeholder_essence_not_stamped(
        self, vault, indexer, config
    ):
        paths = [_make_source(vault, "S0", concepts=["ai-capex", "x"])]
        indexer.rebuild()
        theme_path = mint_theme_from_signal(
            config,
            slug="thin-arc",
            essence="",
            cluster_source_ids=_src_ids(paths),
            cluster_concepts=["ai-capex"],
        )
        fm, _ = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
        assert not fm.get("essence_updated")


class TestSignalExcerpts:
    """S1 — detect_signals sources carry body excerpts for distillation."""

    def test_sources_carry_excerpt(self, vault, indexer, config):
        for i in range(3):
            vault.create_note(
                note_type=NoteType.SOURCE,
                title=f"S{i}",
                body=f"# S{i}\n\n"
                + f"Long analytical prose about capex {i}. " * 3,
                extra_frontmatter={
                    "source_type": "substack",
                    "concepts": ["ai-capex", "hyperscaler"],
                    "proposed_theme": "ai-capex-unwind",
                },
            )
        indexer.rebuild()
        signals = detect_signals(config)
        assert signals
        src = signals[0].sources[0]
        assert "excerpt" in src
        assert "Long analytical prose" in src["excerpt"]
        # Headings are stripped from the excerpt.
        assert "# S" not in src["excerpt"]
