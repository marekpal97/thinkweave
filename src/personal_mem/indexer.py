"""Build and maintain the SQLite index from vault markdown files.

The index is a derived artifact — always rebuildable via `mem index --full`.
Incremental updates use SHA-256 content hashes to skip unchanged files.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

from personal_mem.config import Config, load_config
from personal_mem.landing import LANDING_FILENAMES
from personal_mem.vault import VaultManager, content_hash, extract_wikilinks, parse_frontmatter

log = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notes (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    title        TEXT NOT NULL,
    path         TEXT NOT NULL UNIQUE,
    project      TEXT,
    date         TEXT,
    tags         TEXT,
    content_hash TEXT,
    frontmatter  TEXT,
    body_text    TEXT,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_type ON notes(type);
CREATE INDEX IF NOT EXISTS idx_notes_project ON notes(project);
CREATE INDEX IF NOT EXISTS idx_notes_date ON notes(date);

CREATE TABLE IF NOT EXISTS edges (
    source     TEXT NOT NULL,
    target     TEXT NOT NULL,
    edge_type  TEXT NOT NULL,
    metadata   TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source, target, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);
"""

FTS_SCHEMA_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    id UNINDEXED,
    title,
    body_text,
    tags,
    content='notes',
    content_rowid='rowid'
);
"""

# Frontmatter fields that map to typed edges
EDGE_FIELD_MAP: dict[str, str] = {
    "derived_from": "derived_from",
    "supersedes": "supersedes",
    "related": "relates_to",
    "cites": "cites",
    "implements": "implements",
    "builds_on": "builds_on",
}

# Reverse map: edge_type -> frontmatter field name
EDGE_TYPE_TO_FIELD: dict[str, str] = {v: k for k, v in EDGE_FIELD_MAP.items()}


class Indexer:
    """Builds and maintains the SQLite index from vault markdown."""

    def __init__(self, config: Config | None = None):
        self.config = config or load_config()
        self.vault = VaultManager(self.config)
        self._db: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self.config.mem_dir.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.config.index_db))
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._init_schema()
        return self._db

    def _init_schema(self) -> None:
        self.db.executescript(SCHEMA_SQL)
        self.db.executescript(FTS_SCHEMA_SQL)

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    def rebuild(self, full: bool = False) -> dict:
        """Rebuild the index from vault files.

        Args:
            full: If True, drop and recreate everything. Otherwise incremental.

        Returns:
            Stats dict with counts of indexed, skipped, removed files.
        """
        stats = {"indexed": 0, "skipped": 0, "removed": 0, "edges": 0}

        if full:
            self.db.execute("DELETE FROM edges")
            self.db.execute("DELETE FROM notes")
            self._rebuild_fts()

        # Get existing hashes for incremental mode
        existing: dict[str, str] = {}
        if not full:
            for row in self.db.execute("SELECT path, content_hash FROM notes"):
                existing[row["path"]] = row["content_hash"]

        # Scan all markdown files
        indexed_paths: set[str] = set()
        md_files = self.vault.get_all_md_files()

        for md_file in md_files:
            # Skip landing documents (materialized views, not source material)
            if md_file.name in LANDING_FILENAMES:
                continue
            rel_path = str(md_file.relative_to(self.vault.root))
            indexed_paths.add(rel_path)

            text = md_file.read_text(encoding="utf-8")
            file_hash = content_hash(text)

            # Skip unchanged files in incremental mode
            if not full and existing.get(rel_path) == file_hash:
                stats["skipped"] += 1
                continue

            self._index_file(md_file, text, file_hash, rel_path)
            stats["indexed"] += 1

        # Remove stale entries
        if not full:
            for old_path in set(existing.keys()) - indexed_paths:
                self._remove_by_path(old_path)
                stats["removed"] += 1

        # Rebuild edges from all notes
        stats["edges"] = self._rebuild_edges()

        # Rebuild FTS
        self._rebuild_fts()

        self.db.commit()
        return stats

    def index_file(self, path: Path) -> None:
        """Index or re-index a single file. Used by hooks for incremental updates."""
        if path.name in LANDING_FILENAMES:
            return  # Landing docs are excluded from the index
        if not path.exists():
            rel = str(path.relative_to(self.vault.root))
            self._remove_by_path(rel)
            self.db.commit()
            return

        text = path.read_text(encoding="utf-8")
        file_hash = content_hash(text)
        rel_path = str(path.relative_to(self.vault.root))
        self._index_file(path, text, file_hash, rel_path)
        self._rebuild_fts()
        self.db.commit()

    def _index_file(self, path: Path, text: str, file_hash: str, rel_path: str) -> None:
        """Parse and upsert a single file into the index."""
        fm, body = parse_frontmatter(text)

        note_id = fm.get("id", "")
        if not note_id:
            # Generate a deterministic ID from path
            note_id = f"auto-{content_hash(rel_path)[:8]}"

        note_type = fm.get("type", "note")

        # Extract title
        title = fm.get("title", "")
        if not title:
            for line in body.split("\n"):
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            if not title:
                title = path.stem

        tags = fm.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]

        now = datetime.now(timezone.utc).isoformat()

        self.db.execute(
            """INSERT OR REPLACE INTO notes
               (id, type, title, path, project, date, tags, content_hash, frontmatter, body_text, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                note_id,
                note_type,
                title,
                rel_path,
                fm.get("project", ""),
                str(fm.get("date", "")),
                json.dumps(tags),
                file_hash,
                json.dumps(fm),
                body,
                now,
            ),
        )

    def _remove_by_path(self, rel_path: str) -> None:
        """Remove a note and its edges from the index."""
        row = self.db.execute("SELECT id FROM notes WHERE path = ?", (rel_path,)).fetchone()
        if row:
            note_id = row["id"]
            self.db.execute(
                "DELETE FROM edges WHERE source = ? OR target = ?", (note_id, note_id)
            )
        self.db.execute("DELETE FROM notes WHERE path = ?", (rel_path,))

    def _rebuild_edges(self) -> int:
        """Rebuild all edges from frontmatter fields and wikilinks."""
        self.db.execute("DELETE FROM edges")
        now = datetime.now(timezone.utc).isoformat()
        edge_count = 0

        # Build ID-to-path and slug-to-id maps for wikilink resolution
        slug_to_id: dict[str, str] = {}
        for row in self.db.execute("SELECT id, path, title FROM notes"):
            stem = Path(row["path"]).stem
            slug_to_id[stem] = row["id"]
            slug_to_id[row["title"].lower()] = row["id"]

        for row in self.db.execute("SELECT id, frontmatter, body_text FROM notes"):
            note_id = row["id"]
            try:
                fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
            except json.JSONDecodeError:
                continue

            # Edges from frontmatter fields
            for fm_field, edge_type in EDGE_FIELD_MAP.items():
                targets = fm.get(fm_field, [])
                if isinstance(targets, str):
                    targets = [targets] if targets else []
                for target in targets:
                    target = str(target).strip()
                    if not target:
                        continue
                    # Resolve target — could be an ID or a wikilink name
                    resolved = target if target in slug_to_id.values() else slug_to_id.get(
                        target.lower(), slug_to_id.get(target, "")
                    )
                    if resolved and resolved != note_id:
                        self._insert_edge(note_id, resolved, edge_type, now)
                        edge_count += 1

            # Edges from wikilinks in body
            body = row["body_text"] or ""
            for link in extract_wikilinks(body):
                link_lower = link.lower().strip()
                resolved = slug_to_id.get(link_lower, slug_to_id.get(link, ""))
                if resolved and resolved != note_id:
                    self._insert_edge(note_id, resolved, "relates_to", now)
                    edge_count += 1

        # Concept-based edges: notes sharing 2+ concepts get relates_to edges
        # Resolve aliases to canonical forms for better matching
        try:
            from personal_mem.concepts import build_reverse_map, load_aliases
            reverse_map = build_reverse_map(load_aliases(self.config))
        except Exception:
            reverse_map = {}

        concept_to_notes: dict[str, list[str]] = defaultdict(list)
        for row in self.db.execute("SELECT id, frontmatter FROM notes"):
            fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
            concepts = fm.get("concepts", [])
            if isinstance(concepts, str):
                concepts = [c.strip() for c in concepts.split(",") if c.strip()]
            for concept in concepts:
                canonical = reverse_map.get(concept.lower(), concept.lower())
                concept_to_notes[canonical].append(row["id"])

        pair_shared: dict[tuple, list[str]] = defaultdict(list)
        for concept, note_ids in concept_to_notes.items():
            for a, b in combinations(sorted(set(note_ids)), 2):
                pair_shared[(a, b)].append(concept)

        for (a, b), shared in pair_shared.items():
            if len(shared) >= 2:
                metadata = json.dumps({"via": "concept", "shared": shared})
                self.db.execute(
                    "INSERT OR IGNORE INTO edges (source, target, edge_type, metadata, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (a, b, "relates_to", metadata, now),
                )
                edge_count += 1

        return edge_count

    def _insert_edge(self, source: str, target: str, edge_type: str, created_at: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO edges (source, target, edge_type, created_at) VALUES (?, ?, ?, ?)",
            (source, target, edge_type, created_at),
        )

    def _rebuild_fts(self) -> None:
        """Rebuild the FTS index from the notes table."""
        self.db.execute("INSERT INTO notes_fts(notes_fts) VALUES('rebuild')")

    def get_stats(self) -> dict:
        """Return index statistics."""
        stats = {}
        for row in self.db.execute("SELECT type, COUNT(*) as cnt FROM notes GROUP BY type"):
            stats[f"notes_{row['type']}"] = row["cnt"]
        row = self.db.execute("SELECT COUNT(*) as cnt FROM notes").fetchone()
        stats["notes_total"] = row["cnt"]
        row = self.db.execute("SELECT COUNT(*) as cnt FROM edges").fetchone()
        stats["edges_total"] = row["cnt"]
        return stats
