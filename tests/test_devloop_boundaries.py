"""Enforcing seams for the devloop/ boundary spec (docs/agents/devloop-boundaries.md).

Three contracts the spec states in prose and this file makes falsifiable:

1. **Schema pin** — the derived-index tables/columns devloop reads are built
   here by the REAL thinkweave indexer, so indexer schema drift fails a test
   instead of silently degrading prime to unprimed forever. Both sides of the
   seam appear in one test; the hand-built-schema tests in test_issue_loop.py
   stay as fast unit checks.
2. **Importer allowlist** — which devloop modules may import ``sqlite3``:
   ``{index_client, trajectory.prime}`` after #94, tightened to
   ``{index_client}`` by #100, so the transition is enforced not remembered.
3. **Registry disjointness** — the Gate protocol's structural claim: every kind
   has exactly one verb, and the two registries together cover loop.toml.
"""

from __future__ import annotations

import re
from pathlib import Path

import devloop
from devloop import cli, gates, index_client
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
    tv.vault.create_note(
        NoteType.NOTE,
        "loop trajectory #94",
        body="## What\nSplit the rail.\n\n## Lessons\nSlice, don't retype.\n",
        tags=["loop-run"],
        extra_frontmatter={"concepts": ["devloop-schema-pin"], "issue": 94,
                           "outcome": "shipped"},
    )
    tv.vault.create_note(NoteType.NOTE, "portable lesson",
                         body="Insight bodies are served by builds_on.")
    tv.indexed()

    db_path = index_client.resolve_db_path(None, str(tv.dir))
    assert db_path == str(tv.config.index_db)

    conn = index_client.open_ro(db_path)
    try:
        hits = prime.query_trajectories(conn, ["devloop-schema-pin"], 3)
        assert [h["title"] for h in hits] == ["loop trajectory #94"]
        assert hits[0]["lessons"] == "Slice, don't retype."
        assert hits[0]["issue"] == 94 and hits[0]["outcome"] == "shipped"
        # resolve_insights' own SQL (the type='note' guard + body_text) over the
        # same real schema.
        insight_id = conn.execute(
            "SELECT id FROM notes WHERE title = 'portable lesson'"
        ).fetchone()["id"]
        resolved = prime.resolve_insights(conn, [insight_id])
        assert [r["id"] for r in resolved] == [insight_id]
        assert "served by builds_on" in resolved[0]["body"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. Importer allowlist — the sqlite3 blast radius, enforced

_SQLITE_IMPORT = re.compile(r"^\s*(?:import sqlite3|from sqlite3\b)", re.MULTILINE)


def test_only_the_index_seam_and_prime_import_sqlite3():
    pkg_root = Path(devloop.__file__).resolve().parent
    importers = {
        str(py.relative_to(pkg_root).with_suffix(""))
        for py in pkg_root.rglob("*.py")
        if _SQLITE_IMPORT.search(py.read_text(encoding="utf-8"))
    }
    # #100 moves prime's SQL into index_client and tightens this to a singleton.
    assert importers == {"index_client", "trajectory/prime"}


# ---------------------------------------------------------------------------
# 3. Registry disjointness — one verb per kind, and the pair covers loop.toml


def test_gate_registries_are_disjoint():
    """A kind is executed by the rail or validated by it, never both."""
    assert set(gates.DETERMINISTIC) & gates.JUDGMENT == set()


def test_gate_registries_cover_every_configured_kind():
    kinds = {g["kind"] for g in cli.load_config()["gates"]}
    assert kinds  # the repo's loop.toml actually ships gates
    assert kinds <= set(gates.DETERMINISTIC) | gates.JUDGMENT
