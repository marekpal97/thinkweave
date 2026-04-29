"""Themes — global narrative aggregators with a slow-moving Essence and
an append-only Catalyst log.

Themes are NoteType.THEME — a sibling of note/session/decision/source. They
live at ``vault/themes/{thm-XXXX}-{slug}.md`` (global, not project-nested),
so external sources, news, and research from any project can cite them via
``[[thm-XXXX]]`` or ``relates_to: [thm-XXXX]``.

The ``project:`` frontmatter field on a theme is **informational** (primary
stake) — it never controls filing. Theme dedup, listing, and DAG rendering
all operate over the global set.

This module owns the canonical frontmatter builder, the body skeleton, and
parser helpers shared with the temporal-DAG renderer (see ``temporal.py``,
Workstream C).
"""

from __future__ import annotations


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
    log`` (dated event log; format upgraded for temporal DAG in Workstream
    C), and ``## Open questions`` (probe-style follow-ups).
    """
    return (
        f"# {title}\n\n"
        "## Essence\n\n"
        "_Replace with the working thesis (≤500w). Slow-moving; revised "
        "rarely. Cite finance/* concepts (or analogous invariants for "
        "non-finance themes); never named events._\n\n"
        "## Catalyst log\n\n"
        "_Append-only dated events. Each entry: a date, a one-liner, a "
        "source citation (`[[src-XXXX]]`), a flag (`new` / `confirms` / "
        "`contradicts`), and an optional reference to a prior catalyst._\n\n"
        "## Open questions\n\n"
    )
