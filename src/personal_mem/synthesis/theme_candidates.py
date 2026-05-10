"""Theme-candidate generator for event-grain source types.

Deterministic clustering: when an event-grain source lands, look for
recent (≤``recent_days`` window) sources of the same source_type that
share at least ``min_shared_concepts`` concepts. If a cluster of at
least ``min_cluster_size`` such sources exists and no canonical theme
already covers them, write a candidate stub at
``vault/themes/_candidates/{cand-XXXX}-{slug}.md``.

The cluster check is pure Python — no LLM, no API call. Synthesis (the
proposed title, the essence paragraph) is deferred to
``/themes-resolve --promote``, which runs inline in Claude Code.
Candidates are stubs that capture *what is observable* (the cluster),
not *what it means* (the narrative arc).

Design choices:

- Candidates carry `cand-XXXX` IDs distinct from the canonical `thm-`
  namespace. A promotion mints a fresh `thm-` and discards the cand id.
- Aging is explicit: candidates older than ``stale_days`` without
  promotion get archived to ``vault/themes/_candidates/_archive/`` so
  the active candidate set doesn't sprawl. Archive is reversible (it's
  just a move).
- Coverage check: a cluster is "already covered" when an existing
  active theme cites at least ``min_shared_concepts`` of the cluster's
  shared concepts in its own ``concepts:`` frontmatter.
"""

from __future__ import annotations

import re
import shutil
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from personal_mem.core.config import Config


CANDIDATES_DIR_NAME = "_candidates"
CANDIDATES_ARCHIVE_NAME = "_archive"

# Defaults are intentionally conservative — the floater is supposed to
# be quiet, not noisy. Lower min_cluster_size and you flood candidates;
# raise it and you miss real arcs. 3 is the smallest "this is a thing"
# cluster size; ≤30 days of substack drains comfortably hit that bar.
DEFAULT_RECENT_DAYS = 30
DEFAULT_MIN_CLUSTER_SIZE = 3
DEFAULT_MIN_SHARED_CONCEPTS = 2
DEFAULT_STALE_DAYS = 30


def _candidates_dir(config: Config) -> Path:
    return config.vault_root / "themes" / CANDIDATES_DIR_NAME


def _archive_dir(config: Config) -> Path:
    return _candidates_dir(config) / CANDIDATES_ARCHIVE_NAME


