"""Priority signals + intake registries — ``vault/config/PRIORITIES.yaml``.

The single user-facing surface for what `/discover` surfaces and what
the external-trigger strategies (`rss_poll`, `mail_poll`) enqueue.
Mirrors the posture of ``sources/config.py:load_user_config`` — missing
file → empty dict, malformed YAML → empty dict (errors surface through
``weave doctor``, not at load time).

``focus.*`` is an **optional pin layer**, not the source of truth. "Active"
projects / themes / concepts are computed *behaviourally* by default — recent
session activity and probe pressure (the digest's "## Most actionable" block,
``operations/dream.py``; the SessionStart active-themes section). The lists
below are *appended as pins/boosts* on top of that automatic signal: a pinned
entry surfaces even when currently quiet, but you never have to maintain them
(an empty/missing ``focus`` block just yields pure behavioural salience).

Schema::

    focus:                                      # optional pins — boost, don't replace,
      active_projects: [thinkweave, ...]        #   the behavioural signal
      watch_themes: [thm-aaaa1111, ...]         #   (recent activity + probe pressure)
      research_concepts: [agent-harness, ...]   #   surface even when momentarily quiet

    intake:
      news:
        outlets: {<slug>: {name, feeds, tier, region, prefer_embedded, daily_cap}}
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
only (`[a, b, c]`) — the thinkweave YAML reader silently parses
block-style `- item` lists as empty.

PRIORITIES.yaml lives only at ``vault/config/PRIORITIES.yaml`` — there
is no legacy fallback location. It is the sole feed registry for
news/podcast outlets and newsletter senders; the standalone
``*_feeds.yaml`` files were retired 2026-06-13. Inline ``channels:`` in
``sources.yaml`` survives only as the youtube fallback when the
``intake.<slug>.channels`` block is unset.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from thinkweave.acquisition.sources.config import _parse_simple_yaml


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
    """Pinned project names from ``focus.active_projects``.

    Optional boost layer: the digest's active-focus block ranks projects by
    recent session activity, then appends these pins so a watched-but-quiet
    project still surfaces. Empty list when absent → pure behavioural ranking.
    """
    focus = priorities.get("focus") or {}
    raw = focus.get("active_projects") or []
    if not isinstance(raw, list):
        return []
    return [str(p) for p in raw if p]


def focus_watch_themes(priorities: dict[str, Any]) -> list[str]:
    """Pinned theme IDs from ``focus.watch_themes``.

    Optional boost layer: the SessionStart active-themes section orders by
    project-stake then recency (behavioural), then floats these pins to the
    top; ``decision_review`` likewise biases decisions implementing them.
    Empty list when absent → pure recency.
    """
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


def apply_pins(behavioral_ranked: list[str], pins: list[str]) -> list[str]:
    """Append declared ``focus.*`` pins as a floor beneath a behavioural ranking.

    The single, uniform semantic for ``focus.*`` across the codebase:
    **behavioural activity leads; pins are a guaranteed-present floor.**
    Returns ``behavioral_ranked`` with any pin not already present
    appended (order-preserving, deduped) — so a pinned-but-quiet
    project/concept still surfaces, but never above what the user is
    actually active on. Empty ``pins`` → ``behavioral_ranked`` unchanged.
    """
    out = list(behavioral_ranked)
    seen = set(out)
    for pin in pins:
        if pin and pin not in seen:
            out.append(pin)
            seen.add(pin)
    return out


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
