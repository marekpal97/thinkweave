"""``mem_search`` / ``mem_context`` / ``mem_timeline`` / ``mem_project_snapshot``."""

from __future__ import annotations

from personal_mem.core.config import Config


def tool_schemas() -> list:
    from mcp.types import Tool

    return [
        Tool(
            name="mem_search",
            description=(
                "**Modality: FTS / similarity / fused (composition).**\n\n"
                "Retrieval over note bodies. Three modes:\n\n"
                "- **fts** (default): SQLite FTS5 full-text. Best when you know keywords.\n"
                "- **similar**: cosine over OpenAI embeddings (requires "
                "`mem index --embed` + OPENAI_API_KEY). Best when you don't know exact keywords.\n"
                "- **hybrid**: FTS + similarity fused via reciprocal rank fusion "
                "(k=60). Safe default when uncertain — falls back gracefully.\n\n"
                "List mode: empty `query` returns date-sorted recent notes honouring "
                "all filters. Useful for 'all notes in project X this week'.\n\n"
                "Always search FIRST before creating notes (deduplication).\n\n"
                "Filters:\n"
                "- `type`: single string OR list (e.g. `['source','session','theme']`). "
                'Valid types: "note", "session", "decision", "source", "theme".\n'
                "- `project`: project name (empty = cross-project).\n"
                "- `tags`: broad categories (todo, parked, probe, theme, trade, til, ...).\n"
                "- `concepts`: domain-specific concepts; result must include ≥1.\n"
                "- `since` / `until`: ISO date strings (YYYY-MM-DD); date-window filter."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query. Empty = date-sorted list mode."},
                    "mode": {
                        "type": "string",
                        "enum": ["fts", "similar", "hybrid"],
                        "default": "fts",
                        "description": "fts = keyword, similar = semantic, hybrid = RRF fusion.",
                    },
                    "type": {"description": "Note type filter. String or list. Valid: note, session, decision, source, theme."},
                    "project": {"type": "string", "description": "Filter by project name. Empty = cross-project."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Filter to notes containing ALL of these tags."},
                    "concepts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filter to notes that include ≥1 of these concepts. "
                            "Combine with `query` for text+concept queries."
                        ),
                    },
                    "since": {"type": "string", "description": "Earliest ISO date (YYYY-MM-DD). Optional."},
                    "until": {"type": "string", "description": "Latest ISO date (YYYY-MM-DD). Optional."},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="mem_context",
            description=(
                "**Modality: composition (FTS → similarity-via-concept → recency).**\n\n"
                "Three-layer retrieval, deduplicated. Use when you want a budgeted "
                "blob of relevant notes for a topic, not raw hits.\n\n"
                "Call at session start or before major decisions to ground work in "
                "existing knowledge. When `concepts` is provided, layer 2 expands "
                "from those instead of FTS hits — useful when you know the domain.\n\n"
                "`since` / `until` apply uniformly across all three layers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Filter by project name."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Filter to notes containing ALL of these tags."},
                    "query": {"type": "string", "description": "Relevance query to bias results toward a topic."},
                    "concepts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Concepts to retrieve by. When provided, drives layer 2 directly instead of expanding from FTS hits.",
                    },
                    "type": {"description": "Filter across all three layers. String or list — e.g. ['note','decision','theme']."},
                    "since": {"type": "string", "description": "Earliest ISO date (YYYY-MM-DD)."},
                    "until": {"type": "string", "description": "Latest ISO date (YYYY-MM-DD)."},
                    "limit": {"type": "integer", "default": 5},
                },
            },
        ),
        Tool(
            name="mem_timeline",
            description=(
                "Project evolution across sessions. Two shapes:\n\n"
                "- **With `project`**: chronological detail for that project — "
                "per-session files, commits, test runs, and linked decisions. "
                "Use for onboarding, code review context, post-mortems.\n\n"
                "- **Without `project`**: cross-project activity ranking — "
                "`{project: (sessions, decisions, latest_date)}` sorted by "
                "total activity. Sessions lacking a `project` frontmatter "
                "land in the `_unscoped` bucket. Use to pick the top "
                "active projects in one call (e.g. /discover step 3.1)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project to summarize. Omit for cross-project ranking."},
                    "days": {"type": "integer", "default": 7, "description": "How many days back to look."},
                },
                "required": [],
            },
        ),
        Tool(
            name="mem_project_snapshot",
            description=(
                "On-demand structured project overview — the same payload that the "
                "SessionStart hook injects at session startup.\n\n"
                "Sections: header, MCP tools manifest, last 5 wrapped sessions, "
                "state-of-play landing doc, backlog open items, recent decisions, "
                "open probes, concept histogram, recent sources, retrieval hints.\n\n"
                "Use mid-session when context is fading or to get an overview of "
                "a different project without switching CWDs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project slug."},
                    "sections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Subset of sections to include. Default: all. "
                            "Valid keys: header, tools, sessions, state, backlog, "
                            "decisions, probes, concepts, sources, footer."
                        ),
                    },
                    "budget_tokens": {"type": "integer", "default": 8000, "description": "Soft token budget. Whole sections are dropped if exceeded."},
                },
                "required": ["project"],
            },
        ),
    ]


