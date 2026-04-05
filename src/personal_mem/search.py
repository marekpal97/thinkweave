"""Search and graph traversal over the SQLite index.

Provides FTS5 full-text search and recursive CTE graph traversal.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from personal_mem.config import Config, load_config


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
        note_type: str = "",
        project: str = "",
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        """Full-text search with optional filters."""
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

        if note_type:
            conditions.append("n.type = ?" if query else "type = ?")
            params.append(note_type)
        if project:
            conditions.append("n.project = ?" if query else "project = ?")
            params.append(project)

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
    ) -> list[GraphNode]:
        """Graph traversal from a note using recursive CTE."""
        edge_filter = ""
        params: list = [note_id, depth]
        if edge_types:
            placeholders = ",".join("?" for _ in edge_types)
            edge_filter = f"AND e.edge_type IN ({placeholders})"
            params = [note_id] + edge_types + [depth]

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
            WHERE n.id != ?
        """
        params.append(note_id)

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
        limit: int = 5,
    ) -> list[SearchResult]:
        """Get the most relevant notes for a given context.

        Progressive recall: tries FTS first, falls back to recent notes.
        """
        results = []

        # Try FTS search if query provided
        if query:
            results = self.search(query, project=project, tags=tags, limit=limit)

        # If not enough results, supplement with recent notes
        if len(results) < limit:
            remaining = limit - len(results)
            seen_ids = {r.id for r in results}

            conditions = []
            params: list = []
            if project:
                conditions.append("project = ?")
                params.append(project)

            where = " AND ".join(conditions) if conditions else "1=1"
            sql = f"""
                SELECT id, type, title, path, project, date, tags
                FROM notes WHERE {where}
                ORDER BY date DESC
                LIMIT ?
            """
            params.append(remaining + len(seen_ids))  # overfetch to account for dedup

            for row in self.db.execute(sql, params):
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
                if len(results) >= limit:
                    break

        return results

    def get_note_by_id(self, note_id: str) -> dict | None:
        """Get a single note by ID."""
        row = self.db.execute(
            "SELECT * FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        if row:
            return dict(row)
        return None

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
