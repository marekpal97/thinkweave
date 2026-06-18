"""Canonical source-note frontmatter builder.

Helper used by importers and skills to build source-note frontmatter with
consistent field ordering and names. Not a validator — callers can pass any
extra fields they need via ``**extra``. The goal is to prevent drift like
``author`` vs ``authors`` vs ``publication`` across different importers.
"""

from __future__ import annotations


def build_source_frontmatter(
    source_type: str,
    title: str,
    url: str = "",
    authors: list[str] | None = None,
    **extra: object,
) -> dict:
    """Build a canonical source frontmatter dict.

    Args:
        source_type: canonical source_type slug (must match a SourceTypeSpec
            in ``registry.py`` for routing to work; unregistered types are
            allowed and fall back to the folder layout).
        title: human-readable title for the source.
        url: canonical URL or URI (empty string is legal for e.g. local
            conversations).
        authors: list of author names; normalized to ``[]`` if ``None``.
        **extra: any source-specific fields (publication, published_at,
            raw_path, platform, …). Merged after the canonical fields so
            callers can override if needed.

    Returns:
        A plain ``dict`` suitable for passing as ``extra_frontmatter`` to
        ``VaultManager.create_note`` or as the ``frontmatter`` argument to
        ``weave_create``.
    """
    fm: dict = {
        "source_type": source_type,
        "title": title,
        "url": url,
        "authors": list(authors) if authors else [],
    }
    fm.update(extra)
    return fm