def handle_search(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.retrieval.search import Search

    s = Search(config=cfg)

    mode = args.get("mode", "fts")
    type_arg = args.get("type") or ""
    query = args.get("query", "")
    project = args.get("project", "")
    limit = args.get("limit", 10)

    if mode == "similar":
        results = s.similar(query, project=project, note_type=type_arg, limit=limit)
        if not results:
            s.close()
            msg = (
                "No semantic results — either the embeddings DB is missing "
                "(run `mem index --embed` with OPENAI_API_KEY set) or no "
                "matches above the cosine threshold."
            )
            return [TextContent(type="text", text=msg)]
    elif mode == "hybrid":
        results = s.hybrid_search(query, project=project, note_type=type_arg, limit=limit)
    else:
        results = s.search(
            query=query,
            note_type=type_arg,
            project=project,
            tags=args.get("tags"),
            concepts=args.get("concepts"),
            since=args.get("since", ""),
            until=args.get("until", ""),
            limit=limit,
        )
    s.close()

    if not results:
        return [TextContent(type="text", text="No results found.")]

    lines = []
    for r in results:
        tags = f" [{', '.join(r.tags)}]" if r.tags else ""
        lines.append(f"[{r.type}] {r.title} ({r.id}){tags}")
        if r.snippet:
            lines.append(f"  {r.snippet}")
    return [TextContent(type="text", text="\n".join(lines))]


def handle_context(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.retrieval.search import Search

    s = Search(config=cfg)
    results = s.get_context(
        project=args.get("project", ""),
        tags=args.get("tags"),
        query=args.get("query", ""),
        concepts=args.get("concepts"),
        limit=args.get("limit", 5),
        note_type=args.get("type") or "",
        since=args.get("since", ""),
        until=args.get("until", ""),
    )
    s.close()

    if not results:
        return [TextContent(type="text", text="No context available.")]

    lines = []
    for r in results:
        tags = f" [{', '.join(r.tags)}]" if r.tags else ""
        lines.append(f"[{r.type}] {r.title} ({r.id}){tags}")
    return [TextContent(type="text", text="\n".join(lines))]


def handle_timeline(cfg: Config, args: dict):
    from datetime import date, timedelta

    from mcp.types import TextContent

    from personal_mem.core.schemas import NoteType
    from personal_mem.core.vault import VaultManager
    from personal_mem.retrieval.search import Search

    project = args.get("project", "") or ""
    days = args.get("days", 7)

    s = Search(config=cfg)

    if not project:
        ranking = s.get_cross_project_activity(days=days)
        s.close()
        if not ranking:
            return [TextContent(type="text", text=f"No session or decision activity in the last {days} days.")]
        lines = [f"Cross-project activity (last {days} days, {len(ranking)} projects)", ""]
        for entry in ranking:
            lines.append(
                f"- {entry['project']} — {entry['sessions']} sessions, "
                f"{entry['decisions']} decisions "
                f"(latest: {entry['latest_date'][:10] or '?'})"
            )
        return [TextContent(type="text", text="\n".join(lines))]

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    vm = VaultManager(config=cfg)

    sessions = []
    for note in vm.list_notes(note_type=NoteType.SESSION, limit=100):
        if note.project == project and note.date >= cutoff:
            sessions.append(note)

    sessions.sort(key=lambda n: n.date)

    if not sessions:
        s.close()
        return [TextContent(type="text", text=f"No sessions found for project '{project}' in the last {days} days.")]

    lines = []
    for sess in sessions:
        fm = sess.frontmatter
        files = fm.get("files_touched", [])
        commits = fm.get("commits", [])
        test_runs = fm.get("test_runs", [])
        processed = fm.get("processed", False)

        lines.append(f"## {sess.date} — {sess.title} ({sess.id})")

        if files:
            lines.append(f"Files: {', '.join(files[:5])}"
                         + (f" (+{len(files)-5} more)" if len(files) > 5 else ""))
        if commits:
            for c in commits:
                msg = c.get("message", "")
                h = c.get("hash", "?")
                lines.append(f"Commit: {h} \"{msg}\"")
        if test_runs:
            for t in test_runs:
                p = t.get("passed", 0)
                f_ = t.get("failed", 0)
                lines.append(f"Tests: {p} passed, {f_} failed")

        decisions = []
        for note in vm.list_notes(note_type=NoteType.DECISION, limit=50):
            if note.frontmatter.get("source_session") == sess.id:
                decisions.append(note)

        if decisions:
            lines.append("Decisions:")
            for dec in decisions:
                dfm = dec.frontmatter
                verdict = dfm.get("verdict", "")
                conf = dfm.get("confidence", "")
                status = dfm.get("status", "proposed")
                verdict_str = f" ({verdict}, {conf})" if verdict else ""
                lines.append(f"  - [{status}] {dec.title}{verdict_str}")

        if not processed:
            lines.append("  ⚠ Session not yet processed")
        lines.append("")

    s.close()
    header = f"Timeline: {project} (last {days} days, {len(sessions)} sessions)\n\n"
    return [TextContent(type="text", text=header + "\n".join(lines))]


def handle_project_snapshot(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.retrieval.context import build_project_context

    project = args.get("project", "")
    if not project:
        return [TextContent(type="text", text="project is required.")]

    payload = build_project_context(
        cfg,
        project,
        sections=args.get("sections"),
        budget_tokens=args.get("budget_tokens", 8000),
    )
    return [TextContent(type="text", text=payload)]