def _slugify(text: str, *, max_len: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return (s[:max_len] or "candidate").strip("-")


def _generate_candidate_id() -> str:
    return f"cand-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class ClusterDescriptor:
    """A detected cluster: ≥N event-grain sources sharing ≥M concepts."""

    source_type: str
    source_ids: tuple[str, ...]
    source_titles: tuple[str, ...]
    shared_concepts: tuple[str, ...]


@dataclass
class CandidateOutcome:
    """Stats from one ``scan_candidates`` invocation."""

    candidates_created: list[Path] = field(default_factory=list)
    clusters_skipped_covered: int = 0
    clusters_skipped_existing_candidate: int = 0
    sources_inspected: int = 0


def scan_candidates(
    config: Config,
    *,
    source_type: str = "",
    recent_days: int = DEFAULT_RECENT_DAYS,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    min_shared_concepts: int = DEFAULT_MIN_SHARED_CONCEPTS,
    dry_run: bool = False,
) -> CandidateOutcome:
    """Scan recent event-grain sources and write candidate stubs for any
    qualifying cluster. Returns ``CandidateOutcome``.

    When ``source_type`` is given, restrict the scan to that type only —
    used by the post-ingest hook to limit work to the type that just
    landed. When empty, scan every registered event-grain source type.

    Skips:
        - Clusters where an existing canonical theme cites ≥
          ``min_shared_concepts`` of the cluster's shared concepts (the
          theme already covers it).
        - Clusters where an existing active candidate has the same
          source_type and shares ≥``min_shared_concepts`` of its
          ``cluster_concepts`` frontmatter (deduplication).
    """
    from personal_mem.core.indexer import Indexer
    from personal_mem.sources import registry as source_registry

    outcome = CandidateOutcome()

    # Determine which types to scan.
    if source_type:
        spec = source_registry.get_spec(source_type, vault_root=config.vault_root)
        if spec is None or spec.temporal_grain != "event":
            return outcome
        event_types = [spec.slug]
    else:
        event_types = [
            spec.slug
            for spec in source_registry.all_specs(vault_root=config.vault_root)
            if spec.temporal_grain == "event"
        ]

    if not event_types:
        return outcome

    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)
    cutoff_iso = cutoff.isoformat()

    idx = Indexer(config=config)
    try:
        # Recent sources of the target type(s) with their concepts.
        sources_by_type: dict[str, list[dict]] = {}
        for st in event_types:
            rows = idx.db.execute(
                """
                SELECT n.id, n.title, n.path, n.frontmatter
                FROM notes n
                WHERE n.type = 'source'
                  AND n.date >= ?
                  AND n.id IS NOT NULL
                """,
                (cutoff_iso,),
            ).fetchall()
            matches: list[dict] = []
            for row in rows:
                import json as _json

                fm = (
                    _json.loads(row["frontmatter"])
                    if row["frontmatter"] else {}
                )
                if fm.get("source_type") != st:
                    continue
                concepts = fm.get("concepts") or []
                if isinstance(concepts, str):
                    concepts = [c.strip() for c in concepts.split(",") if c.strip()]
                if not concepts:
                    continue
                matches.append(
                    {
                        "id": row["id"],
                        "title": row["title"] or "",
                        "concepts": [c.lower() for c in concepts],
                    }
                )
            sources_by_type[st] = matches
            outcome.sources_inspected += len(matches)

        # Existing canonical themes' concept coverage.
        theme_concepts: list[set[str]] = []
        theme_rows = idx.db.execute(
            """
            SELECT frontmatter FROM notes
            WHERE type = 'theme' AND id LIKE 'thm-%'
            """
        ).fetchall()
        for row in theme_rows:
            import json as _json

            fm = (
                _json.loads(row["frontmatter"])
                if row["frontmatter"] else {}
            )
            status = (fm.get("status") or "active").split(":")[0]
            if status not in ("active", "candidate"):
                continue
            concepts = fm.get("concepts") or []
            if isinstance(concepts, str):
                concepts = [c.strip() for c in concepts.split(",") if c.strip()]
            theme_concepts.append({c.lower() for c in concepts if c})
    finally:
        idx.close()

    existing_candidates = _read_existing_candidate_concepts(config)

    for st, sources in sources_by_type.items():
        if len(sources) < min_cluster_size:
            continue
        clusters = _detect_clusters(
            sources,
            source_type=st,
            min_cluster_size=min_cluster_size,
            min_shared_concepts=min_shared_concepts,
        )
        for cluster in clusters:
            cluster_concept_set = set(cluster.shared_concepts)

            # Already covered by a canonical theme?
            covered = any(
                len(cluster_concept_set & tc) >= min_shared_concepts
                for tc in theme_concepts
            )
            if covered:
                outcome.clusters_skipped_covered += 1
                continue

            # Already represented by an active candidate?
            duplicate = any(
                cand_type == st
                and len(cluster_concept_set & cand_set) >= min_shared_concepts
                for cand_type, cand_set in existing_candidates
            )
            if duplicate:
                outcome.clusters_skipped_existing_candidate += 1
                continue

            if dry_run:
                outcome.candidates_created.append(
                    Path(f"<dry-run:{st}:{','.join(cluster.shared_concepts)}>")
                )
                continue

            path = _write_candidate(config, cluster)
            outcome.candidates_created.append(path)
            existing_candidates.append((st, cluster_concept_set))

    return outcome


DORMANT_DEFAULT_STALE_DAYS = 90


