"""Declarative source-type registry.

Every source type in personal_mem is described by a single ``SourceTypeSpec``
entry here. ``VaultManager.create_note`` reads the registry to decide how to
route a new source note on disk; CLI commands (``mem sources list/show``)
read it to surface what's available; skills read it via their own
frontmatter to declare which type they handle.

The registry is intentionally **open-world**: ``get_spec`` returns ``None``
for unregistered source types, and the vault falls back to a plain folder
layout with an empty bucket (``sources/<slug>/source.md``). This keeps
ad-hoc experimentation cheap — you can write a source with an unregistered
``source_type`` and it will still land somewhere sensible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Layout = Literal["flat", "folder", "author_folder"]


@dataclass(frozen=True)
class SourceTypeSpec:
    """Declarative spec for a single source type.

    Attributes:
        slug: canonical ``source_type`` value written into frontmatter.
        bucket: subfolder under ``vault/sources/`` (or ``projects/X/sources/``).
        layout: routing pattern — ``flat`` (single file), ``folder`` (slug
            subdirectory with companion raw content), or ``author_folder``
            (author-nested slug subdirectory, falls back to ``folder`` when
            author is missing).
        aliases: legacy ``source_type`` values that should be folded into
            ``slug`` on write. e.g. ``("github",)`` for the ``repo`` slug.
        skills: filenames (without ``.md``) under ``commands/`` that handle
            this source type. Informational only — used by ``mem sources
            show`` to cross-reference.
        description: one-liner shown by ``mem sources list``.
    """

    slug: str
    bucket: str
    layout: Layout
    aliases: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    description: str = ""


REGISTRY: dict[str, SourceTypeSpec] = {
    "paper": SourceTypeSpec(
        slug="paper",
        bucket="papers",
        layout="folder",
        skills=("research", "discover"),
        description=(
            "Research papers (arXiv, PDFs). Import via /research, discover gaps via /discover."
        ),
    ),
    "repo": SourceTypeSpec(
        slug="repo",
        bucket="repos",
        layout="folder",
        aliases=("github",),
        skills=("research", "discover"),
        description=(
            "Code repositories (GitHub, awesome-lists). Import via /research, discover via /discover."
        ),
    ),
    "article": SourceTypeSpec(
        slug="article",
        bucket="articles",
        layout="folder",
        skills=("research", "discover"),
        description=(
            "Blog posts and web articles. Import via /research, discover via /discover."
        ),
    ),
    "conversation": SourceTypeSpec(
        slug="conversation",
        bucket="conversations",
        layout="flat",
        description=(
            "ChatGPT conversation exports. Imported via `mem import chatgpt`."
        ),
    ),
    "substack": SourceTypeSpec(
        slug="substack",
        bucket="substack",
        layout="author_folder",
        skills=("substack",),
        description=(
            "Substack newsletters. Acquired via /substack from the disk inbox."
        ),
    ),
}


def normalize(source_type: str) -> str:
    """Fold legacy aliases into the canonical slug. Unknown types pass through."""
    if not source_type:
        return source_type
    for spec in REGISTRY.values():
        if source_type in spec.aliases:
            return spec.slug
    return source_type


def get_spec(source_type: str) -> SourceTypeSpec | None:
    """Return the spec for a canonical source_type, or ``None`` for unregistered types.

    Unregistered types are intentional — callers (e.g. VaultManager) fall
    back to a folder layout with an empty bucket. See
    ``test_source_global_default`` for the asserted behavior.
    """
    if not source_type:
        return None
    return REGISTRY.get(normalize(source_type))


def all_specs() -> list[SourceTypeSpec]:
    """Return every registered spec in insertion order."""
    return list(REGISTRY.values())
