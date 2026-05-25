"""Tests for ``synthesis/theme_candidates.py`` — source-coupled
theme-candidate floating, archival, and promotion."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager, parse_frontmatter
from personal_mem.synthesis.theme_candidates import (
    CANDIDATES_ARCHIVE_NAME,
    CANDIDATES_DIR_NAME,
    ProposedThemeVote,
    aggregate_proposed_themes,
    archive_stale_candidates,
    detect_signals,
    find_dormant_themes,
    find_resolved_themes,
    mint_theme_from_signal,
    promote_candidate,
    scan_candidates,
)
from personal_mem.synthesis.theme_hub import (
    build_theme_frontmatter,
    render_theme_body_skeleton,
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


def _make_substack_source(
    vault: VaultManager,
    title: str,
    *,
    concepts: list[str],
) -> Path:
    """Create a source note carrying source_type=substack and concepts."""
    return vault.create_note(
        note_type=NoteType.SOURCE,
        title=title,
        body=f"# {title}\n",
        extra_frontmatter={
            "source_type": "substack",
            "concepts": concepts,
        },
    )


class TestScanCandidatesEventGrain:
    """Substack is the canonical event-grain type. Three sources
    sharing two concepts trigger a candidate stub."""

    def test_cluster_of_three_creates_candidate(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        # Creates may auto-fire scan_candidates via the post-write hook
        # in VaultManager._maybe_float_theme_candidate (event-grain
        # sources). Validate by inspecting the candidates directory
        # directly rather than via a second explicit scan, which would
        # dedup against the first stub.
        for i in range(3):
            _make_substack_source(
                vault, f"Source {i}", concepts=["ai-capex", "hyperscaler"]
            )
        indexer.rebuild()
        scan_candidates(config, source_type="substack")

        cdir = config.vault_root / "themes" / CANDIDATES_DIR_NAME
        cand_files = list(cdir.glob("cand-*.md"))
        assert len(cand_files) == 1
        path = cand_files[0]
        assert path.exists()

        fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert fm["status"] == "candidate"
        assert fm["source_type"] == "substack"
        assert fm["candidacy"] == "inferred-from-substack"
        assert fm["cluster_size"] == 3
        # Body lists the cluster sources as wikilinks.
        assert "ai-capex" in body
        assert "hyperscaler" in body

    def test_two_sources_below_threshold(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        _make_substack_source(vault, "A", concepts=["ai-capex", "hyperscaler"])
        _make_substack_source(vault, "B", concepts=["ai-capex", "hyperscaler"])
        indexer.rebuild()

        outcome = scan_candidates(config, source_type="substack")

        assert outcome.candidates_created == []

    def test_concept_overlap_below_threshold(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        # Three sources but each only shares ONE concept with the others —
        # below the default min_shared_concepts=2.
        _make_substack_source(vault, "A", concepts=["ai-capex"])
        _make_substack_source(vault, "B", concepts=["ai-capex"])
        _make_substack_source(vault, "C", concepts=["ai-capex"])
        indexer.rebuild()

        outcome = scan_candidates(config, source_type="substack")

        assert outcome.candidates_created == []


class TestScanCandidatesNonEventGrain:
    """Concept-grain (paper/repo/article) and none-grain (conversation)
    sources never produce candidates, even if they cluster."""

    def test_paper_sources_skipped(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        for i in range(3):
            vault.create_note(
                note_type=NoteType.SOURCE,
                title=f"Paper {i}",
                extra_frontmatter={
                    "source_type": "paper",
                    "concepts": ["ai-capex", "hyperscaler"],
                },
            )
        indexer.rebuild()

        outcome = scan_candidates(config, source_type="paper")

        assert outcome.candidates_created == []

    def test_unspecified_source_type_scans_all_event_grain(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        # A paper cluster + a substack cluster: only the substack one
        # fires. Auto-fire from VaultManager._maybe_float_theme_candidate
        # already lands the candidate on the 3rd substack create; the
        # explicit scan_candidates() here dedups against it. Assert via
        # filesystem.
        for i in range(3):
            vault.create_note(
                note_type=NoteType.SOURCE,
                title=f"Paper {i}",
                extra_frontmatter={
                    "source_type": "paper",
                    "concepts": ["transformer", "attention"],
                },
            )
        for i in range(3):
            _make_substack_source(
                vault, f"Substack {i}", concepts=["ai-capex", "hyperscaler"]
            )
        indexer.rebuild()
        scan_candidates(config)

        cdir = config.vault_root / "themes" / CANDIDATES_DIR_NAME
        cand_files = list(cdir.glob("cand-*.md"))
        assert len(cand_files) == 1
        fm, _ = parse_frontmatter(cand_files[0].read_text(encoding="utf-8"))
        assert fm["source_type"] == "substack"


class TestScanCandidatesDeduplication:
    """Coverage by an existing canonical theme, or by an active
    candidate, prevents a duplicate stub from being written."""

    def test_existing_theme_covers_cluster(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        for i in range(3):
            _make_substack_source(
                vault, f"S{i}", concepts=["ai-capex", "hyperscaler"]
            )
        # An existing canonical theme that cites the same concepts.
        vault.create_note(
            note_type=NoteType.THEME,
            title="AI capex unwind",
            extra_frontmatter={
                "concepts": ["ai-capex", "hyperscaler"],
                "status": "active",
            },
        )
        indexer.rebuild()

        outcome = scan_candidates(config, source_type="substack")

        assert outcome.candidates_created == []
        assert outcome.clusters_skipped_covered == 1

    def test_existing_candidate_dedupes(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        # As of 2026-05-25, _maybe_float_theme_candidate no longer
        # auto-writes stubs — it just keeps the index warm. Stub
        # materialization is an explicit ``mem themes scan-candidates``
        # action (or implicit via /dream's signal-direct mint). The
        # dedup behaviour still matters: a second scan must not mint a
        # duplicate of a stub from a first scan.
        for i in range(3):
            _make_substack_source(
                vault, f"S{i}", concepts=["ai-capex", "hyperscaler"]
            )
        indexer.rebuild()

        # First explicit scan mints the stub.
        scan_candidates(config, source_type="substack")
        cdir = config.vault_root / "themes" / CANDIDATES_DIR_NAME
        assert len(list(cdir.glob("cand-*.md"))) == 1

        # Second explicit scan dedupes against the stub.
        outcome = scan_candidates(config, source_type="substack")
        assert outcome.candidates_created == []
        assert outcome.clusters_skipped_existing_candidate >= 1


class TestDetectSignals:
    """`detect_signals` is the signal-only twin of `scan_candidates` —
    same filter chain, returns ThemeClusterSignal instead of writing
    stubs. Used by /dream to compose real slugs from raw clusters."""

    def test_empty_when_no_clusters(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        # Only two sources sharing concepts — below cluster threshold.
        for i in range(2):
            _make_substack_source(
                vault, f"S{i}", concepts=["ai-capex", "hyperscaler"]
            )
        indexer.rebuild()
        assert detect_signals(config) == []

    def test_surfaces_uncovered_cluster(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        for i in range(3):
            _make_substack_source(
                vault, f"S{i}", concepts=["ai-capex", "hyperscaler"]
            )
        indexer.rebuild()
        signals = detect_signals(config)
        assert len(signals) == 1
        s = signals[0]
        assert s.source_type == "substack"
        assert set(s.shared_concepts) == {"ai-capex", "hyperscaler"}
        assert len(s.cluster_source_ids) == 3

    def test_skips_clusters_covered_by_active_theme(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        for i in range(3):
            _make_substack_source(
                vault, f"S{i}", concepts=["ai-capex", "hyperscaler"]
            )
        # Active theme that covers the cluster's concepts.
        vault.create_note(
            note_type=NoteType.THEME,
            title="AI capex unwind",
            extra_frontmatter={
                "concepts": ["ai-capex", "hyperscaler"],
                "status": "active",
            },
        )
        indexer.rebuild()
        assert detect_signals(config) == []

    def test_skips_clusters_with_existing_candidate(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        # First scan writes a stub. detect_signals must not re-emit
        # the same cluster (the stub dedup also applies to signals).
        for i in range(3):
            _make_substack_source(
                vault, f"S{i}", concepts=["ai-capex", "hyperscaler"]
            )
        indexer.rebuild()
        scan_candidates(config, source_type="substack")
        assert detect_signals(config) == []


class TestMintThemeFromSignal:
    def test_mints_theme_and_backfills_relates_to(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        paths = [
            _make_substack_source(
                vault, f"S{i}", concepts=["ai-capex", "hyperscaler"]
            )
            for i in range(3)
        ]
        indexer.rebuild()

        src_ids: list[str] = []
        for p in paths:
            fm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
            src_ids.append(fm["id"])

        theme_path = mint_theme_from_signal(
            config,
            slug="ai-capex",
            essence="AI capex unwind: hyperscaler spend reversal.",
            cluster_source_ids=src_ids,
            cluster_concepts=["ai-capex", "hyperscaler"],
        )

        # Theme file created with the proposed slug.
        assert theme_path.exists()
        fm, body = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
        assert fm["type"] == "theme"
        assert fm["status"] == "active"
        assert fm["title"] == "ai-capex"
        assert set(fm["cites"]) == set(src_ids)
        assert "ai-capex" in body.lower()
        thm_id = fm["id"]

        # Each source got `relates_to: [thm-XXX]` backfilled.
        for p in paths:
            sfm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
            rel = sfm.get("relates_to") or []
            assert thm_id in rel


class TestArchiveStaleCandidates:
    def test_recent_candidate_kept(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        for i in range(3):
            _make_substack_source(
                vault, f"S{i}", concepts=["ai-capex", "hyperscaler"]
            )
        indexer.rebuild()
        scan_candidates(config, source_type="substack")

        moved = archive_stale_candidates(config, stale_days=30)

        assert moved == []

    def test_aged_candidate_archived(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        import os
        from datetime import datetime, timedelta, timezone

        for i in range(3):
            _make_substack_source(
                vault, f"S{i}", concepts=["ai-capex", "hyperscaler"]
            )
        indexer.rebuild()
        scan_candidates(config, source_type="substack")

        cdir = config.vault_root / "themes" / CANDIDATES_DIR_NAME
        cand_path = next(cdir.glob("cand-*.md"))
        old_ts = (
            datetime.now(timezone.utc) - timedelta(days=60)
        ).timestamp()
        os.utime(cand_path, (old_ts, old_ts))

        moved = archive_stale_candidates(config, stale_days=30)

        assert len(moved) == 1
        archive_dir = cdir / CANDIDATES_ARCHIVE_NAME
        assert (archive_dir / cand_path.name).exists()
        assert not cand_path.exists()


class TestPromoteCandidate:
    def test_mints_thm_id_and_removes_candidate(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        for i in range(3):
            _make_substack_source(
                vault, f"S{i}", concepts=["ai-capex", "hyperscaler"]
            )
        indexer.rebuild()
        scan_candidates(config, source_type="substack")

        cdir = config.vault_root / "themes" / CANDIDATES_DIR_NAME
        cand_path = next(cdir.glob("cand-*.md"))
        cand_id = cand_path.stem.split("-")[0] + "-" + cand_path.stem.split("-")[1]

        target_path = promote_candidate(
            config,
            cand_id,
            title="AI capex unwind 2026",
            essence="Hyperscalers pulled forward GPU spend; 2026 is when ROI gets tested.",
        )

        assert target_path.exists()
        assert target_path.name.startswith("thm-")
        assert "ai-capex-unwind-2026" in target_path.name
        assert not cand_path.exists()

        fm, body = parse_frontmatter(target_path.read_text(encoding="utf-8"))
        assert fm["status"] == "active"
        assert fm["promoted_from"] == cand_id
        assert fm["id"].startswith("thm-")
        assert "ai-capex" in fm["concepts"]
        assert "hyperscaler" in fm["concepts"]
        assert "## Essence" in body
        assert "## Catalyst log" in body
        assert "Hyperscalers pulled forward" in body

    def test_missing_candidate_raises(self, config: Config):
        with pytest.raises(FileNotFoundError):
            promote_candidate(config, "cand-doesnotexist", title="Whatever")


def _make_theme(
    vault: VaultManager,
    title: str,
    *,
    catalyst_dates: list[str] | None = None,
    status: str = "active",
    concepts: list[str] | None = None,
) -> Path:
    """Create a canonical theme file with optional dated catalyst entries."""
    body_parts = [render_theme_body_skeleton(title)]
    if catalyst_dates:
        # Append catalyst log entries in the canonical grammar.
        log_lines = [
            f"- {d} · *new* — entry — [[src-test{i:04d}]]"
            for i, d in enumerate(catalyst_dates)
        ]
        # Replace the placeholder italics block under "## Catalyst log"
        body = body_parts[0]
        body = body.replace(
            "_Append-only. One entry per line, same format as concept hubs:_\n"
            "_`- YYYY-MM-DD · *flag* — one-liner — [[src-XXXX]]`_\n"
            "_Flags: `new`, `agrees`, `contradicts`, `extends`. For the latter "
            "three, append a date pointing to an earlier catalyst:_\n"
            "_`- YYYY-MM-DD · *contradicts YYYY-MM-DD* — text — [[src-XXXX]]`_",
            "\n".join(log_lines),
        )
        body_parts = [body]
    return vault.create_note(
        note_type=NoteType.THEME,
        title=title,
        body=body_parts[0],
        extra_frontmatter=build_theme_frontmatter(
            title,
            status=status,
            concepts=concepts or ["finance-regime"],
        ),
    )


class TestFindDormantThemes:
    """Deterministic dormancy detection — reads catalyst log dates,
    flags themes whose latest entry is older than the cutoff."""

    def test_theme_with_no_catalysts_is_dormant(
        self, vault: VaultManager, config: Config
    ):
        from datetime import date

        _make_theme(vault, "Empty theme")
        result = find_dormant_themes(config, today=date(2026, 5, 10))
        assert len(result) == 1
        path, last = result[0]
        assert last is None
        assert "empty-theme" in path.name

    def test_old_catalyst_is_dormant(
        self, vault: VaultManager, config: Config
    ):
        from datetime import date

        _make_theme(
            vault,
            "Old theme",
            catalyst_dates=["2025-01-15", "2025-02-20"],
        )
        result = find_dormant_themes(
            config, stale_days=90, today=date(2026, 5, 10)
        )
        assert len(result) == 1
        _, last = result[0]
        assert last == date(2025, 2, 20)

    def test_recent_catalyst_is_not_dormant(
        self, vault: VaultManager, config: Config
    ):
        from datetime import date

        _make_theme(
            vault,
            "Recent theme",
            catalyst_dates=["2026-04-15", "2026-05-01"],
        )
        result = find_dormant_themes(
            config, stale_days=90, today=date(2026, 5, 10)
        )
        assert result == []

    def test_resolved_theme_is_skipped(
        self, vault: VaultManager, config: Config
    ):
        from datetime import date

        _make_theme(vault, "Done theme", status="resolved")
        result = find_dormant_themes(config, today=date(2026, 5, 10))
        assert result == []

    def test_merged_theme_is_skipped(
        self, vault: VaultManager, config: Config
    ):
        from datetime import date

        _make_theme(
            vault, "Merged theme", status="merged-into:thm-other"
        )
        result = find_dormant_themes(config, today=date(2026, 5, 10))
        assert result == []


class TestFindResolvedThemes:
    """Deterministic resolution detection — walks the index edges table
    for decisions linked via implements/relates_to, flags themes whose
    decisions are all in terminal status."""

    def _make_decision(
        self,
        vault: VaultManager,
        *,
        title: str,
        implements_theme: str,
        status: str,
    ) -> Path:
        return vault.create_note(
            note_type=NoteType.DECISION,
            title=title,
            body=f"# {title}\n",
            extra_frontmatter={
                "status": status,
                "implements": [implements_theme],
                "concepts": ["finance-regime", "finance-structure"],
            },
        )

    def test_theme_with_all_terminal_decisions_is_resolved(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        path = _make_theme(vault, "Played out theme")
        theme_id = vault.read_note(path).id
        self._make_decision(
            vault,
            title="D1",
            implements_theme=theme_id,
            status="superseded",
        )
        self._make_decision(
            vault,
            title="D2",
            implements_theme=theme_id,
            status="deprecated",
        )
        indexer.rebuild()

        result = find_resolved_themes(config)
        assert len(result) == 1
        result_path, decision_ids = result[0]
        assert result_path == path
        assert len(decision_ids) == 2

    def test_theme_with_active_decision_is_not_resolved(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        path = _make_theme(vault, "Still active theme")
        theme_id = vault.read_note(path).id
        self._make_decision(
            vault,
            title="D1",
            implements_theme=theme_id,
            status="superseded",
        )
        self._make_decision(
            vault,
            title="D2",
            implements_theme=theme_id,
            status="accepted",  # not terminal
        )
        indexer.rebuild()

        result = find_resolved_themes(config)
        assert result == []

    def test_theme_with_no_linked_decisions_is_skipped(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        _make_theme(vault, "Orphan theme")
        indexer.rebuild()

        result = find_resolved_themes(config)
        assert result == []


# ---------------------------------------------------------------------------
# Helper — create event-grain source with optional proposed_theme stamp
# ---------------------------------------------------------------------------


def _make_event_source(
    vault: VaultManager,
    title: str,
    *,
    concepts: list[str],
    proposed_theme: str = "",
    source_type: str = "substack",
) -> Path:
    """Create an event-grain source note with optional proposed_theme stamp."""
    extra: dict = {
        "source_type": source_type,
        "concepts": concepts,
    }
    if proposed_theme:
        extra["proposed_theme"] = proposed_theme
    return vault.create_note(
        note_type=NoteType.SOURCE,
        title=title,
        body=f"# {title}\n",
        extra_frontmatter=extra,
    )


class TestProposedThemeAggregation:
    """``aggregate_proposed_themes`` tallies ``proposed_theme:`` stamps across
    the recent event-grain window, grouped by concept cluster. Mirrors the
    ``proposed_concepts:`` → ontology promotion pathway on the theme side.

    ``detect_signals`` enriches each signal with the top-voted slug so
    ``/dream`` can prefer it over composing a fresh name.
    """

    # ------------------------------------------------------------------
    # aggregate_proposed_themes basic cases
    # ------------------------------------------------------------------

    def test_two_same_slug_votes_one_without(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        """Three sources share ≥2 concepts; two stamp the same proposed_theme.
        Aggregation returns one vote entry with count=2."""
        _make_event_source(
            vault, "S0", concepts=["ai-capex", "hyperscaler"],
            proposed_theme="ai-capex-unwind",
        )
        _make_event_source(
            vault, "S1", concepts=["ai-capex", "hyperscaler"],
            proposed_theme="ai-capex-unwind",
        )
        _make_event_source(
            vault, "S2", concepts=["ai-capex", "hyperscaler"],
            # no proposed_theme — contributes to cluster but not to votes
        )
        indexer.rebuild()

        votes = aggregate_proposed_themes(config)

        assert len(votes) == 1
        v = votes[0]
        assert isinstance(v, ProposedThemeVote)
        assert v.slug == "ai-capex-unwind"
        assert v.votes == 2
        assert len(v.source_ids) == 2
        assert set(v.concepts) == {"ai-capex", "hyperscaler"}

    def test_all_different_slugs_three_single_votes(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        """Three sources each propose a different slug → three single-vote entries."""
        _make_event_source(
            vault, "S0", concepts=["ai-capex", "hyperscaler"],
            proposed_theme="alpha-slug",
        )
        _make_event_source(
            vault, "S1", concepts=["ai-capex", "hyperscaler"],
            proposed_theme="beta-slug",
        )
        _make_event_source(
            vault, "S2", concepts=["ai-capex", "hyperscaler"],
            proposed_theme="gamma-slug",
        )
        indexer.rebuild()

        votes = aggregate_proposed_themes(config)

        assert len(votes) == 3
        slugs = {v.slug for v in votes}
        assert slugs == {"alpha-slug", "beta-slug", "gamma-slug"}
        for v in votes:
            assert v.votes == 1

    def test_no_proposed_theme_anywhere(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        """Three sources form a cluster but none stamp proposed_theme →
        aggregation returns empty list."""
        for i in range(3):
            _make_event_source(
                vault, f"S{i}", concepts=["ai-capex", "hyperscaler"]
            )
        indexer.rebuild()

        votes = aggregate_proposed_themes(config)

        assert votes == []

    def test_single_source_no_cluster(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        """Only one source — no cluster forms (below min_cluster_size=3) →
        no aggregation entry, even if proposed_theme is set."""
        _make_event_source(
            vault, "Solo", concepts=["ai-capex", "hyperscaler"],
            proposed_theme="some-arc",
        )
        indexer.rebuild()

        votes = aggregate_proposed_themes(config)

        assert votes == []

    def test_returns_only_concept_grain_types_skipped(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        """Concept-grain source types (paper) are not scanned even if they
        have proposed_theme stamps."""
        for i in range(3):
            _make_event_source(
                vault, f"P{i}", concepts=["transformer", "attention"],
                proposed_theme="some-arc",
                source_type="paper",
            )
        indexer.rebuild()

        votes = aggregate_proposed_themes(config)

        assert votes == []

    # ------------------------------------------------------------------
    # detect_signals enrichment
    # ------------------------------------------------------------------

    def test_signal_carries_voted_slug_when_votes_exist(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        """When ≥2 sources in a cluster vote for the same slug, the signal
        gets voted_slug=<that slug> and slug_votes=<count>."""
        _make_event_source(
            vault, "S0", concepts=["ai-capex", "hyperscaler"],
            proposed_theme="ai-capex-unwind",
        )
        _make_event_source(
            vault, "S1", concepts=["ai-capex", "hyperscaler"],
            proposed_theme="ai-capex-unwind",
        )
        _make_event_source(
            vault, "S2", concepts=["ai-capex", "hyperscaler"],
        )
        indexer.rebuild()

        signals = detect_signals(config)

        assert len(signals) == 1
        s = signals[0]
        assert s.voted_slug == "ai-capex-unwind"
        assert s.slug_votes == 2

    def test_signal_voted_slug_none_when_no_votes(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        """When no sources in the cluster stamped proposed_theme, the signal's
        voted_slug is None and slug_votes is 0."""
        for i in range(3):
            _make_event_source(
                vault, f"S{i}", concepts=["ai-capex", "hyperscaler"]
            )
        indexer.rebuild()

        signals = detect_signals(config)

        assert len(signals) == 1
        s = signals[0]
        assert s.voted_slug is None
        assert s.slug_votes == 0

    def test_signal_tiebreak_lex_first_slug(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        """When multiple slugs tie on vote count, the lex-earlier (alphabetically
        first) slug wins the voted_slug position on the signal."""
        _make_event_source(
            vault, "S0", concepts=["ai-capex", "hyperscaler"],
            proposed_theme="zebra-arc",
        )
        _make_event_source(
            vault, "S1", concepts=["ai-capex", "hyperscaler"],
            proposed_theme="alpha-arc",
        )
        _make_event_source(
            vault, "S2", concepts=["ai-capex", "hyperscaler"],
            proposed_theme="mango-arc",
        )
        indexer.rebuild()

        signals = detect_signals(config)

        assert len(signals) == 1
        s = signals[0]
        # All three tie at 1 vote; lex-earliest wins.
        assert s.voted_slug == "alpha-arc"
        assert s.slug_votes == 1
