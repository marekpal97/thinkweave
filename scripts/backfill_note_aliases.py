"""Backfill `aliases: [<note-id>]` on every note in the vault.

Obsidian resolves [[note-id]] wikilinks by filename or alias, never by the
frontmatter `id:` field. Notes are filed by slug, so without this alias
clicking [[n-XXX]] / [[dec-XXX]] / [[src-XXX]] in any hub or See-Also list
creates a phantom file at vault root.

This is the one-time migration to add the alias to existing notes. New
notes get the alias automatically via `vault.VaultManager.create_note`.

Usage:
    uv run python scripts/backfill_note_aliases.py [--dry-run]

Idempotent. Safe to re-run.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/home/marekpal97/python_projects/personal_mem/src")

from personal_mem.core.config import load_config
from personal_mem.core.vault import parse_frontmatter, render_frontmatter


def is_note_id(value: str) -> bool:
    """Crude check: starts with a known prefix and a hex tail."""
    if not isinstance(value, str) or "-" not in value:
        return False
    prefix, tail = value.split("-", 1)
    return prefix in {"n", "dec", "src", "ses", "thm"} and len(tail) >= 6


def backfill_one(path: Path, *, dry_run: bool) -> str | None:
    text = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)

    note_id = fm.get("id", "")
    if not is_note_id(note_id):
        return None

    aliases = fm.get("aliases") or []
    if not isinstance(aliases, list):
        aliases = [aliases]
    if note_id in aliases:
        return None

    fm["aliases"] = [note_id, *aliases]
    new_text = render_frontmatter(fm) + "\n\n" + body.lstrip("\n")
    if dry_run:
        return note_id
    path.write_text(new_text, encoding="utf-8")
    return note_id


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    cfg = load_config()
    vault = cfg.vault_root

    updated: list[tuple[Path, str]] = []
    skipped = 0
    examined = 0
    for path in vault.rglob("*.md"):
        if "/.archive/" in str(path) or "/.obsidian/" in str(path):
            continue
        examined += 1
        try:
            result = backfill_one(path, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {path.relative_to(vault)} → error: {exc}")
            continue
        if result:
            updated.append((path, result))
        else:
            skipped += 1

    verb = "would update" if dry_run else "updated"
    print(f"Examined: {examined} notes")
    print(f"Skipped (no id or already aliased): {skipped}")
    print(f"{verb.capitalize()}: {len(updated)} notes")
    if dry_run and updated[:5]:
        print("Sample (first 5):")
        for path, note_id in updated[:5]:
            print(f"  - {path.relative_to(vault)} → +alias {note_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
