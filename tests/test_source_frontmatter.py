"""Unit tests for build_source_frontmatter."""

from __future__ import annotations

from thinkweave.acquisition.sources import build_source_frontmatter


def test_minimal_call_returns_canonical_dict():
    fm = build_source_frontmatter(source_type="paper", title="Attention Is All You Need")
    assert fm["source_type"] == "paper"
    assert fm["title"] == "Attention Is All You Need"
    assert fm["url"] == ""
    assert fm["authors"] == []


def test_empty_url_is_legal():
    """Local content (conversations, non-URL sources) needs empty-url support."""
    fm = build_source_frontmatter(source_type="conversation", title="local chat", url="")
    assert fm["url"] == ""


def test_authors_list_copied_not_aliased():
    authors = ["Vaswani", "Shazeer"]
    fm = build_source_frontmatter(
        source_type="paper", title="x", authors=authors
    )
    assert fm["authors"] == authors
    fm["authors"].append("Added")
    assert authors == ["Vaswani", "Shazeer"]  # caller's list untouched


def test_authors_none_normalizes_to_empty_list():
    fm = build_source_frontmatter(source_type="paper", title="x", authors=None)
    assert fm["authors"] == []


def test_extra_fields_merge_and_override():
    fm = build_source_frontmatter(
        source_type="paper",
        title="x",
        url="https://arxiv.org/abs/1706.03762",
        arxiv_id="1706.03762",
        publication="NeurIPS 2017",
    )
    assert fm["arxiv_id"] == "1706.03762"
    assert fm["publication"] == "NeurIPS 2017"


def test_extra_field_preserved_alongside_canonical():
    """Extra fields sit alongside the canonical keys, not replacing them."""
    fm = build_source_frontmatter(
        source_type="paper",
        title="x",
        url="https://canonical",
        raw_path="raw.md",
    )
    assert fm["url"] == "https://canonical"
    assert fm["raw_path"] == "raw.md"
    # All four canonical keys survive extras
    assert set(fm.keys()) >= {"source_type", "title", "url", "authors", "raw_path"}


def test_returns_plain_dict():
    fm = build_source_frontmatter(source_type="paper", title="x")
    assert isinstance(fm, dict)
    assert type(fm) is dict


def test_substack_pattern_with_author_and_publication():
    fm = build_source_frontmatter(
        source_type="substack",
        title="The Cascade: Food vs. Rates",
        url="https://citrini.substack.com/p/the-cascade",
        authors=["Citrini"],
        publication="Citrini Research",
        raw_path="raw.md",
    )
    assert fm["source_type"] == "substack"
    assert fm["authors"] == ["Citrini"]
    assert fm["publication"] == "Citrini Research"
    assert fm["raw_path"] == "raw.md"


def test_github_repo_pattern():
    fm = build_source_frontmatter(
        source_type="repo",
        title="free-code",
        url="https://github.com/paoloanzn/free-code",
        authors=["paoloanzn"],
        stars=7800,
        language="TypeScript",
    )
    assert fm["source_type"] == "repo"
    assert fm["stars"] == 7800
    assert fm["language"] == "TypeScript"
