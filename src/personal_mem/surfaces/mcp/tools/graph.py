"""``mem_graph`` — filter-dispatched graph queries.

Filters: ``''`` (default outward walk), ``'source_lens'``,
``'decisions_for_file'``, ``'concept_walk'``. Each branch implements a
common-shape query but reads different inputs from ``args``.
"""

from __future__ import annotations

from personal_mem.core.config import Config


def tool_schemas() -> list:
    from mcp.types import Tool

    return [
        Tool(
            name="mem_graph",
            description=(
                "**Modality: graph (recursive CTE over typed edges).**\n\n"
                "Filter-dispatched (Phase 4 C consolidation):\n\n"
                "- `filter=''` (default): walk outward from `id` along typed edges. "
                "Optional `note_type` and `project` filter the projected nodes.\n"
                "- `filter='source_lens'`: given `source_id`, return everything that "
                "cites it, derives from it, or shares concepts with it.\n"
                "- `filter='decisions_for_file'`: given `file_path`, return every "
                "decision whose `file_paths` frontmatter touches it. "
                "Optional `project`, `status`, `limit`.\n"
                "- `filter='concept_walk'`: notes by concept set ops. Args: "
                "`concepts` (list), `match_mode='any|all'`, `min_matches`, plus filters."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "enum": ["", "source_lens", "decisions_for_file", "concept_walk"],
                        "default": "",
                    },
                    "id": {"type": "string"},
                    "depth": {"type": "integer", "default": 2},
                    "edge_types": {"type": "array", "items": {"type": "string"}},
                    "note_type": {},
                    "project": {"type": "string"},
                    "source_id": {"type": "string"},
                    "file_path": {"type": "string"},
                    "status": {"type": "string"},
                    "concepts": {"type": "array", "items": {"type": "string"}},
                    "match_mode": {"type": "string", "enum": ["any", "all"], "default": "any"},
                    "min_matches": {"type": "integer", "default": 0},
                    "since": {"type": "string"},
                    "until": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        ),
    ]


def handle_dispatch(cfg: Config, args: dict):
    flt = args.get("filter", "")
    if flt == "source_lens":
        return _handle_source_lens(cfg, args)
    if flt == "decisions_for_file":
        return _handle_decisions_for_file(cfg, args)
    if flt == "concept_walk":
        from personal_mem.surfaces.mcp.tools.concepts import handle_concept_search

        return handle_concept_search(cfg, args)
    return _handle_graph_walk(cfg, args)


def _handle_graph_walk(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.retrieval.search import Search

    s = Search(config=cfg)
    note_type = args.get("note_type") or ""
    project = args.get("project", "")

    if not note_type and not project:
        text = s.render_graph_text(args["id"], depth=args.get("depth", 2))
        s.close()
        return [TextContent(type="text", text=text)]

    nodes = s.get_related(
        args["id"],
        depth=args.get("depth", 2),
        edge_types=args.get("edge_types"),
        note_type=note_type,
        project=project,
    )
    s.close()

    if not nodes:
        return [TextContent(type="text", text=f"No connected nodes match the filters for {args['id']}.")]

    lines = [f"Graph from {args['id']} (filtered):"]
    for n in nodes:
        lines.append(f"  [{n.type}] {n.title} ({n.id})")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_source_lens(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.retrieval.search import Search

    source_id = args.get("source_id", "")
    if not source_id:
        return [TextContent(type="text", text="source_id is required.")]

    s = Search(config=cfg)
    lens = s.get_source_lens(source_id, limit=args.get("limit", 50))
    s.close()

    src = lens["source"]
    if not src:
        return [TextContent(type="text", text=f"Source note {source_id} not found.")]

    out = [
        f"# Source lens for [{src['id']}] {src['title']}",
        f"_Project: {src['project'] or '(none)'}  •  Date: {src['date'] or '?'}_",
        "",
    ]
    if src["concepts"]:
        out.append(f"**Concepts**: {', '.join(src['concepts'])}")
        out.append("")

    if lens["decisions"]:
        out.append(f"## Decisions ({len(lens['decisions'])})")
        for d in lens["decisions"]:
            out.append(f"- [{d['id']}] {d['title']} _({d['edge_type']}, {d['date']})_")
        out.append("")

    if lens["sessions"]:
        out.append(f"## Sessions ({len(lens['sessions'])})")
        for sess in lens["sessions"]:
            out.append(f"- [{sess['id']}] {sess['title']} _({sess['edge_type']}, {sess['date']})_")
        out.append("")

    other_inbound = [
        e for e in lens["inbound"]
        if e["type"] not in ("decision", "session")
    ]
    if other_inbound:
        out.append(f"## Other inbound notes ({len(other_inbound)})")
        for e in other_inbound:
            out.append(f"- [{e['type']}] [{e['id']}] {e['title']} _({e['edge_type']})_")
        out.append("")

    if lens["shared_concepts"]:
        out.append("## Concept reach")
        for concept, cnt in lens["shared_concepts"][:10]:
            out.append(f"- `{concept}` — used by {cnt} other note(s)")
        out.append("")

    if not lens["inbound"]:
        out.append("_(No inbound edges — source not yet referenced)_")

    return [TextContent(type="text", text="\n".join(out))]


def _handle_decisions_for_file(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.retrieval.search import Search

    file_path = args.get("file_path", "")
    if not file_path:
        return [TextContent(type="text", text="file_path is required.")]

    s = Search(config=cfg)
    results = s.search_decisions_by_file(
        file_path,
        project=args.get("project", ""),
        status=args.get("status", ""),
        limit=args.get("limit", 50),
    )
    s.close()

    if not results:
        return [
            TextContent(
                type="text",
                text=f"No decisions found touching `{file_path}`. (Tip: the path must match exactly as stored in decision frontmatter.)",
            )
        ]

    lines = [f"Decisions touching `{file_path}` ({len(results)}):"]
    for r in results:
        tags = f" [{', '.join(r.tags)}]" if r.tags else ""
        lines.append(f"- [{r.id}] {r.title} _({r.date})_{tags}")
    return [TextContent(type="text", text="\n".join(lines))]
