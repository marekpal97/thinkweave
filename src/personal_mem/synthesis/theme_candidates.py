"""Theme cluster detection for event-grain source types.

Deterministic clustering: look at recent (≤``recent_days``) event-grain
sources sharing ≥``min_shared_concepts`` concepts. A group of
≥``min_cluster_size`` such sources is a *cluster*. Each cluster is
surfaced to ``/dream`` as a :class:`ThemeClusterSignal` carrying enough
raw material — source titles, the per-source ``proposed_theme:`` stamps,
and any active theme whose concepts overlap — for the LLM turn to either
**mint** a new theme or **extend** an existing one.

The cluster check is pure Python — no LLM, no API call. All naming and
the mint/extend decision live in ``/dream``'s judgment turn.

History (2026-05-30 teardown): the prior design materialised ``cand-*``
stub files, ran an exact-match ``proposed_theme`` vote, and auto-resolved
themes from linked-decision status. Inspection of the live vault showed
the stub path produced 38 never-promoted stubs, the vote almost never
aggregated (divergent free-text slugs), and zero decisions ever linked a
theme. All three were removed. What remains: detect clusters, hand
``/dream`` the raw material, mint or extend on its say-so. Themes never
change *status* automatically — that is the user's call by hand.
"""

from __future__ import annotations

import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

from personal_mem.core._utils import as_list
from personal_mem.core.config import Config


# Defaults are intentionally conservative — the detector is supposed to be
# quiet, not noisy. 3 is the smallest "this is a thing" cluster size; ≤30
# days of event-grain drains comfortably hit that bar. User-overridable via
# config ``themes.recent_days`` / ``themes.min_cluster_size`` /
# ``themes.min_shared_concepts`` (resolved in :func:`detect_signals`).
DEFAULT_RECENT_DAYS = 30
DEFAULT_MIN_CLUSTER_SIZE = 3
DEFAULT_MIN_SHARED_CONCEPTS = 2

# Name clusters key on the worker's ``proposed_theme:`` stamp, which is a
# *much* stronger arc signal than incidental concept overlap — so they
# clear the bar at 2 sources where concept clusters need 3.
DEFAULT_MIN_NAME_CLUSTER_SIZE = 2

# How many overlapping active themes / cluster sources to attach to a
# signal. Bounded so the /dream payload stays small.
MAX_COVERING_THEMES = 5
MAX_SIGNAL_SOURCES = 15

# A concept on more than this fraction of the recent source pool is
# "generic" (risk-management, liquidity, …): it carries almost no
# topical information, so it is dropped from covering-theme overlap
# scoring. This is the D2 fix — without it, an arc routes to whichever
# theme happens to share a generic concept (iran-war → housing-bust).
# Config ``themes.generic_concept_ratio`` overrides.
GENERIC_CONCEPT_DF_RATIO = 0.5

# Tokens too generic to bind two ``proposed_theme`` slugs into one arc
# family. Without this, ``alpha-arc`` / ``beta-arc`` would merge on
# "arc", and every "us-*" slug would merge on "us".
_NAME_STOPWORDS = frozenset(
    {
        "us", "eu", "uk", "the", "of", "and", "a", "an", "to", "in",
        "macro", "market", "markets", "global", "risk", "wave", "cycle",
        "arc", "era", "regime", "story", "play", "push", "window", "boom",
        "trend", "theme", "outlook",
    }
)

# Two slugs join the same arc family when their significant-token sets
# overlap at Jaccard ≥ this. iran-war vs iran-war-resolution → 2/3 = 0.67
# (merge); condo-bust vs housing-deleveraging → 0 (left to the LLM).
# Config ``themes.name_family_jaccard`` overrides.
_NAME_FAMILY_JACCARD = 0.5


