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

Users can also register new source types **without editing this file** by
dropping entries into ``<vault_root>/.mem/source_types.yaml`` — see
``load_user_specs`` below and the ``mem sources scaffold`` CLI command.
User-side specs are consulted before the in-code REGISTRY when callers
pass a ``vault_root`` to ``get_spec``/``all_specs``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Layout = Literal["flat", "folder", "author_folder"]
_VALID_LAYOUTS: tuple[str, ...] = ("flat", "folder", "author_folder")

TemporalGrain = Literal["event", "concept", "none"]
_VALID_TEMPORAL_GRAINS: tuple[str, ...] = ("event", "concept", "none")


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
        temporal_grain: how the source type relates to time-ordered narrative.
            ``event`` — emits temporally-anchored signals (substack, news);
            theme-candidate floater runs at post-ingest and may write stubs
            to ``vault/themes/_candidates/``.
            ``concept`` — emits domain knowledge with no inherent time arc
            (paper, repo, article); concept hubs handle synthesis, no theme
            floating.
            ``none`` — no synthesis hook (conversation, ad-hoc capture).
            Default is ``concept`` so adding a new source type doesn't
            silently start floating themes.
    """

    slug: str
    bucket: str
    layout: Layout
    aliases: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    description: str = ""
    temporal_grain: TemporalGrain = "concept"


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
        temporal_grain="none",
    ),
    "substack": SourceTypeSpec(
        slug="substack",
        bucket="substack",
        layout="author_folder",
        skills=("substack",),
        description=(
            "Substack newsletters. Acquired via /substack from the disk inbox."
        ),
        temporal_grain="event",
    ),
    "news": SourceTypeSpec(
        slug="news",
        bucket="news",
        layout="author_folder",
        # `news` is the one-off URL ingest skill; `drain` handles the
        # queued/cron path via the `research-news-worker` subagent. No
        # `research-news` skill — news intentionally skips Path A of the
        # router (sequential Skill dispatch) in favour of triage+writer
        # fan-out. See commands/news.md and commands/drain.md.
        skills=("news", "drain"),
        description=(
            "Curated financial / macro news. RSS+cron intake, theme-triaged "
            "via Haiku, subagent drain. Outlet drives author folder."
        ),
        temporal_grain="event",
    ),
}


def normalize(source_type: str, vault_root: Path | None = None) -> str:
    """Fold legacy aliases into the canonical slug. Unknown types pass through.

    When ``vault_root`` is provided, user-side aliases declared in
    ``<vault_root>/.mem/source_types.yaml`` are consulted alongside the
    in-code REGISTRY. User aliases win when there's overlap.
    """
    if not source_type:
        return source_type
    if vault_root is not None:
        for spec in load_user_specs(vault_root).values():
            if source_type in spec.aliases:
                return spec.slug
    for spec in REGISTRY.values():
        if source_type in spec.aliases:
            return spec.slug
    return source_type


def get_spec(
    source_type: str, vault_root: Path | None = None
) -> SourceTypeSpec | None:
    """Return the spec for a canonical source_type, or ``None`` for unregistered types.

    User-side specs (from ``<vault_root>/.mem/source_types.yaml``) are
    consulted first when ``vault_root`` is provided; the in-code REGISTRY
    is the fallback. Unregistered types are intentional — callers (e.g.
    VaultManager) fall back to a folder layout with an empty bucket. See
    ``test_source_global_default`` for the asserted behavior.

    Backwards-compatible: callers that don't pass ``vault_root`` see only
    the in-code REGISTRY, exactly as before.
    """
    if not source_type:
        return None
    canonical = normalize(source_type, vault_root=vault_root)
    if vault_root is not None:
        user_specs = load_user_specs(vault_root)
        if canonical in user_specs:
            return user_specs[canonical]
    return REGISTRY.get(canonical)


def all_specs(vault_root: Path | None = None) -> list[SourceTypeSpec]:
    """Return every registered spec.

    With a ``vault_root``, user-side specs are merged on top of the in-code
    REGISTRY (user wins on slug collision). Without one, only in-code
    REGISTRY entries are returned, in insertion order — preserving the
    pre-overlay contract.
    """
    if vault_root is None:
        return list(REGISTRY.values())
    user_specs = load_user_specs(vault_root)
    merged: dict[str, SourceTypeSpec] = dict(REGISTRY)
    merged.update(user_specs)
    return list(merged.values())


# ---------------------------------------------------------------------------
# User-side overlay loader
# ---------------------------------------------------------------------------


def load_user_specs(vault_root: Path) -> dict[str, SourceTypeSpec]:
    """Read ``<vault_root>/.mem/source_types.yaml`` and parse SourceTypeSpec
    entries.

    File shape (top-level keys are slugs, values are spec mappings)::

        podcast:
          bucket: podcasts
          layout: folder
          description: "Podcast episodes."
          aliases: [pod, audio]
          skills: [podcast]
        email:
          bucket: emails
          layout: flat
          description: "Email threads."

    Missing file → empty dict (no error). Malformed YAML or invalid entries
    → empty dict + stderr warning, mirroring config.py's posture (the
    framework should stay alive when a half-edited overlay is in flight;
    ``mem doctor`` is where the real surfacing happens).
    """
    from personal_mem.sources.config import _parse_simple_yaml

    user_path = Path(vault_root) / ".mem" / "source_types.yaml"
    if not user_path.exists():
        return {}
    try:
        doc = _parse_simple_yaml(user_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(
            f"warning: malformed {user_path}: {exc} — ignoring user source_types overlay",
            file=sys.stderr,
        )
        return {}
    if not isinstance(doc, dict):
        return {}

    out: dict[str, SourceTypeSpec] = {}
    for slug, payload in doc.items():
        if not isinstance(payload, dict):
            print(
                f"warning: source_types.yaml entry for {slug!r} is not a mapping — skipping",
                file=sys.stderr,
            )
            continue
        bucket = payload.get("bucket", "")
        layout = payload.get("layout", "folder")
        if layout not in _VALID_LAYOUTS:
            print(
                f"warning: source_types.yaml entry for {slug!r} has invalid layout "
                f"{layout!r} (must be one of {_VALID_LAYOUTS}) — skipping",
                file=sys.stderr,
            )
            continue
        aliases_raw = payload.get("aliases", []) or []
        skills_raw = payload.get("skills", []) or []
        aliases = tuple(str(a) for a in aliases_raw) if isinstance(aliases_raw, list) else ()
        skills = tuple(str(s) for s in skills_raw) if isinstance(skills_raw, list) else ()
        temporal_grain = payload.get("temporal_grain", "concept")
        if temporal_grain not in _VALID_TEMPORAL_GRAINS:
            print(
                f"warning: source_types.yaml entry for {slug!r} has invalid "
                f"temporal_grain {temporal_grain!r} (must be one of "
                f"{_VALID_TEMPORAL_GRAINS}) — defaulting to 'concept'",
                file=sys.stderr,
            )
            temporal_grain = "concept"
        out[str(slug)] = SourceTypeSpec(
            slug=str(slug),
            bucket=str(bucket),
            layout=layout,  # type: ignore[arg-type]
            aliases=aliases,
            skills=skills,
            description=str(payload.get("description", "")),
            temporal_grain=temporal_grain,  # type: ignore[arg-type]
        )
    return out
