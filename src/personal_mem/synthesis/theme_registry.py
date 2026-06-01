"""Theme registry — ``vault/.mem/themes.yaml``.

Structural symmetry with the concept ontology (``vault/.mem/ontology.yaml``):
a machine-maintained manifest of every canonical theme, kept in sync by
mutation hooks in ``mint_theme_from_signal`` and
the ``dream`` apply step (status changes). The registry enables an O(1)
``is_canonical`` lookup at note-create time so that ``relates_to: [thm-X]``
references can be soft-validated without a vault scan.

YAML shape::

    themes:
      - id: thm-ccc63520
        slug: iran-hormuz-shock
        status: active
        concepts: [geopolitics, macro-trading, commodities]
        parent: null
        project: ""

Functions
---------
rebuild(config)     Glob vault/themes/ (not _candidates/), parse each via
                    parse_frontmatter, write themes.yaml. Returns count.
load(config)        Read themes.yaml → dict keyed by thm-id. Empty if missing.
upsert(config, thm_id, fields)
                    Incremental update/insert. Idempotent.
remove(config, thm_id)
                    Delete an entry. Returns True if it existed.
is_canonical(config, thm_id)
                    O(1) lookup via load().

Design notes
------------
- Uses ``pyyaml`` (yaml.safe_load / yaml.safe_dump) — the themes list is a
  sequence of dicts, a shape the hand-rolled sources.yaml parser doesn't
  support.
- Mutation failures must not cascade: every call site wraps this module
  defensively (``except Exception``). The registry is a derived artifact;
  the canonical truth is the markdown files. A stale or missing registry
  is a degraded state (no validation gate) not a hard failure.
- ``rebuild()`` is the recovery path when the registry drifts — exposed as
  ``mem themes rebuild-registry``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from personal_mem.core.config import Config

logger = logging.getLogger(__name__)

_REGISTRY_RELPATH = Path(".mem") / "themes.yaml"


def _registry_path(config: Config) -> Path:
    return config.vault_root / _REGISTRY_RELPATH


def _themes_dir(config: Config) -> Path:
    return config.vault_root / "themes"


def _entry_from_fm(fm: dict) -> dict[str, Any]:
    """Build a registry entry dict from a theme's frontmatter."""
    concepts = fm.get("concepts") or []
    if isinstance(concepts, str):
        concepts = [c.strip() for c in concepts.split(",") if c.strip()]
    return {
        "id": fm.get("id", ""),
        "slug": _slug_from_fm(fm),
        "status": fm.get("status", "active"),
        "concepts": list(concepts),
        "parent": fm.get("parent") or None,
        "project": fm.get("project") or "",
    }


def _slug_from_fm(fm: dict) -> str:
    """Derive a slug from the theme frontmatter.

    Preference order:
    1. ``slug:`` field (if set explicitly)
    2. ``title:`` field (the rename convention from 2026-05-25 — themes use
       title as the kebab slug, e.g. ``title: "iran-hormuz-shock"``)
    3. Derive from ``aliases:`` list (first entry that doesn't look like a thm-id)
    4. Empty string — caller can enrich later
    """
    if fm.get("slug"):
        return str(fm["slug"])
    if fm.get("title"):
        return str(fm["title"])
    aliases = fm.get("aliases") or []
    if isinstance(aliases, list):
        for alias in aliases:
            if alias and not str(alias).startswith("thm-"):
                return str(alias)
    return ""


def rebuild(config: Config) -> int:
    """Glob ``vault/themes/*.md`` (not ``_candidates/``), parse frontmatter,
    write ``vault/.mem/themes.yaml``. Returns the count of themes written.

    The registry is rebuilt from scratch on each call. This is the recovery
    path when entries drift (e.g. manual file edits, failed upserts).
    Candidates at ``vault/themes/_candidates/`` and their archive
    ``_candidates/_archive/`` are excluded by the non-recursive glob.
    """
    import yaml
    from personal_mem.core.vault import parse_frontmatter

    themes_dir = _themes_dir(config)
    entries: list[dict[str, Any]] = []

    if themes_dir.exists():
        for path in sorted(themes_dir.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
                fm, _ = parse_frontmatter(text)
            except Exception as e:  # noqa: BLE001
                logger.debug("theme_registry.rebuild: skipping %s — %s", path, e)
                continue

            thm_id = fm.get("id", "")
            if not thm_id or not str(thm_id).startswith("thm-"):
                continue  # skip non-canonical (candidates without thm- id)

            entries.append(_entry_from_fm(fm))

    registry_path = _registry_path(config)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"themes": entries}
    registry_path.write_text(
        yaml.safe_dump(payload, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    return len(entries)


def load(config: Config) -> dict[str, dict[str, Any]]:
    """Read ``vault/.mem/themes.yaml`` and return a dict keyed by thm-id.

    Returns an empty dict when the file does not exist or is empty.
    Each value is ``{id, slug, status, concepts, parent, project}``.
    """
    import yaml

    path = _registry_path(config)
    if not path.exists():
        return {}

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("theme_registry.load: failed to parse %s — %s", path, e)
        return {}

    if not raw or "themes" not in raw:
        return {}

    result: dict[str, dict[str, Any]] = {}
    for entry in raw.get("themes") or []:
        thm_id = entry.get("id", "")
        if thm_id:
            result[thm_id] = dict(entry)
    return result


def upsert(config: Config, thm_id: str, fields: dict) -> None:
    """Update or insert a theme entry in the registry.

    Reads the current registry, merges ``fields`` into the existing entry (if
    any) or creates a new one, and rewrites the file. Idempotent: calling twice
    with identical fields produces the same YAML.

    When updating an existing entry, only the keys present in ``fields`` are
    overwritten — other fields (slug, concepts, etc.) are preserved. This makes
    partial updates safe: ``upsert(cfg, thm_id, {"status": "dormant"})`` can be
    called from the dream apply step without clobbering the slug or concepts.
    """
    import yaml

    registry = load(config)

    # Start from the existing entry (if any) so partial updates preserve fields.
    existing = registry.get(thm_id, {})
    entry: dict[str, Any] = dict(existing)
    entry.update(fields)

    # Ensure all canonical fields are present with sensible defaults.
    entry["id"] = thm_id
    entry.setdefault("slug", "")
    entry.setdefault("status", "active")
    entry.setdefault("concepts", [])
    entry.setdefault("parent", None)
    entry.setdefault("project", "")

    registry[thm_id] = entry

    registry_path = _registry_path(config)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"themes": list(registry.values())}
    registry_path.write_text(
        yaml.safe_dump(payload, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )


def remove(config: Config, thm_id: str) -> bool:
    """Remove a theme entry from the registry.

    Returns ``True`` if the entry existed and was removed, ``False`` if it
    was not present. Used when themes are deleted (rare).
    """
    import yaml

    registry = load(config)
    if thm_id not in registry:
        return False

    del registry[thm_id]
    registry_path = _registry_path(config)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"themes": list(registry.values())}
    registry_path.write_text(
        yaml.safe_dump(payload, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    return True


def is_canonical(config: Config, thm_id: str) -> bool:
    """Return ``True`` iff ``thm_id`` is present in the registry.

    O(1) lookup via ``load()``. Returns ``False`` when the registry does not
    exist (degraded state — the soft gate drops to no-op rather than
    blocking all creates).
    """
    return thm_id in load(config)