def _iter_canonical_theme_paths(config: Config):
    """Yield paths to canonical themes — top-level ``*.md`` files in
    ``vault/themes/``. Candidates live in the ``_candidates/`` subdirectory
    and are excluded by non-recursive globbing.

    Theme files are named ``<slug>.md`` (the ``thm-XXXX`` ID lives in
    frontmatter, not the filename) for raw ``create_note`` themes;
    promotion-from-candidate produces ``<thm-id>-<slug>.md`` files. Both
    shapes match this glob.
    """
    themes_dir = config.vault_root / "themes"
    if not themes_dir.exists():
        return
    for path in themes_dir.glob("*.md"):
        yield path


def find_dormant_themes(
    config: Config,
    *,
    stale_days: int = DORMANT_DEFAULT_STALE_DAYS,
    today: date | None = None,
) -> list[tuple[Path, date | None]]:
    """Return canonical themes whose catalyst log hasn't moved in
    ``stale_days`` days (or never had an entry).

    Deterministic replacement for the LLM-judgment dormancy check in
    ``/themes-resolve``. Returns ``[(path, last_catalyst_date_or_None)]``
    so the caller can present a table without re-reading each theme.

    Themes already in a terminal status (``resolved`` or
    ``merged-into:thm-*``) are skipped — dormancy doesn't apply once a
    theme has stopped being active. ``today`` is the cutoff anchor;
    omit for the real current date (used by tests).
    """
    from personal_mem.synthesis.theme_hub import (
        THEME_STATUS_RESOLVED,
        last_catalyst_date,
        parse_theme,
    )

    anchor = today or date.today()
    cutoff = anchor - timedelta(days=stale_days)
    out: list[tuple[Path, date | None]] = []
    for path in _iter_canonical_theme_paths(config):
        try:
            hub = parse_theme(path)
        except Exception:
            continue
        status = hub.frontmatter.get("status", "")
        if status == THEME_STATUS_RESOLVED or status.startswith("merged-into:"):
            continue
        last = last_catalyst_date(path)
        if last is None or last < cutoff:
            out.append((path, last))
    return out


def find_resolved_themes(config: Config) -> list[tuple[Path, list[str]]]:
    """Return canonical themes whose linked decisions are all in a
    terminal state (``superseded`` or ``deprecated``).

    Walks the index ``edges`` table for ``implements`` / ``relates_to``
    edges pointing at each theme, then reads each linked decision's
    ``status`` from the notes table. A theme is "resolved" when it has at
    least one linked decision and *all* of them are in terminal status.
    Themes with zero decision links are skipped (not resolved — orphan).

    Returns ``[(path, [linked_decision_ids])]`` so the caller can show
    which decisions drove the verdict. Themes already marked ``resolved``
    or ``merged-into:thm-*`` are skipped. Deterministic replacement for
    the LLM-judgment "thesis played out" check in ``/themes-resolve``.
    """
    import json

    from personal_mem.core.indexer import Indexer
    from personal_mem.synthesis.theme_hub import THEME_STATUS_RESOLVED, parse_theme

    out: list[tuple[Path, list[str]]] = []
    idx = Indexer(config=config)
    try:
        for path in _iter_canonical_theme_paths(config):
            try:
                hub = parse_theme(path)
            except Exception:
                continue
            status = hub.frontmatter.get("status", "")
            if status == THEME_STATUS_RESOLVED or status.startswith("merged-into:"):
                continue
            theme_id = hub.frontmatter.get("id", "")
            if not theme_id:
                continue
            rows = idx.db.execute(
                """
                SELECT DISTINCT n.id, n.frontmatter
                FROM edges e
                JOIN notes n ON n.id = e.source
                WHERE e.target = ?
                  AND e.edge_type IN ('implements', 'relates_to')
                  AND n.type = 'decision'
                """,
                (theme_id,),
            ).fetchall()
            if not rows:
                continue
            linked_ids: list[str] = []
            all_terminal = True
            for row in rows:
                try:
                    fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
                except (json.JSONDecodeError, TypeError):
                    fm = {}
                dec_status = fm.get("status", "")
                linked_ids.append(row["id"])
                if dec_status not in ("superseded", "deprecated"):
                    all_terminal = False
                    break
            if all_terminal and linked_ids:
                out.append((path, linked_ids))
    finally:
        idx.close()
    return out


