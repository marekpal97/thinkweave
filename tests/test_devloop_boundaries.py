"""Enforcing seams for the devloop/ boundary spec (docs/agents/devloop-boundaries.md).

Two contracts the spec states in prose and this file makes falsifiable:

1. **Schema pin** — the derived-index tables/columns devloop reads are built
   here by the REAL thinkweave indexer, so indexer schema drift fails a test
   instead of silently degrading prime to unprimed forever. Both sides of the
   seam appear in one test; the hand-built-schema tests in test_issue_loop.py
   stay as fast unit checks.
2. **Importer allowlist** — which devloop modules may import ``sqlite3``:
   ``{index_client}`` since #100 moved prime's SQL into the seam, so the
   singleton is enforced not remembered.
"""

from __future__ import annotations

import ast
from pathlib import Path

import devloop
from devloop import index_client
from devloop.trajectory import prime
from thinkweave.core.schemas import NoteType

# ---------------------------------------------------------------------------
# 1. Schema pin — devloop's SQL against an index built by the real indexer


def test_index_schema_pin_against_the_real_indexer(vault_factory):
    """resolve_db_path → open_ro → the trajectory queries, end to end over an
    index the thinkweave indexer produced. Pins the tables (notes, note_tags,
    note_concepts) and the columns devloop reads (id, type, title, date,
    frontmatter, body_text / note_id, tag / note_id, concept)."""
    tv = vault_factory()
    insight_path = tv.vault.create_note(NoteType.NOTE, "portable lesson",
                                        body="Insight bodies are served by builds_on.")
    insight_id = tv.vault.read_note(insight_path).id
    tv.vault.create_note(
        NoteType.NOTE,
        "loop trajectory #94",
        body="## What\nSplit the rail.\n\n## How it went\nOne fix round.\n",
        tags=["loop-run"],
        extra_frontmatter={"concepts": ["devloop-schema-pin"], "issue": 94,
                           "outcome": "shipped", "builds_on": [insight_id]},
    )
    tv.indexed()

    db_path = index_client.resolve_db_path(None, str(tv.dir))
    assert db_path == str(tv.config.index_db)

    conn = index_client.open_ro(db_path)
    try:
        hits = prime.query_trajectories(conn, ["devloop-schema-pin"], 3)
        assert [h["title"] for h in hits] == ["loop trajectory #94"]
        assert hits[0]["issue"] == 94 and hits[0]["outcome"] == "shipped"
        # The builds_on link resolved through the real schema to the insight body.
        assert [i["id"] for i in hits[0]["insights"]] == [insight_id]
        assert "served by builds_on" in hits[0]["insights"][0]["body"]
        # resolve_insights' own SQL (the type='note' guard + body_text) over the
        # same real schema.
        resolved = prime.resolve_insights(conn, [insight_id])
        assert [r["id"] for r in resolved] == [insight_id]
        assert "served by builds_on" in resolved[0]["body"]
    finally:
        conn.close()


def test_fts_query_serves_when_the_concepts_are_dead_vocabulary(vault_factory):
    """#100's dead-join fix, pinned against an index the real indexer built.

    The diagnosed failure (owner, 2026-08-01): the write side speaks ontology,
    the read side was handed GitHub labels, so ``query_trajectories([labels])``
    matched 0 of 20 live trajectory notes. Prime v3 fuses an FTS leg over
    ``notes_fts``, so the issue's own text finds the trajectory the dead
    concepts miss — this also pins the fts5 table + the ``rank`` ordering the
    fusion leg reads.
    """
    tv = vault_factory()
    insight_path = tv.vault.create_note(NoteType.NOTE, "portable lesson",
                                        body="Fuse the FTS leg with concepts.")
    insight_id = tv.vault.read_note(insight_path).id
    traj_path = tv.vault.create_note(
        NoteType.NOTE,
        "loop trajectory #100",
        body="## What\nPrime v3 fuses FTS with concept match.\n",
        tags=["loop-run"],
        extra_frontmatter={"concepts": ["devloop-schema-pin"], "issue": 100,
                           "outcome": "shipped", "builds_on": [insight_id]},
    )
    traj_id = tv.vault.read_note(traj_path).id
    tv.indexed()

    conn = index_client.open_ro(index_client.resolve_db_path(None, str(tv.dir)))
    try:
        labels = ["enhancement", "ready-for-agent", "track:E-devloop"]
        # The dead join: GH labels as concepts match nothing (they were never
        # written as concepts — the strict gate shunts them to proposed).
        assert prime.query_trajectories(conn, labels, 3) == []
        # Same dead concepts + the issue's text as the FTS query → serves.
        hits = prime.query_trajectories(
            conn, labels, 3, query="W4a: prime v3 — fuse FTS with concept match")
        assert [h["id"] for h in hits] == [traj_id]
        assert [i["id"] for i in hits[0]["insights"]] == [insight_id]
        assert "Fuse the FTS leg with concepts." in hits[0]["insights"][0]["body"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. Importer allowlist — the sqlite3 blast radius, enforced

def _imports_sqlite3(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            a.name.split(".")[0] == "sqlite3" for a in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "sqlite3":
            return True
    return False


def test_index_client_is_the_only_sqlite3_importer():
    pkg_root = Path(devloop.__file__).resolve().parent
    importers = set()
    for py in pkg_root.rglob("*.py"):
        if _imports_sqlite3(ast.parse(py.read_text(encoding="utf-8"))):
            rel = py.relative_to(pkg_root.parent).with_suffix("")
            importers.add(".".join(p for p in rel.parts if p != "__init__"))
    # #100 moved prime's SQL into index_client — the seam is a singleton.
    assert importers == {"devloop.index_client"}
