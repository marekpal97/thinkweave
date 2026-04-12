"""Add relates_to edges between notes that share 2+ file references.

Scans the ## Files sections of imported notes, finds pairs within the
same project that reference the same files, and adds relates_to edges.

Usage: uv run python scripts/enrich_file_edges.py [--dry-run] [--min-shared N]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from personal_mem.config import load_config
from personal_mem.indexer import Indexer
from personal_mem.vault import VaultManager

# Match file paths in **Read**: ... and **Modified**: ... lines
_FILES_RE = re.compile(r"\*\*(?:Read|Modified)\*\*:\s*(.+)")


def extract_files_from_body(body: str) -> set[str]:
    """Extract file paths from ## Files section of a note body."""
    files: set[str] = set()
    for match in _FILES_RE.finditer(body):
        for f in match.group(1).split(","):
            f = f.strip()
            if f:
                files.add(f)
    return files


def main():
    parser = argparse.ArgumentParser(description="Add file-path-based relates_to edges")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-shared", type=int, default=2, help="Minimum shared files for an edge")
    args = parser.parse_args()

    config = load_config()
    vm = VaultManager(config=config)

    conn = sqlite3.connect(str(config.index_db))
    conn.row_factory = sqlite3.Row

    # Get all imported non-session notes with their body text
    rows = conn.execute("""
        SELECT id, project, body_text FROM notes
        WHERE frontmatter LIKE '%imported_from%' AND type != 'session'
        ORDER BY project
    """).fetchall()

    # Build file→notes mapping per project
    project_file_map: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    for row in rows:
        note_id = row["id"]
        project = row["project"]
        body = row["body_text"] or ""
        files = extract_files_from_body(body)
        for f in files:
            project_file_map[project][f].append(note_id)

    # Find pairs sharing min_shared files
    edges_to_add: list[tuple[str, str, list[str]]] = []  # (source, target, shared_files)

    for project, file_map in project_file_map.items():
        # Build pair→shared_files
        pair_shared: dict[tuple[str, str], list[str]] = defaultdict(list)
        for filepath, note_ids in file_map.items():
            unique = sorted(set(note_ids))
            for i, a in enumerate(unique):
                for b in unique[i + 1:]:
                    pair_shared[(a, b)].append(filepath)

        for (a, b), shared in pair_shared.items():
            if len(shared) >= args.min_shared:
                edges_to_add.append((a, b, shared))

    conn.close()

    if args.dry_run:
        # Group by project for display
        print(f"\n── Dry Run: {len(edges_to_add)} edges to add ────────────────")
        for source, target, shared in edges_to_add[:20]:
            print(f"  {source} ↔ {target} ({len(shared)} shared: {', '.join(shared[:3])}{'...' if len(shared) > 3 else ''})")
        if len(edges_to_add) > 20:
            print(f"  ... and {len(edges_to_add) - 20} more")
        return

    if not edges_to_add:
        print("No file-path-based edges to add.")
        return

    # Write edges directly to the index DB
    idx = Indexer(config=config)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    added = 0
    for source, target, shared in edges_to_add:
        metadata = json.dumps({"via": "shared_files", "shared": shared})
        try:
            idx.db.execute(
                "INSERT OR IGNORE INTO edges (source, target, edge_type, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (source, target, "relates_to", metadata, now),
            )
            added += 1
        except Exception:
            pass

    idx.db.commit()
    idx.close()

    print(f"Added {added} file-path-based relates_to edges.")


if __name__ == "__main__":
    main()
