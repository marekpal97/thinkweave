"""One-shot data migrations between personal_mem refactors.

Migrations are intentionally simple and idempotent — re-running one is
always safe. Each function takes a ``vault_root`` (or full ``Config``)
and returns a count of records affected. Wire-up to the CLI lives in
``mem doctor --migrate`` (Phase 1) and is invoked manually after upgrade.
"""

from __future__ import annotations

from pathlib import Path

from personal_mem.core._utils import as_list
from personal_mem.core.config import Config
from personal_mem.core.vault import VaultManager, parse_frontmatter
from personal_mem.acquisition.sources import load_user_config
from personal_mem.acquisition.sources.queue import Queue
from personal_mem.acquisition.sources.registry import normalize


def migrate_todo_research_to_queue(vault_root: Path) -> int:
    """Move ``todo+research`` notes with a ``source_type`` into per-type queues.

    For each note in the vault tagged both ``todo`` and ``research``:

    1. Read the note's frontmatter; extract ``source_type`` (default
       ``article`` for legacy notes that pre-date the field).
    2. Enqueue a record into the matching :class:`Queue` carrying the
       URL (parsed from the body), title, original note id, and any
       concepts that were already assigned.
    3. Strip the ``todo`` tag from the note's frontmatter so it stops
       showing up in ``mem backlog`` and the legacy ``/research --queue``
       sweep. The note itself isn't deleted — it's downgraded to a
       general-purpose research stub.

    The migration is idempotent: notes that have already had ``todo``
    stripped are silently skipped, and the queue's per-type
    ``dedup_keys`` (from ``sources.yaml``) ensure a re-run doesn't
    enqueue duplicates.

    Returns the count of notes successfully migrated.
    """
    config = Config(vault_root=Path(vault_root))
    return _run(config)


def _run(config: Config) -> int:
    vm = VaultManager(config=config)
    if not vm.root.exists():
        return 0

    cfg = load_user_config(vm.root)
    sources_cfg = cfg.get("sources", {})

    migrated = 0
    for md_file in vm.get_all_md_files():
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = parse_frontmatter(text)
        if not fm:
            continue
        tags = fm.get("tags") or []
        if not isinstance(tags, list):
            continue
        if "todo" not in tags or "research" not in tags:
            continue

        source_type = normalize(fm.get("source_type", "") or "article")
        keys = (
            sources_cfg.get(source_type, {}).get("dedup_keys") or ["url", "title"]
        )
        url = _extract_url(body)
        title = fm.get("title", "") or md_file.stem

        item = {
            "url": url,
            "title": title,
            "source_note_id": fm.get("id", ""),
            "concepts": fm.get("concepts") or [],
        }

        queue = Queue.for_source_type(source_type, vm.root)
        if queue.dedup_check(item, keys):
            # Already in the queue (or recently archived) — strip todo
            # anyway so the note exits the backlog UNION.
            _strip_todo_tag(md_file, fm, body)
            continue

        queue.enqueue(item)
        _strip_todo_tag(md_file, fm, body)
        migrated += 1

    return migrated


def _extract_url(body: str) -> str:
    """Pull the first http(s):// URL out of the body. Empty string if none."""
    for token in body.split():
        if token.startswith("http://") or token.startswith("https://"):
            # Strip trailing punctuation that often clings to URLs.
            return token.rstrip(".,;:)\"")
    return ""


def _strip_todo_tag(path: Path, fm: dict, body: str) -> None:
    """Rewrite ``path`` with ``todo`` removed from its tags. Other
    frontmatter fields and the body are preserved verbatim."""
    from personal_mem.core.vault import render_frontmatter

    tags = [t for t in as_list(fm.get("tags")) if t != "todo"]
    fm = dict(fm)
    fm["tags"] = tags
    new_text = render_frontmatter(fm) + "\n" + body.lstrip("\n")
    path.write_text(new_text, encoding="utf-8")
