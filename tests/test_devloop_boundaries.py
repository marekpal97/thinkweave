"""The cross-repo seam: thinkweave as a *consumer* of the devloop package.

devloop lives in funloops now (``packages/devloop``, issue #151); thinkweave
pins it as a dev-dependency and keeps only the contracts a host owns. The
boundary spec itself moved with the code — read it at
``packages/devloop/docs/agents/devloop-boundaries.md`` in funloops.

Two contracts, both of which only thinkweave can enforce:

1. **Schema pin** — the derived-index tables/columns devloop reads are built
   here by the REAL thinkweave indexer, so indexer schema drift fails a test
   instead of silently degrading prime to unprimed forever. funloops cannot
   run this: it has no thinkweave to import. This is the seam that must go red
   when *either* side moves.
2. **Consumption** — devloop is resolved from the installed package (never a
   directory in this tree), and the ``scripts/issue_loop.py`` shim's resolved
   config is byte-identical to what it printed before the carve.

Not here any more: the sqlite3 importer allowlist and the boundary-doc pins.
Those police devloop's own internals, which is funloops' CI's job now
(``packages/devloop/tests/test_devloop_boundaries.py``).

Pin-update dance — what to do when this file goes red
-----------------------------------------------------
The pin is ``[tool.uv.sources] devloop.rev`` in ``pyproject.toml`` (resolved
into ``uv.lock``). A red schema-pin test means thinkweave's indexer and the
pinned devloop's SQL disagree. Two legitimate fixes, in preference order:

1. **The indexer changed and devloop should follow** — file the fix against
   funloops, land it there, then bump ``rev`` here (``uv lock --upgrade-package
   devloop``) in the SAME PR as the indexer change. Never merge the indexer
   change with this test skipped or the seam is decorative.
2. **The indexer changed and devloop should NOT follow** — the columns devloop
   reads still exist, only the fixture drifted: fix the fixture here, leave the
   pin alone.

Never "fix" a red by loosening the assertions: the failure mode this seam
exists to catch (prime silently degrading to unprimed forever) is invisible in
production, so the test is the only alarm. ``build_prime_payload`` swallows
``sqlite3.Error`` by design.

One known fragility, stated so it is not mistaken for real drift: the pin
drives ``prime.query_trajectories`` / ``prime.resolve_insights``, which are
module-level but NOT in ``devloop.trajectory.__all__``. A pure rename in
funloops reds this test without any schema having moved. That is a false
positive on rule 1 — re-point the import, do not bump the pin.

Regenerating the golden: an *intentional* edit to ``docs/agents/loop.toml``
changes the resolved config, so re-capture it deliberately —
``python scripts/issue_loop.py config > tests/devloop_golden_config.json`` —
and say why in the commit. An *unintentional* change is what it catches.
"""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path

from devloop import index_client
from devloop.trajectory import prime

from thinkweave.core.schemas import NoteType

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIM = REPO_ROOT / "scripts" / "issue_loop.py"
GOLDEN = REPO_ROOT / "tests" / "devloop_golden_config.json"

# The full subcommand surface. The issue says "8"; `validate` (#99) made it 9 —
# pinned by enumeration so a silently dropped verb fails rather than shrinks.
SUBCOMMANDS = ["plan", "claim", "release", "config", "check", "validate",
               "prime", "triage", "trajectory"]

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
# 2. Consumption — devloop arrives as a dependency, and the shim survives it


def test_devloop_is_an_installed_package_not_a_directory_in_this_tree():
    """The carve, made falsifiable. A re-vendored ``devloop/`` at the repo root
    would shadow the pinned package on ``sys.path`` (pytest puts the rootdir
    first) and every other test here would keep passing against the copy —
    which is exactly how the pin would rot unnoticed.

    ``direct_url.json`` (PEP 610) is where the *provenance* survives install,
    so it — not a path prefix — is what says "this came from funloops". It
    holds for both supported modes: the committed git pin and the documented
    editable dev override, which differ in scheme but not in origin. (A path
    check cannot work here: ``.venv/`` lives inside the repo root.)
    """
    assert not (REPO_ROOT / "devloop").exists(), "devloop/ is funloops' now — do not re-vendor"
    origin = json.loads(distribution("devloop").read_text("direct_url.json") or "{}")
    assert "funloops" in origin.get("url", ""), origin


def test_shim_config_is_byte_identical_to_the_pre_carve_capture():
    """AC2. ``tests/devloop_golden_config.json`` is the independent oracle: it
    is the literal stdout of this command at thinkweave@de35d9c, captured
    before the package could resolve from anywhere but the tree.

    Byte-identical, not shape-equal: nothing about *thinkweave's* gate pipeline
    was supposed to change, so any diff is a regression. (funloops keeps the
    same bytes as its own carve-out golden but compares only the shape — it
    legitimately re-configured itself for funloops.)

    Runs the shim in a subprocess from the repo root, so what is under test is
    the real invocation crons and muscle memory use — including devloop's
    cwd-upward ``find_config`` walk landing on thinkweave's ``loop.toml``.
    """
    out = subprocess.run([sys.executable, str(SHIM), "config"], cwd=REPO_ROOT,
                         capture_output=True, text=True, check=True)
    assert out.stdout == GOLDEN.read_text(encoding="utf-8")


def test_every_subcommand_dispatches_through_the_shim():
    """AC2's other half: the shim reaches the whole parser, not just `config`.
    ``--help`` exits 0 only if the subparser resolved."""
    for name in SUBCOMMANDS:
        r = subprocess.run([sys.executable, str(SHIM), name, "--help"], cwd=REPO_ROOT,
                           capture_output=True, text=True, check=False)
        assert r.returncode == 0, f"{name}: rc={r.returncode} {r.stderr}"


def test_python_m_devloop_reaches_the_same_rail():
    """The packaged entry point works at thinkweave's repo root. Pre-carve this
    could not work: the in-tree ``devloop/`` shadowed the package and had no
    ``__main__``. Byte-equal to the golden (the shim is pinned to it above) —
    one rail, two front doors."""
    mod = subprocess.run([sys.executable, "-m", "devloop", "config"], cwd=REPO_ROOT,
                         capture_output=True, text=True, check=True)
    assert mod.stdout == GOLDEN.read_text(encoding="utf-8")
