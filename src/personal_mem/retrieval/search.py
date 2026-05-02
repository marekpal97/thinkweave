"""Search and graph traversal over the SQLite index.

Provides FTS5 full-text search and recursive CTE graph traversal.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from personal_mem.core.config import Config, load_config


@dataclass
class SearchResult:
    id: str
    type: str
    title: str
    path: str
    project: str
    date: str
    tags: list[str]
    snippet: str = ""
    rank: float = 0.0


@dataclass
class GraphNode:
    id: str
    type: str
    title: str
    edges: list[GraphEdge] = field(default_factory=list)


@dataclass
class GraphEdge:
    source: str
    target: str
    edge_type: str


class Search:
    """Search and graph traversal over the vault index."""

    def __init__(self, config: Config | None = None):
        self.config = config or load_config()
        self._db: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            db_path = self.config.index_db
            if not db_path.exists():
                raise FileNotFoundError(
                    f"Index not found at {db_path}. Run `mem index` first."
                )
            self._db = sqlite3.connect(str(db_path))
            self._db.row_factory = sqlite3.Row
        return self._db

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    def search(
        self,
        query: str,
        note_type: str | list[str] = "",
        project: str = "",
        tags: list[str] | None = None,
        concepts: list[str] | None = None,
        since: str = "",
        until: str = "",
        limit: int = 10,
    ) -> list[SearchResult]:
        """Full-text search with optional filters.

        Filters:
            note_type: str or list — restrict to one or more types.
            project: project name.
            tags: list of tags; AND-joined.
            concepts: list of concepts; result must include at least one
                (post-filter via the ``note_concepts`` join table).
            since / until: ISO date strings (YYYY-MM-DD); date window.

        When ``query`` is empty, returns the most recent notes matching
        the filters (date desc) — list mode for "give me everything for
        project X this week" without needing an FTS term.
        """
        # Normalize note_type to a list for consistent filter handling
        type_list = [note_type] if isinstance(note_type, str) else list(note_type)
        type_list = [t for t in type_list if t]

        conditions = []
        params: list = []

        if query:
            # FTS5 search — quote the query so hyphens and special chars
            # are treated as literals, not FTS5 operators
            fts_query = '"' + query.replace('"', '""') + '"'
            sql = """
                SELECT n.id, n.type, n.title, n.path, n.project, n.date, n.tags,
                       snippet(notes_fts, 2, '>>>', '<<<', '...', 32) as snippet,
                       rank
                FROM notes_fts f
                JOIN notes n ON n.rowid = f.rowid
                WHERE notes_fts MATCH ?
            """
            params.append(fts_query)
        else:
            sql = """
                SELECT id, type, title, path, project, date, tags, '' as snippet, 0 as rank
                FROM notes WHERE 1=1
            """

        col_prefix = "n." if query else ""

        if type_list:
            if len(type_list) == 1:
                conditions.append(f"{col_prefix}type = ?")
                params.append(type_list[0])
            else:
                placeholders = ",".join("?" for _ in type_list)
                conditions.append(f"{col_prefix}type IN ({placeholders})")
                params.extend(type_list)
        if project:
            conditions.append(f"{col_prefix}project = ?")
            params.append(project)
        if tags:
            for tag in tags:
                conditions.append(f'{col_prefix}tags LIKE ?')
                params.append(f'%"{tag}"%')
        if since:
            conditions.append(f"{col_prefix}date >= ?")
            params.append(since)
        if until:
            conditions.append(f"{col_prefix}date <= ?")
            params.append(until)
        if concepts:
            # Restrict to notes that include at least one of the listed
            # concepts via the note_concepts table. EXISTS keeps the
            # primary query simple regardless of FTS vs list mode.
            placeholders = ",".join("?" for _ in concepts)
            id_col = f"{col_prefix}id"
            conditions.append(
                f"EXISTS (SELECT 1 FROM note_concepts nc "
                f"WHERE nc.note_id = {id_col} "
                f"AND nc.concept IN ({placeholders}))"
            )
            params.extend([c.lower() for c in concepts])

        if conditions:
            sql += " AND " + " AND ".join(conditions)

        if query:
            sql += " ORDER BY rank"
        else:
            sql += " ORDER BY date DESC"

        sql += " LIMIT ?"
        params.append(limit)

        results = []
        for row in self.db.execute(sql, params):
            tags_list = json.loads(row["tags"]) if row["tags"] else []
            results.append(
                SearchResult(
                    id=row["id"],
                    type=row["type"],
                    title=row["title"],
                    path=row["path"],
                    project=row["project"] or "",
                    date=row["date"] or "",
                    tags=tags_list,
                    snippet=row["snippet"] or "",
                    rank=row["rank"] if row["rank"] else 0.0,
                )
            )
        return results

    def get_related(
        self,
        note_id: str,
        depth: int = 2,
        edge_types: list[str] | None = None,
        note_type: str | list[str] = "",
        project: str = "",
    ) -> list[GraphNode]:
        """Graph traversal from a note using recursive CTE.

        ``note_type`` and ``project`` filter the *projected* nodes (the
        outer SELECT after the recursive walk). They do not prune the
        traversal itself — the walk still reaches through nodes of any
        type — but only matching nodes are returned. This keeps "show
        me the source notes connected to this decision" simple without
        rewriting the recursive CTE.
        """
        edge_filter = ""
        params: list = [note_id, depth]
        if edge_types:
            placeholders = ",".join("?" for _ in edge_types)
            edge_filter = f"AND e.edge_type IN ({placeholders})"
            params = [note_id] + edge_types + [depth]

        type_list = [note_type] if isinstance(note_type, str) else list(note_type)
        type_list = [t for t in type_list if t]

        outer_filters = ["n.id != ?"]
        outer_params: list = [note_id]
        if type_list:
            placeholders = ",".join("?" for _ in type_list)
            outer_filters.append(f"n.type IN ({placeholders})")
            outer_params.extend(type_list)
        if project:
            outer_filters.append("n.project = ?")
            outer_params.append(project)

        outer_where = " AND ".join(outer_filters)

        sql = f"""
            WITH RECURSIVE reachable(id, depth) AS (
                SELECT ?, 0
                UNION
                SELECT CASE
                    WHEN e.source = reachable.id THEN e.target
                    ELSE e.source
                END, reachable.depth + 1
                FROM reachable
                JOIN edges e ON (e.source = reachable.id OR e.target = reachable.id)
                    {edge_filter}
                WHERE reachable.depth < ?
            )
            SELECT DISTINCT n.id, n.type, n.title
            FROM reachable r
            JOIN notes n ON n.id = r.id
            WHERE {outer_where}
        """
        params.extend(outer_params)

        nodes: dict[str, GraphNode] = {}
        for row in self.db.execute(sql, params):
            nodes[row["id"]] = GraphNode(
                id=row["id"], type=row["type"], title=row["title"]
            )

        # Fetch edges between these nodes
        if nodes:
            all_ids = list(nodes.keys()) + [note_id]
            placeholders = ",".join("?" for _ in all_ids)
            edge_sql = f"""
                SELECT source, target, edge_type FROM edges
                WHERE source IN ({placeholders}) AND target IN ({placeholders})
            """
            for row in self.db.execute(edge_sql, all_ids + all_ids):
                edge = GraphEdge(
                    source=row["source"],
                    target=row["target"],
                    edge_type=row["edge_type"],
                )
                if row["source"] in nodes:
                    nodes[row["source"]].edges.append(edge)
                if row["target"] in nodes:
                    nodes[row["target"]].edges.append(edge)

        return list(nodes.values())

    def get_context(
        self,
        project: str = "",
        tags: list[str] | None = None,
        query: str = "",
        concepts: list[str] | None = None,
        limit: int = 5,
        note_type: str | list[str] = "",
        since: str = "",
        until: str = "",
    ) -> list[SearchResult]:
        """Get the most relevant notes for a given context.

        Three-layer retrieval:
        1. FTS search (if query provided)
        2. Concept expansion — pull related notes sharing concepts
           from FTS hits or from explicitly provided concepts
        3. Recency supplement

        ``note_type`` filters all three layers — pass e.g. ``["source","session"]``
        to restrict the context to specific kinds of notes. ``since`` /
        ``until`` are ISO date strings (YYYY-MM-DD) and likewise apply
        across all three layers.
        """
        # Normalize type filter to a list
        type_list = [note_type] if isinstance(note_type, str) else list(note_type)
        type_list = [t for t in type_list if t]

        results = []
        seen_ids: set[str] = set()

        # Layer 1: FTS search if query provided
        if query:
            results = self.search(
                query,
                project=project,
                tags=tags,
                limit=limit,
                note_type=type_list if type_list else "",
                since=since,
                until=until,
            )
            seen_ids = {r.id for r in results}

        # Layer 2: Concept expansion
        if len(results) < limit:
            # Determine concepts to expand on
            expand_concepts = list(concepts) if concepts else []
            if not expand_concepts and results:
                # Extract concepts from FTS hits
                for r in results:
                    for row in self.db.execute(
                        "SELECT concept FROM note_concepts WHERE note_id = ?", (r.id,)
                    ):
                        if row["concept"] not in expand_concepts:
                            expand_concepts.append(row["concept"])

            if expand_concepts:
                remaining = limit - len(results)
                placeholders = ",".join("?" for _ in expand_concepts)
                conditions = [f"nc.concept IN ({placeholders})"]
                params: list = list(expand_concepts)

                if project:
                    conditions.append("n.project = ?")
                    params.append(project)

                if type_list:
                    type_placeholders = ",".join("?" for _ in type_list)
                    conditions.append(f"n.type IN ({type_placeholders})")
                    params.extend(type_list)

                if since:
                    conditions.append("n.date >= ?")
                    params.append(since)
                if until:
                    conditions.append("n.date <= ?")
                    params.append(until)

                if seen_ids:
                    id_placeholders = ",".join("?" for _ in seen_ids)
                    conditions.append(f"n.id NOT IN ({id_placeholders})")
                    params.extend(seen_ids)

                where = " AND ".join(conditions)
                sql = f"""
                    SELECT n.id, n.type, n.title, n.path, n.project, n.date, n.tags,
                           COUNT(DISTINCT nc.concept) as shared_count
                    FROM note_concepts nc
                    JOIN notes n ON n.id = nc.note_id
                    WHERE {where}
                    GROUP BY n.id
                    ORDER BY shared_count DESC, n.date DESC
                    LIMIT ?
                """
                params.append(remaining)

                for row in self.db.execute(sql, params):
                    tags_list = json.loads(row["tags"]) if row["tags"] else []
                    if tags and not set(tags).issubset(set(tags_list)):
                        continue
                    results.append(
                        SearchResult(
                            id=row["id"],
                            type=row["type"],
                            title=row["title"],
                            path=row["path"],
                            project=row["project"] or "",
                            date=row["date"] or "",
                            tags=tags_list,
                        )
                    )
                    seen_ids.add(row["id"])

        # Layer 3: Recency supplement
        if len(results) < limit:
            remaining = limit - len(results)

            conditions_r = []
            params_r: list = []
            if project:
                conditions_r.append("project = ?")
                params_r.append(project)
            if type_list:
                type_placeholders = ",".join("?" for _ in type_list)
                conditions_r.append(f"type IN ({type_placeholders})")
                params_r.extend(type_list)
            if since:
                conditions_r.append("date >= ?")
                params_r.append(since)
            if until:
                conditions_r.append("date <= ?")
                params_r.append(until)

            where_r = " AND ".join(conditions_r) if conditions_r else "1=1"
            sql = f"""
                SELECT id, type, title, path, project, date, tags
                FROM notes WHERE {where_r}
                ORDER BY date DESC
                LIMIT ?
            """
            params_r.append(remaining + len(seen_ids))  # overfetch to account for dedup

            for row in self.db.execute(sql, params_r):
                if row["id"] in seen_ids:
                    continue
                tags_list = json.loads(row["tags"]) if row["tags"] else []
                if tags and not set(tags).issubset(set(tags_list)):
                    continue
                results.append(
                    SearchResult(
                        id=row["id"],
                        type=row["type"],
                        title=row["title"],
                        path=row["path"],
                        project=row["project"] or "",
                        date=row["date"] or "",
                        tags=tags_list,
                    )
                )
                seen_ids.add(row["id"])
                if len(results) >= limit:
                    break

        return results

    def search_by_concept(
        self,
        concept: str | list[str],
        project: str = "",
        note_type: str | list[str] = "",
        limit: int = 20,
        *,
        match_mode: str = "any",
        min_matches: int = 0,
        since: str = "",
        until: str = "",
    ) -> list[SearchResult]:
        """Find notes by concept(s). Supports single concept or list with
        ``any`` (union) or ``all`` (intersection) semantics.

        Args:
            concept: Single concept string or list. Strings are lowercased.
            project: Optional project filter. Empty = cross-project.
            note_type: Optional type filter. Accepts a string or a list.
            limit: Max results.
            match_mode: ``"any"`` → return notes matching *any* concept (default,
                preserves old single-concept behavior). ``"all"`` → return notes
                matching *all* concepts (intersection).
            min_matches: When ``match_mode="all"``, require at least this many
                distinct concept matches. 0 means "require all concepts".

        Returns results sorted by shared-concept count (when multi-concept) or
        date desc otherwise.
        """
        # Normalize inputs
        concepts = [concept] if isinstance(concept, str) else list(concept)
        concepts = [c.lower() for c in concepts if c]
        if not concepts:
            return []

        type_list = [note_type] if isinstance(note_type, str) else list(note_type)
        type_list = [t for t in type_list if t]

        # Build concept placeholders for IN (?, ?, ...)
        concept_placeholders = ",".join("?" for _ in concepts)

        conditions = [f"nc.concept IN ({concept_placeholders})"]
        params: list = list(concepts)

        if project:
            conditions.append("n.project = ?")
            params.append(project)

        if type_list:
            type_placeholders = ",".join("?" for _ in type_list)
            conditions.append(f"n.type IN ({type_placeholders})")
            params.extend(type_list)

        if since:
            conditions.append("n.date >= ?")
            params.append(since)
        if until:
            conditions.append("n.date <= ?")
            params.append(until)

        where = " AND ".join(conditions)

        # Intersection mode — HAVING on distinct concept count. Default
        # threshold is len(concepts) (all concepts must match); caller may
        # override with min_matches to allow partial intersection.
        if match_mode == "all" and len(concepts) > 1:
            threshold = min_matches if min_matches > 0 else len(concepts)
            sql = f"""
                SELECT n.id, n.type, n.title, n.path, n.project, n.date, n.tags,
                       COUNT(DISTINCT nc.concept) AS match_count
                FROM note_concepts nc
                JOIN notes n ON n.id = nc.note_id
                WHERE {where}
                GROUP BY n.id
                HAVING COUNT(DISTINCT nc.concept) >= ?
                ORDER BY match_count DESC, n.date DESC
                LIMIT ?
            """
            params.append(threshold)
        else:
            sql = f"""
                SELECT DISTINCT n.id, n.type, n.title, n.path, n.project, n.date, n.tags
                FROM note_concepts nc
                JOIN notes n ON n.id = nc.note_id
                WHERE {where}
                ORDER BY n.date DESC
                LIMIT ?
            """
        params.append(limit)

        results = []
        for row in self.db.execute(sql, params):
            tags_list = json.loads(row["tags"]) if row["tags"] else []
            results.append(
                SearchResult(
                    id=row["id"],
                    type=row["type"],
                    title=row["title"],
                    path=row["path"],
                    project=row["project"] or "",
                    date=row["date"] or "",
                    tags=tags_list,
                )
            )
        return results

    def get_project_concepts(self, project: str) -> dict[str, int]:
        """Get concept frequency for a specific project."""
        sql = """
            SELECT nc.concept, COUNT(*) as cnt
            FROM note_concepts nc
            JOIN notes n ON n.id = nc.note_id
            WHERE n.project = ?
            GROUP BY nc.concept
            ORDER BY cnt DESC
        """
        return {row["concept"]: row["cnt"] for row in self.db.execute(sql, (project,))}

    def get_concept_source_counts(
        self, concepts: list[str]
    ) -> dict[str, dict]:
        """Bulk source-count + URL lookup for a list of concepts.

        Collapses /discover's O(N) per-concept under-source fan-out into a
        single JOIN. For each input concept returns the full set of source
        notes tagged with it (id, title, url) — the caller uses the count
        for the <2 under-sourced threshold and the urls as the per-gap
        dedup reference.

        Input concepts are case-insensitive (concepts are stored lowercased
        by the indexer). Concepts with zero sources still appear in the
        output with count=0 and an empty sources list, so the caller can
        iterate the result dict without a KeyError.
        """
        result: dict[str, dict] = {
            c: {"count": 0, "sources": []} for c in concepts
        }
        if not concepts:
            return result

        placeholders = ",".join(["?"] * len(concepts))
        sql = f"""
            SELECT nc.concept, n.id, n.title, n.frontmatter
            FROM note_concepts nc
            JOIN notes n ON n.id = nc.note_id
            WHERE nc.concept IN ({placeholders}) AND n.type = 'source'
            ORDER BY nc.concept, n.date DESC
        """
        lowered = [c.lower() for c in concepts]
        # Map lowercased → original so the caller gets back the keys it passed in
        key_map = {c.lower(): c for c in concepts}
        for row in self.db.execute(sql, lowered):
            key = key_map.get(row["concept"], row["concept"])
            fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
            result[key]["count"] += 1
            result[key]["sources"].append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "url": fm.get("url", ""),
                }
            )
        return result

    def get_cross_project_activity(self, days: int = 14) -> list[dict]:
        """Rank projects by recent session + decision activity.

        Returns a list of dicts sorted by total activity (sessions +
        decisions) descending. Each entry has project name, session count,
        decision count, and the most recent note date in the window.
        Sessions lacking a `project` frontmatter field (NULL or empty in
        the index) are bucketed under `_unscoped` — this matches the
        on-disk `projects/_unscoped/sessions/` directory where
        project-less sessions land.
        """
        from datetime import date, timedelta

        cutoff = (date.today() - timedelta(days=days)).isoformat()
        sql = """
            SELECT
                COALESCE(NULLIF(project, ''), '_unscoped') AS project,
                type,
                COUNT(*) AS cnt,
                MAX(date) AS latest_date
            FROM notes
            WHERE date >= ? AND type IN ('session', 'decision')
            GROUP BY COALESCE(NULLIF(project, ''), '_unscoped'), type
        """
        agg: dict[str, dict] = {}
        for row in self.db.execute(sql, (cutoff,)):
            proj = row["project"]
            entry = agg.setdefault(
                proj,
                {"project": proj, "sessions": 0, "decisions": 0, "latest_date": ""},
            )
            if row["type"] == "session":
                entry["sessions"] = row["cnt"]
            elif row["type"] == "decision":
                entry["decisions"] = row["cnt"]
            latest = row["latest_date"] or ""
            if latest > entry["latest_date"]:
                entry["latest_date"] = latest

        ranked = sorted(
            agg.values(),
            key=lambda e: (e["sessions"] + e["decisions"], e["latest_date"]),
            reverse=True,
        )
        return ranked

    def get_concept_cooccurrence(
        self, concept: str, limit: int = 10
    ) -> list[tuple[str, int]]:
        """Find concepts that frequently co-occur with the given concept."""
        sql = """
            SELECT nc2.concept, COUNT(*) as cnt
            FROM note_concepts nc1
            JOIN note_concepts nc2 ON nc1.note_id = nc2.note_id
            WHERE nc1.concept = ? AND nc2.concept != ?
            GROUP BY nc2.concept
            ORDER BY cnt DESC
            LIMIT ?
        """
        return [
            (row["concept"], row["cnt"])
            for row in self.db.execute(sql, (concept.lower(), concept.lower(), limit))
        ]

    def get_note_by_id(self, note_id: str) -> dict | None:
        """Get a single note by ID."""
        row = self.db.execute(
            "SELECT * FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        if row:
            return dict(row)
        return None

    def get_source_lens(
        self,
        source_id: str,
        *,
        limit: int = 50,
    ) -> dict:
        """Walk outward from a source note — everything citing, linking to,
        or sharing concepts with it.

        Returns a dict with:
            - ``source``: the source note itself (id, title, project, concepts) or None
            - ``inbound``: notes with any edge pointing at the source (wikilinks, cites, etc.)
            - ``decisions``: decisions that cite the source (subset of inbound, type=decision)
            - ``sessions``: sessions derived from or touching the source (subset of inbound)
            - ``shared_concepts``: [(concept, cooccurring_note_count), ...] — "what
              concepts does this source contribute to across the vault"

        One call returns everything needed to answer "what did this source
        feed into?". Inbound detection uses the existing ``edges`` table,
        which is populated from both frontmatter fields (cites, derived_from,
        etc.) and wikilinks — no new indexer work needed.
        """
        result: dict = {
            "source": None,
            "inbound": [],
            "decisions": [],
            "sessions": [],
            "shared_concepts": [],
        }

        src = self.get_note_by_id(source_id)
        if not src:
            return result

        # Source concepts for shared-concept analysis
        src_concepts = [
            row["concept"]
            for row in self.db.execute(
                "SELECT concept FROM note_concepts WHERE note_id = ?", (source_id,)
            )
        ]

        result["source"] = {
            "id": src["id"],
            "type": src["type"],
            "title": src["title"],
            "project": src["project"] or "",
            "date": src["date"] or "",
            "concepts": src_concepts,
        }

        # Inbound edges — any note that points at this source
        inbound_sql = """
            SELECT DISTINCT n.id, n.type, n.title, n.path, n.project, n.date, n.tags,
                            e.edge_type
            FROM edges e
            JOIN notes n ON n.id = e.source
            WHERE e.target = ?
            ORDER BY n.date DESC
            LIMIT ?
        """
        for row in self.db.execute(inbound_sql, (source_id, limit)):
            tags_list = json.loads(row["tags"]) if row["tags"] else []
            entry = {
                "id": row["id"],
                "type": row["type"],
                "title": row["title"],
                "project": row["project"] or "",
                "date": row["date"] or "",
                "edge_type": row["edge_type"],
                "tags": tags_list,
            }
            result["inbound"].append(entry)
            if row["type"] == "decision":
                result["decisions"].append(entry)
            elif row["type"] == "session":
                result["sessions"].append(entry)

        # Shared concepts — for each of the source's concepts, count how many
        # *other* notes also use it (a rough "reach" signal).
        if src_concepts:
            concept_placeholders = ",".join("?" for _ in src_concepts)
            reach_sql = f"""
                SELECT nc.concept, COUNT(DISTINCT nc.note_id) AS cnt
                FROM note_concepts nc
                WHERE nc.concept IN ({concept_placeholders})
                  AND nc.note_id != ?
                GROUP BY nc.concept
                ORDER BY cnt DESC
            """
            reach_params = list(src_concepts) + [source_id]
            result["shared_concepts"] = [
                (row["concept"], row["cnt"])
                for row in self.db.execute(reach_sql, reach_params)
            ]

        return result

    def search_decisions_by_file(
        self,
        file_path: str,
        *,
        project: str = "",
        status: str = "",
        limit: int = 50,
    ) -> list[SearchResult]:
        """Return every decision that touched a given file path.

        Uses the ``decision_files`` indexer table (populated from the
        ``file_paths`` frontmatter field of type=decision notes). One indexed
        JOIN replaces scanning every decision's frontmatter.
        """
        conditions = ["df.file_path = ?", "n.type = 'decision'"]
        params: list = [file_path]

        if project:
            conditions.append("n.project = ?")
            params.append(project)

        where = " AND ".join(conditions)
        sql = f"""
            SELECT n.id, n.type, n.title, n.path, n.project, n.date, n.tags,
                   n.frontmatter
            FROM decision_files df
            JOIN notes n ON n.id = df.decision_id
            WHERE {where}
            ORDER BY n.date DESC
            LIMIT ?
        """
        params.append(limit)

        results = []
        for row in self.db.execute(sql, params):
            tags_list = json.loads(row["tags"]) if row["tags"] else []

            # Filter by status if requested — cheap post-filter on small result sets.
            if status:
                try:
                    fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
                except json.JSONDecodeError:
                    fm = {}
                if fm.get("status") != status:
                    continue

            results.append(
                SearchResult(
                    id=row["id"],
                    type=row["type"],
                    title=row["title"],
                    path=row["path"],
                    project=row["project"] or "",
                    date=row["date"] or "",
                    tags=tags_list,
                )
            )
        return results

    def similar(
        self,
        query: str,
        *,
        project: str = "",
        note_type: str | list[str] = "",
        limit: int = 10,
    ) -> list[SearchResult]:
        """Semantic search via cached embeddings. Soft-fails if embeddings
        aren't configured (missing API key, missing embeddings db).

        Returns SearchResult rows enriched from the main notes table so the
        caller gets titles/paths/etc., not just IDs.
        """
        try:
            from personal_mem.core.embeddings import EmbeddingSearch
        except ImportError:
            return []

        try:
            es = EmbeddingSearch(config=self.config)
            if not self.config.embeddings_db.exists():
                return []
            hits = es.search(
                query, limit=limit, project=project, note_type=note_type
            )
            es.close()
        except (FileNotFoundError, ValueError, ImportError):
            return []

        if not hits:
            return []

        # Join against the main index to hydrate titles etc.
        id_to_score = {nid: score for nid, score in hits}
        placeholders = ",".join("?" for _ in id_to_score)
        rows = self.db.execute(
            f"SELECT id, type, title, path, project, date, tags "
            f"FROM notes WHERE id IN ({placeholders})",
            list(id_to_score.keys()),
        ).fetchall()

        results = []
        for row in rows:
            tags_list = json.loads(row["tags"]) if row["tags"] else []
            results.append(
                SearchResult(
                    id=row["id"],
                    type=row["type"],
                    title=row["title"],
                    path=row["path"],
                    project=row["project"] or "",
                    date=row["date"] or "",
                    tags=tags_list,
                    rank=id_to_score[row["id"]],
                )
            )
        # Preserve semantic ranking order
        results.sort(key=lambda r: r.rank, reverse=True)
        return results

    def hybrid_search(
        self,
        query: str,
        *,
        project: str = "",
        note_type: str | list[str] = "",
        limit: int = 10,
        rrf_k: int = 60,
    ) -> list[SearchResult]:
        """Hybrid retrieval: run FTS + semantic, fuse with reciprocal rank fusion.

        RRF score: ``Σ 1/(k + rank_i)`` across retrievers. k=60 is the
        standard constant from the original RRF paper; it needs no tuning.

        Falls back gracefully: if embeddings are unavailable, returns
        FTS-only results. If FTS returns nothing (e.g. empty query), returns
        semantic-only.
        """
        # Run both retrievers independently
        fts_results = self.search(
            query, project=project, note_type=note_type, limit=max(limit * 2, 20)
        )
        sem_results = self.similar(
            query, project=project, note_type=note_type, limit=max(limit * 2, 20)
        )

        # Edge cases — one retriever empty
        if not sem_results:
            return fts_results[:limit]
        if not fts_results:
            return sem_results[:limit]

        # RRF fusion: score[id] = Σ 1/(k + rank_in_retriever_i)
        # Rank is 1-indexed to match the standard formulation.
        scores: dict[str, float] = {}
        rows_by_id: dict[str, SearchResult] = {}

        for rank, r in enumerate(fts_results, start=1):
            scores[r.id] = scores.get(r.id, 0.0) + 1.0 / (rrf_k + rank)
            rows_by_id[r.id] = r

        for rank, r in enumerate(sem_results, start=1):
            scores[r.id] = scores.get(r.id, 0.0) + 1.0 / (rrf_k + rank)
            rows_by_id.setdefault(r.id, r)

        # Sort by fused score, return top N with the fused score on .rank
        ranked_ids = sorted(scores.keys(), key=lambda i: scores[i], reverse=True)
        merged: list[SearchResult] = []
        for nid in ranked_ids[:limit]:
            r = rows_by_id[nid]
            r.rank = scores[nid]
            merged.append(r)
        return merged

    def render_graph_text(self, note_id: str, depth: int = 2) -> str:
        """Render a text representation of the local graph."""
        center = self.get_note_by_id(note_id)
        if not center:
            return f"Note {note_id} not found."

        lines = [f"[{center['type']}] {center['title']} ({note_id})"]
        nodes = self.get_related(note_id, depth=depth)

        for node in nodes:
            for edge in node.edges:
                if edge.source == note_id:
                    lines.append(f"  --{edge.edge_type}--> [{node.type}] {node.title}")
                elif edge.target == note_id:
                    lines.append(f"  <--{edge.edge_type}-- [{node.type}] {node.title}")
                else:
                    lines.append(f"  ~{edge.edge_type}~ [{node.type}] {node.title}")

        return "\n".join(lines)

    def render_graph_mermaid(self, note_id: str, depth: int = 2) -> str:
        """Render a Mermaid diagram of the local graph."""
        center = self.get_note_by_id(note_id)
        if not center:
            return f"Note {note_id} not found."

        lines = ["graph LR"]
        safe = lambda s: s.replace('"', "'")
        lines.append(f'  {note_id}["{safe(center["title"])}"]')

        nodes = self.get_related(note_id, depth=depth)
        seen_edges: set[tuple] = set()

        for node in nodes:
            lines.append(f'  {node.id}["{safe(node.title)}"]')
            for edge in node.edges:
                key = (edge.source, edge.target, edge.edge_type)
                if key not in seen_edges:
                    seen_edges.add(key)
                    lines.append(f"  {edge.source} -->|{edge.edge_type}| {edge.target}")

        return "\n".join(lines)
