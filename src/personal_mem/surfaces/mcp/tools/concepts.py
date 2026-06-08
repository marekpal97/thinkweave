"""``mem_concepts`` — action-dispatched (list/tighten/merge/search/source_counts/drift)."""

from __future__ import annotations

import json

from personal_mem.core._utils import as_list
from personal_mem.core.config import Config


def tool_schemas() -> list:
    from mcp.types import Tool

    return [
        Tool(
            name="mem_concepts",
            description=(
                "Unified concept tool. Action-dispatched (Phase 4 C consolidation).\n\n"
                "- `action='list'` (default): list concepts with counts. Args: prefix, min_count.\n"
                "- `action='tighten'`: find near-duplicate concept pairs (Levenshtein).\n"
                "- `action='merge'`: rename concept across all notes. Args: from_concept, to_concept.\n"
                "- `action='search'`: find notes by concepts (union/intersection). "
                "Args: concept | concepts, match_mode='any|all', min_matches, project, type, "
                "project_concepts, cooccurrence, since, until, limit.\n"
                "- `action='source_counts'`: bulk source-count for concepts. Args: concepts.\n"
                "- `action='drift'`: drift report (near-dupes + ontology candidates + "
                "optional redundant-hub pairs). Args: project, threshold, max_items, hubs, hub_jaccard.\n"
                "- `action='canonical_for'` (C19b): top-PageRank notes for a concept's "
                "induced subgraph — the canonical/most-central notes on a topic. Requires "
                "dream cycle with `dream_compute_pagerank=true`. Args: concept, limit (default 5).\n\n"
                "Concepts are domain-specific terms (e.g. write-ahead-log) — distinct from "
                "tags which are broad categories. Use action='list' BEFORE assigning concepts "
                "to new notes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list", "tighten", "merge", "search",
                            "source_counts", "drift", "canonical_for",
                        ],
                        "default": "list",
                    },
                    "prefix": {"type": "string"},
                    "min_count": {"type": "integer", "default": 1},
                    "from_concept": {"type": "string"},
                    "to_concept": {"type": "string"},
                    "concept": {"type": "string"},
                    "concepts": {"type": "array", "items": {"type": "string"}},
                    "match_mode": {"type": "string", "enum": ["any", "all"], "default": "any"},
                    "min_matches": {"type": "integer", "default": 0},
                    "project": {"type": "string"},
                    "type": {},
                    "project_concepts": {"type": "boolean", "default": False},
                    "cooccurrence": {"type": "boolean", "default": False},
                    "since": {"type": "string"},
                    "until": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                    "threshold": {"type": "integer", "default": 5},
                    "max_items": {"type": "integer", "default": 5},
                    "hubs": {"type": "boolean", "default": False},
                    "hub_jaccard": {"type": "number", "default": 0.4},
                },
            },
        ),
    ]


def handle_dispatch(cfg: Config, args: dict):
    action = args.get("action", "list")
    if action == "tighten":
        return _handle_tighten(cfg, args)
    if action == "merge":
        return _handle_merge(cfg, args)
    if action == "search":
        return handle_concept_search(cfg, args)
    if action == "source_counts":
        return _handle_source_counts(cfg, args)
    if action == "drift":
        return _handle_drift(cfg, args)
    if action == "canonical_for":
        return _handle_canonical_for(cfg, args)
    return _handle_list(cfg, args)


def _handle_canonical_for(cfg: Config, args: dict):
    """C19b — top-PageRank notes for the concept's induced subgraph.

    Returns the most central notes on a topic per PageRank computed
    during the last dream cycle (requires ``dream_compute_pagerank=true``
    config flag). Empty result when no PageRank has been computed yet
    or when the concept subgraph is too small/large/edgeless.
    """
    from mcp.types import TextContent

    from personal_mem.core.indexer import Indexer
    from personal_mem.synthesis.centrality import canonical_for

    concept = (args.get("concept") or "").strip().lower()
    if not concept:
        return [TextContent(type="text", text="concept required")]
    limit = int(args.get("limit", 5))

    idx = Indexer(config=cfg)
    try:
        rows = canonical_for(idx.db, concept, limit=limit)
    finally:
        idx.close()

    if not rows:
        return [
            TextContent(
                type="text",
                text=(
                    f"No PageRank scores for '{concept}'. Either the "
                    "dream cycle hasn't run with dream_compute_pagerank "
                    "enabled, or the concept subgraph is too small/large."
                ),
            )
        ]
    return [TextContent(type="text", text=json.dumps(rows, indent=2))]


def _handle_list(cfg: Config, args: dict):
    from collections import defaultdict

    from mcp.types import TextContent

    from personal_mem.core.indexer import Indexer

    idx = Indexer(config=cfg)
    concept_counts: dict[str, int] = defaultdict(int)
    for row in idx.db.execute("SELECT frontmatter FROM notes"):
        fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
        for c in as_list(fm.get("concepts")):
            concept_counts[c.lower()] += 1
    idx.close()

    prefix = args.get("prefix", "").lower()
    min_count = args.get("min_count", 1)

    filtered = sorted(
        ((c, n) for c, n in concept_counts.items()
         if n >= min_count and c.startswith(prefix)),
        key=lambda x: (-x[1], x[0]),
    )

    if not filtered:
        return [TextContent(type="text", text="No concepts found.")]

    lines = [f"{count:3d}  {concept}" for concept, count in filtered]
    header = f"Concepts ({len(filtered)} total):\n"
    return [TextContent(type="text", text=header + "\n".join(lines))]


