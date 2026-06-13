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

#: Grain-coarsening near-clique floor. Stricter than the pairwise merge
#: threshold because collapsing N fine terms onto one coarse term is a
#: destructive fold — a false-positive member costs more than a missed
#: cluster (which simply re-surfaces next cycle).
DEFAULT_COARSEN_THRESHOLD = 0.85

#: Default cap on members in one coarsening cluster (bounds clique-grow).
DEFAULT_COARSEN_MAX_SIZE = 6


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


# ---------------------------------------------------------------------------
# Grain coarsening — N-ary near-clique clusters (drift v2)
# ---------------------------------------------------------------------------


def _greedy_cliques(
    vectors: dict[str, list[float]],
    threshold: float,
    max_size: int,
) -> list[tuple[list[str], float, float]]:
    """Greedy complete-linkage near-cliques over the cosine graph.

    A cluster is a set where **every** pairwise cosine ≥ ``threshold`` — a
    near-clique, not a connected component, so it cannot chain (``a~b~c~d``
    with ``a⊥d`` never forms one cluster). Seeds from the tightest unclaimed
    edge and grows by the candidate that cliques with all current members,
    highest min-cosine first. Deterministic (sorted tie-breaks). Each node
    lands in at most one cluster. 2-member cliques (plain pairs) are emitted
    too. Returns ``[(sorted_members, avg_cos, min_cos), ...]`` ranked by
    ``min_cos`` (cohesion) descending.
    """
    edges = cosine_pairs(vectors, threshold=threshold)
    adj: dict[str, dict[str, float]] = {}
    for a, b, cos in edges:
        adj.setdefault(a, {})[b] = cos
        adj.setdefault(b, {})[a] = cos

    claimed: set[str] = set()
    clusters: list[tuple[list[str], float, float]] = []
    for a, b, _cos in edges:
        if a in claimed or b in claimed:
            continue
        members = [a, b]
        while len(members) < max_size:
            common: set[str] | None = None
            for m in members:
                nbrs = set(adj.get(m, {}))
                common = nbrs if common is None else (common & nbrs)
            common = (common or set()) - set(members) - claimed
            if not common:
                break
            best: str | None = None
            best_mincos = -1.0
            for c in sorted(common):  # sorted → deterministic tie-break
                mc = min(adj[c][m] for m in members)
                if mc > best_mincos:
                    best_mincos = mc
                    best = c
            if best is None:
                break
            members.append(best)

        pair_coss = [
            adj[members[i]][members[j]]
            for i in range(len(members))
            for j in range(i + 1, len(members))
        ]
        avg = round(sum(pair_coss) / len(pair_coss), 4)
        mn = round(min(pair_coss), 4)
        claimed.update(members)
        clusters.append((sorted(members), avg, mn))

    clusters.sort(key=lambda t: t[2], reverse=True)
    return clusters


def concept_clusters(
    vectors: dict[str, list[float]],
    *,
    threshold: float = DEFAULT_COARSEN_THRESHOLD,
    max_size: int = DEFAULT_COARSEN_MAX_SIZE,
) -> list[tuple[list[str], float, float]]:
    """Near-clique concept clusters for grain coarsening (see :func:`_greedy_cliques`)."""
    return _greedy_cliques(vectors, threshold, max_size)


def theme_clusters(
    vectors: dict[str, list[float]],
    *,
    threshold: float = DEFAULT_COARSEN_THRESHOLD,
    max_size: int = DEFAULT_COARSEN_MAX_SIZE,
) -> list[tuple[list[str], float, float]]:
    """Near-clique theme clusters (thm-id members) for arc coarsening."""
    return _greedy_cliques(vectors, threshold, max_size)


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