def archive_stale_candidates(
    config: Config,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    dry_run: bool = False,
) -> list[Path]:
    """Move candidates older than ``stale_days`` into the archive subdir.

    Returns the list of paths that were moved (or would move, when
    ``dry_run=True``). Sets nothing else; promotion via ``/themes-resolve
    --promote <cand-id>`` is the only path back from an archived
    candidate, and that's a fresh promotion not a resurrection.
    """
    cdir = _candidates_dir(config)
    if not cdir.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    moved: list[Path] = []
    for path in cdir.glob("cand-*.md"):
        try:
            stat = path.stat()
        except OSError:
            continue
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        if mtime > cutoff:
            continue
        moved.append(path)
        if dry_run:
            continue
        archive = _archive_dir(config)
        archive.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), archive / path.name)
    return moved


def promote_candidate(
    config: Config,
    candidate_id: str,
    *,
    title: str,
    essence: str = "",
    project: str = "",
    parent: str = "",
) -> Path:
    """Mint a `thm-` ID, write a canonical theme file at
    ``vault/themes/{thm-XXXX}-{slug}.md`` from the candidate, delete the
    candidate stub. Returns the new theme path.

    The caller (``/themes-resolve --promote``) supplies ``title`` and
    optionally ``essence`` after reading the candidate stub; this
    function does the file moves and frontmatter assembly only.

    When ``parent`` is given (must be a ``thm-XXXXXXXX`` id), the new
    theme is recorded as a child of that parent — reflected as a
    ``parent: thm-X`` frontmatter field. Two-tier hierarchy mirrors how
    the concept ontology nests broad → narrow.
    """
    from personal_mem.core.indexer import Indexer
    from personal_mem.core.vault import parse_frontmatter

    cdir = _candidates_dir(config)
    matches = list(cdir.glob(f"{candidate_id}-*.md"))
    if not matches:
        raise FileNotFoundError(
            f"No candidate stub found for id {candidate_id} in {cdir}"
        )
    cand_path = matches[0]
    fm, _ = parse_frontmatter(cand_path.read_text(encoding="utf-8"))

    cluster_sources = fm.get("cluster_sources") or []
    if isinstance(cluster_sources, str):
        cluster_sources = [
            s.strip() for s in cluster_sources.split(",") if s.strip()
        ]
    cluster_concepts = fm.get("cluster_concepts") or []
    if isinstance(cluster_concepts, str):
        cluster_concepts = [
            c.strip() for c in cluster_concepts.split(",") if c.strip()
        ]
    candidacy = fm.get("candidacy") or "inferred"

    thm_id = f"thm-{uuid.uuid4().hex[:8]}"
    slug = _slugify(title)
    themes_dir = config.vault_root / "themes"
    themes_dir.mkdir(parents=True, exist_ok=True)
    target_path = themes_dir / f"{thm_id}-{slug}.md"

    today = datetime.now(timezone.utc).isoformat()
    body_lines: list[str] = [
        "---",
        "type: theme",
        f"id: {thm_id}",
        f"date: \"{today}\"",
        f'title: "{title}"',
        "status: active",
        f"promoted_from: {candidate_id}",
        f"promotion_origin: {candidacy}",
    ]
    if cluster_concepts:
        body_lines.append(f"concepts: [{', '.join(cluster_concepts)}]")
    if cluster_sources:
        body_lines.append(f"cites: [{', '.join(cluster_sources)}]")
    if project:
        body_lines.append(f"project: {project}")
    if parent:
        body_lines.append(f"parent: {parent}")
    body_lines.append(f"aliases: [{thm_id}]")
    body_lines.append("---")
    body_lines.append("")
    body_lines.append(f"# {title}")
    body_lines.append("")
    body_lines.append("## Essence")
    body_lines.append("")
    body_lines.append(essence or "_Awaiting first synthesis pass._")
    body_lines.append("")
    body_lines.append("## Catalyst log")
    body_lines.append("")
    if cluster_sources:
        for src_id in cluster_sources:
            body_lines.append(f"- {today[:10]}: cluster seed [[{src_id}]] *new*")
    body_lines.append("")
    body_lines.append("## Open questions")
    body_lines.append("")

    target_path.write_text("\n".join(body_lines) + "\n", encoding="utf-8")
    cand_path.unlink()

    idx = Indexer(config=config)
    idx.rebuild(full=False)
    idx.close()

    return target_path


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _detect_clusters(
    sources: list[dict],
    *,
    source_type: str,
    min_cluster_size: int,
    min_shared_concepts: int,
) -> list[ClusterDescriptor]:
    """Find concept combinations that ≥``min_cluster_size`` sources share.

    Greedy: each source contributes pairs of its concepts; a pair seen
    on ≥k sources triggers a cluster. We pick the *most-supported*
    concept set per cluster (the largest subset of shared concepts
    common to ≥k sources), break ties on cluster size.
    """
    if min_shared_concepts < 1:
        raise ValueError("min_shared_concepts must be >= 1")

    # Map concept-pair → list of source dicts that include both.
    pair_to_sources: dict[tuple[str, ...], list[dict]] = {}
    for src in sources:
        concepts = sorted(set(src["concepts"]))
        if len(concepts) < min_shared_concepts:
            continue
        # All k-element subsets — but cap at min_shared_concepts to keep
        # combinatorics bounded; typical concept lists are <10 entries.
        from itertools import combinations

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


