"""Import observations from claude-mem SQLite database.

Reads ~/.claude-mem/claude-mem.db and converts observations to vault notes.
One-time migration + incremental sync.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from personal_mem.config import Config, load_config
from personal_mem.indexer import Indexer
from personal_mem.schemas import NoteType
from personal_mem.vault import VaultManager, content_hash


_DEFAULT_CLAUDE_MEM_DB = Path.home() / ".claude-mem" / "claude-mem.db"


def import_claude_mem(
    config: Config | None = None,
    db_path: Path | None = None,
) -> dict:
    """Import observations from claude-mem into the vault.

    Returns stats dict with imported/skipped counts.
    """
    config = config or load_config()
    db_path = db_path or _DEFAULT_CLAUDE_MEM_DB

    if not db_path.exists():
        return {"imported": 0, "skipped": 0, "error": f"Database not found: {db_path}"}

    vm = VaultManager(config=config)
    vm.ensure_dirs()
    idx = Indexer(config=config)

    stats = {"imported": 0, "skipped": 0}

    # Read claude-mem observations
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute(
            "SELECT * FROM observations ORDER BY created_at"
        ).fetchall()
    except sqlite3.OperationalError:
        # Table might not exist or different schema
        conn.close()
        return {"imported": 0, "skipped": 0, "error": "Could not read observations table"}

    # Track content hashes to avoid duplicates
    existing_hashes: set[str] = set()
    for note in vm.list_notes(limit=10000):
        existing_hashes.add(content_hash(note.body))

    for row in rows:
        text = row["content"] if "content" in row.keys() else str(dict(row))
        chash = content_hash(text)

        if chash in existing_hashes:
            stats["skipped"] += 1
            continue

        # Map claude-mem fields to vault note
        obs_type = row.get("type", "observation") if "type" in row.keys() else "observation"
        project = row.get("project", "") if "project" in row.keys() else ""
        tags = [obs_type]

        # Extract concepts if available
        if "concepts" in row.keys() and row["concepts"]:
            try:
                import json
                concepts = json.loads(row["concepts"])
                if isinstance(concepts, list):
                    tags.extend(concepts[:5])
            except (json.JSONDecodeError, TypeError):
                pass

        title = text[:60].replace("\n", " ").strip()
        if len(text) > 60:
            title += "..."

        path = vm.create_note(
            NoteType.NOTE,
            title=title,
            body=text,
            project=project,
            tags=tags,
            extra_frontmatter={"imported_from": "claude-mem"},
        )

        idx.index_file(path)
        existing_hashes.add(chash)
        stats["imported"] += 1

    conn.close()
    idx.close()
    return stats
