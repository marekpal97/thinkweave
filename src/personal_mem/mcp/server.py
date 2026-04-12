"""MCP server for personal_mem — 8 tools for any agent to interact with the vault.

Run: python -m personal_mem.mcp.server
Transport: stdio

Requires: pip install personal-mem[mcp]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def _parse_candidate_insights(body: str) -> list[dict]:
    """Parse ## Candidate Insights section into structured insights.

    Each blank-line-separated block becomes an insight.
    First non-empty line of a block is the title; rest is the body.
    """
    insights: list[dict] = []
    in_section = False
    current_block: list[str] = []

    for line in body.split("\n"):
        if line.strip().startswith("## Candidate Insights"):
            in_section = True
            continue
        if in_section and line.strip().startswith("## "):
            break
        if in_section:
            stripped = line.strip()
            if not stripped and current_block:
                _flush_insight(current_block, insights)
                current_block = []
            elif stripped:
                current_block.append(line)

    if current_block:
        _flush_insight(current_block, insights)

    return insights


def _flush_insight(lines: list[str], insights: list[dict]) -> None:
    """Convert a block of lines into an insight dict."""
    if not lines:
        return
    title_line = lines[0].strip().lstrip("-#*").strip()
    # Strip ★ Insight markers and dash lines
    title_line = re.sub(r"^★\s*Insight[─ ]*", "", title_line).strip()
    if not title_line or all(c in "─-=" for c in title_line):
        # Title was just a marker/separator — use first content line instead
        for line in lines[1:]:
            candidate = line.strip().lstrip("-#*").strip()
            candidate = re.sub(r"^★\s*Insight[─ ]*", "", candidate).strip()
            if candidate and not all(c in "─-=" for c in candidate):
                title_line = candidate
                break
    if not title_line:
        return
    body = "\n".join(lines[1:]).strip() if len(lines) > 1 else title_line
    # Strip closing dash lines from body
    body = re.sub(r"\n[─-]{3,}\s*$", "", body).strip()
    insights.append({"title": title_line, "body": body or title_line})


def _append_to_section(path: Path, section_header: str, content: str) -> None:
    """Append content under a specific section in a markdown file."""
    text = path.read_text(encoding="utf-8")
    if section_header in text:
        idx = text.index(section_header) + len(section_header)
        nl = text.index("\n", idx)
        text = text[: nl + 1] + content + "\n" + text[nl + 1 :]
    else:
        text = text.rstrip() + f"\n\n{section_header}\n{content}\n"
    path.write_text(text, encoding="utf-8")


def _strip_section(body: str, heading: str) -> str:
    """Remove a markdown section — delegates to vault.strip_section."""
    from personal_mem.vault import strip_section

    return strip_section(body, heading)


def main() -> None:
    try:
        import mcp.server.stdio
        from mcp.server import NotificationOptions, Server
        from mcp.server.models import InitializationOptions
        from mcp.types import TextContent, Tool
    except ImportError:
        print("MCP server requires: pip install personal-mem[mcp]", file=sys.stderr)
        sys.exit(1)

    import asyncio

    from personal_mem.config import load_config
    from personal_mem.indexer import Indexer
    from personal_mem.schemas import EdgeType, NoteMeta, NoteType
    from personal_mem.search import Search
    from personal_mem.vault import VaultManager, parse_frontmatter, render_frontmatter

    server = Server("personal-mem")
    cfg = load_config()

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="mem_search",
                description=(
                    "Search the knowledge vault. Supports three modes:\n\n"
                    "- **fts** (default): SQLite FTS5 full-text search. Fast, exact. "
                    "Best when you know keywords.\n"
                    "- **similar**: Semantic search via cached embeddings (requires "
                    "`mem index --embed` + OPENAI_API_KEY). Best when you don't know "
                    "exact keywords — finds ideas by meaning.\n"
                    "- **hybrid**: Fuses FTS + semantic via reciprocal rank fusion "
                    "(k=60). Best default for exploration — gracefully falls back to "
                    "FTS if embeddings aren't configured.\n\n"
                    "Use this FIRST before creating notes to check if similar knowledge "
                    "already exists. Deduplication is critical — search before you create.\n\n"
                    "Filters:\n"
                    "- `type`: single note type string OR a list (e.g. `['source','session']`). "
                    'Valid types: "note", "session", "decision", "source".\n'
                    "- `project`: project name (empty = cross-project).\n"
                    "- `tags`: broad categories (debugging, performance, todo, til).\n\n"
                    "Empty `query` + filters returns date-sorted recent notes."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query. Empty string returns date-sorted recent notes.",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["fts", "similar", "hybrid"],
                            "default": "fts",
                            "description": (
                                "Search mode. 'fts' = keyword (default, back-compat), "
                                "'similar' = semantic only, 'hybrid' = RRF fusion of both."
                            ),
                        },
                        "type": {
                            "description": (
                                "Note type filter. String or list of strings: 'note' (reusable knowledge), "
                                "'session' (work logs), 'decision' (architectural choices), 'source' (external references)."
                            ),
                        },
                        "project": {
                            "type": "string",
                            "description": "Filter by project name. Empty = cross-project.",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Filter to notes containing ALL of these tags.",
                        },
                        "limit": {"type": "integer", "default": 10},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="mem_create",
                description=(
                    "Create a new note in the knowledge vault. Always mem_search first to avoid "
                    "duplicates.\n\n"
                    "Note types and WHEN to use each:\n"
                    '- "note": Reusable knowledge, patterns, gotchas, how-tos. The default. '
                    "Use for insights useful beyond this session.\n"
                    '- "decision": Architectural or design choices with a lifecycle '
                    "(proposed -> accepted -> deprecated -> superseded). Use ONLY for choices that "
                    "constrain future work. Include Context, Decision, and Consequences sections "
                    'in the body. Always starts as "proposed".\n'
                    '- "session": Work session logs. Normally auto-created by hooks — only create '
                    "manually if hooks are not installed.\n"
                    '- "source": External references (articles, podcasts, papers). '
                    "Set source_type, url, and authors in frontmatter.\n\n"
                    "Linking guidance (set via frontmatter field):\n"
                    "- derived_from: [session-id] — when extracting knowledge from a session\n"
                    "- builds_on: [note-id] — when extending existing knowledge\n"
                    "- supersedes: [note-id] — when replacing outdated knowledge\n"
                    "- implements: [decision-id] — when code/config implements a decision\n"
                    "- cites: [source-id] — when referencing external material\n"
                    "- concepts: [list] — domain-specific technical terms for thematic "
                    "graph linking (e.g. [\"write-ahead-log\", \"fts5\"]). Notes sharing 2+ "
                    "concepts are auto-linked. Call mem_concepts first to reuse existing labels.\n\n"
                    "Tags vs concepts:\n"
                    "- tags: broad categories for filtering and organization "
                    "(e.g. \"debugging\", \"performance\", \"todo\", \"til\")\n"
                    "- concepts: precise technical vocabulary for knowledge graph edges "
                    "(e.g. \"write-ahead-log\", \"sqlite-wal\", \"recursive-cte\")\n"
                    "Do not duplicate between them — a term belongs in one or the other.\n\n"
                    "Source type conventions for type=\"source\": article, paper, podcast, video, "
                    "github, book, tweet, transcript. Sources can be project-scoped or global."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["note", "session", "decision", "source"],
                            "description": (
                                'Note type. "note" for reusable knowledge (default choice). '
                                '"decision" for architectural choices with lifecycle tracking. '
                                '"session" for work logs (usually auto-created). '
                                '"source" for external references.'
                            ),
                        },
                        "title": {"type": "string"},
                        "body": {"type": "string", "description": "Markdown body content."},
                        "project": {"type": "string"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Broad categories for filtering (e.g. debugging, performance, "
                                "todo, til). NOT for technical terms — use concepts in "
                                "frontmatter for those."
                            ),
                        },
                        "frontmatter": {
                            "type": "object",
                            "description": (
                                "Additional frontmatter fields. Use for edge declarations: "
                                "derived_from (list of source IDs), builds_on (list of note IDs), "
                                "supersedes (list of note IDs), implements (list of decision IDs), "
                                "cites (list of source IDs). For decisions: status is auto-set to "
                                '"proposed". For sources: set source_type, url, authors.'
                            ),
                        },
                        "session_id": {
                            "type": "string",
                            "description": (
                                "Optional session ID — either a session note ID "
                                "(e.g. ses-a1b2c3d4) or CLAUDE_SESSION_ID UUID. "
                                "Places the note inside that session's folder, "
                                "creating it eagerly if needed. "
                                "If omitted, standalone notes go to sessions/misc/."
                            ),
                        },
                    },
                    "required": ["type", "title"],
                },
            ),
            Tool(
                name="mem_read",
                description=(
                    "Read the full markdown content of a note by its ID "
                    '(e.g. "n-a1b2c3d4", "dec-e5f6g7h8").\n\n'
                    "Returns the complete file including YAML frontmatter and body. "
                    "Use after mem_search to inspect a note's full content before deciding "
                    "to link, update, or extract from it."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Note ID (e.g. 'n-a1b2c3d4') or relative vault path.",
                        },
                    },
                    "required": ["id"],
                },
            ),
            Tool(
                name="mem_link",
                description=(
                    "Create a typed directed edge between two notes in the knowledge graph.\n"
                    "Prefer setting edges via frontmatter fields in mem_create when possible. "
                    "Use mem_link for edges discovered after creation.\n\n"
                    "Edge types and when to use:\n"
                    '- "derived_from": Note was extracted/distilled from source note '
                    "(e.g. insight from session)\n"
                    '- "builds_on": Note extends or refines the target\'s knowledge\n'
                    '- "supersedes": Note replaces outdated target note\n'
                    '- "implements": Note describes implementation of a decision\n'
                    '- "relates_to": General topical relationship (weakest — use sparingly)\n'
                    '- "cites": Note references an external source note'
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string", "description": "ID of the note that has the relationship."},
                        "target_id": {"type": "string", "description": "ID of the note being referenced."},
                        "edge_type": {
                            "type": "string",
                            "enum": [e.value for e in EdgeType],
                            "description": (
                                "Relationship type. Use derived_from for extraction lineage, "
                                "builds_on for refinement, supersedes for replacement, "
                                "implements for decision realization, cites for references, "
                                "relates_to only as a last resort."
                            ),
                        },
                    },
                    "required": ["source_id", "target_id", "edge_type"],
                },
            ),
            Tool(
                name="mem_context",
                description=(
                    "Get the most relevant knowledge notes for a given task context.\n"
                    "Uses three-layer retrieval: FTS search, concept expansion, then "
                    "recency supplement.\n\n"
                    "Call at session start or before major decisions to ground work in "
                    "existing knowledge. Helps avoid re-discovering known patterns or "
                    "contradicting past decisions.\n\n"
                    "When concepts are provided, skips FTS and retrieves directly by "
                    "concept — useful when you know the domain you're working in."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Filter by project name."},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Filter to notes containing ALL of these tags.",
                        },
                        "query": {
                            "type": "string",
                            "description": "Relevance query to bias results toward a topic.",
                        },
                        "concepts": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Concepts to retrieve by (e.g. ['pytorch', 'neural-networks']). "
                                "When provided, uses concept-based retrieval instead of FTS."
                            ),
                        },
                        "type": {
                            "description": (
                                "Filter to specific note types. String or list of strings "
                                "(e.g. 'source' or ['source','session']). Applies across "
                                "all 3 retrieval layers."
                            ),
                        },
                        "limit": {"type": "integer", "default": 5},
                    },
                },
            ),
            Tool(
                name="mem_graph",
                description=(
                    "Traverse the knowledge graph outward from a note, showing connected "
                    "notes and their edge types.\n\n"
                    "Use to understand how a note relates to the broader knowledge base. "
                    "Useful before creating links to see what connections already exist."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Starting note ID."},
                        "depth": {"type": "integer", "default": 2, "description": "How many hops to traverse."},
                        "edge_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Filter to only these edge types (e.g. [\"derived_from\", \"builds_on\"]).",
                        },
                    },
                    "required": ["id"],
                },
            ),
            Tool(
                name="mem_update",
                description=(
                    "Update an existing note's frontmatter or append to its body.\n\n"
                    "Common uses:\n"
                    '- Change decision status: frontmatter={"status": "accepted"}\n'
                    "  Valid statuses: proposed -> accepted -> deprecated -> superseded\n"
                    '- Add tags: frontmatter={"tags": ["new-tag"]} (merges with existing)\n'
                    '- Remove tags: remove_tags=["todo"] (removes from tag list)\n'
                    '- Add edge: frontmatter={"derived_from": ["ses-xxx"]} (merges with existing)\n'
                    '- Append content: body_append="## New Section\\nContent here."\n\n'
                    "Lists in frontmatter are merged (no duplicates). Scalars are overwritten.\n"
                    "The note is re-indexed automatically after update."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Note ID to update (e.g. 'dec-a1b2c3d4').",
                        },
                        "frontmatter": {
                            "type": "object",
                            "description": (
                                "Frontmatter fields to update. Lists are merged, scalars overwritten. "
                                "Common: status, tags, derived_from, builds_on, supersedes."
                            ),
                        },
                        "remove_tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Tags to remove from the note (e.g. [\"todo\"]). "
                                "Use to mark follow-ups as done."
                            ),
                        },
                        "body_append": {
                            "type": "string",
                            "description": "Markdown text to append to the note body.",
                        },
                    },
                    "required": ["id"],
                },
            ),
            Tool(
                name="mem_extract",
                description=(
                    "Extract structured knowledge and decisions from a session.\n\n"
                    "Creates knowledge notes and decision notes inside the session folder "
                    "with derived_from links, writes a summary, strips raw event logs, "
                    "archives buffer as events.jsonl, and marks the session processed.\n\n"
                    "Call at the end of a productive work session. Provide curated "
                    "insights and decisions for best results.\n\n"
                    "QUALITY GUIDANCE:\n"
                    "- Insights should capture personal experience and context, not "
                    "restate textbook facts. Include what surprised you, what went wrong, "
                    "and non-obvious implications.\n"
                    "- Decisions need substantive Context sections explaining the problem "
                    "and alternatives considered, not just the conclusion.\n"
                    "- Both successful AND abandoned approaches should be recorded "
                    "(no survivorship bias).\n\n"
                    "If the session has auto_extracted=true (from Stop hook), use "
                    "force=true to enrich it with LLM-generated insights and decisions.\n\n"
                    "IMPORTANT: Every insight and decision MUST include concepts (min 2). "
                    "Notes with <2 concepts cannot auto-link in the knowledge graph. "
                    "Call mem_concepts first to load existing labels and reuse them."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": (
                                "Session note ID (e.g. 'ses-a1b2c3d4') or CLAUDE_SESSION_ID. "
                                "If no matching session note exists, one is auto-created."
                            ),
                        },
                        "summary": {
                            "type": "string",
                            "description": (
                                "2-3 sentence summary of what was accomplished. "
                                "If omitted, auto-generated from extracted notes."
                            ),
                        },
                        "insights": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "body": {"type": "string", "description": "Markdown body for the note."},
                                    "tags": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "concepts": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": (
                                            "REQUIRED. Domain-specific technical terms for graph linking "
                                            "(e.g. write-ahead-log, recursive-cte). Minimum 2 concepts "
                                            "per insight — notes with <2 concepts cannot auto-link in "
                                            "the knowledge graph. Call mem_concepts first to reuse "
                                            "existing labels."
                                        ),
                                    },
                                },
                                "required": ["title", "body", "concepts"],
                            },
                            "description": (
                                "Knowledge to extract as notes. Max 3 — quality over quantity. "
                                "Each becomes a note with derived_from link to this session. "
                                "Every insight MUST include concepts (min 2) for graph connectivity. "
                                "If omitted, parses ## Candidate Insights from the session body."
                            ),
                        },
                        "decisions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "rationale": {
                                        "type": "string",
                                        "description": "WHY this change was made — the reasoning behind the decision.",
                                    },
                                    "file_paths": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Files affected by this decision.",
                                    },
                                    "outcome": {
                                        "type": "string",
                                        "enum": ["committed", "abandoned", "partial"],
                                        "description": "Was this change committed, abandoned, or partially done?",
                                    },
                                    "tags": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Broad categories (e.g. refactor, bugfix, performance).",
                                    },
                                    "summary": {
                                        "type": "string",
                                        "description": (
                                            "One-sentence summary of the decision. "
                                            "Used in DECISIONS.md landing page. Keep concise."
                                        ),
                                    },
                                    "concepts": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": (
                                            "REQUIRED. Domain-specific technical terms for graph linking "
                                            "(e.g. write-ahead-log, recursive-cte). Minimum 2 concepts "
                                            "per decision — decisions with <2 concepts cluster separately "
                                            "from the knowledge graph. Call mem_concepts first to reuse "
                                            "existing labels."
                                        ),
                                    },
                                    "supersedes": {
                                        "type": "string",
                                        "description": "ID of decision this replaces.",
                                    },
                                    "cites": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Source note IDs that informed this decision.",
                                    },
                                    "plan_ref": {
                                        "type": "string",
                                        "description": (
                                            "Which plan item this decision implements "
                                            "(e.g. 'Step 3: Replace auth middleware')."
                                        ),
                                    },
                                },
                                "required": ["title", "rationale", "outcome", "concepts"],
                            },
                            "description": (
                                "Significant decisions from this session — both successful and "
                                "abandoned. Include rationale (WHY), affected files, and outcome. "
                                "Focus on decisions that matter for project evolution. "
                                "Typical sessions have 2-5 decisions, but include all that are significant."
                            ),
                        },
                        "project": {
                            "type": "string",
                            "description": (
                                "Project name. Required when no session note exists yet "
                                "(e.g. non-code conversations). Ignored if session already exists."
                            ),
                        },
                        "plan_path": {
                            "type": "string",
                            "description": (
                                "File path of the plan used during this session. "
                                "Stored in session context.plan for traceability."
                            ),
                        },
                        "plan_summary": {
                            "type": "string",
                            "description": (
                                "Brief summary of the plan's main tasks/items (2-5 lines). "
                                "Stored alongside plan_path in session context.plan."
                            ),
                        },
                        "force": {
                            "type": "boolean",
                            "description": "Re-extract even if session is already marked processed.",
                        },
                    },
                    "required": ["session_id"],
                },
            ),
            Tool(
                name="mem_judge",
                description=(
                    "Evaluate decision notes based on downstream evidence.\n\n"
                    "Updates verdict (kept/superseded/reverted/unknown) and confidence "
                    "score on decision frontmatter. No LLM — pure graph traversal and "
                    "git state checks.\n\n"
                    "Use after extraction to assess which decisions held up, or any time "
                    "later to reconcile with post-session events (commits that happened "
                    "after the session, files that were reverted, etc.).\n\n"
                    "Evaluation logic:\n"
                    "- committed + tests pass → kept (0.9)\n"
                    "- re-edited by later decision → superseded (0.7)\n"
                    "- committed, files deleted → reverted (0.6)\n"
                    "- committed, not tested → kept (0.6)\n"
                    "- not committed → unknown (0.0)"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Evaluate all decisions derived from this session.",
                        },
                        "decision_id": {
                            "type": "string",
                            "description": "Evaluate a single decision by ID.",
                        },
                        "project": {
                            "type": "string",
                            "description": "Evaluate all decisions in a project.",
                        },
                    },
                },
            ),
            Tool(
                name="mem_unlink",
                description=(
                    "Remove a typed edge between two notes.\n"
                    "Use to correct wrong links or clean up stale edges."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "target_id": {"type": "string"},
                        "edge_type": {
                            "type": "string",
                            "enum": [e.value for e in EdgeType],
                        },
                    },
                    "required": ["source_id", "target_id", "edge_type"],
                },
            ),
            Tool(
                name="mem_concepts",
                description=(
                    "List all concepts in the vault with note counts.\n\n"
                    "Concepts are domain-specific technical terms (e.g. write-ahead-log, "
                    "recursive-cte) — distinct from tags which are broad categories "
                    "(e.g. debugging, todo). Use BEFORE assigning concepts to new notes — "
                    "reuse existing labels for consistency. Notes sharing 2+ concepts are "
                    "auto-linked in the knowledge graph."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "prefix": {
                            "type": "string",
                            "description": "Filter concepts starting with this prefix.",
                        },
                        "min_count": {
                            "type": "integer",
                            "default": 1,
                            "description": "Minimum note count to include.",
                        },
                    },
                },
            ),
            Tool(
                name="mem_concepts_tighten",
                description=(
                    "Find near-duplicate concepts that may need merging.\n\n"
                    "Uses string similarity (edit distance, substring, stem matching) "
                    "to identify concept pairs that likely refer to the same thing. "
                    "Returns suggested merges — does not auto-apply.\n\n"
                    "Call periodically to keep the concept vocabulary clean."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="mem_concepts_merge",
                description=(
                    "Merge one concept into another across all vault notes.\n\n"
                    "Renames `from_concept` to `to_concept` in every note's frontmatter "
                    "and updates the aliases file so the old name auto-resolves in future.\n\n"
                    "Use after reviewing mem_concepts_tighten suggestions."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "from_concept": {
                            "type": "string",
                            "description": "The concept to rename/remove.",
                        },
                        "to_concept": {
                            "type": "string",
                            "description": "The canonical concept to merge into.",
                        },
                    },
                    "required": ["from_concept", "to_concept"],
                },
            ),
            Tool(
                name="mem_concept_search",
                description=(
                    "Find notes by one or more concepts, with intersection or union semantics.\n\n"
                    "- `concept` (single str) or `concepts` (list) — accepts either.\n"
                    "- `match_mode='any'` (default, union) — matches notes touching any of the concepts.\n"
                    "- `match_mode='all'` (intersection) — matches notes touching every concept. "
                    "Set `min_matches=N` to require at least N (partial intersection).\n\n"
                    "`project` is optional — omit to search cross-project. `type` accepts a string "
                    "or a list of types.\n\n"
                    "Also supports listing all concepts for a project (omit concept, provide project, "
                    "set `project_concepts=true`) or finding co-occurring concepts (`cooccurrence=true`).\n\n"
                    "Example: `concepts=['machine-learning','pytorch'], match_mode='all', "
                    "type=['source','session']` → all source+session notes touching both concepts."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "concept": {
                            "type": "string",
                            "description": "Single concept. Use `concepts` list for multi-concept.",
                        },
                        "concepts": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of concepts (multi-concept search).",
                        },
                        "match_mode": {
                            "type": "string",
                            "enum": ["any", "all"],
                            "default": "any",
                            "description": (
                                "'any' = union (default, matches any concept). "
                                "'all' = intersection (matches every concept)."
                            ),
                        },
                        "min_matches": {
                            "type": "integer",
                            "default": 0,
                            "description": (
                                "Minimum distinct concepts a note must match (only used in "
                                "match_mode='all'). 0 = require all concepts in the list."
                            ),
                        },
                        "project": {
                            "type": "string",
                            "description": "Filter by project. Empty = cross-project.",
                        },
                        "type": {
                            "description": "Note type filter — string or list of strings.",
                        },
                        "limit": {"type": "integer", "default": 20},
                        "project_concepts": {
                            "type": "boolean",
                            "default": False,
                            "description": "If true, return concept frequency for the project instead of notes.",
                        },
                        "cooccurrence": {
                            "type": "boolean",
                            "default": False,
                            "description": "If true, return concepts that co-occur with the given concept.",
                        },
                    },
                },
            ),
            Tool(
                name="mem_landing",
                description=(
                    "Generate project landing documents (DECISIONS.md, BACKLOG.md, STATE.md).\n\n"
                    "DECISIONS.md: Decision ledger with table + Mermaid DAG. Auto-generated.\n"
                    "BACKLOG.md: Open items (todo), stalled proposals, parked items. Auto-generated.\n"
                    "STATE.md: Data-driven skeleton — for best results, read the generated "
                    "STATE.md and enhance it with your own judgment about what matters most "
                    "for the human to understand. Use state_context=true to get raw data "
                    "for writing a richer narrative.\n\n"
                    "Documents are excluded from the vault index (they're views, not source).\n"
                    "Run after extraction to refresh DECISIONS and BACKLOG. Only update STATE "
                    "if the session genuinely changed the project's big picture."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project": {
                            "type": "string",
                            "description": "Project name.",
                        },
                        "doc": {
                            "type": "string",
                            "enum": ["all", "decisions", "backlog", "state"],
                            "default": "all",
                            "description": "Which document(s) to generate.",
                        },
                        "state_context": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "If true, returns structured context data for STATE.md "
                                "instead of writing the file. Use this to write a richer "
                                "narrative STATE.md with your own judgment."
                            ),
                        },
                    },
                    "required": ["project"],
                },
            ),
            Tool(
                name="mem_enrich",
                description=(
                    "LLM-assisted concept assignment for vault notes missing concepts.\n\n"
                    "Sends batches of notes to claude-haiku with the full ontology as context. "
                    "Writes assigned concepts to markdown frontmatter (permanent, Obsidian-visible). "
                    "After enrichment, automatically rebuilds the index and re-runs mem_connect "
                    "to materialize new edges as wikilinks.\n\n"
                    "Run this to fix sessions (0% concept coverage), decisions (60% missing), "
                    "and any imported notes (claude-mem, ChatGPT) that lack concepts.\n\n"
                    "Requires ANTHROPIC_API_KEY in environment."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project": {
                            "type": "string",
                            "description": "Scope to one project. Empty = all projects.",
                        },
                        "note_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Types to enrich. Default: [session, note, decision, source].",
                        },
                        "limit": {
                            "type": "integer",
                            "default": 0,
                            "description": "Max notes to process. 0 = no limit.",
                        },
                        "force": {
                            "type": "boolean",
                            "default": False,
                            "description": "Re-enrich notes that already have concepts.",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "default": False,
                            "description": "Show what would be done without writing.",
                        },
                    },
                    "required": [],
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
                        "project": {
                            "type": "string",
                            "description": "Project to summarize. Omit for cross-project ranking.",
                        },
                        "days": {
                            "type": "integer",
                            "default": 7,
                            "description": "How many days back to look.",
                        },
                    },
                    "required": [],
                },
            ),
            Tool(
                name="mem_concept_source_counts",
                description=(
                    "Bulk source-count + URL lookup for a list of concepts.\n\n"
                    "Collapses /discover's O(N) per-concept under-source fan-out "
                    "into a single JOIN. For each input concept returns the full "
                    "set of source notes tagged with it — id, title, and url. "
                    "Concepts with zero sources still appear in the output with "
                    "count=0, so callers can iterate without KeyError checks.\n\n"
                    "Use for: under-sourced gap identification (count < 2) plus "
                    "per-gap dedup set collection (urls → gap_sources)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "concepts": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of concepts to look up.",
                        },
                    },
                    "required": ["concepts"],
                },
            ),
            Tool(
                name="mem_source_lens",
                description=(
                    "Given a source note ID (e.g. an imported ChatGPT conversation, a "
                    "paper, a web clipping), return everything that cites it, derives "
                    "from it, or shares concepts with it.\n\n"
                    "Returns: the source, inbound-edge notes (decisions, sessions, "
                    "other notes that reference it), and a concept reach report "
                    "(how many other notes use each of the source's concepts).\n\n"
                    "Use when you want to 'walk out' from a specific source and see "
                    "its influence across the vault."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source_id": {
                            "type": "string",
                            "description": "The note ID of the source (e.g. 'src-chatgpt-0a1b2c3d').",
                        },
                        "limit": {
                            "type": "integer",
                            "default": 50,
                            "description": "Max inbound notes to return.",
                        },
                    },
                    "required": ["source_id"],
                },
            ),
            Tool(
                name="mem_decisions_for_file",
                description=(
                    "Return every decision whose `file_paths` frontmatter includes the "
                    "given file path. Answers 'every decision ever made touching "
                    "src/personal_mem/vault.py' in one indexed JOIN.\n\n"
                    "Uses the `decision_files` indexer table — no frontmatter scanning."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Path to query, e.g. 'src/personal_mem/vault.py'. Must match the stored path exactly.",
                        },
                        "project": {
                            "type": "string",
                            "description": "Optional project filter.",
                        },
                        "status": {
                            "type": "string",
                            "description": "Optional status filter: proposed, accepted, deprecated, superseded.",
                        },
                        "limit": {"type": "integer", "default": 50},
                    },
                    "required": ["file_path"],
                },
            ),
            Tool(
                name="mem_concepts_drift",
                description=(
                    "Advisory drift report for the concept ontology. Read-only — "
                    "never modifies anything.\n\n"
                    "Surfaces three kinds of drift:\n"
                    "1. Near-duplicate concepts (e.g. 'neural-network' ≈ 'neural-networks')\n"
                    "2. New concept candidates — concepts with count >= 5 that are "
                    "NOT listed in ontology.yaml (ontology growth signals)\n"
                    "3. Ontology staleness — ontology.yaml has been edited since "
                    "the last `mem concepts hubs` run\n\n"
                    "Use as a mem-wrap step (advisory) — surface findings, don't act on "
                    "them. Acting requires user confirmation (`mem concepts merge`, etc.)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project": {
                            "type": "string",
                            "description": "Optional project scope. Empty = vault-wide.",
                        },
                        "threshold": {
                            "type": "integer",
                            "default": 5,
                            "description": "Minimum count for concept candidates to surface.",
                        },
                        "max_items": {
                            "type": "integer",
                            "default": 5,
                            "description": "Max items per drift category to return.",
                        },
                    },
                },
            ),
            Tool(
                name="mem_project_snapshot",
                description=(
                    "On-demand structured project overview — the same payload that the "
                    "SessionStart hook injects at session startup.\n\n"
                    "Sections: header, MCP tools manifest, last 5 wrapped sessions, "
                    "STATE.md, BACKLOG open items, recent decisions, open probes, "
                    "concept histogram, recent sources, retrieval hints.\n\n"
                    "Use mid-session when context is fading or to get an overview of "
                    "a different project without switching CWDs."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project": {
                            "type": "string",
                            "description": "Project slug.",
                        },
                        "sections": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Subset of sections to include. Default: all. "
                                "Valid keys: header, tools, sessions, state, backlog, "
                                "decisions, probes, concepts, sources, footer."
                            ),
                        },
                        "budget_tokens": {
                            "type": "integer",
                            "default": 8000,
                            "description": "Soft token budget. Whole sections are dropped if exceeded.",
                        },
                    },
                    "required": ["project"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name == "mem_search":
            return _handle_search(arguments)
        elif name == "mem_create":
            return _handle_create(arguments)
        elif name == "mem_read":
            return _handle_read(arguments)
        elif name == "mem_link":
            return _handle_link(arguments)
        elif name == "mem_context":
            return _handle_context(arguments)
        elif name == "mem_graph":
            return _handle_graph(arguments)
        elif name == "mem_update":
            return _handle_update(arguments)
        elif name == "mem_extract":
            return _handle_extract(arguments)
        elif name == "mem_judge":
            return _handle_judge(arguments)
        elif name == "mem_unlink":
            return _handle_unlink(arguments)
        elif name == "mem_concepts":
            return _handle_concepts(arguments)
        elif name == "mem_concepts_tighten":
            return _handle_concepts_tighten(arguments)
        elif name == "mem_concepts_merge":
            return _handle_concepts_merge(arguments)
        elif name == "mem_concept_search":
            return _handle_concept_search(arguments)
        elif name == "mem_enrich":
            return _handle_enrich(arguments)
        elif name == "mem_landing":
            return _handle_landing(arguments)
        elif name == "mem_timeline":
            return _handle_timeline(arguments)
        elif name == "mem_concept_source_counts":
            return _handle_concept_source_counts(arguments)
        elif name == "mem_source_lens":
            return _handle_source_lens(arguments)
        elif name == "mem_decisions_for_file":
            return _handle_decisions_for_file(arguments)
        elif name == "mem_concepts_drift":
            return _handle_concepts_drift(arguments)
        elif name == "mem_project_snapshot":
            return _handle_project_snapshot(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    def _handle_search(args: dict) -> list[TextContent]:
        s = Search(config=cfg)

        mode = args.get("mode", "fts")
        # note_type accepts either a string or a list
        type_arg = args.get("type") or ""
        query = args.get("query", "")
        project = args.get("project", "")
        limit = args.get("limit", 10)

        if mode == "similar":
            results = s.similar(
                query, project=project, note_type=type_arg, limit=limit
            )
            if not results:
                s.close()
                msg = (
                    "No semantic results — either the embeddings DB is missing "
                    "(run `mem index --embed` with OPENAI_API_KEY set) or no "
                    "matches above the cosine threshold."
                )
                return [TextContent(type="text", text=msg)]
        elif mode == "hybrid":
            results = s.hybrid_search(
                query, project=project, note_type=type_arg, limit=limit
            )
        else:
            # Default FTS mode — preserves back-compat
            results = s.search(
                query=query,
                note_type=type_arg,
                project=project,
                tags=args.get("tags"),
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

    def _handle_create(args: dict) -> list[TextContent]:
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        note_type = NoteType(args["type"])

        path = vm.create_note(
            note_type=note_type,
            title=args["title"],
            body=args.get("body", ""),
            project=args.get("project", ""),
            tags=args.get("tags"),
            extra_frontmatter=args.get("frontmatter"),
            session_id=args.get("session_id", ""),
        )

        # Index it
        idx = Indexer(config=cfg)
        idx.index_file(path)
        idx.close()

        note = vm.read_note(path)
        msg = f"Created {note.type.value} [{note.id}] at {note.path}"
        # For source notes, include the directory path so callers can save
        # raw content (PDFs, snapshots) alongside the source note.
        if note_type == NoteType.SOURCE:
            msg += f"\nSource directory: {path.parent}"
        return [
            TextContent(
                type="text",
                text=msg,
            )
        ]

    def _handle_read(args: dict) -> list[TextContent]:
        note_id = args["id"]
        s = Search(config=cfg)
        note = s.get_note_by_id(note_id)
        s.close()

        if not note:
            return [TextContent(type="text", text=f"Note {note_id} not found.")]

        vm = VaultManager(config=cfg)
        full_path = vm.root / note["path"]
        if full_path.exists():
            content = full_path.read_text(encoding="utf-8")
            return [TextContent(type="text", text=content)]

        return [TextContent(type="text", text=json.dumps(dict(note), indent=2))]

    def _handle_link(args: dict) -> list[TextContent]:
        from personal_mem.indexer import EDGE_TYPE_TO_FIELD

        source_id = args["source_id"]
        target_id = args["target_id"]
        edge_type = args["edge_type"]

        idx = Indexer(config=cfg)
        src = idx.db.execute("SELECT path FROM notes WHERE id = ?", (source_id,)).fetchone()
        tgt = idx.db.execute("SELECT id FROM notes WHERE id = ?", (target_id,)).fetchone()

        if not src:
            idx.close()
            return [TextContent(type="text", text=f"Source note {source_id} not found.")]
        if not tgt:
            idx.close()
            return [TextContent(type="text", text=f"Target note {target_id} not found.")]

        vm = VaultManager(config=cfg)
        fm_field = EDGE_TYPE_TO_FIELD[edge_type]
        source_path = vm.root / src["path"]
        vm.update_note(source_path, frontmatter_updates={fm_field: [target_id]})

        idx.index_file(source_path)
        idx.close()
        return [
            TextContent(
                type="text",
                text=f"Linked {source_id} --{edge_type}--> {target_id}",
            )
        ]

    def _handle_context(args: dict) -> list[TextContent]:
        s = Search(config=cfg)
        results = s.get_context(
            project=args.get("project", ""),
            tags=args.get("tags"),
            query=args.get("query", ""),
            concepts=args.get("concepts"),
            limit=args.get("limit", 5),
            note_type=args.get("type") or "",
        )
        s.close()

        if not results:
            return [TextContent(type="text", text="No context available.")]

        lines = []
        for r in results:
            tags = f" [{', '.join(r.tags)}]" if r.tags else ""
            lines.append(f"[{r.type}] {r.title} ({r.id}){tags}")
        return [TextContent(type="text", text="\n".join(lines))]

    def _handle_concept_search(args: dict) -> list[TextContent]:
        s = Search(config=cfg)

        # Mode 1: Project concept frequency
        if args.get("project_concepts") and args.get("project"):
            concept_counts = s.get_project_concepts(args["project"])
            s.close()
            if not concept_counts:
                return [TextContent(type="text", text=f"No concepts in project '{args['project']}'.")]
            lines = [f"Concepts in project '{args['project']}' ({len(concept_counts)} total):", ""]
            for concept, count in sorted(concept_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  {count:3d}  {concept}")
            return [TextContent(type="text", text="\n".join(lines))]

        # Mode 2: Concept co-occurrence
        if args.get("cooccurrence") and args.get("concept"):
            cooccur = s.get_concept_cooccurrence(args["concept"], limit=args.get("limit", 10))
            s.close()
            if not cooccur:
                return [TextContent(type="text", text=f"No co-occurring concepts for '{args['concept']}'.")]
            lines = [f"Concepts co-occurring with '{args['concept']}':", ""]
            for concept, count in cooccur:
                lines.append(f"  {count:3d}  {concept}")
            return [TextContent(type="text", text="\n".join(lines))]

        # Mode 3: Notes by one or more concepts
        concept_list: list[str]
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

    def _handle_graph(args: dict) -> list[TextContent]:
        s = Search(config=cfg)
        text = s.render_graph_text(args["id"], depth=args.get("depth", 2))
        s.close()
        return [TextContent(type="text", text=text)]

    def _handle_update(args: dict) -> list[TextContent]:
        note_id = args["id"]
        s = Search(config=cfg)
        note = s.get_note_by_id(note_id)
        s.close()

        if not note:
            return [TextContent(type="text", text=f"Note {note_id} not found.")]

        vm = VaultManager(config=cfg)
        full_path = vm.root / note["path"]

        if not full_path.exists():
            return [TextContent(type="text", text=f"File not found: {note['path']}")]

        fm_updates = args.get("frontmatter")
        body_append = args.get("body_append", "")
        remove_tags = args.get("remove_tags")

        if not fm_updates and not body_append and not remove_tags:
            return [TextContent(type="text", text="Nothing to update. Provide frontmatter, body_append, or remove_tags.")]

        vm.update_note(full_path, frontmatter_updates=fm_updates, body_append=body_append, remove_tags=remove_tags)

        # Re-index
        idx = Indexer(config=cfg)
        idx.index_file(full_path)
        idx.close()

        updated = vm.read_note(full_path)
        parts = [f"Updated {updated.type.value} [{updated.id}]"]
        if fm_updates:
            parts.append(f"frontmatter: {list(fm_updates.keys())}")
        if remove_tags:
            parts.append(f"removed tags: {remove_tags}")
        if body_append:
            parts.append(f"appended {len(body_append)} chars")
        return [TextContent(type="text", text=", ".join(parts))]

    def _handle_extract(args: dict) -> list[TextContent]:
        from datetime import date

        session_id = args["session_id"]
        force = args.get("force", False)

        # Look up the session note
        s = Search(config=cfg)
        session_row = s.get_note_by_id(session_id)
        s.close()

        if session_row and session_row["type"] != "session":
            return [TextContent(
                type="text",
                text=f"Note {session_id} is type '{session_row['type']}', not 'session'.",
            )]

        vm = VaultManager(config=cfg)

        if not session_row:
            # Auto-create session note for non-code conversations
            project = args.get("project", "") or cfg.default_project
            summary = args.get("summary", "")
            title = summary[:60] if summary else "conversation"
            session_path = vm.create_note(
                note_type=NoteType.SESSION,
                title=title,
                body="## Summary\n\n## Events\n",
                project=project,
                extra_frontmatter={"source_session": session_id},
            )
            idx = Indexer(config=cfg)
            idx.index_file(session_path)
            idx.close()
        else:
            session_path = vm.root / session_row["path"]
            if not session_path.exists():
                return [TextContent(type="text", text=f"Session file not found: {session_row['path']}")]

        session_note = vm.read_note(session_path)

        # Check processed flag
        if session_note.frontmatter.get("processed") and not force:
            processed_at = session_note.frontmatter.get("processed_at", "unknown date")
            return [TextContent(
                type="text",
                text=f"Session {session_id} already processed on {processed_at}. Use force=true to re-extract.",
            )]

        project = session_note.project

        # Store plan reference on session if provided
        plan_path = args.get("plan_path", "")
        plan_summary = args.get("plan_summary", "")
        if plan_path or plan_summary:
            plan_ctx = {}
            if plan_path:
                plan_ctx["path"] = plan_path
            if plan_summary:
                plan_ctx["summary"] = plan_summary
            vm.update_note(
                session_path,
                frontmatter_updates={"context": {"plan": plan_ctx}},
            )

        # Clean up existing derived notes from prior extraction
        # This prevents duplicates when force=true or extract runs multiple times
        session_dir = session_path.parent
        idx = Indexer(config=cfg)
        for md_file in session_dir.glob("*.md"):
            if md_file.name == "session.md":
                continue
            try:
                fm, _ = parse_frontmatter(md_file.read_text(encoding="utf-8"))
                derived = fm.get("derived_from", [])
                if isinstance(derived, str):
                    derived = [derived]
                if session_id in derived or session_note.id in derived:
                    rel = str(md_file.relative_to(vm.root))
                    idx._remove_by_path(rel)
                    md_file.unlink()
            except Exception:
                continue

        # Determine insights to extract
        insights = args.get("insights")
        if not insights:
            insights = _parse_candidate_insights(session_note.body)

        # Cap at 3
        insights = insights[:3]

        # Create notes for each insight
        created = []
        created_decisions = []
        for insight in insights:
            title = insight["title"]
            body = insight["body"]
            tags = insight.get("tags", [])
            concepts = insight.get("concepts", [])

            extra_fm: dict = {"derived_from": [session_id]}
            if concepts:
                extra_fm["concepts"] = concepts

            path = vm.create_note(
                note_type=NoteType.NOTE,
                title=title,
                body=body,
                project=project,
                tags=tags,
                extra_frontmatter=extra_fm,
                output_dir=session_path.parent,
            )
            idx.index_file(path)
            note = vm.read_note(path)
            created.append(note)

        # Create decision notes
        decisions = args.get("decisions", [])
        for dec in decisions:
            # Map outcome to initial status
            outcome = dec.get("outcome", "committed")
            status = {
                "committed": "accepted",
                "abandoned": "proposed",
                "partial": "proposed",
            }.get(outcome, "proposed")

            # Build extra frontmatter
            extra_fm: dict = {
                "status": status,
                "committed": outcome == "committed",
                "source_session": session_id,
                "derived_from": [session_id],
            }
            if dec.get("file_paths"):
                extra_fm["file_paths"] = dec["file_paths"]
            if dec.get("concepts"):
                extra_fm["concepts"] = dec["concepts"]
            if dec.get("supersedes"):
                extra_fm["supersedes"] = dec["supersedes"]
            if dec.get("cites"):
                extra_fm["cites"] = dec["cites"]
            if dec.get("plan_ref"):
                extra_fm["plan_ref"] = dec["plan_ref"]
            if dec.get("summary"):
                extra_fm["summary"] = dec["summary"]

            # Build body from rationale
            rationale = dec.get("rationale", "")
            dec_body = f"## Context\n\n{rationale}\n\n## Decision\n\n{dec['title']}"
            if outcome == "abandoned":
                dec_body += "\n\n## Consequences\n\nApproach was abandoned."

            path = vm.create_note(
                note_type=NoteType.DECISION,
                title=dec["title"],
                body=dec_body,
                project=project,
                tags=dec.get("tags", []),
                extra_frontmatter=extra_fm,
                output_dir=session_path.parent,
            )
            idx.index_file(path)
            dec_note = vm.read_note(path)
            created_decisions.append(dec_note)

        # Fast-path commit-decision linking: correlate session commits to decisions
        session_commits = session_note.frontmatter.get("commits", [])
        if created_decisions and session_commits:
            for dec_note in created_decisions:
                dec_files = dec_note.frontmatter.get("file_paths", [])
                if not dec_files:
                    continue
                dec_basenames = {Path(fp).name for fp in dec_files}
                matched_hashes = []
                for commit in session_commits:
                    commit_files = commit.get("files", [])
                    commit_hash = commit.get("hash", "")
                    if not commit_hash or not commit_files:
                        continue
                    commit_basenames = {Path(f).name for f in commit_files}
                    if dec_basenames & commit_basenames:
                        matched_hashes.append(commit_hash)
                if matched_hashes:
                    vm.update_note(
                        vm.root / dec_note.path,
                        frontmatter_updates={"commit_refs": matched_hashes},
                    )

        # Suggest relevant source links and concept similarity warnings
        # This is best-effort — never abort extraction over suggestions
        suggestions = []
        try:
            if created_decisions:
                from personal_mem.concepts import get_all_concepts, suggest_similar

                all_concepts = get_all_concepts(idx.db)
                existing_list = list(all_concepts.keys())

                s = Search(config=cfg)
                for dec_note in created_decisions:
                    dec_concepts = dec_note.frontmatter.get("concepts", [])
                    if dec_concepts:
                        for concept in dec_concepts:
                            similar = suggest_similar(concept, existing_list)
                            for sim in similar:
                                if sim != concept.lower():
                                    suggestions.append(
                                        f"  ⚠ Concept '{concept}' is similar to existing "
                                        f"'{sim}' ({all_concepts.get(sim, 0)} notes). "
                                        f"Consider mem_concepts_merge if they mean the same thing."
                                    )
                        for concept in dec_concepts[:3]:
                            results = s.search(query=concept, note_type="source", limit=3)
                            for r in results:
                                suggestions.append(
                                    f"  Tip: {dec_note.id} shares concept '{concept}' with "
                                    f"source {r.title} ({r.id}). Consider mem_link with cites."
                                )
                s.close()
        except Exception:
            pass

        # Add summary to session note
        summary_text = args.get("summary", "")
        all_created = created + created_decisions
        if not summary_text and all_created:
            note_titles = ", ".join(n.title for n in created) if created else ""
            dec_titles = ", ".join(n.title for n in created_decisions) if created_decisions else ""
            parts = []
            if note_titles:
                parts.append(f"{len(created)} notes: {note_titles}")
            if dec_titles:
                parts.append(f"{len(created_decisions)} decisions: {dec_titles}")
            summary_text = f"Extracted {'; '.join(parts)}."

        if summary_text:
            # On re-extraction (force=true), replace existing summary
            if force and "## Summary" in session_note.body:
                cur_text = session_path.read_text(encoding="utf-8")
                cur_fm, cur_body = parse_frontmatter(cur_text)
                cur_body = _strip_section(cur_body, "## Summary")
                cur_body = cur_body.rstrip() + f"\n\n## Summary\n{summary_text}\n"
                session_path.write_text(
                    render_frontmatter(cur_fm) + "\n\n" + cur_body, encoding="utf-8"
                )
            elif "## Summary" in session_note.body:
                _append_to_section(session_path, "## Summary", summary_text)
            else:
                vm.update_note(session_path, body_append=f"## Summary\n{summary_text}")

        # Mark session as processed
        today = date.today().isoformat()
        fm_updates: dict = {"processed": True, "processed_at": today}
        # Strip auto_extracted flag on full re-extraction
        if session_note.frontmatter.get("auto_extracted"):
            fm_updates["auto_extracted"] = False
        vm.update_note(
            session_path,
            frontmatter_updates=fm_updates,
        )

        # Post-extraction cleanup: strip raw events and candidate insights
        session_text = session_path.read_text(encoding="utf-8")
        fm_part, body_part = parse_frontmatter(session_text)
        cleaned_body = _strip_section(body_part, "## Events")
        cleaned_body = _strip_section(cleaned_body, "## Candidate Insights")
        if cleaned_body != body_part:
            content = render_frontmatter(fm_part) + "\n\n" + cleaned_body
            session_path.write_text(content, encoding="utf-8")

        idx.index_file(session_path)
        idx.close()

        # Build report
        report_lines = [f"Extracted from session {session_id}:"]
        if summary_text:
            report_lines.append(f"Summary: {summary_text}")
        for note in created:
            report_lines.append(
                f"  Created [{note.type.value}] {note.title} ({note.id}) derived_from={session_id}"
            )
        for dec_note in created_decisions:
            outcome_str = dec_note.frontmatter.get("committed", False)
            report_lines.append(
                f"  Created [{dec_note.type.value}] {dec_note.title} ({dec_note.id}) "
                f"status={dec_note.frontmatter.get('status')}, committed={outcome_str}"
            )
        if not all_created:
            report_lines.append("  No insights or decisions extracted.")
        for s in suggestions[:5]:
            report_lines.append(s)

        # Archive event buffer to session directory
        try:
            from personal_mem.hooks.handler import archive_buffer
            source_session = session_note.frontmatter.get("source_session", session_id)
            archive_buffer(cfg.mem_dir, source_session, session_path.parent)
        except Exception:
            pass  # Buffer archive is best-effort

        report_lines.append(f"Session marked processed={today}")
        return [TextContent(type="text", text="\n".join(report_lines))]

    def _handle_judge(args: dict) -> list[TextContent]:
        from collections import defaultdict

        from personal_mem.judge import evaluate_decision

        vm = VaultManager(config=cfg)
        s = Search(config=cfg)

        # Collect target decisions
        target_decisions: list[NoteMeta] = []

        if args.get("decision_id"):
            row = s.get_note_by_id(args["decision_id"])
            if row and row["type"] == "decision":
                note = vm.read_note(vm.root / row["path"])
                target_decisions.append(note)
        elif args.get("session_id"):
            # Find decisions with source_session matching this session
            for note in vm.list_notes(note_type=NoteType.DECISION, limit=100):
                if note.frontmatter.get("source_session") == args["session_id"]:
                    target_decisions.append(note)
        elif args.get("project"):
            for note in vm.list_notes(note_type=NoteType.DECISION, limit=100):
                if note.project == args["project"]:
                    target_decisions.append(note)

        if not target_decisions:
            s.close()
            return [TextContent(type="text", text="No decisions found to evaluate.")]

        # Get all decisions for supersession checks
        all_decisions = list(vm.list_notes(note_type=NoteType.DECISION, limit=500))

        # Evaluate each decision
        results = []
        for dec in target_decisions:
            # Load session meta for test info
            session_id = dec.frontmatter.get("source_session", "")
            session_meta = None
            if session_id:
                session_row = s.get_note_by_id(session_id)
                if session_row:
                    session_meta = vm.read_note(vm.root / session_row["path"])

            result = evaluate_decision(dec, all_decisions, session_meta)

            # Update decision frontmatter with verdict, temporal fields, and commit refs
            fm_updates: dict = {
                "verdict": result["verdict"],
                "confidence": result["confidence"],
                "judged_at": result["judged_at"],
            }
            if result["blame_lines"] >= 0:
                fm_updates["blame_lines"] = result["blame_lines"]
            if result.get("commit_refs"):
                fm_updates["commit_refs"] = result["commit_refs"]
                if not dec.frontmatter.get("committed"):
                    fm_updates["committed"] = True
            vm.update_note(
                vm.root / dec.path,
                frontmatter_updates=fm_updates,
            )
            # Re-index
            idx = Indexer(config=cfg)
            idx.index_file(vm.root / dec.path)
            idx.close()

            # Update status based on verdict
            status_map = {
                "kept": "accepted",
                "superseded": "superseded",
                "reverted": "deprecated",
            }
            new_status = status_map.get(result["verdict"])
            if new_status and new_status != dec.frontmatter.get("status"):
                vm.update_note(
                    vm.root / dec.path,
                    frontmatter_updates={"status": new_status},
                )

            results.append(
                f"  {dec.id} ({dec.title}): {result['verdict']} "
                f"(confidence={result['confidence']}) — {result['evidence']}"
            )

        s.close()
        lines = [f"Evaluated {len(results)} decisions:"] + results
        return [TextContent(type="text", text="\n".join(lines))]

    def _handle_unlink(args: dict) -> list[TextContent]:
        from personal_mem.indexer import EDGE_TYPE_TO_FIELD

        source_id = args["source_id"]
        target_id = args["target_id"]
        edge_type = args["edge_type"]

        idx = Indexer(config=cfg)
        src = idx.db.execute("SELECT path FROM notes WHERE id = ?", (source_id,)).fetchone()

        if not src:
            idx.close()
            return [TextContent(type="text", text=f"Source note {source_id} not found.")]

        vm = VaultManager(config=cfg)
        source_path = vm.root / src["path"]
        note = vm.read_note(source_path)
        fm_field = EDGE_TYPE_TO_FIELD[edge_type]

        targets = note.frontmatter.get(fm_field, [])
        if isinstance(targets, str):
            targets = [targets] if targets else []

        if target_id not in targets:
            idx.close()
            return [TextContent(type="text", text="No matching edge found.")]

        new_targets = [t for t in targets if t != target_id]
        # Direct write: update_note merges lists, but we need to replace
        from personal_mem.vault import parse_frontmatter, render_frontmatter
        text = source_path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        if new_targets:
            fm[fm_field] = new_targets
        else:
            fm.pop(fm_field, None)
        content = render_frontmatter(fm) + "\n\n" + body
        source_path.write_text(content, encoding="utf-8")

        idx.index_file(source_path)
        idx.close()
        return [TextContent(
            type="text",
            text=f"Removed edge: {source_id} --{edge_type}--> {target_id}",
        )]

    def _handle_concepts(args: dict) -> list[TextContent]:
        from collections import defaultdict

        idx = Indexer(config=cfg)
        concept_counts: dict[str, int] = defaultdict(int)
        for row in idx.db.execute("SELECT frontmatter FROM notes"):
            fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
            concepts = fm.get("concepts", [])
            if isinstance(concepts, str):
                concepts = [c.strip() for c in concepts.split(",") if c.strip()]
            for c in concepts:
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

    def _handle_concepts_tighten(args: dict) -> list[TextContent]:
        from personal_mem.concepts import (
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

    def _handle_concepts_merge(args: dict) -> list[TextContent]:
        from personal_mem.concepts import (
            load_aliases,
            merge_concept_in_notes,
            save_aliases,
        )

        from_concept = args["from_concept"].lower()
        to_concept = args["to_concept"].lower()

        if from_concept == to_concept:
            return [TextContent(type="text", text="from_concept and to_concept are the same.")]

        # Rename in all notes
        changed = merge_concept_in_notes(cfg.vault_root, from_concept, to_concept)

        # Update aliases file
        aliases = load_aliases(cfg)
        # Ensure to_concept is canonical; add from_concept as alias
        existing_aliases = aliases.get(to_concept, [])
        if from_concept not in existing_aliases:
            existing_aliases.append(from_concept)
        # If from_concept was itself a canonical, absorb its aliases
        if from_concept in aliases:
            for old_alias in aliases.pop(from_concept):
                if old_alias != to_concept and old_alias not in existing_aliases:
                    existing_aliases.append(old_alias)
        aliases[to_concept] = existing_aliases
        save_aliases(cfg, aliases)

        # Rebuild index to update concept edges
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

    def _handle_landing(args: dict) -> list[TextContent]:
        from personal_mem.landing import (
            state_of_play_context,
            write_landing_docs,
        )

        project = args["project"]
        doc = args.get("doc", "all")
        state_context = args.get("state_context", False)

        # If state_context requested, return raw data instead of writing
        if state_context:
            context_text = state_of_play_context(cfg, project)
            return [TextContent(type="text", text=context_text)]

        written = write_landing_docs(cfg, project, docs=doc)
        lines = [f"Generated landing documents for {project}:"]
        for filename, path in written.items():
            lines.append(f"  {filename} → {path.relative_to(cfg.vault_root)}")
        return [TextContent(type="text", text="\n".join(lines))]

    def _handle_enrich(args: dict) -> list[TextContent]:
        from personal_mem.enrich import enrich
        from personal_mem.indexer import Indexer

        note_types = args.get("note_types") or ["session", "note", "decision", "source"]
        stats = enrich(
            cfg,
            project=args.get("project", ""),
            note_types=note_types,
            limit=args.get("limit", 0),
            force=args.get("force", False),
            dry_run=args.get("dry_run", False),
        )

        dry = args.get("dry_run", False)
        lines = [
            f"{'[dry run] ' if dry else ''}Concept enrichment complete:",
            f"  enriched: {stats['enriched']}",
            f"  skipped: {stats['skipped']}",
            f"  errors: {stats['errors']}",
            f"  concepts assigned: {stats['new_concepts']}",
        ]

        if not dry and stats["enriched"] > 0:
            idx = Indexer(config=cfg)
            istats = idx.rebuild(full=True)
            cstats = idx.materialize_links(max_links=5)
            idx.rebuild(full=False)  # pick up new wikilinks
            idx.close()
            lines += [
                f"\nReindexed: {istats['edges']} edges",
                f"Materialized: {cstats['links_written']} wikilinks into {cstats['notes_updated']} notes",
            ]

        return [TextContent(type="text", text="\n".join(lines))]

    def _handle_timeline(args: dict) -> list[TextContent]:
        from datetime import date, timedelta

        project = args.get("project", "") or ""
        days = args.get("days", 7)

        s = Search(config=cfg)

        # Cross-project ranking mode — no project argument.
        if not project:
            ranking = s.get_cross_project_activity(days=days)
            s.close()
            if not ranking:
                return [TextContent(
                    type="text",
                    text=f"No session or decision activity in the last {days} days.",
                )]
            lines = [
                f"Cross-project activity (last {days} days, {len(ranking)} projects)",
                "",
            ]
            for entry in ranking:
                lines.append(
                    f"- {entry['project']} — {entry['sessions']} sessions, "
                    f"{entry['decisions']} decisions "
                    f"(latest: {entry['latest_date'][:10] or '?'})"
                )
            return [TextContent(type="text", text="\n".join(lines))]

        # Single-project detailed mode — existing behavior.
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        vm = VaultManager(config=cfg)

        sessions = []
        for note in vm.list_notes(note_type=NoteType.SESSION, limit=100):
            if note.project == project and note.date >= cutoff:
                sessions.append(note)

        sessions.sort(key=lambda n: n.date)

        if not sessions:
            s.close()
            return [TextContent(
                type="text",
                text=f"No sessions found for project '{project}' in the last {days} days.",
            )]

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

            # Find decisions linked to this session
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

    def _handle_concept_source_counts(args: dict) -> list[TextContent]:
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

    def _handle_source_lens(args: dict) -> list[TextContent]:
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

    def _handle_decisions_for_file(args: dict) -> list[TextContent]:
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

    def _handle_project_snapshot(args: dict) -> list[TextContent]:
        from personal_mem.context import build_project_context

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

    def _handle_concepts_drift(args: dict) -> list[TextContent]:
        from personal_mem.concepts import drift_report, format_drift_report

        report = drift_report(
            cfg,
            project=args.get("project", ""),
            threshold=args.get("threshold", 5),
            max_items=args.get("max_items", 5),
        )
        text = format_drift_report(report)
        return [TextContent(type="text", text=text)]

    async def run():
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(run())


if __name__ == "__main__":
    main()
