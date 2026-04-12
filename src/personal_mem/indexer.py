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

CREATE TABLE IF NOT EXISTS note_concepts (
    note_id  TEXT NOT NULL,
    concept  TEXT NOT NULL,
    domain   TEXT,
    PRIMARY KEY (note_id, concept)
);

CREATE INDEX IF NOT EXISTS idx_nc_concept ON note_concepts(concept);
CREATE INDEX IF NOT EXISTS idx_nc_note ON note_concepts(note_id);
CREATE INDEX IF NOT EXISTS idx_nc_domain ON note_concepts(domain);

-- Decision → file paths mapping (populated from decision frontmatter
-- file_paths field). Enables one-JOIN answering of "every decision that
-- touched this file", which otherwise requires scanning every decision's
-- frontmatter.
CREATE TABLE IF NOT EXISTS decision_files (
    decision_id TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    PRIMARY KEY (decision_id, file_path)
);

CREATE INDEX IF NOT EXISTS idx_df_path ON decision_files(file_path);
CREATE INDEX IF NOT EXISTS idx_df_decision ON decision_files(decision_id);

CREATE TABLE IF NOT EXISTS note_tags (
    note_id  TEXT NOT NULL,
    tag      TEXT NOT NULL,
    PRIMARY KEY (note_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_nt_tag ON note_tags(tag);
CREATE INDEX IF NOT EXISTS idx_nt_note ON note_tags(note_id);

CREATE TABLE IF NOT EXISTS concept_hierarchy (
    concept   TEXT NOT NULL,
    ancestor  TEXT NOT NULL,
    depth     INTEGER NOT NULL,
    PRIMARY KEY (concept, ancestor)
);

CREATE INDEX IF NOT EXISTS idx_ch_ancestor ON concept_hierarchy(ancestor);
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
            self.db.execute("DELETE FROM note_concepts")
            self.db.execute("DELETE FROM note_tags")
            self.db.execute("DELETE FROM decision_files")
            self.db.execute("DELETE FROM concept_hierarchy")
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

        # Rebuild edges and FTS only when content changed
        if full or stats["indexed"] > 0 or stats["removed"] > 0:
            stats["edges"] = self._rebuild_edges()
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

        self._sync_concepts(note_id, fm)
        self._sync_tags(note_id, tags)
        self._sync_decision_files(note_id, note_type, fm)

    def _sync_decision_files(self, note_id: str, note_type: str, fm: dict) -> None:
        """Sync decision_files rows from a decision note's ``file_paths`` frontmatter.

        No-ops for non-decision notes. Overwrites existing rows on re-index
        so edits to ``file_paths`` are reflected.
        """
        self.db.execute("DELETE FROM decision_files WHERE decision_id = ?", (note_id,))
        if note_type != "decision":
            return

        raw = fm.get("file_paths") or []
        if isinstance(raw, str):
            raw = [raw]
        for fp in raw:
            fp = str(fp).strip()
            if not fp:
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO decision_files (decision_id, file_path) VALUES (?, ?)",
                (note_id, fp),
            )

    def _sync_concepts(self, note_id: str, fm: dict) -> None:
        """Sync note_concepts rows for a note.

        Resolves aliases to canonical forms and looks up ontology domain.
        """
        self.db.execute("DELETE FROM note_concepts WHERE note_id = ?", (note_id,))
        concepts = fm.get("concepts", [])
        if isinstance(concepts, str):
            concepts = [c.strip() for c in concepts.split(",") if c.strip()]
        if not concepts:
            return

        # Lazy-load alias reverse map and concept-to-domain map
        if not hasattr(self, "_reverse_map"):
            try:
                from personal_mem.concepts import (
                    build_reverse_map,
                    concept_to_domains,
                    load_aliases,
                    load_ontology,
                )
                self._reverse_map = build_reverse_map(load_aliases(self.config))
                self._concept_domains = concept_to_domains(load_ontology())
            except Exception:
                self._reverse_map = {}
                self._concept_domains = {}

        for c in concepts:
            canonical = self._reverse_map.get(c.lower(), c.lower())
            domains = self._concept_domains.get(canonical, [])
            domain = domains[0] if domains else None
            self.db.execute(
                "INSERT OR IGNORE INTO note_concepts (note_id, concept, domain) VALUES (?, ?, ?)",
                (note_id, canonical, domain),
            )

    def _sync_tags(self, note_id: str, tags: list[str]) -> None:
        """Sync note_tags rows for a note."""
        self.db.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
        for tag in tags:
            tag = tag.strip().lower()
            if tag:
                self.db.execute(
                    "INSERT OR IGNORE INTO note_tags (note_id, tag) VALUES (?, ?)",
                    (note_id, tag),
                )

    def _remove_by_path(self, rel_path: str) -> None:
        """Remove a note, its edges, its concepts, tags, and decision-file links."""
        row = self.db.execute("SELECT id FROM notes WHERE path = ?", (rel_path,)).fetchone()
        if row:
            note_id = row["id"]
            self.db.execute(
                "DELETE FROM edges WHERE source = ? OR target = ?", (note_id, note_id)
            )
            self.db.execute("DELETE FROM note_concepts WHERE note_id = ?", (note_id,))
            self.db.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            self.db.execute("DELETE FROM decision_files WHERE decision_id = ?", (note_id,))
        self.db.execute("DELETE FROM notes WHERE path = ?", (rel_path,))

    def _rebuild_edges(self) -> int:
        """Rebuild all edges from frontmatter, wikilinks, concepts, tags, and session structure."""
        self.db.execute("DELETE FROM edges")
        now = datetime.now(timezone.utc).isoformat()
        edge_count = 0
        total_notes = self.db.execute("SELECT COUNT(*) FROM notes").fetchone()[0]

        # ── Lookup maps ──────────────────────────────────────────────
        slug_to_id: dict[str, str] = {}
        id_to_type: dict[str, str] = {}
        id_to_path: dict[str, str] = {}
        for row in self.db.execute("SELECT id, path, title, type FROM notes"):
            stem = Path(row["path"]).stem
            slug_to_id[stem] = row["id"]
            slug_to_id[row["title"].lower()] = row["id"]
            id_to_type[row["id"]] = row["type"]
            id_to_path[row["id"]] = row["path"]

        # ── 1. Frontmatter edges ─────────────────────────────────────
        for row in self.db.execute("SELECT id, frontmatter, body_text FROM notes"):
            note_id = row["id"]
            try:
                fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
            except json.JSONDecodeError:
                continue

            for fm_field, edge_type in EDGE_FIELD_MAP.items():
                targets = fm.get(fm_field, [])
                if isinstance(targets, str):
                    targets = [targets] if targets else []
                for target in targets:
                    target = str(target).strip()
                    if not target:
                        continue
                    resolved = target if target in slug_to_id.values() else slug_to_id.get(
                        target.lower(), slug_to_id.get(target, "")
                    )
                    if resolved and resolved != note_id:
                        self._insert_edge(note_id, resolved, edge_type, now)
                        edge_count += 1

            # ── 2. Wikilink edges (with type inference) ──────────────
            body = row["body_text"] or ""
            for link in extract_wikilinks(body):
                link_lower = link.lower().strip()
                resolved = slug_to_id.get(link_lower, slug_to_id.get(link, ""))
                if resolved and resolved != note_id:
                    # Infer edge type from target note type
                    target_type = id_to_type.get(resolved, "note")
                    if target_type == "source":
                        wl_edge_type = "cites"
                    elif target_type == "session":
                        wl_edge_type = "derived_from"
                    else:
                        wl_edge_type = "relates_to"
                    self._insert_edge(note_id, resolved, wl_edge_type, now)
                    edge_count += 1

        # ── 3. Session directory inference ────────────────────────────
        # Notes living in a session directory get derived_from edges
        # to the session.md in that directory.
        session_dir_to_id: dict[str, str] = {}
        for nid, path in id_to_path.items():
            if path.endswith("/session.md") or path.endswith("\\session.md"):
                session_dir = str(Path(path).parent)
                session_dir_to_id[session_dir] = nid

        for nid, path in id_to_path.items():
            parent = str(Path(path).parent)
            session_id = session_dir_to_id.get(parent)
            if session_id and nid != session_id:
                meta = json.dumps({"via": "session_dir"})
                self.db.execute(
                    "INSERT OR IGNORE INTO edges (source, target, edge_type, metadata, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (nid, session_id, "derived_from", meta, now),
                )
                edge_count += 1

        # ── 4. Concept-based edges (configurable threshold + freq cap) ─
        cfg = self.config
        concept_freq_cap = int(total_notes * cfg.concept_edge_max_freq_pct) if total_notes else 0

        concept_to_notes: dict[str, list[str]] = defaultdict(list)
        for row in self.db.execute("SELECT note_id, concept FROM note_concepts"):
            concept_to_notes[row["concept"]].append(row["note_id"])

        pair_shared: dict[tuple, list[str]] = defaultdict(list)
        for concept, note_ids in concept_to_notes.items():
            unique_ids = sorted(set(note_ids))
            # Skip overly broad concepts that would create noisy edges
            if len(unique_ids) > concept_freq_cap > 0:
                continue
            for a, b in combinations(unique_ids, 2):
                pair_shared[(a, b)].append(concept)

        for (a, b), shared in pair_shared.items():
            if len(shared) >= cfg.concept_edge_threshold:
                metadata = json.dumps({"via": "concept", "shared": shared})
                self.db.execute(
                    "INSERT OR IGNORE INTO edges (source, target, edge_type, metadata, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (a, b, "relates_to", metadata, now),
                )
                edge_count += 1

        # ── 5. Tag-based edges ────────────────────────────────────────
        tag_freq_cap = int(total_notes * cfg.tag_edge_max_freq_pct) if total_notes else 0
        excluded_tags = set(cfg.tag_edge_exclude)

        tag_to_notes: dict[str, list[str]] = defaultdict(list)
        for row in self.db.execute("SELECT note_id, tag FROM note_tags"):
            tag_to_notes[row["tag"]].append(row["note_id"])

        tag_pair_shared: dict[tuple, list[str]] = defaultdict(list)
        for tag, note_ids in tag_to_notes.items():
            if tag in excluded_tags:
                continue
            unique_ids = sorted(set(note_ids))
            if len(unique_ids) > tag_freq_cap > 0:
                continue
            for a, b in combinations(unique_ids, 2):
                tag_pair_shared[(a, b)].append(tag)

        for (a, b), shared in tag_pair_shared.items():
            if len(shared) >= cfg.tag_edge_threshold:
                metadata = json.dumps({"via": "tag", "shared": shared})
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

    def materialize_links(self, max_links: int = 7, dry_run: bool = False) -> dict:
        """Write a managed ``## See Also`` section into each note with its top connections.

        This makes SQLite-computed edges visible to Obsidian's graph view.
        The section is stripped and rewritten each time — safe to re-run.

        Link selection uses diversity-aware ordering to prevent archipelagos:
        1. Structural edges first (derived_from, cites, etc.) — always included.
        2. Concept edges round-robin across shared-concept groups so each
           topic cluster gets at least one bridge edge.

        Args:
            max_links: Maximum wikilinks per note.
            dry_run: If True, compute but don't write files.

        Returns:
            Stats: notes_updated, notes_skipped, links_written.
        """
        from personal_mem.vault import strip_section

        stats = {"notes_updated": 0, "notes_skipped": 0, "links_written": 0}
        SEE_ALSO = "## See Also"

        STRUCTURAL_TYPES = {"derived_from", "cites", "builds_on", "supersedes", "implements"}

        all_notes = {
            row["id"]: row
            for row in self.db.execute("SELECT id, path, title FROM notes")
        }

        for note_id, note_row in all_notes.items():
            edges = self.db.execute(
                "SELECT source, target, edge_type, metadata FROM edges "
                "WHERE source = ? OR target = ?",
                (note_id, note_id),
            ).fetchall()

            if not edges:
                stats["notes_skipped"] += 1
                continue

            # Separate structural edges from concept/tag edges
            structural: list[tuple[str, str]] = []
            concept_edges: list[tuple[str, str, str]] = []  # (target, edge_type, first_concept)
            seen: set[str] = set()

            for edge in edges:
                other = edge["target"] if edge["source"] == note_id else edge["source"]
                if other in seen or other not in all_notes:
                    continue
                seen.add(other)

                if edge["edge_type"] in STRUCTURAL_TYPES:
                    structural.append((other, edge["edge_type"]))
                else:
                    # Extract the first shared concept for grouping
                    meta = edge["metadata"] or "{}"
                    try:
                        shared = json.loads(meta).get("shared", [])
                    except (json.JSONDecodeError, AttributeError):
                        shared = []
                    group_key = shared[0] if shared else "_tag"
                    concept_edges.append((other, edge["edge_type"], group_key))

            # Build final link list with diversity
            linked: list[tuple[str, str]] = []

            # 1. Always include structural edges (up to half the budget)
            structural_budget = max(2, max_links // 2)
            for target, etype in structural[:structural_budget]:
                linked.append((target, etype))

            # 2. Round-robin across concept groups for remaining slots
            remaining = max_links - len(linked)
            if remaining > 0 and concept_edges:
                # Group by first shared concept
                groups: dict[str, list[tuple[str, str]]] = {}
                for target, etype, group in concept_edges:
                    if target not in {t for t, _ in linked}:
                        groups.setdefault(group, []).append((target, etype))

                # Round-robin: take 1 from each group, repeat until full
                added = set(t for t, _ in linked)
                group_keys = list(groups.keys())
                idx = 0
                while len(linked) < max_links and group_keys:
                    key = group_keys[idx % len(group_keys)]
                    candidates = groups[key]
                    found = False
                    while candidates:
                        target, etype = candidates.pop(0)
                        if target not in added:
                            linked.append((target, etype))
                            added.add(target)
                            found = True
                            break
                    if not candidates:
                        group_keys.remove(key)
                        if not group_keys:
                            break
                        idx = idx % len(group_keys) if group_keys else 0
                    else:
                        idx += 1

            if not linked:
                stats["notes_skipped"] += 1
                continue

            # Build the See Also section
            lines = [SEE_ALSO, ""]
            for target_id, edge_type in linked:
                target = all_notes[target_id]
                stem = Path(target["path"]).stem
                label = edge_type.replace("_", " ") if edge_type != "relates_to" else ""
                if label:
                    lines.append(f"- [[{stem}]] _{label}_")
                else:
                    lines.append(f"- [[{stem}]]")
            see_also_text = "\n".join(lines) + "\n"

            if dry_run:
                stats["notes_updated"] += 1
                stats["links_written"] += len(linked)
                continue

            # Read file, strip old See Also, append new one
            file_path = self.vault.root / note_row["path"]
            if not file_path.exists():
                stats["notes_skipped"] += 1
                continue

            text = file_path.read_text(encoding="utf-8")
            text = strip_section(text, SEE_ALSO)
            text = text.rstrip() + "\n\n" + see_also_text
            file_path.write_text(text, encoding="utf-8")

            stats["notes_updated"] += 1
            stats["links_written"] += len(linked)

        return stats

    def get_stats(self) -> dict:
        """Return index statistics."""
        stats = {}
        for row in self.db.execute("SELECT type, COUNT(*) as cnt FROM notes GROUP BY type"):
            stats[f"notes_{row['type']}"] = row["cnt"]
        row = self.db.execute("SELECT COUNT(*) as cnt FROM notes").fetchone()
        stats["notes_total"] = row["cnt"]
        row = self.db.execute("SELECT COUNT(*) as cnt FROM edges").fetchone()
        stats["edges_total"] = row["cnt"]
        row = self.db.execute("SELECT COUNT(DISTINCT concept) as cnt FROM note_concepts").fetchone()
        stats["concepts_total"] = row["cnt"]
        return stats
