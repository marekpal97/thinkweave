"""Ontology geometry — embedding-space helpers for dedup hygiene.

The hygiene rails (dream drift v2, `/mem-resolve-concepts`,
`/themes-resolve`) detect near-duplicate *concepts* and *themes* by
where their content lives in embedding space, not just by string shape:

- a **concept centroid** is the mean of the cached embeddings of the
  notes carrying that concept (``note_concepts`` ⨝ ``.mem/embeddings.db``);
  concepts with too few embedded notes fall back to embedding the term
  string itself (one small provider batch, soft-fail);
- a **theme vector** is the theme note's own cached embedding — themes
  are ordinary notes to ``EmbeddingSearch.compute_all`` so no extra
  embedding work exists here;
- **cosine pairs** above a threshold are surfaced to the model with an
  evidence packet (domains, counts, co-occurrence, sample titles) so the
  merge worker judges from contents without extra ``mem_read`` round-trips.

Judgment memory lives in the existing dream maintenance log
(``vault/.mem/maintenance.jsonl``) — :func:`judged_pairs` reads the
per-cycle ``verdicts`` block back so already-judged pairs (merged or
ruled distinct) stop re-surfacing. No separate ledger file, by design:
dream's audit trail is the single place ontology mutations are recorded.

Everything here is read-only over the two SQLite caches; pure stdlib
(``math.sumprod`` when available) — no numpy, per the lightweight
dependency doctrine.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from typing import Any

from personal_mem.core.config import Config

log = logging.getLogger(__name__)

#: Minimum embedded notes for a stable usage centroid; below this the
#: term string itself is embedded instead (a 2-note centroid says more
#: about those two notes than about the concept).
MIN_NOTES_FOR_CENTROID = 3

#: Default surface threshold — pairs at or above go to the model.
DEFAULT_COSINE_THRESHOLD = 0.8


def _dot(a: list[float], b: list[float]) -> float:
    sumprod = getattr(math, "sumprod", None)
    if sumprod is not None:
        return sumprod(a, b)
    return sum(x * y for x, y in zip(a, b))


def _normalize(vec: list[float]) -> list[float] | None:
    norm = math.sqrt(_dot(vec, vec))
    if norm == 0:
        return None
    return [x / norm for x in vec]


def _load_note_vectors(cfg: Config) -> dict[str, list[float]]:
    """note_id → embedding from the ``.mem/embeddings.db`` cache.

    Returns ``{}`` when the cache is missing — callers degrade to
    string-only detection rather than failing the scan.
    """
    from personal_mem.core.embeddings import _unpack_embedding

    if not cfg.embeddings_db.exists():
        return {}
    db = sqlite3.connect(str(cfg.embeddings_db))
    db.row_factory = sqlite3.Row
    try:
        return {
            row["note_id"]: _unpack_embedding(row["embedding"])
            for row in db.execute("SELECT note_id, embedding FROM embeddings")
        }
    finally:
        db.close()


def _concept_note_ids(cfg: Config) -> dict[str, list[str]]:
    """concept → [note_id] from the main index. ``{}`` if no index."""
    if not cfg.index_db.exists():
        return {}
    db = sqlite3.connect(str(cfg.index_db))
    db.row_factory = sqlite3.Row
    try:
        out: dict[str, list[str]] = {}
        for row in db.execute("SELECT concept, note_id FROM note_concepts"):
            out.setdefault(row["concept"].lower(), []).append(row["note_id"])
        return out
    finally:
        db.close()


def concept_centroids(
    cfg: Config,
    *,
    embed_fallback: bool = True,
    min_notes: int = MIN_NOTES_FOR_CENTROID,
) -> dict[str, list[float]]:
    """concept → unit-norm centroid of its notes' cached embeddings.

    Concepts with fewer than ``min_notes`` embedded notes are batched
    through the embedding provider as bare term strings (when
    ``embed_fallback`` and a provider is reachable; otherwise they are
    silently absent from the result — string-rule detection still covers
    them). Returned vectors are L2-normalized so downstream cosine is a
    plain dot product.
    """
    note_vecs = _load_note_vectors(cfg)
    by_concept = _concept_note_ids(cfg)
    if not by_concept:
        return {}

    out: dict[str, list[float]] = {}
    sparse: list[str] = []
    for concept, ids in by_concept.items():
        vecs = [note_vecs[i] for i in ids if i in note_vecs]
        if len(vecs) < min_notes:
            sparse.append(concept)
            continue
        dim = len(vecs[0])
        mean = [0.0] * dim
        for v in vecs:
            if len(v) != dim:  # mixed-model cache rows — skip mismatches
                continue
            for j, x in enumerate(v):
                mean[j] += x
        mean = [x / len(vecs) for x in mean]
        unit = _normalize(mean)
        if unit is not None:
            out[concept] = unit

    if sparse and embed_fallback:
        try:
            from personal_mem.core.embeddings import EmbeddingSearch

            es = EmbeddingSearch(config=cfg)
            try:
                # Embed the bare terms (dashes → spaces reads better to
                # the model: "write ahead log" not "write-ahead-log").
                texts = [c.replace("-", " ") for c in sparse]
                vecs = es._call_api(texts)
            finally:
                es.close()
            for concept, vec in zip(sparse, vecs):
                unit = _normalize(vec)
                if unit is not None:
                    out[concept] = unit
        except Exception as exc:  # noqa: BLE001 — no key / offline: string pairs still cover these
            log.warning(
                "term-embedding fallback failed for %d sparse concepts (%s); "
                "they get string-match dedup evidence only",
                len(sparse),
                exc,
            )
    return out


def theme_vectors(cfg: Config) -> dict[str, list[float]]:
    """thm-id → unit-norm cached embedding of the theme note itself."""
    if not cfg.index_db.exists():
        return {}
    db = sqlite3.connect(str(cfg.index_db))
    db.row_factory = sqlite3.Row
    try:
        theme_ids = [
            row["id"]
            for row in db.execute("SELECT id FROM notes WHERE type = 'theme'")
        ]
    finally:
        db.close()
    note_vecs = _load_note_vectors(cfg)
    out: dict[str, list[float]] = {}
    for tid in theme_ids:
        vec = note_vecs.get(tid)
        if vec is None:
            continue
        unit = _normalize(vec)
        if unit is not None:
            out[tid] = unit
    return out


def cosine_pairs(
    vectors: dict[str, list[float]],
    *,
    threshold: float = DEFAULT_COSINE_THRESHOLD,
) -> list[tuple[str, str, float]]:
    """All pairs at or above ``threshold``, sorted by cosine descending.

    Assumes unit-norm input (as produced by :func:`concept_centroids` /
    :func:`theme_vectors`) so similarity is a single dot product.
    """
    keys = sorted(vectors)
    out: list[tuple[str, str, float]] = []
    for i, a in enumerate(keys):
        va = vectors[a]
        for b in keys[i + 1 :]:
            vb = vectors[b]
            if len(va) != len(vb):
                continue
            cos = _dot(va, vb)
            if cos >= threshold:
                out.append((a, b, round(cos, 4)))
    out.sort(key=lambda t: t[2], reverse=True)
    return out


def concept_domain_map(ontology: dict[str, list[str]]) -> dict[str, list[str]]:
    """concept → [domains it appears under] (domain keys map to themselves)."""
    out: dict[str, list[str]] = {}
    for domain, concepts in ontology.items():
        out.setdefault(domain.lower(), []).append(domain)
        for c in concepts:
            out.setdefault(c.lower(), []).append(domain)
    return out


def build_concept_evidence(
    cfg: Config,
    pairs: list[tuple[str, str, float | None, str]],
    *,
    sample_titles: int = 3,
) -> list[dict[str, Any]]:
    """Evidence packets for concept pairs — what the merge worker judges from.

    ``pairs`` rows are ``(a, b, cosine_or_None, reason)`` — cosine is
    ``None`` for string-rule pairs whose centroids weren't computable.
    Packet shape::

        {"from": a, "to": b, "cosine": 0.87, "reason": "...",
         "same_domain": bool, "domains": {a: [...], b: [...]},
         "note_counts": {a: n, b: m}, "cooccurrence": k,
         "sample_titles": {a: [...], b: [...]}}
    """
    from personal_mem.synthesis.concepts import load_ontology

    dmap = concept_domain_map(load_ontology())

    counts: dict[str, int] = {}
    titles: dict[str, list[str]] = {}
    cooc: dict[frozenset[str], int] = {}
    if cfg.index_db.exists():
        db = sqlite3.connect(str(cfg.index_db))
        db.row_factory = sqlite3.Row
        try:
            wanted = {c for a, b, _, _ in pairs for c in (a, b)}
            for c in wanted:
                row = db.execute(
                    "SELECT COUNT(*) AS n FROM note_concepts WHERE concept = ?",
                    (c,),
                ).fetchone()
                counts[c] = row["n"] if row else 0
                titles[c] = [
                    r["title"]
                    for r in db.execute(
                        """
                        SELECT n.title FROM notes n
                        JOIN note_concepts nc ON nc.note_id = n.id
                        WHERE nc.concept = ?
                        ORDER BY n.date DESC LIMIT ?
                        """,
                        (c, sample_titles),
                    )
                ]
            for a, b, _, _ in pairs:
                key = frozenset((a, b))
                if key in cooc:
                    continue
                row = db.execute(
                    """
                    SELECT COUNT(*) AS n FROM note_concepts x
                    JOIN note_concepts y ON y.note_id = x.note_id
                    WHERE x.concept = ? AND y.concept = ?
                    """,
                    (a, b),
                ).fetchone()
                cooc[key] = row["n"] if row else 0
        finally:
            db.close()

    out: list[dict[str, Any]] = []
    for a, b, cos, reason in pairs:
        da, db_ = dmap.get(a, []), dmap.get(b, [])
        packet = {
            "from": a,
            "to": b,
            "cosine": cos,
            "reason": reason,
            "same_domain": bool(set(da) & set(db_)),
            "domains": {a: da, b: db_},
            "note_counts": {a: counts.get(a, 0), b: counts.get(b, 0)},
            "cooccurrence": cooc.get(frozenset((a, b)), 0),
            "sample_titles": {a: titles.get(a, []), b: titles.get(b, [])},
        }
        out.append(packet)
    return out


# ---------------------------------------------------------------------------
# Judgment memory — read back from the dream maintenance log
# ---------------------------------------------------------------------------

PairKey = tuple[str, frozenset]


def pair_key(kind: str, a: str, b: str) -> PairKey:
    """Canonical exclusion key — order-insensitive within a kind."""
    return (kind, frozenset((a.lower().strip(), b.lower().strip())))


def judged_pairs(cfg: Config) -> set[PairKey]:
    """Every pair a past dream cycle already ruled on (merged OR distinct).

    Reads the per-cycle ``verdicts`` block out of
    ``vault/.mem/maintenance.jsonl``. Excluding these from the scan is
    what makes the drift pool drain instead of re-litigating the same
    head-of-list pairs every cycle. Corrupt lines are skipped — the log
    is append-only and may interleave with older schema versions.
    """
    from personal_mem.operations.dream import maintenance_log_path

    path = maintenance_log_path(cfg)
    if not path.exists():
        return set()
    out: set[PairKey] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        verdicts = entry.get("verdicts") or {}
        if not isinstance(verdicts, dict):
            continue
        for m in verdicts.get("merges") or []:
            if isinstance(m, dict) and m.get("from") and m.get("to"):
                out.add(pair_key("concept", m["from"], m["to"]))
        for m in verdicts.get("theme_merges") or []:
            if isinstance(m, dict) and m.get("from_id") and m.get("to_id"):
                out.add(pair_key("theme", m["from_id"], m["to_id"]))
        for d in verdicts.get("distinct_pairs") or []:
            if not isinstance(d, dict):
                continue
            pair = d.get("pair") or []
            if len(pair) == 2:
                out.add(pair_key(str(d.get("kind") or "concept"), pair[0], pair[1]))
    return out
