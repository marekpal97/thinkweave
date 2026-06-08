"""Build and maintain the SQLite index from vault markdown files.

The index is a derived artifact — always rebuildable via `mem index --full`.
Incremental updates use SHA-256 content hashes to skip unchanged files.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

from personal_mem.core._utils import as_list
from personal_mem.core.config import Config, load_config
from personal_mem.synthesis.landing import (
    LANDING_FILENAMES,
    landing_filename_set,
)
from personal_mem.core.vault import (
    VaultManager,
    content_hash,
    extract_wikilink_ids,
    parse_frontmatter,
)

log = logging.getLogger(__name__)

# Companion files written alongside source notes (raw content, snapshots).
# These are not vault notes — they're archival artifacts referenced from
# the canonical source.md. Indexing them as untyped phantom notes pollutes
# the graph and FTS results.
SOURCE_COMPANION_FILENAMES = {"raw.md", "raw.txt", "snapshot.md"}

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
    updated_at   TEXT NOT NULL,
    file_mtime   REAL
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
    -- C19a: edge weight. For concept/tag edges this is len(metadata["shared"]) —
    -- the count of shared concepts/tags driving the edge — so the graph walk
    -- can rank neighbours by tie-strength. Structural edges (supersedes,
    -- derived_from, session_dir, etc.) default to 1.0.
    weight     REAL NOT NULL DEFAULT 1.0,
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

-- Context served to a session, projected from per-session retrieval_log.jsonl.
-- One row per (session, note, source). `source` is the closed-set distinction
-- between SessionStart payload notes ('startup') and on-the-fly MCP retrievals
-- during the session ('onthefly'). Feeds the RLVR decision-context export.
-- Rebuildable from retrieval_log.jsonl — markdown stays truth.
CREATE TABLE IF NOT EXISTS context_served (
    session_id TEXT NOT NULL,
    note_id    TEXT NOT NULL,
    source     TEXT NOT NULL CHECK(source IN ('startup', 'onthefly')),
    ts         TEXT,
    PRIMARY KEY (session_id, note_id, source)
);

CREATE INDEX IF NOT EXISTS idx_cs_session ON context_served(session_id);
CREATE INDEX IF NOT EXISTS idx_cs_note ON context_served(note_id);

-- C19b: per-concept-induced-subgraph PageRank scores.
-- rank_type is keyed by 'pagerank:{concept}' so multiple ranking schemes
-- can co-exist (e.g. future global PageRank, betweenness centrality).
-- Computed during the dream apply phase when dream_compute_pagerank is on;
-- consumed by mem_concepts(action='canonical_for', concept=X).
CREATE TABLE IF NOT EXISTS graph_ranks (
    note_id     TEXT NOT NULL,
    rank_type   TEXT NOT NULL,
    score       REAL NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (note_id, rank_type)
);

CREATE INDEX IF NOT EXISTS idx_gr_type ON graph_ranks(rank_type);
CREATE INDEX IF NOT EXISTS idx_gr_score ON graph_ranks(score DESC);
"""