def _handle_tighten(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.core.indexer import Indexer
    from personal_mem.synthesis.concepts import (
        find_near_duplicates,
        get_all_concepts,
        load_aliases,
    )

    idx = Indexer(config=cfg)
    concept_counts = get_all_concepts(idx.db)
    idx.close()

    if not concept_counts:
        return [TextContent(type="text", text="No concepts in vault.")]

    aliases = load_aliases(cfg)
    duplicates = find_near_duplicates(list(concept_counts.keys()))

    if not duplicates:
        lines = [f"No near-duplicates found among {len(concept_counts)} concepts."]
        if aliases:
            lines.append(f"{len(aliases)} canonical aliases already configured.")
        return [TextContent(type="text", text="\n".join(lines))]

    lines = [f"Found {len(duplicates)} potential duplicate(s) among {len(concept_counts)} concepts:\n"]
    for a, b, reason in duplicates:
        count_a = concept_counts.get(a, 0)
        count_b = concept_counts.get(b, 0)
        lines.append(f"  {a} ({count_a}) ↔ {b} ({count_b})  — {reason}")

    lines.append("\nTo merge, call mem_concepts_merge with from_concept and to_concept.")
    lines.append("Tip: merge the less-used concept into the more-used one.")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_merge(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.core.indexer import Indexer
    from personal_mem.synthesis.concepts import (
        load_aliases,
        merge_concept_in_notes,
        save_aliases,
    )

    from_concept = args["from_concept"].lower()
    to_concept = args["to_concept"].lower()

    if from_concept == to_concept:
        return [TextContent(type="text", text="from_concept and to_concept are the same.")]

    changed = merge_concept_in_notes(cfg.vault_root, from_concept, to_concept)

    aliases = load_aliases(cfg)
    existing_aliases = aliases.get(to_concept, [])
    if from_concept not in existing_aliases:
        existing_aliases.append(from_concept)
    if from_concept in aliases:
        for old_alias in aliases.pop(from_concept):
            if old_alias != to_concept and old_alias not in existing_aliases:
                existing_aliases.append(old_alias)
    aliases[to_concept] = existing_aliases
    save_aliases(cfg, aliases)

    idx = Indexer(config=cfg)
    idx.rebuild(full=True)
    idx.close()

    return [TextContent(
        type="text",
        text=(
            f"Merged '{from_concept}' → '{to_concept}': {changed} notes updated.\n"
            f"Alias saved. Index rebuilt."
        ),
    )]


def handle_concept_search(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.retrieval.search import Search

    s = Search(config=cfg)

    if args.get("project_concepts") and args.get("project"):
        concept_counts = s.get_project_concepts(args["project"])
        s.close()
        if not concept_counts:
            return [TextContent(type="text", text=f"No concepts in project '{args['project']}'.")]
        lines = [f"Concepts in project '{args['project']}' ({len(concept_counts)} total):", ""]
        for concept, count in sorted(concept_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {count:3d}  {concept}")
        return [TextContent(type="text", text="\n".join(lines))]

    if args.get("cooccurrence") and args.get("concept"):
        cooccur = s.get_concept_cooccurrence(args["concept"], limit=args.get("limit", 10))
        s.close()
        if not cooccur:
            return [TextContent(type="text", text=f"No co-occurring concepts for '{args['concept']}'.")]
        lines = [f"Concepts co-occurring with '{args['concept']}':", ""]
        for concept, count in cooccur:
            lines.append(f"  {count:3d}  {concept}")
        return [TextContent(type="text", text="\n".join(lines))]

    if args.get("concepts"):
        raw = args["concepts"]
        concept_list = raw if isinstance(raw, list) else [raw]
    elif args.get("concept"):
        concept_list = [args["concept"]]
    else:
        s.close()
        return [TextContent(type="text", text="Provide a concept, concepts list, or set project_concepts=true.")]

    results = s.search_by_concept(
        concept=concept_list,
        project=args.get("project", ""),
        note_type=args.get("type") or "",
        limit=args.get("limit", 20),
        match_mode=args.get("match_mode", "any"),
        min_matches=args.get("min_matches", 0),
        since=args.get("since", ""),
        until=args.get("until", ""),
    )
    s.close()

    label = concept_list[0] if len(concept_list) == 1 else f"{len(concept_list)} concepts ({args.get('match_mode', 'any')})"
    if not results:
        return [TextContent(type="text", text=f"No notes with {label}.")]

    lines = [f"Notes with {label} ({len(results)}):"]
    for r in results:
        tags = f" [{', '.join(r.tags)}]" if r.tags else ""
        lines.append(f"  [{r.type}] {r.title} ({r.id}){tags}")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_source_counts(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.retrieval.search import Search

    concepts = args.get("concepts", []) or []
    if not concepts:
        return [TextContent(type="text", text="No concepts provided.")]

    s = Search(config=cfg)
    result = s.get_concept_source_counts(concepts)
    s.close()

    lines = [f"Source counts for {len(concepts)} concept(s):", ""]
    for concept in concepts:
        entry = result.get(concept, {"count": 0, "sources": []})
        under = " **UNDER-SOURCED**" if entry["count"] < 2 else ""
        lines.append(f"## {concept} — {entry['count']} source(s){under}")
        for src in entry["sources"]:
            url = f"  <{src['url']}>" if src.get("url") else ""
            lines.append(f"  - [{src['id']}] {src['title']}{url}")
        lines.append("")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_drift(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.synthesis.concepts import drift_report, format_drift_report

    report = drift_report(
        cfg,
        project=args.get("project", ""),
        threshold=args.get("threshold", 5),
        max_items=args.get("max_items", 5),
    )
    text = format_drift_report(report)
    return [TextContent(type="text", text=text)]
