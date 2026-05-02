"""Themes — global narrative aggregators with a slow-moving Essence and
an append-only Catalyst log.

Themes are NoteType.THEME — a sibling of note/session/decision/source. They
live at ``vault/themes/{thm-XXXX}-{slug}.md`` (global, not project-nested),
so external sources, news, and research from any project can cite them via
``[[thm-XXXX]]`` or ``relates_to: [thm-XXXX]``.

The ``project:`` frontmatter field on a theme is **informational** (primary
stake) — it never controls filing. Theme dedup, listing, and DAG rendering
all operate over the global set.

This module owns the canonical theme frontmatter builder, the body skeleton,
the lifecycle constants, and a thin parser that delegates to the unified
``Hub`` spine for catalyst-log parsing. Concept hubs and theme hubs share
that spine — see ``synthesis/hub.py`` — so the catalyst-log grammar lives
in exactly one place.

The theme-hub *specialisation* (vs concept hubs):

- **Identity**: UUID-shaped (``thm-XXXX``), not a vocabulary term.
- **Auto-update**: none. Catalysts are authored manually (or by skills
  like ``/themes-resolve``), not extracted from sessions on every run.
- **Lifecycle**: ``active`` → ``dormant`` / ``resolved`` / ``merged-into:thm-X``.
- **Citation direction**: notes cite a theme via ``relates_to: [thm-X]``,
  not via ``concepts: [...]``.
- **Storage**: ``vault/themes/`` (global), never project-nested.
"""

from __future__ import annotations

from personal_mem.synthesis.hub import (
    CATALYST_LOG_HEADING,
    Hub,
    HubLogEntry,
    parse_log_section,
)

# Re-export so callers continue to import LogEntry from the theme module.
LogEntry = HubLogEntry

# Canonical lifecycle states for themes. ``merged-into:thm-XXXXXXXX`` is a
# sentinel form for survivors-with-a-reference, written by the dedup skill.
THEME_STATUS_ACTIVE = "active"
THEME_STATUS_DORMANT = "dormant"
THEME_STATUS_RESOLVED = "resolved"

THEME_STATUSES = (
    THEME_STATUS_ACTIVE,
    THEME_STATUS_DORMANT,
    THEME_STATUS_RESOLVED,
)


def build_theme_frontmatter(
    title: str,
    project: str = "",
    concepts: list[str] | None = None,
    relates_to: list[str] | None = None,
    status: str = THEME_STATUS_ACTIVE,
    **extra: object,
) -> dict:
    """Build a canonical theme frontmatter dict.

    Args:
        title: human-readable theme title (e.g. "AI capex unwind 2026").
        project: optional primary-stake project. Informational only —
            does not affect filing (themes are global).
        concepts: ontology concepts the theme cites. By convention these
            are stable invariants (``finance/regime``, ``finance/structure``,
            etc.) — not named events. Defaults to ``[]``.
        relates_to: list of other theme IDs (e.g. ``["thm-aaaa1111"]``)
            this theme relates to. Defaults to ``[]``.
        status: lifecycle state — one of THEME_STATUSES, or the sentinel
            ``merged-into:thm-XXXX`` written by the dedup skill. Defaults
            to ``active``.
        **extra: any theme-specific fields. Merged after canonical fields
            so callers can override.

    Returns:
        Plain ``dict`` suitable for ``extra_frontmatter`` on
        ``VaultManager.create_note(note_type=NoteType.THEME, ...)``. The
        ``id``, ``type``, and ``date`` fields are added by ``create_note``.
    """
    fm: dict = {
        "title": title,
        "status": status,
        "concepts": list(concepts) if concepts else [],
        "relates_to": list(relates_to) if relates_to else [],
    }
    if project:
        fm["project"] = project
    fm.update(extra)
    return fm


def render_theme_body_skeleton(title: str) -> str:
    """Initial body for a new theme.

    Three sections — ``## Essence`` (slow-moving thesis), ``## Catalyst
    log`` (dated event log; same grammar as concept hubs so the temporal-
    DAG renderer consumes both), and ``## Open questions`` (probe follow-
    ups). The Evolution view is a derived render in THEMES.md, not stored
    on the theme page.
    """
    return (
        f"# {title}\n\n"
        "## Essence\n\n"
        "_Replace with the working thesis (≤500w). Slow-moving; revised "
        "rarely. Cite finance/* concepts (or analogous invariants for "
        "non-finance themes); never named events._\n\n"
        f"{CATALYST_LOG_HEADING}\n\n"
        "_Append-only. One entry per line, same format as concept hubs:_\n"
        "_`- YYYY-MM-DD · *flag* — one-liner — [[src-XXXX]]`_\n"
        "_Flags: `new`, `agrees`, `contradicts`, `extends`. For the latter "
        "three, append a date pointing to an earlier catalyst:_\n"
        "_`- YYYY-MM-DD · *contradicts YYYY-MM-DD* — text — [[src-XXXX]]`_\n\n"
        "## Open questions\n\n"
    )


def parse_theme_catalyst_log(body: str) -> list[HubLogEntry]:
    """Parse the ``## Catalyst log`` section of a theme body.

    Returns a list of ``HubLogEntry`` records — the same dataclass concept
    hubs use, sourced from ``synthesis.hub``. Empty list if the section is
    absent or empty.
    """
    return parse_log_section(body, CATALYST_LOG_HEADING)


def parse_theme(path) -> Hub:
    """Parse a theme file from disk into a unified ``Hub`` view.

    Convenience wrapper that exposes the shared spine on the theme
    surface. Theme-specific helpers (``build_theme_frontmatter``,
    ``render_theme_body_skeleton``, status constants) stay here; the log
    grammar lives in ``Hub.parse``.
    """
    return Hub.parse(path)