def build_concept_cluster_evidence(
    cfg: Config,
    clusters: list[tuple[list[str], float, float]],
    *,
    sample_titles: int = 3,
) -> list[dict[str, Any]]:
    """Evidence packets for concept coarsening clusters.

    Per cluster the worker sees, for each member: its ontology domains,
    note count, and recent sample titles; plus ``common_domain`` (the
    intersection of all members' domains, a strong COLLAPSE signal) and
    ``canonical_target_hint`` (a member that is itself an ontology domain
    key, e.g. ``greeks`` — the obvious fold target; ``None`` → the worker
    proposes a new coarse term). Hint only; the worker owns the target.
    """
    from personal_mem.synthesis.concepts import load_ontology

    ontology = load_ontology()
    dmap = concept_domain_map(ontology)
    domain_keys = {d.lower() for d in ontology}

    members_all = {m for members, _, _ in clusters for m in members}
    counts: dict[str, int] = {}
    titles: dict[str, list[str]] = {}
    if cfg.index_db.exists() and members_all:
        db = sqlite3.connect(str(cfg.index_db))
        db.row_factory = sqlite3.Row
        try:
            for c in members_all:
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
        finally:
            db.close()

    out: list[dict[str, Any]] = []
    for members, avg, mn in clusters:
        common = set.intersection(*[set(dmap.get(m, [])) for m in members]) if members else set()
        out.append(
            {
                "members": members,
                "avg_cosine": avg,
                "min_cosine": mn,
                "domains": {m: dmap.get(m, []) for m in members},
                "note_counts": {m: counts.get(m, 0) for m in members},
                "sample_titles": {m: titles.get(m, []) for m in members},
                "common_domain": sorted(common)[0] if common else None,
                "canonical_target_hint": next(
                    (m for m in members if m in domain_keys), None
                ),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Judgment memory — read back from the dream maintenance log
# ---------------------------------------------------------------------------

PairKey = tuple[str, frozenset]
ClusterKey = tuple[str, frozenset]


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


def cluster_key(kind: str, members: list[str]) -> ClusterKey:
    """Canonical exclusion key for an N-ary cluster — order-insensitive."""
    return (kind, frozenset(str(m).lower().strip() for m in members))


def judged_clusters(cfg: Config) -> tuple[set[tuple[str, str]], set[ClusterKey]]:
    """Cluster-scope verdict memory, read back from the maintenance log.

    Returns ``(coarsened_members, distinct_cluster_keys)``:

    - ``coarsened_members`` — ``{(kind, term)}`` for every term folded away
      (coarsening members minus the target, theme-coarsening members minus
      the survivor, plus pairwise ``merges`` ``from`` / ``theme_merges``
      ``from_id``). A candidate cluster touching any of these is **stale**
      (the term no longer exists) → drop on overlap.
    - ``distinct_cluster_keys`` — exact :func:`cluster_key` set for clusters
      ruled DISTINCT. Drop only on exact match (the members stay live and
      may legitimately re-cluster differently later).

    This pair of sets is the anti-oscillation guard: once a cluster is
    COLLAPSEd or ruled DISTINCT it cannot re-surface in the nightly loop.
    """
    from personal_mem.operations.dream import maintenance_log_path

    coarsened: set[tuple[str, str]] = set()
    distinct_keys: set[ClusterKey] = set()
    path = maintenance_log_path(cfg)
    if not path.exists():
        return coarsened, distinct_keys

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
            if isinstance(m, dict) and m.get("from"):
                coarsened.add(("concept", str(m["from"]).lower().strip()))
        for m in verdicts.get("theme_merges") or []:
            if isinstance(m, dict) and m.get("from_id"):
                coarsened.add(("theme", str(m["from_id"]).lower().strip()))
        for c in verdicts.get("coarsenings") or []:
            if not isinstance(c, dict):
                continue
            target = str(c.get("target") or "").lower().strip()
            for mem in c.get("members") or []:
                ml = str(mem).lower().strip()
                if ml and ml != target:
                    coarsened.add(("concept", ml))
        for c in verdicts.get("theme_coarsenings") or []:
            if not isinstance(c, dict):
                continue
            surv = str(c.get("survivor_id") or "").lower().strip()
            for mem in c.get("members") or []:
                ml = str(mem).lower().strip()
                if ml and ml != surv:
                    coarsened.add(("theme", ml))
        for d in verdicts.get("distinct_clusters") or []:
            if not isinstance(d, dict):
                continue
            members = d.get("members") or []
            if len(members) >= 2:
                distinct_keys.add(
                    cluster_key(str(d.get("kind") or "concept"), members)
                )
    return coarsened, distinct_keys
