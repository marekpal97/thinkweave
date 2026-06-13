"""Priority signals + intake registries — ``vault/config/PRIORITIES.yaml``.

The single user-facing surface for what `/discover` surfaces and what
the external-trigger strategies (`rss_poll`, `mail_poll`) enqueue.
Mirrors the posture of ``sources/config.py:load_user_config`` — missing
file → empty dict, malformed YAML → empty dict (errors surface through
``mem doctor``, not at load time).

Schema::

    focus:
      active_projects: [personal_mem, ...]      # foreground in landing docs
      watch_themes: [thm-aaaa1111, ...]         # surface in STATE.md + /discover bias

    intake:
      news:
        outlets: {<slug>: {name, feeds, tier, region, prefer_embedded, daily_cap}}
        drain_window_days: 7
      podcast_events:
        outlets: {<slug>: {name, feeds, tier, language, daily_cap}}
      podcast_concepts:
        outlets: {...}
      youtube_events:
        channels: [UCxxx, ...]
        lookback_days: 1
        drain_batch_max: 5
      youtube_concepts:
        channels: [...]
        lookback_days: 1
        drain_batch_max: 5
      newsletter_events:
        senders: [...]
      newsletter_concepts:
        senders: [...]

Source-type slugs as keys preserve the grain split. Inline list syntax
only (`[a, b, c]`) — the personal_mem YAML reader silently parses
block-style `- item` lists as empty.

PRIORITIES.yaml lives only at ``vault/config/PRIORITIES.yaml`` — there
is no legacy fallback location. Before the user creates the file, the
strategies' legacy reads (`news_feeds.yaml`, inline `sources.yaml`
fields) supply the data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from personal_mem.acquisition.sources.config import _parse_simple_yaml


_PRIORITIES_FILENAME = "PRIORITIES.yaml"


def priorities_path(vault_root: Path | None) -> Path | None:
    """Canonical location of PRIORITIES.yaml, or None if no vault is configured."""
    if vault_root is None:
        return None
    return Path(vault_root) / "config" / _PRIORITIES_FILENAME


def load_priorities(vault_root: Path | None) -> dict[str, Any]:
    """Return the parsed PRIORITIES.yaml dict, or empty dict when absent.

    Missing file → ``{}``. Malformed YAML → ``{}`` (silent — same posture
    as ``load_user_config``). Callers should treat empty dict as "no
    priority signals declared" and fall through to legacy paths.
    """
    path = priorities_path(vault_root)
    if path is None or not path.exists():
        return {}
    try:
        doc = _parse_simple_yaml(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    if not isinstance(doc, dict):
        return {}
    return doc


def focus_active_projects(priorities: dict[str, Any]) -> list[str]:
    """List of project names from ``focus.active_projects``."""
    focus = priorities.get("focus") or {}
    raw = focus.get("active_projects") or []
    if not isinstance(raw, list):
        return []
    return [str(p) for p in raw if p]


def focus_watch_themes(priorities: dict[str, Any]) -> list[str]:
    """List of theme IDs from ``focus.watch_themes``."""
    focus = priorities.get("focus") or {}
    raw = focus.get("watch_themes") or []
    if not isinstance(raw, list):
        return []
    return [str(t) for t in raw if t]


def focus_concepts(priorities: dict[str, Any]) -> list[str]:
    """Concept slugs the user declared as research focus.

    Read from ``focus.research_concepts`` (the YAML key that survived the
    2026-06-06 ``concept_coverage`` strategy deletion). Exposed through a
    generic ``focus_concepts`` name so consumers — chiefly the
    ``focus_research`` discover strategy — aren't tied to the historical
    naming. Returns empty list when absent or malformed; the strategy
    treats that as "no declared focus" and emits no gaps.
    """
    focus = priorities.get("focus") or {}
    raw = focus.get("research_concepts") or []
    if not isinstance(raw, list):
        return []
    return [str(c) for c in raw if c]


def intake_for(priorities: dict[str, Any], source_type: str) -> dict[str, Any]:
    """Return ``intake.<source_type>`` dict (or empty if unset).

    The source_type slug is normalised to underscore form (``newsletter_events``,
    not ``newsletter-events``) so it can be used as a YAML key.
    """
    intake = priorities.get("intake") or {}
    if not isinstance(intake, dict):
        return {}
    key = source_type.replace("-", "_")
    block = intake.get(key)
    if not isinstance(block, dict):
        return {}
    return block