FTS_SCHEMA_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    id UNINDEXED,
    title,
    body_text,
    tags,
    content='notes',
    content_rowid='rowid',
    tokenize="unicode61 remove_diacritics 2 tokenchars '-_'"
);
"""

# Marker matched in the `sql` column of ``sqlite_master`` to detect whether the
# live ``notes_fts`` table was created with the explicit tokenizer above. Older
# vaults (pre-A4) created the table with the SQLite default (unicode61, which
# splits on `-` and `_`); on first connect we detect that, drop the table, and
# let ``executescript`` recreate it with the correct tokenizer. Index content
# is rebuilt by ``_rebuild_fts`` the next time it's called — full rebuilds
# call it unconditionally, and `_index_file` triggers an FTS sync on every
# touched note. Existing data is safe; only the FTS index itself is regenerated.
_FTS_TOKENIZER_MARKER = "tokenchars '-_'"

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
            # Wait up to 30s for a busy lock before raising. Without this,
            # concurrent writers (e.g. `mem hubs run` applying entries while
            # a live MCP server indexer is writing) fail immediately with
            # `sqlite3.OperationalError: database is locked`. With the
            # timeout set, SQLite's default behaviour is to block and retry
            # internally, which is exactly what we want for these short
            # contended windows.
            self._db.execute("PRAGMA busy_timeout = 30000")
            self._init_schema()
        return self._db

    def _init_schema(self) -> None:
        self.db.executescript(SCHEMA_SQL)
        # FTS5 tokenizer migration (A4). Earlier vaults created `notes_fts`
        # with the default tokenizer (`unicode61` only), which splits dash-
        # and underscore-form concepts (`write-ahead-log` → 3 tokens). Detect
        # the absence of the explicit tokenizer marker in the live DDL and
        # drop the virtual table so the executescript below recreates it
        # with the new tokenizer. Repopulation happens on the next
        # `_rebuild_fts()` call (always run by `rebuild(full=True)` and by
        # every `_index_file` touch); the underlying `notes` table is
        # untouched, so a fresh `mem index --full` after upgrade is enough
        # to bring queries back online with whole-token matching for
        # dash/underscore concepts.
        existing_fts_sql_row = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='notes_fts'"
        ).fetchone()
        existing_fts_sql = (
            existing_fts_sql_row[0] if existing_fts_sql_row else None
        )
        if existing_fts_sql and _FTS_TOKENIZER_MARKER not in existing_fts_sql:
            # Drop the *_fts shadow tables FTS5 creates alongside the virtual
            # table; CREATE VIRTUAL TABLE will regenerate them.
            self.db.execute("DROP TABLE IF EXISTS notes_fts")
        self.db.executescript(FTS_SCHEMA_SQL)
        # Defensive ALTER for databases created before the file_mtime column
        # was added (P0-8 mtime gate). CREATE TABLE IF NOT EXISTS won't add
        # columns to a pre-existing table, so we add it here if missing.
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(notes)")}
        if "file_mtime" not in cols:
            self.db.execute("ALTER TABLE notes ADD COLUMN file_mtime REAL")

        # C19a: edge weights. Older vaults created `edges` without the
        # weight column. Add it now and backfill from `metadata` — for
        # concept/tag edges weight = len(metadata["shared"]); structural
        # edges keep the DEFAULT 1.0. The backfill is idempotent: a
        # subsequent migration call sees the column present and skips.
        edge_cols = {r[1] for r in self.db.execute("PRAGMA table_info(edges)")}
        if "weight" not in edge_cols:
            self.db.execute(
                "ALTER TABLE edges ADD COLUMN weight REAL NOT NULL DEFAULT 1.0"
            )
            for row in self.db.execute(
                "SELECT source, target, edge_type, metadata FROM edges"
            ):
                meta_raw = row["metadata"] or "{}"
                try:
                    meta = json.loads(meta_raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(meta, dict):
                    continue
                if meta.get("via") not in ("concept", "tag"):
                    continue
                shared = meta.get("shared") or []
                if not isinstance(shared, list) or not shared:
                    continue
                self.db.execute(
                    "UPDATE edges SET weight = ? WHERE source = ? "
                    "AND target = ? AND edge_type = ?",
                    (
                        float(len(shared)),
                        row["source"],
                        row["target"],
                        row["edge_type"],
                    ),
                )
            self.db.commit()

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

        Performance note: in incremental mode we mtime-gate the per-file read.
        For each markdown file we compare ``stat().st_mtime`` against the
        cached ``file_mtime`` in SQLite; unchanged files skip ``read_text`` +
        ``content_hash`` entirely. On a 6.5k-file vault on WSL→9P this drops
        no-op rebuild from ~25s to ~5s (still bottlenecked by rglob + stat).

        Edge & context_served scope: in incremental mode the per-rebuild set
        of *changed note IDs* is collected from ``_index_file`` and threaded
        into :meth:`_rebuild_edges_incremental` (only edges touching changed
        notes are recomputed, not the full 326K-edge teardown the full path
        does) and :meth:`_rebuild_context_served` (only sessions whose
        ``session.md`` was re-indexed re-project their retrieval log). See
        ``_rebuild_edges_incremental`` for the documented staleness gap;
        ``rebuild(full=True)`` / ``mem index --full`` heals it periodically.
        """
        stats = {"indexed": 0, "skipped": 0, "removed": 0, "edges": 0}
        changed_ids: set[str] = set()

        if full:
            self.db.execute("DELETE FROM edges")
            self.db.execute("DELETE FROM note_concepts")
            self.db.execute("DELETE FROM note_tags")
            self.db.execute("DELETE FROM decision_files")
            self.db.execute("DELETE FROM concept_hierarchy")
            self.db.execute("DELETE FROM context_served")
            self.db.execute("DELETE FROM graph_ranks")
            self.db.execute("DELETE FROM notes")
            self._rebuild_fts()

        # Pre-fetch hash + mtime maps in one query for incremental mode.
        existing_hash: dict[str, str] = {}
        existing_mtime: dict[str, float | None] = {}
        if not full:
            for row in self.db.execute(
                "SELECT path, content_hash, file_mtime FROM notes"
            ):
                existing_hash[row["path"]] = row["content_hash"]
                existing_mtime[row["path"]] = row["file_mtime"]

        # Scan all markdown files
        indexed_paths: set[str] = set()
        md_files = self.vault.get_all_md_files()

        # Pull the user-configured landing-filename set once per rebuild,
        # so a vault that renamed any landing doc still excludes
        # whatever the user actually called it.
        landing_skip = landing_filename_set(self.config.vault_root)

        for md_file in md_files:
            # Skip landing documents (materialized views, not source material)
            if md_file.name in landing_skip:
                continue
            # Skip source companion files (raw.md, snapshot.md, etc.) — these
            # are archival artifacts of source notes, not standalone notes.
            if md_file.name in SOURCE_COMPANION_FILENAMES:
                continue
            rel_path = str(md_file.relative_to(self.vault.root))
            # Skip the reports/ tree — cron dream/discover summaries are
            # materialized narrative (like landing docs), not source material.
            if rel_path.replace("\\", "/").startswith("reports/"):
                continue
            indexed_paths.add(rel_path)

            # Layer 1: mtime gate. If the file's mtime matches the cached
            # value, skip the read_text + content_hash entirely. This is the
            # hot path on a no-op rebuild.
            if not full:
                cached_mtime = existing_mtime.get(rel_path)
                if cached_mtime is not None:
                    try:
                        file_mtime = md_file.stat().st_mtime
                    except OSError:
                        file_mtime = None
                    if file_mtime is not None and file_mtime <= cached_mtime:
                        stats["skipped"] += 1
                        continue

            text = md_file.read_text(encoding="utf-8")
            file_hash = content_hash(text)

            # Layer 2: content-hash gate. Catches files whose mtime advanced
            # (e.g. `touch` with no content change, atime updates) but body
            # is unchanged.
            if not full and existing_hash.get(rel_path) == file_hash:
                # Refresh the cached mtime so future runs short-circuit
                # at layer 1 instead of falling through here every time.
                try:
                    self.db.execute(
                        "UPDATE notes SET file_mtime = ? WHERE path = ?",
                        (md_file.stat().st_mtime, rel_path),
                    )
                except OSError:
                    pass
                stats["skipped"] += 1
                continue

            changed_ids.add(self._index_file(md_file, text, file_hash, rel_path))
            stats["indexed"] += 1

        # Remove stale entries (their edges + concept/tag rows are cleared
        # by _remove_by_path, so removed notes need no changed_ids tracking).
        if not full:
            for old_path in set(existing_hash.keys()) - indexed_paths:
                self._remove_by_path(old_path)
                stats["removed"] += 1

        # Rebuild edges and FTS only when content changed.
        if full:
            stats["edges"] = self._rebuild_edges()
            self._rebuild_fts()
        elif stats["indexed"] > 0 or stats["removed"] > 0:
            if changed_ids:
                stats["edges"] = self._rebuild_edges_incremental(changed_ids)
            self._rebuild_fts()

        # Project retrieval_log.jsonl sidecars into context_served. On the
        # full path we walk every session; on incremental we only re-project
        # sessions whose ``session.md`` was re-indexed this rebuild — the
        # ``session.md`` is rewritten by ``mem_extract`` each wrap, so the
        # current session lands in ``changed_ids`` naturally.
        if full:
            self._rebuild_context_served()
        else:
            self._rebuild_context_served(only_ids=changed_ids)

        self.db.commit()
        return stats

    def index_paths(self, paths: Iterable[Path]) -> dict:
        """Index a targeted set of paths, skipping the vault-wide rglob.

        For callers that already know which files changed (e.g.
        ``operations/wrap.py`` knows the session_dir, hook handlers know the
        single touched file). Skips the rglob entirely; only the supplied
        paths are considered. Edges + FTS are rebuilt once at the end.

        Args:
            paths: Iterable of absolute or vault-relative paths. Paths that
                don't exist trigger a remove. Paths outside the vault are
                ignored.

        Returns:
            Stats dict with counts of indexed, skipped, removed files.
            ``edges`` is set when a global rebuild was triggered.
        """
        stats = {"indexed": 0, "skipped": 0, "removed": 0, "edges": 0}
        changed_ids: set[str] = set()

        # Pre-fetch existing hash + mtime for just the candidate paths.
        # Falling back to a per-path SELECT keeps this O(len(paths)).
        existing_hash: dict[str, str] = {}
        existing_mtime: dict[str, float | None] = {}

        # Normalize paths and split into existing / missing buckets.
        for raw in paths:
            p = Path(raw)
            if not p.is_absolute():
                p = self.vault.root / p
            try:
                rel = str(p.relative_to(self.vault.root))
            except ValueError:
                # Path is outside the vault — skip silently.
                continue

            if p.name in LANDING_FILENAMES:
                continue
            if p.name in SOURCE_COMPANION_FILENAMES:
                continue

            row = self.db.execute(
                "SELECT content_hash, file_mtime FROM notes WHERE path = ?",
                (rel,),
            ).fetchone()
            if row is not None:
                existing_hash[rel] = row["content_hash"]
                existing_mtime[rel] = row["file_mtime"]

            if not p.exists():
                self._remove_by_path(rel)
                stats["removed"] += 1
                continue

            # mtime gate
            cached_mtime = existing_mtime.get(rel)
            if cached_mtime is not None:
                try:
                    file_mtime = p.stat().st_mtime
                except OSError:
                    file_mtime = None
                if file_mtime is not None and file_mtime <= cached_mtime:
                    stats["skipped"] += 1
                    continue

            text = p.read_text(encoding="utf-8")
            file_hash = content_hash(text)

            if existing_hash.get(rel) == file_hash:
                try:
                    self.db.execute(
                        "UPDATE notes SET file_mtime = ? WHERE path = ?",
                        (p.stat().st_mtime, rel),
                    )
                except OSError:
                    pass
                stats["skipped"] += 1
                continue

            changed_ids.add(self._index_file(p, text, file_hash, rel))
            stats["indexed"] += 1

        if stats["indexed"] > 0 or stats["removed"] > 0:
            if changed_ids:
                stats["edges"] = self._rebuild_edges_incremental(changed_ids)
            self._rebuild_fts()

        self.db.commit()
        return stats

    def index_file(self, path: Path) -> None:
        """Index or re-index a single file. Used by hooks for incremental updates."""
        if path.name in landing_filename_set(self.config.vault_root):
            return  # Landing docs are excluded from the index
        if path.name in SOURCE_COMPANION_FILENAMES:
            return  # Source companion artifacts (raw.md, snapshot.md, etc.)
        if not path.exists():
            rel = str(path.relative_to(self.vault.root))
            self._remove_by_path(rel)
            self.db.commit()
            return

        text = path.read_text(encoding="utf-8")
        file_hash = content_hash(text)
        rel_path = str(path.relative_to(self.vault.root))
        self._index_file(path, text, file_hash, rel_path)

        # If this is a session note, project its sibling retrieval_log.jsonl
        # opportunistically — wrap-finalize's incremental index pass relies on
        # this to pick up the buffer that archive_buffer just split out.
        try:
            fm, _ = parse_frontmatter(text)
            if fm.get("type") == "session":
                sess_id = fm.get("id", "")
                if sess_id:
                    self._project_session_retrieval_log(
                        sess_id, path.parent / "retrieval_log.jsonl"
                    )
        except Exception:
            log.exception("context_served projection failed for %s", rel_path)
        self._rebuild_fts()
        self.db.commit()

    def _index_file(self, path: Path, text: str, file_hash: str, rel_path: str) -> str:
        """Parse and upsert a single file into the index. Returns the note_id."""
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

        tags = as_list(fm.get("tags"))

        now = datetime.now(timezone.utc).isoformat()

        try:
            file_mtime = path.stat().st_mtime
        except OSError:
            file_mtime = None

        self.db.execute(
            """INSERT OR REPLACE INTO notes
               (id, type, title, path, project, date, tags, content_hash, frontmatter, body_text, updated_at, file_mtime)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                file_mtime,
            ),
        )

        self._sync_concepts(note_id, fm)
        self._sync_tags(note_id, tags)
        self._sync_decision_files(note_id, note_type, fm)
        return note_id

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
        concepts = as_list(fm.get("concepts"))
        if not concepts:
            return

        # Lazy-load alias reverse map and concept-to-domain map
        if not hasattr(self, "_reverse_map"):
            try:
                from personal_mem.synthesis.concepts import (
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
            # Resolve links by note id (bare ``[[dec-X]]``) and by vault path
            # sans .md (path-based ``[[notes/foo|dec-X]]`` — extract_wikilink_ids
            # recovers the id, but key the path too as a belt-and-braces fallback).
            slug_to_id[row["id"].lower()] = row["id"]
            rel = str(row["path"] or "").replace("\\", "/")
            if rel.endswith(".md"):
                rel = rel[:-3]
            if rel:
                slug_to_id[rel.lower()] = row["id"]
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
            for link in extract_wikilink_ids(body):
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
                    "INSERT OR IGNORE INTO edges "
                    "(source, target, edge_type, metadata, created_at, weight) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (nid, session_id, "derived_from", meta, now, 1.0),
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
                    "INSERT OR IGNORE INTO edges "
                    "(source, target, edge_type, metadata, created_at, weight) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (a, b, "relates_to", metadata, now, float(len(shared))),
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
                    "INSERT OR IGNORE INTO edges "
                    "(source, target, edge_type, metadata, created_at, weight) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (a, b, "relates_to", metadata, now, float(len(shared))),
                )
                edge_count += 1

        return edge_count

    def _rebuild_edges_incremental(self, changed_ids: set[str]) -> int:
        """Rebuild ONLY the edges touching the given changed note IDs.

        Used by the incremental rebuild path (``rebuild(full=False)`` and
        ``index_paths``). Deletes all edges where ``source`` OR ``target``
        is in ``changed_ids`` and recomputes them from current frontmatter,
        body, concepts, and tags.

        **Outbound** categories (frontmatter, wikilink, session-dir) are
        recomputed per changed note — fully scoped.

        **Pairwise** categories (concept, tag) pair each changed note only
        against notes that share at least one of its concepts/tags. Pair
        order is sorted so resulting edges PK-collide with full-rebuild
        output (the full path uses ``combinations(sorted(...))`` and the
        edges table PK is ``(source, target, edge_type)``).

        **Staleness trade-off:** if an OLD unchanged note links (frontmatter
        or wikilink) to a NEWLY created note, that old note's outbound
        edges are not recomputed (its body never changed), so the inbound
        edge is missed. The full path (``rebuild(full=True)`` /
        ``mem index --full``) recomputes everything and heals this on its
        periodic run. ``materialize_links`` reads edges live, so the impact
        is brief under-linking of one note's ``## See Also``, never a wrong
        edge. The freq-cap on concept/tag groups is also recomputed against
        the current total — old surviving pair-edges may reflect a stale
        cap, which the periodic full rebuild also heals.

        Cost: O(|S| × avg_concepts × avg_group_size) for a typical wrap's
        ~5 changed notes, vs the full path's global O(notes²) pair walk.

        Returns the count of edges inserted during this call.
        """
        if not changed_ids:
            return 0

        now = datetime.now(timezone.utc).isoformat()
        edge_count = 0
        changed_list = list(changed_ids)
        # SQLite's compile-time variable limit is 999 by default; chunk to be safe.
        CHUNK = 500

        # ── Delete edges incident to any changed note (both directions) ──
        # Pairwise edges like (other, X) where X is changed-as-target must
        # also be cleared so we can re-emit them with current shared concepts.
        for i in range(0, len(changed_list), CHUNK):
            chunk = changed_list[i:i + CHUNK]
            placeholders = ",".join("?" * len(chunk))
            self.db.execute(
                f"DELETE FROM edges WHERE source IN ({placeholders}) "
                f"OR target IN ({placeholders})",
                chunk + chunk,
            )

        # ── Lookup maps (one cheap SELECT — same shape as full rebuild) ──
        slug_to_id: dict[str, str] = {}
        id_to_type: dict[str, str] = {}
        id_to_path: dict[str, str] = {}
        for row in self.db.execute("SELECT id, path, title, type FROM notes"):
            stem = Path(row["path"]).stem
            slug_to_id[stem] = row["id"]
            slug_to_id[row["title"].lower()] = row["id"]
            # Resolve links by note id (bare ``[[dec-X]]``) and by vault path
            # sans .md (path-based ``[[notes/foo|dec-X]]`` — extract_wikilink_ids
            # recovers the id, but key the path too as a belt-and-braces fallback).
            slug_to_id[row["id"].lower()] = row["id"]
            rel = str(row["path"] or "").replace("\\", "/")
            if rel.endswith(".md"):
                rel = rel[:-3]
            if rel:
                slug_to_id[rel.lower()] = row["id"]
            id_to_type[row["id"]] = row["type"]
            id_to_path[row["id"]] = row["path"]
        all_ids = set(slug_to_id.values())
        total_notes = len(id_to_type)

        # ── 1+2. Outbound frontmatter + wikilink edges (changed notes) ──
        changed_rows: list = []
        for i in range(0, len(changed_list), CHUNK):
            chunk = changed_list[i:i + CHUNK]
            placeholders = ",".join("?" * len(chunk))
            changed_rows.extend(
                self.db.execute(
                    f"SELECT id, frontmatter, body_text FROM notes "
                    f"WHERE id IN ({placeholders})",
                    chunk,
                ).fetchall()
            )

        for row in changed_rows:
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
                    resolved = target if target in all_ids else slug_to_id.get(
                        target.lower(), slug_to_id.get(target, "")
                    )
                    if resolved and resolved != note_id:
                        self._insert_edge(note_id, resolved, edge_type, now)
                        edge_count += 1

            body = row["body_text"] or ""
            for link in extract_wikilink_ids(body):
                link_lower = link.lower().strip()
                resolved = slug_to_id.get(link_lower, slug_to_id.get(link, ""))
                if resolved and resolved != note_id:
                    target_type = id_to_type.get(resolved, "note")
                    if target_type == "source":
                        wl_edge_type = "cites"
                    elif target_type == "session":
                        wl_edge_type = "derived_from"
                    else:
                        wl_edge_type = "relates_to"
                    self._insert_edge(note_id, resolved, wl_edge_type, now)
                    edge_count += 1

        # ── 3. Session-directory inference ───────────────────────────────
        session_dir_to_id: dict[str, str] = {}
        for nid, path in id_to_path.items():
            if path.endswith("/session.md") or path.endswith("\\session.md"):
                session_dir_to_id[str(Path(path).parent)] = nid

        # 3a. Each changed note → its session (outbound derived_from).
        for nid in changed_ids:
            path = id_to_path.get(nid)
            if not path:
                continue
            sess_id = session_dir_to_id.get(str(Path(path).parent))
            if sess_id and nid != sess_id:
                meta = json.dumps({"via": "session_dir"})
                self.db.execute(
                    "INSERT OR IGNORE INTO edges "
                    "(source, target, edge_type, metadata, created_at, weight) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (nid, sess_id, "derived_from", meta, now, 1.0),
                )
                edge_count += 1

        # 3b. If a changed note IS itself a session.md, the DELETE wiped its
        # siblings' inbound derived_from edges. Re-emit them.
        for nid in changed_ids:
            if id_to_type.get(nid) != "session":
                continue
            path = id_to_path.get(nid, "")
            if not (path.endswith("/session.md") or path.endswith("\\session.md")):
                continue
            session_dir = str(Path(path).parent)
            for sibling_id, sibling_path in id_to_path.items():
                if sibling_id == nid:
                    continue
                if str(Path(sibling_path).parent) == session_dir:
                    meta = json.dumps({"via": "session_dir"})
                    self.db.execute(
                        "INSERT OR IGNORE INTO edges "
                        "(source, target, edge_type, metadata, created_at, weight) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (sibling_id, nid, "derived_from", meta, now, 1.0),
                    )
                    edge_count += 1

        # ── 4. Concept-based edges (changed × all-sharing-concept) ───────
        cfg = self.config
        concept_freq_cap = int(
            total_notes * cfg.concept_edge_max_freq_pct
        ) if total_notes else 0

        # Fetch each changed note's concepts.
        changed_concepts: dict[str, set[str]] = defaultdict(set)
        for i in range(0, len(changed_list), CHUNK):
            chunk = changed_list[i:i + CHUNK]
            placeholders = ",".join("?" * len(chunk))
            for row in self.db.execute(
                f"SELECT note_id, concept FROM note_concepts "
                f"WHERE note_id IN ({placeholders})",
                chunk,
            ):
                changed_concepts[row["note_id"]].add(row["concept"])

        # Group cache so concepts shared by multiple changed notes only
        # incur one note_concepts scan.
        concept_group_cache: dict[str, list[str]] = {}

        def _concept_group(c: str) -> list[str]:
            if c not in concept_group_cache:
                concept_group_cache[c] = sorted({
                    r["note_id"] for r in self.db.execute(
                        "SELECT note_id FROM note_concepts WHERE concept = ?", (c,)
                    )
                })
            return concept_group_cache[c]

        pair_shared: dict[tuple, list[str]] = defaultdict(list)
        for nid, concepts in changed_concepts.items():
            for c in concepts:
                group = _concept_group(c)
                if len(group) > concept_freq_cap > 0:
                    continue
                for other in group:
                    if other == nid:
                        continue
                    a, b = (nid, other) if nid < other else (other, nid)
                    pair_shared[(a, b)].append(c)

        for (a, b), shared in pair_shared.items():
            shared_unique = sorted(set(shared))
            if len(shared_unique) >= cfg.concept_edge_threshold:
                metadata = json.dumps({"via": "concept", "shared": shared_unique})
                self.db.execute(
                    "INSERT OR IGNORE INTO edges "
                    "(source, target, edge_type, metadata, created_at, weight) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (a, b, "relates_to", metadata, now, float(len(shared_unique))),
                )
                edge_count += 1

        # ── 5. Tag-based edges (mirror of category 4) ────────────────────
        tag_freq_cap = int(
            total_notes * cfg.tag_edge_max_freq_pct
        ) if total_notes else 0
        excluded_tags = set(cfg.tag_edge_exclude)

        changed_tags: dict[str, set[str]] = defaultdict(set)
        for i in range(0, len(changed_list), CHUNK):
            chunk = changed_list[i:i + CHUNK]
            placeholders = ",".join("?" * len(chunk))
            for row in self.db.execute(
                f"SELECT note_id, tag FROM note_tags "
                f"WHERE note_id IN ({placeholders})",
                chunk,
            ):
                tag = row["tag"]
                if tag in excluded_tags:
                    continue
                changed_tags[row["note_id"]].add(tag)

        tag_group_cache: dict[str, list[str]] = {}

        def _tag_group(t: str) -> list[str]:
            if t not in tag_group_cache:
                tag_group_cache[t] = sorted({
                    r["note_id"] for r in self.db.execute(
                        "SELECT note_id FROM note_tags WHERE tag = ?", (t,)
                    )
                })
            return tag_group_cache[t]

        tag_pair_shared: dict[tuple, list[str]] = defaultdict(list)
        for nid, tags in changed_tags.items():
            for t in tags:
                group = _tag_group(t)
                if len(group) > tag_freq_cap > 0:
                    continue
                for other in group:
                    if other == nid:
                        continue
                    a, b = (nid, other) if nid < other else (other, nid)
                    tag_pair_shared[(a, b)].append(t)

        for (a, b), shared in tag_pair_shared.items():
            shared_unique = sorted(set(shared))
            if len(shared_unique) >= cfg.tag_edge_threshold:
                metadata = json.dumps({"via": "tag", "shared": shared_unique})
                self.db.execute(
                    "INSERT OR IGNORE INTO edges "
                    "(source, target, edge_type, metadata, created_at, weight) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (a, b, "relates_to", metadata, now, float(len(shared_unique))),
                )
                edge_count += 1

        return edge_count

    def _insert_edge(
        self,
        source: str,
        target: str,
        edge_type: str,
        created_at: str,
        weight: float = 1.0,
    ) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO edges "
            "(source, target, edge_type, created_at, weight) "
            "VALUES (?, ?, ?, ?, ?)",
            (source, target, edge_type, created_at, float(weight)),
        )

    def _rebuild_fts(self) -> None:
        """Rebuild the FTS index from the notes table."""
        self.db.execute("INSERT INTO notes_fts(notes_fts) VALUES('rebuild')")

    # ------------------------------------------------------------------
    # RLVR substrate: context_served projection (slice 3)
    # ------------------------------------------------------------------

    def _rebuild_context_served(self, only_ids: set[str] | None = None) -> None:
        """Project sessions' ``retrieval_log.jsonl`` into ``context_served``.

        Called at the end of :meth:`rebuild`. By default walks every indexed
        session note (the `notes` table tells us which) and feeds each
        sibling ``retrieval_log.jsonl`` through
        :meth:`_project_session_retrieval_log`.

        Args:
            only_ids: When supplied, restrict the projection to session notes
                whose id is in this set. The session-type filter in the SELECT
                silently drops any non-session IDs in the set. Pass an empty
                set to no-op; pass ``None`` (the default, used by full rebuild)
                to walk every session. Used by ``rebuild(full=False)`` to skip
                the all-sessions walk — the current session's ``session.md``
                lands in ``changed_ids`` naturally (``mem_extract`` rewrites
                it each wrap), so its retrieval log gets re-projected.

        Cost: one ``SELECT id, path FROM notes WHERE type='session'`` plus a
        stat-and-read per session folder. Most session folders won't have a
        retrieval log yet — pre-RLVR sessions skip silently. Idempotent.
        """
        if only_ids is not None and not only_ids:
            return
        if only_ids:
            chunk = list(only_ids)
            placeholders = ",".join("?" * len(chunk))
            rows = self.db.execute(
                f"SELECT id, path FROM notes "
                f"WHERE type = 'session' AND id IN ({placeholders})",
                chunk,
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT id, path FROM notes WHERE type = 'session'"
            ).fetchall()
        for row in rows:
            sess_id = row["id"]
            rel_path = row["path"]
            sess_path = self.vault.root / rel_path
            if not sess_path.exists():
                continue
            log_path = sess_path.parent / "retrieval_log.jsonl"
            try:
                self._project_session_retrieval_log(sess_id, log_path)
            except Exception:
                log.exception("context_served projection failed for %s", sess_id)

    def _project_session_retrieval_log(
        self, session_id: str, log_path: Path
    ) -> int:
        """Parse one ``retrieval_log.jsonl`` and upsert its rows.

        Each ``startup`` event contributes one row per ``returned_id`` with
        ``source='startup'``. Each ``retrieval`` event contributes one per
        ``returned_id`` with ``source='onthefly'``. If the same note appears
        in both, both rows persist — the export-side code decides precedence
        (onthefly wins over startup-only).

        Returns the number of rows upserted; 0 if the log file is missing.
        Tolerates malformed lines line-by-line.
        """
        if not log_path.exists():
            return 0

        upserted = 0
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type", "")
                if etype == "startup":
                    src = "startup"
                elif etype == "retrieval":
                    src = "onthefly"
                else:
                    continue
                ts = ev.get("ts", "")
                returned_ids = ev.get("returned_ids", []) or []
                for nid in returned_ids:
                    if not isinstance(nid, str) or not nid:
                        continue
                    self.db.execute(
                        "INSERT OR REPLACE INTO context_served "
                        "(session_id, note_id, source, ts) VALUES (?, ?, ?, ?)",
                        (session_id, nid, src, ts),
                    )
                    upserted += 1
        return upserted

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
        from personal_mem.core.vault import strip_section

        stats = {"notes_updated": 0, "notes_skipped": 0, "links_written": 0}
        SEE_ALSO = "## See Also"

        STRUCTURAL_TYPES = {"derived_from", "cites", "builds_on", "supersedes", "implements"}

        all_notes = {
            row["id"]: row
            for row in self.db.execute("SELECT id, path, title FROM notes")
        }

        def _strip_stale_see_also(note_row) -> bool:
            """Remove a leftover ## See Also from a note that no longer has
            any links to show. Without this a note that lost all its edges
            keeps a stale (and possibly pre-alias bare-id) section forever.
            Returns True if a section was removed."""
            if dry_run:
                return False
            fp = self.vault.root / note_row["path"]
            if not fp.exists():
                return False
            txt = fp.read_text(encoding="utf-8")
            stripped = strip_section(txt, SEE_ALSO)
            if stripped != txt:
                fp.write_text(stripped.rstrip() + "\n", encoding="utf-8")
                return True
            return False

        for note_id, note_row in all_notes.items():
            edges = self.db.execute(
                "SELECT source, target, edge_type, metadata FROM edges "
                "WHERE source = ? OR target = ?",
                (note_id, note_id),
            ).fetchall()

            if not edges:
                if _strip_stale_see_also(note_row):
                    stats["notes_updated"] += 1
                else:
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
                    # Concept/tag edge. Only genuinely-related notes belong in
                    # See Also: tag edges (news+finance etc.) are filter facets,
                    # not real connections — excluded entirely. Concept edges
                    # need >=2 shared concepts; a single shared concept bridges
                    # unrelated domains (a macro note and a history note both
                    # tagged `geopolitics`). The underlying graph edges are
                    # untouched — this only gates the rendered See Also list.
                    meta = edge["metadata"] or "{}"
                    try:
                        parsed = json.loads(meta)
                    except (json.JSONDecodeError, TypeError):
                        parsed = {}
                    if parsed.get("via") == "tag":
                        continue
                    shared = parsed.get("shared", [])
                    if len(shared) < 2:
                        continue
                    concept_edges.append((other, edge["edge_type"], shared[0]))

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
                if _strip_stale_see_also(note_row):
                    stats["notes_updated"] += 1
                else:
                    stats["notes_skipped"] += 1
                continue

            # Build the See Also section. Link by vault-relative PATH (sans
            # .md) — the same structural form concept-hub links use. Obsidian
            # resolves a path link by file location, so it NEVER spawns a
            # phantom stub, even on notes that predate the `aliases:` backfill.
            # (Bare `[[<id>]]` resolves only via the alias; a path link does
            # not depend on it. A filename stem alone is ambiguous — every
            # folder-layout source is `source.md` — but the full path is
            # unique, which is exactly why the id+alias workaround existed.)
            lines = [SEE_ALSO, ""]
            for target_id, edge_type in linked:
                target = all_notes[target_id]
                title = (target["title"] or "").replace("|", " ").replace("]", " ").strip()
                rel = str(target["path"] or "").replace("\\", "/")
                if rel.endswith(".md"):
                    rel = rel[:-3]
                ref = rel or target_id  # fall back to id-alias if path is missing
                link = f"[[{ref}|{title}]]" if title else f"[[{ref}]]"
                label = edge_type.replace("_", " ") if edge_type != "relates_to" else ""
                if label:
                    lines.append(f"- {link} _{label}_")
                else:
                    lines.append(f"- {link}")
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
