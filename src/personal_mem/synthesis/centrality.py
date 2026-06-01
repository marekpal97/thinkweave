"""C19b — Per-concept-induced-subgraph PageRank.

Today retrieval answers "how well does this note match your query?"
(FTS keyword density / embedding cosine similarity). PageRank
answers a different question: "**how central is this note in the
vault?**" — query-independent importance from the edge graph. The
concept-hub-on-X outranks an offhand mention of X because dozens of
other notes link to the hub.

Per-concept-induced-subgraph (not global): for each concept C, we
PageRank only notes mentioning C. The global graph collapses to
landing docs (they're linked from everything); a per-concept subgraph
surfaces the actual canonical note ON C.

Runs during the dream apply phase (post `indexer.rebuild`), gated by
``cfg.dream_compute_pagerank``. Scores live in the ``graph_ranks``
table, keyed by ``rank_type = f"pagerank:{concept}"`` so multiple
ranking schemes can co-exist.

Pure Python power iteration — vault-scale per-concept subgraphs are
small (~50–500 nodes), so 25 iterations of O(N²) is well under a
second. Adding numpy would be a bigger dep change than the cost
saves.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def compute_concept_pagerank(
    db: Any,
    concept: str,
    damping: float = 0.85,
    max_iter: int = 50,
    tol: float = 1e-6,
    max_nodes: int = 500,
) -> dict[str, float]:
    """Compute PageRank over the subgraph of notes mentioning ``concept``.

    Args:
        db: open SQLite connection (must have ``row_factory = Row``).
        concept: canonical concept slug.
        damping: random-walk damping factor (PageRank default 0.85).
        max_iter: power-iteration ceiling.
        tol: L1 convergence threshold.
        max_nodes: skip subgraphs larger than this — likely too broad
            (e.g. ``llm``, ``training``) for a per-concept hub query.

    Returns:
        ``{note_id: score}``. Empty when subgraph is too small (<2
        nodes), too large (>``max_nodes``), or has no internal edges.

    The walk is undirected (``relates_to`` is symmetric; structural
    edges contribute weight 1.0 in either direction). Weights come from
    the ``edges.weight`` column (C19a). Dangling-node columns (no
    out-edges within the subgraph) are replaced with the uniform
    teleport vector — keeps the matrix stochastic and the iteration
    convergent.
    """
    note_ids = [
        r["note_id"]
        for r in db.execute(
            "SELECT note_id FROM note_concepts WHERE concept = ?", (concept,)
        )
    ]
    n = len(note_ids)
    if n < 2 or n > max_nodes:
        return {}

    id_to_idx = {nid: i for i, nid in enumerate(note_ids)}

    # Build weighted adjacency. Undirected: add both source→target and
    # target→source so the walk can step either way along an edge.
    placeholders = ",".join("?" * n)
    mat: list[list[float]] = [[0.0] * n for _ in range(n)]
    for row in db.execute(
        f"SELECT source, target, weight FROM edges "
        f"WHERE source IN ({placeholders}) AND target IN ({placeholders})",
        list(note_ids) + list(note_ids),
    ):
        s = row["source"]
        t = row["target"]
        if s == t:
            continue
        i = id_to_idx.get(s)
        j = id_to_idx.get(t)
        if i is None or j is None:
            continue
        w = float(row["weight"] or 1.0)
        mat[i][j] += w
        mat[j][i] += w

    # Normalize columns (PageRank's M is column-stochastic).
    # Replace zero-column dangling nodes with the uniform teleport
    # column so the iteration converges.
    uniform = 1.0 / n
    for j in range(n):
        col_sum = sum(mat[i][j] for i in range(n))
        if col_sum == 0:
            for i in range(n):
                mat[i][j] = uniform
        else:
            for i in range(n):
                mat[i][j] /= col_sum

    # Early-out if no edges exist at all — every column is uniform and
    # the walk collapses to the teleport vector. Returning {} signals
    # callers to skip storage for this concept.
    edge_rows = db.execute(
        f"SELECT COUNT(*) AS c FROM edges "
        f"WHERE source IN ({placeholders}) AND target IN ({placeholders}) "
        f"AND source != target",
        list(note_ids) + list(note_ids),
    ).fetchone()
    if not edge_rows or edge_rows["c"] == 0:
        return {}

    v = [uniform] * n
    teleport = uniform * (1.0 - damping)
    for _ in range(max_iter):
        v_new = [
            damping * sum(mat[i][j] * v[j] for j in range(n)) + teleport
            for i in range(n)
        ]
        delta = sum(abs(v_new[i] - v[i]) for i in range(n))
        v = v_new
        if delta < tol:
            break

    return {note_ids[i]: v[i] for i in range(n)}


def store_concept_pagerank(
    db: Any, concept: str, scores: dict[str, float]
) -> int:
    """Persist scores to ``graph_ranks``. Returns rows inserted.

    Clears prior scores for ``rank_type = f"pagerank:{concept}"`` first
    so re-runs replace rather than accumulate. ``computed_at`` is the
    current UTC ISO timestamp.
    """
    if not scores:
        return 0
    rank_type = f"pagerank:{concept}"
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "DELETE FROM graph_ranks WHERE rank_type = ?", (rank_type,)
    )
    db.executemany(
        "INSERT INTO graph_ranks "
        "(note_id, rank_type, score, computed_at) "
        "VALUES (?, ?, ?, ?)",
        [(nid, rank_type, score, now) for nid, score in scores.items()],
    )
    db.commit()
    return len(scores)


def compute_all_concept_pageranks(
    db: Any, *, min_notes: int = 2, max_nodes: int = 500
) -> dict[str, int]:
    """Compute + persist PageRank for every concept with ≥ ``min_notes``
    occurrences in the index.

    Returns ``{concept: nodes_scored}`` for diagnostic logging. Concepts
    that don't pass the inner thresholds (no internal edges, subgraph
    too small/large) are silently skipped.
    """
    out: dict[str, int] = {}
    rows = db.execute(
        "SELECT concept, COUNT(*) AS n FROM note_concepts "
        "GROUP BY concept HAVING n >= ?",
        (min_notes,),
    ).fetchall()
    for row in rows:
        concept = row["concept"]
        scores = compute_concept_pagerank(db, concept, max_nodes=max_nodes)
        if scores:
            count = store_concept_pagerank(db, concept, scores)
            if count:
                out[concept] = count
    return out


def canonical_for(
    db: Any, concept: str, limit: int = 5
) -> list[dict[str, Any]]:
    """Return top-PageRank notes for ``concept``'s subgraph.

    Each entry: ``{id, title, type, score}``. Empty when no PageRank
    has been computed for this concept (e.g. concept too broad, no
    internal edges, or dream cycle hasn't run with the gate enabled).
    """
    rank_type = f"pagerank:{concept}"
    rows = db.execute(
        "SELECT gr.note_id, gr.score, n.title, n.type FROM graph_ranks gr "
        "JOIN notes n ON n.id = gr.note_id "
        "WHERE gr.rank_type = ? "
        "ORDER BY gr.score DESC, n.title ASC LIMIT ?",
        (rank_type, limit),
    ).fetchall()
    return [
        {
            "id": r["note_id"],
            "title": r["title"],
            "type": r["type"],
            "score": float(r["score"]),
        }
        for r in rows
    ]