def _slugify(text: str, *, max_len: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return (s[:max_len] or "theme").strip("-")


# Excerpt length for per-source body snippets attached to signals. Long
# enough for the theme worker to distill a 1-2 sentence catalyst artifact,
# short enough that MAX_SIGNAL_SOURCES excerpts stay under ~10KB/signal.
EXCERPT_CHARS = 600


def _excerpt(body_text: str, *, max_len: int = EXCERPT_CHARS) -> str:
    """First ``max_len`` chars of prose — headings stripped, word-boundary cut."""
    if not body_text:
        return ""
    prose = "\n".join(
        ln for ln in body_text.splitlines() if not ln.lstrip().startswith("#")
    ).strip()
    if len(prose) <= max_len:
        return prose
    cut = prose[:max_len]
    sp = cut.rfind(" ")
    if sp > max_len // 2:
        cut = cut[:sp]
    return cut.rstrip() + "…"


@dataclass(frozen=True)
class ClusterDescriptor:
    """A detected cluster: ≥N event-grain sources sharing ≥M concepts."""

    source_type: str
    source_ids: tuple[str, ...]
    source_titles: tuple[str, ...]
    shared_concepts: tuple[str, ...]


@dataclass(frozen=True)
class ThemeClusterSignal:
    """An enriched cluster surfaced to ``/dream``.

    Fields:
        source_type: the event-grain source type the cluster came from.
        cluster_kind: ``"name"`` (grouped on the ``proposed_theme:``
            stamp — the primary, concept-independent path) or
            ``"concept"`` (the fallback path for *unstamped* sources,
            grouped on shared concepts). Name clusters are the strong
            signal; concept clusters catch arcs the worker never named.
        label: for name clusters, the most-supported ``proposed_theme``
            slug in the family — the arc's working name. Empty for
            concept clusters.
        shared_concepts: the concepts that describe the cluster. For name
            clusters these *describe* the arc (ranked by how many cluster
            sources carry them); they did not drive grouping. For concept
            clusters they are the concepts that drove grouping.
        sources: one dict per cluster source, newest first, capped at
            ``MAX_SIGNAL_SOURCES``. Shape:
            ``{"id", "title", "proposed_theme", "date", "excerpt"}`` —
            ``excerpt`` is ~600 chars of body prose so the theme worker
            can distill per-source catalyst artifacts without extra
            ``mem_read`` round-trips.
        proposed_names: distinct-source tally of the ``proposed_theme:``
            stamps in the cluster — ``{slug: n_distinct_sources}`` (D1:
            this counts *sources*, never appearances-across-clusters, so
            the number is the honest support). May be empty (concept
            cluster / unstamped sources).
        related_names: other ``proposed_theme`` slugs in the same arc
            family that did *not* win the label, ``{slug: n_sources}`` —
            so ``/dream`` can see the variants folded in.
        covering_themes: active/canonical themes whose ``concepts:``
            overlap this cluster, ranked by IDF-weighted topical score
            (D2: generic concepts on >50% of the pool are excluded, so
            the ranking reflects *topical* overlap, not coincidental
            shared boilerplate). Shape: ``{"theme_id", "slug",
            "concepts", "overlap", "score", "status"}``. Non-empty →
            ``/dream`` should usually EXTEND the top theme rather than
            mint a near-duplicate.
    """

    source_type: str
    shared_concepts: list[str]
    cluster_kind: str = "concept"
    label: str = ""
    sources: list[dict] = field(default_factory=list)
    proposed_names: dict = field(default_factory=dict)
    related_names: dict = field(default_factory=dict)
    covering_themes: list[dict] = field(default_factory=list)

    @property
    def cluster_source_ids(self) -> list[str]:
        return [s["id"] for s in self.sources]

    @property
    def cluster_source_titles(self) -> list[str]:
        return [s.get("title", "") for s in self.sources]


def detect_signals(
    config: Config,
    *,
    source_type: str = "",
    recent_days: int | None = None,
    min_cluster_size: int | None = None,
    min_shared_concepts: int | None = None,
    min_name_cluster_size: int = DEFAULT_MIN_NAME_CLUSTER_SIZE,
) -> list[ThemeClusterSignal]:
    """Detect enriched theme cluster signals for ``/dream``.

    Two clustering paths, name-primary (2026-05-30 round-2 — concepts
    demoted from grouping key to descriptor):

    1. **name clusters** (primary) — recent event-grain sources are
       grouped on their ``proposed_theme:`` stamp, with a conservative
       token-Jaccard merge folding fragmented variants (``iran-war`` /
       ``iran-war-resolution``) into one arc. The worker naming an arc
       is a far stronger signal than incidental concept overlap, so
       these clear at ``min_name_cluster_size`` (default 2) sources.
    2. **concept clusters** (fallback) — only *unstamped* sources (the
       worker named no arc) fall through to the old concept-combination
       clustering, so unnamed arcs still surface.

    Clusters already covered by an active theme are **not** suppressed —
    the covering theme is attached in ``covering_themes`` (ranked by
    label↔slug token match + IDF-weighted concept overlap, generic
    concepts dropped) so ``/dream`` can EXTEND rather than mint a
    duplicate.

    When ``source_type`` is given, restrict to that one type; otherwise
    scan every registered event-grain source type.

    Detection thresholds default from config (``themes.recent_days`` /
    ``themes.min_cluster_size`` / ``themes.min_shared_concepts``, plus
    ``themes.name_family_jaccard`` and ``themes.generic_concept_ratio``
    threaded into the family-merge / covering-theme helpers); explicit
    kwargs override per-call.
    """
    import json as _json

    from personal_mem.core.indexer import Indexer
    from personal_mem.acquisition.sources import registry as source_registry

    if recent_days is None:
        recent_days = int(
            getattr(config, "theme_recent_days", DEFAULT_RECENT_DAYS)
            or DEFAULT_RECENT_DAYS
        )
    if min_cluster_size is None:
        min_cluster_size = int(
            getattr(config, "theme_min_cluster_size", DEFAULT_MIN_CLUSTER_SIZE)
            or DEFAULT_MIN_CLUSTER_SIZE
        )
    if min_shared_concepts is None:
        min_shared_concepts = int(
            getattr(
                config, "theme_min_shared_concepts", DEFAULT_MIN_SHARED_CONCEPTS
            )
            or DEFAULT_MIN_SHARED_CONCEPTS
        )
    name_family_jaccard = float(
        getattr(config, "theme_name_family_jaccard", _NAME_FAMILY_JACCARD)
        or _NAME_FAMILY_JACCARD
    )
    generic_concept_ratio = float(
        getattr(config, "theme_generic_concept_ratio", GENERIC_CONCEPT_DF_RATIO)
        or GENERIC_CONCEPT_DF_RATIO
    )

    # Which event-grain types to scan.
    if source_type:
        spec = source_registry.get_spec(source_type, vault_root=config.vault_root)
        if spec is None or spec.temporal_grain != "event":
            return []
        event_types = [spec.slug]
    else:
        event_types = [
            spec.slug
            for spec in source_registry.all_specs(vault_root=config.vault_root)
            if spec.temporal_grain == "event"
        ]
    if not event_types:
        return []

    cutoff_iso = (
        datetime.now(timezone.utc) - timedelta(days=recent_days)
    ).isoformat()

    idx = Indexer(config=config)
    try:
        # Recent event-grain sources, grouped by type.
        sources_by_type: dict[str, list[dict]] = {}
        for st in event_types:
            rows = idx.db.execute(
                """
                SELECT n.id, n.title, n.date, n.frontmatter, n.body_text
                FROM notes n
                WHERE n.type = 'source'
                  AND n.date >= ?
                  AND n.id IS NOT NULL
                """,
                (cutoff_iso,),
            ).fetchall()
            matches: list[dict] = []
            for row in rows:
                fm = _json.loads(row["frontmatter"]) if row["frontmatter"] else {}
                if fm.get("source_type") != st:
                    continue
                concepts = as_list(fm.get("concepts"))
                if not concepts:
                    continue
                # Already filed to a theme → settled. Re-clustering it is
                # pure noise (it can only re-propose an arc it's already
                # on), so it never enters detection.
                if any(
                    str(r).startswith("thm-")
                    for r in as_list(fm.get("relates_to"))
                ):
                    continue
                matches.append(
                    {
                        "id": row["id"],
                        "title": row["title"] or "",
                        "date": row["date"] or "",
                        "concepts": [c.lower() for c in concepts],
                        "proposed_theme": (fm.get("proposed_theme") or "").strip(),
                        "excerpt": _excerpt(row["body_text"] or ""),
                    }
                )
            sources_by_type[st] = matches

        # Canonical themes (for the covering_themes / extend signal).
        themes: list[dict] = []
        for row in idx.db.execute(
            """
            SELECT id, title, frontmatter FROM notes
            WHERE type = 'theme' AND id LIKE 'thm-%'
            """
        ).fetchall():
            fm = _json.loads(row["frontmatter"]) if row["frontmatter"] else {}
            themes.append(
                {
                    "theme_id": row["id"],
                    "slug": fm.get("title") or row["title"] or row["id"],
                    "concepts": {c.lower() for c in as_list(fm.get("concepts"))},
                    "status": (fm.get("status") or "active").split(":")[0],
                }
            )
    finally:
        idx.close()

    # Concept document-frequency across the whole recent event-grain pool
    # — used to drop generic concepts from covering-theme scoring (D2).
    pool = [s for srcs in sources_by_type.values() for s in srcs]
    pool_size = len(pool)
    concept_df: Counter = Counter()
    for s in pool:
        for c in set(s["concepts"]):
            concept_df[c] += 1

    signals: list[ThemeClusterSignal] = []
    for st, sources in sources_by_type.items():
        # 1. PRIMARY — name clusters on proposed_theme (concept-free).
        for label, members, name_tally in _cluster_by_proposed_theme(
            sources,
            min_cluster_size=min_name_cluster_size,
            family_jaccard=name_family_jaccard,
        ):
            signals.append(
                _build_signal(
                    st,
                    members,
                    themes,
                    concept_df,
                    pool_size,
                    kind="name",
                    label=label,
                    name_tally=name_tally,
                    generic_ratio=generic_concept_ratio,
                )
            )

        # 2. FALLBACK — concept clusters over UNSTAMPED sources only.
        unstamped = [s for s in sources if not s["proposed_theme"]]
        if len(unstamped) >= min_cluster_size:
            by_id = {s["id"]: s for s in unstamped}
            for cluster in _detect_clusters(
                unstamped,
                source_type=st,
                min_cluster_size=min_cluster_size,
                min_shared_concepts=min_shared_concepts,
            ):
                members = [
                    by_id[sid] for sid in cluster.source_ids if sid in by_id
                ]
                signals.append(
                    _build_signal(
                        st,
                        members,
                        themes,
                        concept_df,
                        pool_size,
                        kind="concept",
                        forced_concepts=list(cluster.shared_concepts),
                        generic_ratio=generic_concept_ratio,
                    )
                )
    return signals


def _build_signal(
    source_type: str,
    members: list[dict],
    themes: list[dict],
    concept_df: Counter,
    pool_size: int,
    *,
    kind: str,
    label: str = "",
    name_tally: dict | None = None,
    forced_concepts: list[str] | None = None,
    generic_ratio: float = GENERIC_CONCEPT_DF_RATIO,
) -> ThemeClusterSignal:
    """Assemble a :class:`ThemeClusterSignal` from a cluster's members."""
    members = sorted(members, key=lambda s: s.get("date", ""), reverse=True)
    n = len(members)
    source_dicts = [
        {
            "id": s["id"],
            "title": s["title"],
            "proposed_theme": s["proposed_theme"],
            "date": s["date"],
            "excerpt": s.get("excerpt", ""),
        }
        for s in members[:MAX_SIGNAL_SOURCES]
    ]

    # Concept support *within* the cluster — descriptive, not the key.
    csupport: Counter = Counter()
    for s in members:
        for c in set(s["concepts"]):
            csupport[c] += 1
    if forced_concepts is not None:
        shared_concepts = list(forced_concepts)
    else:
        # Concepts on ≥ half the cluster, most-supported first; if none
        # clears the bar, take the top 3 so the arc still reads.
        shared_concepts = [c for c, k in csupport.most_common() if k * 2 >= n]
        if not shared_concepts:
            shared_concepts = [c for c, _ in csupport.most_common(3)]

    if name_tally:
        proposed_names = dict(
            sorted(name_tally.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        related_names = {k: v for k, v in proposed_names.items() if k != label}
    else:
        proposed_names, related_names = {}, {}

    covering = _rank_covering(
        csupport, n, label, themes, concept_df, pool_size,
        generic_ratio=generic_ratio,
    )

    return ThemeClusterSignal(
        source_type=source_type,
        shared_concepts=shared_concepts,
        cluster_kind=kind,
        label=label,
        sources=source_dicts,
        proposed_names=proposed_names,
        related_names=related_names,
        covering_themes=covering,
    )


def _rank_covering(
    csupport: Counter,
    n: int,
    label: str,
    themes: list[dict],
    concept_df: Counter,
    pool_size: int,
    *,
    generic_ratio: float = GENERIC_CONCEPT_DF_RATIO,
) -> list[dict]:
    """Rank active themes as EXTEND targets for a cluster (the D2 fix).

    Score = label↔slug token match (the strong, concept-independent
    signal) × 10 + IDF-weighted concept overlap. Concepts on >half the
    recent pool are *generic* and contribute nothing — this is what stops
    ``iran-war`` from routing to ``housing-bust-cycle`` on a shared
    ``risk-management`` stamp. The IDF filter only kicks in once the pool
    is big enough (≥8) for document-frequency to mean anything.
    """
    label_toks = _name_tokens(label) if label else set()
    apply_generic = pool_size >= 8
    covering: list[dict] = []
    for t in themes:
        shared = set(csupport) & t["concepts"]
        slug_toks = _name_tokens(t["slug"])
        if label_toks and slug_toks:
            name_match = len(label_toks & slug_toks) / len(label_toks | slug_toks)
        else:
            name_match = 0.0

        cscore = 0.0
        topical = 0  # non-generic shared concepts
        for c in shared:
            df = concept_df.get(c, 0)
            if apply_generic and pool_size and df / pool_size > generic_ratio:
                continue  # generic concept — no topical signal
            topical += 1
            idf = math.log((pool_size + 1) / (df + 1)) + 1.0
            cscore += (csupport[c] / n) * idf

        # A label↔slug token hit is a near-certain route; concept overlap
        # is only trustworthy when ≥2 *non-generic* concepts agree (one
        # shared desk/method concept like `fundamental-analysis` is noise
        # in a finance-news corpus). Drop everything else.
        if name_match == 0.0 and topical < 2:
            continue

        score = name_match * 10.0 + cscore
        covering.append(
            {
                "theme_id": t["theme_id"],
                "slug": t["slug"],
                "concepts": sorted(t["concepts"]),
                "overlap": len(shared),
                "name_match": round(name_match, 3),
                "score": round(score, 4),
                "status": t["status"],
            }
        )
    # Name-matched themes rank categorically above concept-only ones.
    covering.sort(
        key=lambda c: (
            c["name_match"] == 0.0,
            -c["name_match"],
            -c["score"],
            -c["overlap"],
            c["slug"],
        )
    )
    return covering[:MAX_COVERING_THEMES]


def _name_tokens(slug: str) -> set[str]:
    """Significant tokens of a slug — drops generic/stopword tokens."""
    return {
        t
        for t in re.split(r"[-_\s]+", (slug or "").lower())
        if len(t) >= 3 and t not in _NAME_STOPWORDS
    }


def _cluster_by_proposed_theme(
    sources: list[dict],
    *,
    min_cluster_size: int,
    family_jaccard: float = _NAME_FAMILY_JACCARD,
) -> list[tuple[str, list[dict], dict]]:
    """Group *stamped* sources into arc families on ``proposed_theme``.

    Exact-name buckets first, then a token-Jaccard union-find merges
    near-variant slugs into one family. Concepts play no part — this is
    the concept-independent primary path. Returns ``(label, members,
    name_tally)`` per family with ≥``min_cluster_size`` distinct sources,
    where ``label`` is the most-supported variant and ``name_tally`` maps
    each variant slug to its distinct-source count (D1 — honest support).
    """
    by_name: dict[str, list[dict]] = {}
    for s in sources:
        pt = s.get("proposed_theme")
        if pt:
            by_name.setdefault(pt, []).append(s)
    if not by_name:
        return []

    names = list(by_name)
    parent = {nm: nm for nm in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    toks = {nm: _name_tokens(nm) for nm in names}
    for i in range(len(names)):
        ti = toks[names[i]]
        if not ti:
            continue
        for j in range(i + 1, len(names)):
            tj = toks[names[j]]
            if not tj:
                continue
            if len(ti & tj) / len(ti | tj) >= family_jaccard:
                parent[find(names[i])] = find(names[j])

    fams: dict[str, list[str]] = {}
    for nm in names:
        fams.setdefault(find(nm), []).append(nm)

    out: list[tuple[str, list[dict], dict]] = []
    for variants in fams.values():
        seen: set[str] = set()
        members: list[dict] = []
        name_tally: Counter = Counter()
        for nm in variants:
            for s in by_name[nm]:
                if s["id"] in seen:
                    continue
                seen.add(s["id"])
                members.append(s)
                name_tally[nm] += 1
        if len(members) < min_cluster_size:
            continue
        label = name_tally.most_common(1)[0][0]
        out.append((label, members, dict(name_tally)))
    return out


def _catalyst_map(catalysts: list[dict] | None) -> dict[str, tuple[str, str]]:
    """Normalize worker-provided catalyst entries → ``{source_id: (text, flag)}``.

    Defensive against worker drift: missing/blank text drops the entry
    (caller falls back to its generic line), unknown flags fall back to
    ``new``, over-long text is clipped at a word boundary (~300 chars).
    """
    from personal_mem.synthesis.hub import ALLOWED_FLAGS, FLAG_NEW

    out: dict[str, tuple[str, str]] = {}
    for c in catalysts or []:
        if not isinstance(c, dict):
            continue
        sid = str(c.get("source_id") or "").strip()
        text = " ".join(str(c.get("text") or "").split())
        if not sid or not text:
            continue
        if len(text) > 300:
            cut = text[:300]
            sp = cut.rfind(" ")
            if sp > 150:
                cut = cut[:sp]
            text = cut.rstrip() + "…"
        flag = str(c.get("flag") or "").strip().lower()
        if flag not in ALLOWED_FLAGS:
            flag = FLAG_NEW
        out[sid] = (text, flag)
    return out


def mint_theme_from_signal(
    config: Config,
    *,
    slug: str,
    essence: str,
    cluster_source_ids: list[str],
    cluster_concepts: list[str],
    candidacy: str = "inferred-from-signal",
    project: str = "",
    parent: str = "",
    title: str = "",
    catalysts: list[dict] | None = None,
    rebuild_index: bool = True,
) -> Path:
    """Mint a canonical theme from a cluster signal.

    Writes ``vault/themes/{thm-XXXX}-{slug}.md`` and backfills
    ``relates_to: [thm-id]`` on each cluster source so source→theme edges
    exist in both directions. Returns the new theme path. Used by the
    ``/dream`` apply phase's ``theme_mints`` plan key.

    ``title`` (optional) is a human display title ("Iran–Hormuz supply
    shock") written to ``title:`` frontmatter and the H1; the slug stays
    the filename + registry key. ``catalysts`` (optional) carries
    per-source distillations ``{source_id, text, flag?}`` composed by the
    dream theme worker — seed log entries use them instead of the generic
    "cluster seed" text.
    """
    from personal_mem.core.indexer import Indexer

    thm_id = f"thm-{uuid.uuid4().hex[:8]}"
    file_slug = _slugify(slug)
    themes_dir = config.vault_root / "themes"
    themes_dir.mkdir(parents=True, exist_ok=True)
    # Pure-slug filename (like concept hubs) — the thm-id stays in
    # frontmatter + aliases, so links resolve by path or id alias. Disambiguate
    # a slug collision with a numeric suffix; the registry is keyed by id.
    target_path = themes_dir / f"{file_slug}.md"
    _n = 1
    while target_path.exists():
        target_path = themes_dir / f"{file_slug}-{_n}.md"
        _n += 1
    today = datetime.now(timezone.utc).isoformat()

    from personal_mem.core.vault import render_frontmatter

    display_title = (title or "").strip() or slug
    # Emit through the shared escaping-aware renderer — news-derived
    # display titles can carry double quotes / colons, and a hand-rolled
    # f-string frontmatter block is a YAML-injection hole.
    fm: dict = {
        "type": "theme",
        "id": thm_id,
        "date": today,
        "title": display_title,
        "status": "active",
        "promotion_origin": candidacy,
    }
    if essence.strip():
        # Stamp only when a real essence was supplied — the placeholder
        # fallback below must still read as "never synthesised" to the
        # dream essence worker's candidate scan.
        fm["essence_updated"] = today[:10]
    if cluster_concepts:
        fm["concepts"] = list(cluster_concepts)
    if cluster_source_ids:
        fm["cites"] = list(cluster_source_ids)
    if project:
        fm["project"] = project
    if parent:
        fm["parent"] = parent
    fm["aliases"] = [thm_id]
    frontmatter_block = render_frontmatter(fm)

    # Body uses the shared Hub spine so the catalyst-log grammar is
    # byte-identical to concept hubs. (The previous hand-rolled
    # `- DATE: cluster seed [[src]] *new*` form diverged from the canonical
    # `- DATE · *new* — text — [[src]]` grammar the Hub parser expects, so
    # minted catalyst logs rendered as empty.)
    from personal_mem.synthesis.concept_hub import _safe_hub_maps
    from personal_mem.synthesis.hub import FLAG_NEW, Hub, HubLogEntry

    cmap = _catalyst_map(catalysts)
    log = [
        HubLogEntry(
            date=today[:10],
            flag=cmap.get(src_id, ("", FLAG_NEW))[1],
            text=cmap.get(src_id, ("cluster seed", FLAG_NEW))[0] or "cluster seed",
            citation=src_id,
        )
        for src_id in cluster_source_ids
    ]
    hub = Hub(
        id=thm_id,
        title=display_title,
        essence=essence or "_Awaiting first synthesis pass._",
        log=log,
    )
    # Path-based citations so clicking a seeded source navigates to the note
    # instead of spawning a phantom stub (sources are slug-filed, so a bare
    # [[src-id]] would resolve only via the fragile alias); title aliases so the
    # log shows the headline, not an opaque src-id.
    _mint_idmap, _mint_titles, _ = _safe_hub_maps(config)
    body = hub.render(
        include_open_questions=True, idmap=_mint_idmap, title_map=_mint_titles
    )

    target_path.write_text(frontmatter_block + "\n\n" + body + "\n", encoding="utf-8")

    idx = Indexer(config=config)
    try:
        _backfill_relates_to(config, idx, cluster_source_ids, thm_id)
        if rebuild_index:
            idx.rebuild(full=False)
    finally:
        idx.close()

    _sync_registry(
        config,
        thm_id,
        slug=slug,
        concepts=list(cluster_concepts),
        parent=parent,
        project=project,
        status="active",
    )
    return target_path


def extend_theme_with_sources(
    config: Config,
    *,
    theme_id: str,
    source_ids: list[str],
    catalysts: list[dict] | None = None,
    rebuild_index: bool = True,
) -> int:
    """Attach newly-arrived sources to an existing theme.

    The *extend* path — the steady-state case where new event-grain
    sources land on an arc a theme already tracks. For each source not
    already cited:

    1. backfill ``relates_to: [theme_id]`` on the source frontmatter,
    2. add the source id to the theme's ``cites:`` frontmatter,
    3. append a catalyst-log line under ``## Catalyst log``.

    ``catalysts`` (optional) carries per-source distillations
    ``{source_id, text, flag?}`` composed by the dream theme worker; a
    source with an entry gets that text + flag as its catalyst line,
    sources without one fall back to the generic ``extend`` line
    (back-compat with legacy plans).

    Essence rewrites are NOT done here — ``/dream`` edits the essence
    directly when it judges the thesis has moved. Returns the number of
    sources actually linked (already-cited sources are skipped). Used by
    the ``/dream`` apply phase's ``theme_extensions`` plan key.
    """
    from personal_mem.core.indexer import Indexer
    from personal_mem.core.vault import parse_frontmatter, render_frontmatter

    idx = Indexer(config=config)
    linked = 0
    try:
        row = idx.db.execute(
            "SELECT path FROM notes WHERE id = ?", (theme_id,)
        ).fetchone()
        if not row:
            raise FileNotFoundError(f"theme {theme_id} not found in index")
        theme_path = config.vault_root / row["path"]
        if not theme_path.exists():
            raise FileNotFoundError(f"theme file missing: {theme_path}")

        from personal_mem.synthesis.hub import (
            FLAG_NEW,
            HubLogEntry,
            build_id_path_map,
            build_id_title_map,
        )

        fm, body = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
        cites = as_list(fm.get("cites"))
        existing = set(cites)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Path-based citations, canonical catalyst grammar. The old hand-rolled
        # `- DATE: extend … [[src]] *new*` form both diverged from the grammar
        # the Hub parser expects (so it rendered as plain text, not a log entry)
        # and used a bare [[src-id]] link that spawns phantom stubs. The title
        # alias now carries the headline, so the entry text drops the redundant
        # label and is just the bare flag verb.
        idmap = build_id_path_map(idx.db)
        title_map = build_id_title_map(idx.db)

        cmap = _catalyst_map(catalysts)
        new_lines: list[str] = []
        for src_id in source_ids:
            if src_id in existing:
                continue
            _backfill_relates_to(config, idx, [src_id], theme_id)
            cites.append(src_id)
            existing.add(src_id)
            text, flag = cmap.get(src_id, ("extend", FLAG_NEW))
            entry = HubLogEntry(
                date=today, flag=flag, text=text, citation=src_id
            )
            new_lines.append(entry.render(idmap=idmap, title_map=title_map))
            linked += 1

        if linked:
            fm["cites"] = cites
            body = _append_catalyst_lines(body, new_lines)
            theme_path.write_text(
                render_frontmatter(fm) + "\n" + body, encoding="utf-8"
            )
            if rebuild_index:
                idx.rebuild(full=False)
    finally:
        idx.close()
    return linked


def merge_theme_into(
    config: Config,
    *,
    from_id: str,
    to_id: str,
    enqueue_seam: bool = True,
    rebuild_index: bool = True,
) -> dict:
    """Merge duplicate theme ``from_id`` into survivor ``to_id``.

    The deterministic half of a theme dedup — survivor election and the
    duplicate judgment itself happen in the LLM turn (``/dream``'s merge
    worker via the ``theme_merges`` plan key, or ``/themes-resolve`` run
    by hand). Steps, mirroring the concept-hub merge (shared Hub spine):

    1. Fold the loser's catalyst log + essence into the survivor
       (:func:`personal_mem.synthesis.hub.fold_hub_logs` — interleave,
       dedup shared citations, stamp ``fold_pending_*`` provenance).
    2. Union the loser's ``cites:`` into the survivor's frontmatter and
       carry over any ``## Open questions`` content.
    3. Repoint every note whose ``relates_to:`` references the loser.
    4. Tombstone: the loser file stays on disk with
       ``status: merged-into:<to_id>`` (reversible by hand); the registry
       row is updated to match.
    5. Enqueue the survivor on the seam-link queue so the dream phase-2
       worker judges cross-parent catalyst pairs.

    Returns a stats dict (fold stats + ``cites_added`` +
    ``relates_repointed``).
    """
    from personal_mem.core.indexer import Indexer
    from personal_mem.core.vault import parse_frontmatter, render_frontmatter
    from personal_mem.synthesis.hub import (
        OPEN_QUESTIONS_HEADING,
        Hub,
        build_id_path_map,
        build_id_title_map,
        fold_hub_logs,
        replace_section_body,
        set_frontmatter_keys,
    )

    if not from_id or not to_id or from_id == to_id:
        raise ValueError("merge_theme_into needs two distinct theme ids")

    idx = Indexer(config=config)
    try:
        rows = {
            row["id"]: row["path"]
            for row in idx.db.execute(
                "SELECT id, path FROM notes WHERE type = 'theme' AND id IN (?, ?)",
                (from_id, to_id),
            )
        }
        if from_id not in rows or to_id not in rows:
            missing = [t for t in (from_id, to_id) if t not in rows]
            raise ValueError(f"unknown theme id(s): {', '.join(missing)}")
        from_path = config.vault_root / rows[from_id]
        to_path = config.vault_root / rows[to_id]
        if not from_path.exists() or not to_path.exists():
            raise ValueError("theme file missing on disk")

        idmap = build_id_path_map(idx.db)
        title_map = build_id_title_map(idx.db)
        path_to_id = {v: k for k, v in idmap.items()}

        loser = Hub.parse(from_path, hub_id=from_id, path_to_id=path_to_id)

        stats = fold_hub_logs(
            to_path,
            from_path,
            loser_id=from_id,
            path_to_id=path_to_id,
            idmap=idmap,
            title_map=title_map,
        )

        # Cites union + open-questions carry-over on the survivor (re-read:
        # the fold just rewrote the file).
        fm, body = parse_frontmatter(to_path.read_text(encoding="utf-8"))
        cites = as_list(fm.get("cites"))
        loser_fm, _ = parse_frontmatter(from_path.read_text(encoding="utf-8"))
        added = [c for c in as_list(loser_fm.get("cites")) if c not in cites]
        if added:
            fm["cites"] = cites + added
        if loser.open_questions.strip():
            existing_oq = Hub.parse(to_path, hub_id=to_id).open_questions.strip()
            merged_oq = (
                (existing_oq + "\n\n" if existing_oq else "")
                + f"*(from `{from_id}`)*\n"
                + loser.open_questions.strip()
            )
            body = replace_section_body(
                body, OPEN_QUESTIONS_HEADING, merged_oq.splitlines()
            )
        to_path.write_text(
            render_frontmatter(fm) + "\n" + body.lstrip("\n"), encoding="utf-8"
        )

        repointed = _repoint_relates_to(config, idx, from_id, to_id)

        set_frontmatter_keys(from_path, {"status": f"merged-into:{to_id}"})
        try:
            # Partial upsert — only status changes; slug/concepts/parent are
            # preserved from the existing registry row. Best-effort: registry
            # drift is repairable via `mem themes rebuild-registry`.
            from personal_mem.synthesis import theme_registry

            theme_registry.upsert(
                config, from_id, {"status": f"merged-into:{to_id}"}
            )
        except Exception:  # noqa: BLE001
            pass

        if enqueue_seam and stats.get("fold_dates"):
            from personal_mem.operations import seam_link_queue

            seam_link_queue.enqueue(
                config,
                hub_kind="theme",
                hub_id=to_id,
                folded_from=from_id,
                fold_dates=stats["fold_dates"],
                reason="theme_merged",
            )

        if rebuild_index:
            idx.rebuild(full=False)
    finally:
        idx.close()

    stats["cites_added"] = len(added)
    stats["relates_repointed"] = repointed
    return stats


def _repoint_relates_to(config, idx, from_id: str, to_id: str) -> int:
    """Rewrite ``relates_to: [from_id]`` → ``to_id`` on every referencing note.

    Candidate notes come from a coarse ``frontmatter LIKE`` scan over the
    index; each hit is precision-checked against the parsed frontmatter
    before rewriting. Returns the number of notes repointed.
    """
    from personal_mem.core.vault import parse_frontmatter, render_frontmatter

    rows = idx.db.execute(
        "SELECT id, path FROM notes WHERE frontmatter LIKE ? AND id != ?",
        (f"%{from_id}%", from_id),
    ).fetchall()
    repointed = 0
    for row in rows:
        note_path = config.vault_root / row["path"]
        if not note_path.exists():
            continue
        fm, body = parse_frontmatter(note_path.read_text(encoding="utf-8"))
        rel = as_list(fm.get("relates_to"))
        if from_id not in rel:
            continue
        new_rel = [r for r in rel if r != from_id]
        if to_id not in new_rel:
            new_rel.append(to_id)
        fm["relates_to"] = new_rel
        note_path.write_text(
            render_frontmatter(fm) + "\n" + body, encoding="utf-8"
        )
        repointed += 1
    return repointed


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _append_catalyst_lines(body: str, lines: list[str]) -> str:
    """Insert ``lines`` right after the ``## Catalyst log`` header.

    Falls back to appending a fresh section if the header is absent.
    """
    if not lines:
        return body
    block = "\n".join(lines)
    marker = "## Catalyst log"
    idx = body.find(marker)
    if idx == -1:
        return body.rstrip() + f"\n\n{marker}\n\n{block}\n"
    # Insert after the header line (and its trailing newline).
    nl = body.find("\n", idx)
    if nl == -1:
        return body + f"\n{block}\n"
    return body[: nl + 1] + "\n" + block + "\n" + body[nl + 1 :]


def _backfill_relates_to(config, idx, source_ids, thm_id) -> dict:
    """Add ``relates_to: [thm_id]`` to each source. Returns {id: title}."""
    from personal_mem.core.vault import parse_frontmatter, render_frontmatter

    titles: dict = {}
    for src_id in source_ids:
        row = idx.db.execute(
            "SELECT path, title FROM notes WHERE id = ?", (src_id,)
        ).fetchone()
        if not row:
            continue
        titles[src_id] = row["title"] or ""
        src_path = config.vault_root / row["path"]
        if not src_path.exists():
            continue
        fm, body = parse_frontmatter(src_path.read_text(encoding="utf-8"))
        rel = as_list(fm.get("relates_to"))
        if thm_id in rel:
            continue
        fm["relates_to"] = rel + [thm_id]
        # render_frontmatter ends without a trailing blank line; add one so
        # the body's first heading isn't glued to the closing ``---``.
        src_path.write_text(
            render_frontmatter(fm) + "\n" + body, encoding="utf-8"
        )
    return titles


def _sync_registry(config, thm_id, *, slug, concepts, parent, project, status):
    """Best-effort theme-registry upsert — failure must not propagate."""
    try:
        from personal_mem.synthesis import theme_registry

        theme_registry.upsert(
            config,
            thm_id,
            {
                "slug": slug,
                "status": status,
                "concepts": list(concepts),
                "parent": parent or None,
                "project": project or "",
            },
        )
    except Exception:  # noqa: BLE001
        pass


def _detect_clusters(
    sources: list[dict],
    *,
    source_type: str,
    min_cluster_size: int,
    min_shared_concepts: int,
) -> list[ClusterDescriptor]:
    """Find concept combinations that ≥``min_cluster_size`` sources share.

    Each source contributes its ``min_shared_concepts``-element concept
    subsets; a subset seen on ≥k sources triggers a cluster. The
    most-supported subsets come first; clusters with an identical source
    set are de-duplicated.
    """
    if min_shared_concepts < 1:
        raise ValueError("min_shared_concepts must be >= 1")

    from itertools import combinations

    pair_to_sources: dict[tuple[str, ...], list[dict]] = {}
    for src in sources:
        concepts = sorted(set(src["concepts"]))
        if len(concepts) < min_shared_concepts:
            continue
        for combo in combinations(concepts, min_shared_concepts):
            pair_to_sources.setdefault(combo, []).append(src)

    clusters: list[ClusterDescriptor] = []
    seen_signatures: set[tuple[str, ...]] = set()
    for combo, srcs in sorted(
        pair_to_sources.items(), key=lambda kv: (-len(kv[1]), kv[0])
    ):
        if len(srcs) < min_cluster_size:
            continue
        signature = tuple(sorted({s["id"] for s in srcs}))
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        clusters.append(
            ClusterDescriptor(
                source_type=source_type,
                source_ids=tuple(s["id"] for s in srcs),
                source_titles=tuple(s["title"] for s in srcs),
                shared_concepts=tuple(combo),
            )
        )
    return clusters
