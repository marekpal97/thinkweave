"""The package's single seam into the derived SQLite index.

Stdlib only, strictly read-only, never imports ``thinkweave`` (the rail may
run where the package is not installed). Every SQL statement devloop issues
lives here — ``trajectory.prime`` composes over the rows this module returns
and never speaks sqlite (the importer-allowlist test in
tests/test_devloop_boundaries.py enforces the singleton).

Retrieval shape (#100): trajectory candidates come from two independent
retrievers — concept match and full-text match over the issue's own words —
fused by reciprocal rank fusion. One leg alone was dead by construction: the
write side tags notes with ontology concepts while the read side was handed
GitHub labels, so the concept join matched nothing on the live index.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tomllib
from pathlib import Path

# The seam's error type, re-exported so callers can degrade on an index
# problem without importing sqlite3 themselves (see the importer-allowlist
# test in tests/test_devloop_boundaries.py).
Error = sqlite3.Error
# Aliases so no caller ever imports sqlite3 itself (cli's degrade guard now,
# prime's annotations post-#100) — keeps the importer-allowlist seam tight.
Connection = sqlite3.Connection


def open_ro(db_path: str) -> sqlite3.Connection:
    """Open the derived index strictly read-only (never mutate derived state)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _read_weave_dir_override(vault_root: Path) -> Path | None:
    """Honor a top-level ``weave_dir`` in the vault's config.toml.

    PR #10 relocates derived state (index.db, embeddings.db, buffer/) off the
    vault path — on 9P-mounted vaults the live index is ``<weave_dir>/index.db``,
    NOT ``<vault>/.weave/index.db``. Mirror ``core.config``'s resolution: ``~``
    expands, a relative value anchors at ``vault_root``, absolute passes
    through. Read ``config/config.toml`` first, then the legacy
    ``.weave/config.toml``. Malformed/unreadable config or an absent key →
    ``None`` (fall back to the legacy layout; never crash).
    """
    for rel in ("config/config.toml", ".weave/config.toml"):
        path = vault_root / rel
        if not path.exists():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        value = data.get("weave_dir")
        if isinstance(value, str) and value.strip():
            resolved = Path(value).expanduser()
            return resolved if resolved.is_absolute() else vault_root / resolved
    return None


# ---------------------------------------------------------------------------
# Trajectory retrieval — two legs, fused

# The retrieval doctrine's fusion constant (the main package's config knob is
# `retrieval.rrf_k`, same default). The rail reads no vault config, so it is a
# constant here rather than a knob nobody would turn.
RRF_K = 60

# The candidate columns prime composes over. `frontmatter` carries builds_on /
# outcome / issue; the note body itself is never served — only its insights are.
_CANDIDATE_COLS = "n.id, n.title, n.date, n.frontmatter"

# fts5 tokenizes on `-` and `_` as word chars (indexer.py's tokenchars); mirror
# that so a term the index holds is a term we can ask for.
_FTS_TERM = re.compile(r"[0-9A-Za-z_-]{3,}")


def _fts_match_expr(text: str) -> str:
    """Encode free text (an issue title/body) as an OR-of-terms fts5 MATCH.

    The query is user-shaped, so nothing but indexable terms survives: keeping
    only tokenizer-legal runs of 3+ chars makes fts5 operator soup (``*``,
    ``NEAR``, unbalanced quotes) structurally unreachable rather than caught
    after the fact. Each term is quoted so a leading ``-`` stays a literal.
    Terms dedupe (order-preserving) and cap at 24 — a long issue body must not
    become an unbounded query. No terms → ``''``, the caller's signal to skip
    the leg entirely (``MATCH ''`` is itself a syntax error).

    ponytail: no stopword filter, so common issue-prose words ("the", "with")
    are real OR terms and the leg can touch most of the index — ~100ms at 6k
    notes, paid once at claim time. Upgrade path when it stops being cheap:
    drop terms whose document frequency exceeds a threshold.
    """
    terms = list(dict.fromkeys(_FTS_TERM.findall(text)))[:24]
    return " OR ".join(f'"{t}"' for t in terms)