def _read_existing_candidate_concepts(
    config: Config,
) -> list[tuple[str, set[str]]]:
    """Return ``[(source_type, {concepts}), ...]`` for active candidates.

    Used to deduplicate against active candidates before writing a new
    one. Archive is excluded — archived candidates can re-emerge if the
    same cluster reforms.
    """
    from personal_mem.core.vault import parse_frontmatter

    cdir = _candidates_dir(config)
    if not cdir.exists():
        return []
    out: list[tuple[str, set[str]]] = []
    for path in cdir.glob("cand-*.md"):
        try:
            fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        st = fm.get("source_type") or ""
        concepts = fm.get("cluster_concepts") or []
        if isinstance(concepts, str):
            concepts = [c.strip() for c in concepts.split(",") if c.strip()]
        if not concepts:
            continue
        out.append((str(st), {c.lower() for c in concepts}))
    return out


def _write_candidate(config: Config, cluster: ClusterDescriptor) -> Path:
    cand_id = _generate_candidate_id()
    # Heuristic title from the shared concepts — promotion writes the
    # real title via /themes-resolve. The slug just needs to be readable.
    slug = _slugify("-".join(cluster.shared_concepts))
    cdir = _candidates_dir(config)
    cdir.mkdir(parents=True, exist_ok=True)
    path = cdir / f"{cand_id}-{slug}.md"

    today = datetime.now(timezone.utc).isoformat()
    lines = [
        "---",
        "type: theme",
        f"id: {cand_id}",
        f"date: \"{today}\"",
        f"source_type: {cluster.source_type}",
        f"candidacy: inferred-from-{cluster.source_type}",
        "status: candidate",
        f"cluster_size: {len(cluster.source_ids)}",
        f"cluster_sources: [{', '.join(cluster.source_ids)}]",
        f"cluster_concepts: [{', '.join(cluster.shared_concepts)}]",
        f"aliases: [{cand_id}]",
        "---",
        "",
        f"# Candidate: {' / '.join(cluster.shared_concepts)}",
        "",
        "## Cluster",
        "",
        f"Detected from {len(cluster.source_ids)} recent "
        f"`{cluster.source_type}` sources sharing concepts: "
        f"{', '.join(cluster.shared_concepts)}.",
        "",
    ]
    for sid, title in zip(cluster.source_ids, cluster.source_titles):
        title_text = title or sid
        lines.append(f"- [[{sid}]] — {title_text}")
    lines.append("")
    lines.append(
        "Promote with `/themes-resolve --promote " + cand_id + "` if this "
        "represents a real narrative arc; otherwise leave to age out, or "
        "delete the file."
    )
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
