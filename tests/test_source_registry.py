"""Unit tests for the source-type registry.

Covers the SourceTypeSpec dataclass, the five registered types, alias
normalization, and the open-world fallback for unregistered types.
"""

from __future__ import annotations

from personal_mem.sources import (
    REGISTRY,
    SourceTypeSpec,
    all_specs,
    get_spec,
    normalize,
)


def test_registry_has_expected_slugs():
    """Every slug documented in ARCHITECTURE.md must be registered."""
    expected = {
        "paper",
        "repo",
        "article",
        "conversation",
        "substack",
        "news",
        "newsletter-events",
        "newsletter-concepts",
        "youtube-events",
        "youtube-concepts",
    }
    assert set(REGISTRY.keys()) == expected


def test_every_spec_is_frozen_dataclass():
    """SourceTypeSpec is frozen so accidental mutation raises."""
    spec = REGISTRY["paper"]
    try:
        spec.slug = "mutated"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("SourceTypeSpec should be frozen")


def test_all_specs_returns_every_entry_in_insertion_order():
    specs = all_specs()
    assert [s.slug for s in specs] == [
        "paper",
        "repo",
        "article",
        "conversation",
        "substack",
        "news",
        "newsletter-events",
        "newsletter-concepts",
        "youtube-events",
        "youtube-concepts",
    ]


def test_layouts_cover_all_three_patterns():
    """flat + folder + author_folder are all used — proves the dispatch code paths matter."""
    layouts = {spec.layout for spec in all_specs()}
    assert layouts == {"flat", "folder", "author_folder"}


def test_get_spec_canonical_slug():
    spec = get_spec("paper")
    assert spec is not None
    assert spec.bucket == "papers"
    assert spec.layout == "folder"


def test_get_spec_alias_github_resolves_to_repo():
    spec = get_spec("github")
    assert spec is not None
    assert spec.slug == "repo"
    assert spec.bucket == "repos"


def test_get_spec_unregistered_returns_none():
    """Open-world: unregistered types are legal, callers handle fallback."""
    assert get_spec("podcast") is None
    assert get_spec("") is None


def test_normalize_alias_folds_to_slug():
    assert normalize("github") == "repo"
    assert normalize("arxiv") in {"arxiv", "paper"}  # arxiv isn't in aliases, should pass through


def test_normalize_canonical_slug_unchanged():
    assert normalize("paper") == "paper"
    assert normalize("substack") == "substack"


def test_normalize_unregistered_passes_through():
    """Unregistered types stay as-is, they're not an error."""
    assert normalize("podcast") == "podcast"
    assert normalize("custom-xyz") == "custom-xyz"


def test_substack_uses_author_folder_layout():
    spec = get_spec("substack")
    assert spec is not None
    assert spec.layout == "author_folder"
    assert "substack" in spec.skills


def test_conversation_uses_flat_layout():
    spec = get_spec("conversation")
    assert spec is not None
    assert spec.layout == "flat"


def test_paper_and_repo_share_research_and_discover_skills():
    for slug in ("paper", "repo", "article"):
        spec = get_spec(slug)
        assert spec is not None
        assert "research" in spec.skills
        assert "discover" in spec.skills


def test_dataclass_direct_construction():
    """SourceTypeSpec can be built without going through the registry."""
    spec = SourceTypeSpec(
        slug="podcast",
        bucket="podcasts",
        layout="folder",
        description="test",
    )
    assert spec.slug == "podcast"
    assert spec.aliases == ()
    assert spec.skills == ()


# ---------------------------------------------------------------------------
# Newsletter source-type pair — slug encodes the grain, not the topic.
# Two specs, one pipeline (commands/newsletter.md + research-newsletter-worker).
# ---------------------------------------------------------------------------


def test_newsletter_events_is_event_grain():
    """newsletter-events triggers the theme-candidate floater on create."""
    spec = get_spec("newsletter-events")
    assert spec is not None
    assert spec.slug == "newsletter-events"
    assert spec.layout == "author_folder"
    assert spec.temporal_grain == "event"
    assert "newsletter" in spec.skills


def test_newsletter_concepts_is_concept_grain():
    """newsletter-concepts uses concept hubs only — no theme floating."""
    spec = get_spec("newsletter-concepts")
    assert spec is not None
    assert spec.slug == "newsletter-concepts"
    assert spec.layout == "author_folder"
    assert spec.temporal_grain == "concept"
    assert "newsletter" in spec.skills


def test_newsletter_pair_shares_layout_and_skill():
    """The pair is intentionally symmetric except on temporal_grain."""
    events = get_spec("newsletter-events")
    concepts = get_spec("newsletter-concepts")
    assert events is not None and concepts is not None
    assert events.layout == concepts.layout == "author_folder"
    assert set(events.skills) == set(concepts.skills)
    # The whole point of the pair: only grain differs.
    assert events.temporal_grain != concepts.temporal_grain


# ---------------------------------------------------------------------------
# YouTube source-type pair — same slug-encodes-grain pattern as newsletter.
# Two specs, one pipeline (commands/youtube.md + research-youtube-worker).
# ---------------------------------------------------------------------------


def test_youtube_events_is_event_grain():
    """youtube-events triggers the theme-candidate floater on create."""
    spec = get_spec("youtube-events")
    assert spec is not None
    assert spec.slug == "youtube-events"
    assert spec.layout == "author_folder"
    assert spec.temporal_grain == "event"
    assert "youtube" in spec.skills


def test_youtube_concepts_is_concept_grain():
    """youtube-concepts uses concept hubs only — no theme floating."""
    spec = get_spec("youtube-concepts")
    assert spec is not None
    assert spec.slug == "youtube-concepts"
    assert spec.layout == "author_folder"
    assert spec.temporal_grain == "concept"
    assert "youtube" in spec.skills


def test_youtube_pair_shares_layout_and_skill():
    """The pair is intentionally symmetric except on temporal_grain."""
    events = get_spec("youtube-events")
    concepts = get_spec("youtube-concepts")
    assert events is not None and concepts is not None
    assert events.layout == concepts.layout == "author_folder"
    assert set(events.skills) == set(concepts.skills)
    assert events.temporal_grain != concepts.temporal_grain