def _by_concepts(conn: sqlite3.Connection, concepts: list[str], scan_cap: int) -> list[dict]:
    """Leg 1: ``[loop-run]`` notes carrying ANY of the concepts, recency first."""
    placeholders = ",".join("?" * len(concepts))
    return [dict(r) for r in conn.execute(
        f"""SELECT DISTINCT {_CANDIDATE_COLS}
            FROM notes n
            JOIN note_tags t ON t.note_id = n.id AND t.tag = 'loop-run'
            JOIN note_concepts c ON c.note_id = n.id
            WHERE c.concept IN ({placeholders})
            ORDER BY n.date DESC, n.id DESC
            LIMIT ?""",
        [*concepts, scan_cap],
    )]


def _by_fts(conn: sqlite3.Connection, match: str, scan_cap: int) -> list[dict]:
    """Leg 2: ``[loop-run]`` notes matching the text, fts5 relevance order."""
    return [dict(r) for r in conn.execute(
        f"""SELECT {_CANDIDATE_COLS}
            FROM notes_fts f
            JOIN notes n ON n.rowid = f.rowid
            WHERE notes_fts MATCH ?
              AND EXISTS (SELECT 1 FROM note_tags t
                          WHERE t.note_id = n.id AND t.tag = 'loop-run')
            ORDER BY f.rank
            LIMIT ?""",
        [match, scan_cap],
    )]


def _rrf(rankings: list[list[dict]]) -> list[dict]:
    """Reciprocal rank fusion: ``score[id] = Σ 1/(RRF_K + rank_i)``, 1-indexed.

    Ties keep first-seen order (dict insertion + a stable sort), so a single
    ranking fuses to itself byte-for-byte — concept-only retrieval is unchanged
    from before the FTS leg existed.
    """
    scores: dict[str, float] = {}
    rows: dict[str, dict] = {}
    for ranking in rankings:
        for rank, row in enumerate(ranking, start=1):
            scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (RRF_K + rank)
            rows.setdefault(row["id"], row)
    return sorted(rows.values(), key=lambda r: -scores[r["id"]])


def trajectory_candidates(
    conn: sqlite3.Connection, concepts: list[str], query: str = "",
    scan_cap: int = 40,
) -> list[dict]:
    """Read-only: ``[loop-run]`` note rows for the concept and text legs, fused.

    Returns ``[{id, title, date, frontmatter}]`` in fused rank order, at most
    ``scan_cap`` per leg. Empty ``concepts`` degrades to FTS-only, empty
    ``query`` to concept-only, both empty to ``[]``. The FTS leg is best-effort
    only while the other leg is carrying: a broken ``notes_fts`` with nothing
    else retrieved raises to the caller's degrade-to-unprimed guard.
    """
    rankings = []
    if concepts:
        rankings.append(_by_concepts(conn, concepts, scan_cap))
    match = _fts_match_expr(query)
    if match:
        try:
            rankings.append(_by_fts(conn, match, scan_cap))
        except sqlite3.Error:
            if not any(rankings):
                raise
    return _rrf(rankings)


def note_bodies(conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    """Read-only: ``{id: body_text}`` for the given ids that are ``type='note'``.

    The type guard is load-bearing: a ``builds_on`` list may name a decision or
    session id, and prime serves only insight-note bodies as color. Ids that
    don't resolve are simply absent from the mapping.
    """
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, body_text FROM notes WHERE type = 'note' AND id IN ({placeholders})",
        ids,
    ).fetchall()
    return {r["id"]: (r["body_text"] or "").strip() for r in rows}


def resolve_db_path(db: str | None, vault: str | None) -> str | None:
    """Resolve the read-only index db path without importing thinkweave.

    ``--db`` wins; else derive from ``--vault``: ``<weave_dir>/index.db`` when
    the vault's config.toml overrides ``weave_dir`` (PR #10), otherwise the
    legacy ``<vault>/.weave/index.db``; else ``THINKWEAVE_INDEX_DB``. Returns
    None when nothing resolves — the prime then serves an empty (unprimed)
    block rather than guessing a path (never touch an ambient real vault).
    """
    if db:
        return db
    if vault:
        vault_root = Path(vault)
        weave_dir = _read_weave_dir_override(vault_root) or (vault_root / ".weave")
        return str(weave_dir / "index.db")
    return os.environ.get("THINKWEAVE_INDEX_DB") or None
