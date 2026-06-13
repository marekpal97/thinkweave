"""``weave_create`` / ``weave_read`` / ``weave_update`` / ``weave_link`` / ``weave_unlink``.

Note CRUD + edge management. Handlers are thin wrappers over
``thinkweave.operations.notes`` — the operations seam owns the
``VaultManager`` / ``Indexer`` dance, the strict ontology gate, and
the indexer-after-write invariants. This module's job is the MCP
input shape (the JSON Schemas returned by :func:`tool_schemas`) and
the MCP output shape (``TextContent`` envelopes). No business logic.
"""

from __future__ import annotations

import json

from thinkweave.core.config import Config
from thinkweave.core.schemas import EdgeType, NoteType


def tool_schemas() -> list:
    from mcp.types import Tool

    return [
        Tool(
            name="weave_create",
            description=(
                "Create a new note in the knowledge vault. Always weave_search first to avoid "
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
                "Set source_type, url, and authors in frontmatter.\n"
                '- "digest": Daily knowledge-first summary written by '
                "dream-digest-worker (phase 2 of /dream). Grain-split: files at "
                "digests/YYYY-MM-DD-<grain>.md (grain ∈ {concept, event}). "
                "Normal hand-authored writes should not target this type.\n\n"
                "Linking guidance (set via frontmatter field):\n"
                "- derived_from: [session-id] — when extracting knowledge from a session\n"
                "- builds_on: [note-id] — when extending existing knowledge\n"
                "- supersedes: [note-id] — when replacing outdated knowledge\n"
                "- implements: [decision-id] — when code/config implements a decision\n"
                "- cites: [source-id] — when referencing external material\n"
                "- concepts: [list] — domain-specific technical terms for thematic "
                "graph linking (e.g. [\"write-ahead-log\", \"fts5\"]). Notes sharing 2+ "
                "concepts are auto-linked. Call weave_concepts first to reuse existing labels.\n\n"
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
                        "enum": ["note", "session", "decision", "source", "digest"],
                        "description": (
                            'Note type. "note" for reusable knowledge (default choice). '
                            '"decision" for architectural choices with lifecycle tracking. '
                            '"session" for work logs (usually auto-created). '
                            '"source" for external references. '
                            '"digest" for daily knowledge-delta summaries (written by dream-digest-worker).'
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
            name="weave_read",
            description=(
                "Read the full markdown content of a note by its ID "
                '(e.g. "n-a1b2c3d4", "dec-e5f6g7h8").\n\n'
                "Returns the complete file including YAML frontmatter and body. "
                "Use after weave_search to inspect a note's full content before deciding "
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
            name="weave_link",
            description=(
                "Create a typed directed edge between two notes in the knowledge graph.\n"
                "Prefer setting edges via frontmatter fields in weave_create when possible. "
                "Use weave_link for edges discovered after creation.\n\n"
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
            name="weave_update",
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
            name="weave_unlink",
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
    ]


def handle_create(cfg: Config, args: dict):
    from mcp.types import TextContent

    from thinkweave.operations.notes import create_note

    note_type = NoteType(args["type"])
    result = create_note(
        cfg,
        note_type=note_type,
        title=args["title"],
        body=args.get("body", ""),
        project=args.get("project", ""),
        tags=args.get("tags"),
        extra_frontmatter=args.get("frontmatter") or None,
        session_id=args.get("session_id", ""),
    )
    note = result.note

    if result.existed:
        # Dedup gate hit — caller (worker, importer, /research direct) gets
        # the existing note id back instead of a fresh duplicate. Phrasing
        # is explicit so workers can branch on it: "idempotent_skip" mirrors
        # the worker outcome vocabulary in the research-* skill specs.
        msg = (
            f"idempotent_skip: source already exists as [{note.id}] at {note.path} "
            f"(matched on configured dedup_keys)"
        )
        return [TextContent(type="text", text=msg)]

    msg = f"Created {note.type.value} [{note.id}] at {note.path}"
    if note_type == NoteType.SOURCE:
        # note.path is the relative path to source.md; parent is the bucket folder.
        msg += f"\nSource directory: {(cfg.vault_root / note.path).parent}"
    return [TextContent(type="text", text=msg)]


def handle_read(cfg: Config, args: dict):
    from mcp.types import TextContent

    from thinkweave.core.vault import VaultManager
    from thinkweave.retrieval.search import Search

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


def handle_link(cfg: Config, args: dict):
    from mcp.types import TextContent

    from thinkweave.operations.notes import link_notes

    source_id = args["source_id"]
    target_id = args["target_id"]
    edge_type = args["edge_type"]

    try:
        link_notes(cfg, source_id, target_id, edge_type)
    except FileNotFoundError as e:
        msg = str(e)
        if "Source note" in msg:
            return [TextContent(type="text", text=f"Source note {source_id} not found.")]
        return [TextContent(type="text", text=f"Target note {target_id} not found.")]
    return [TextContent(type="text", text=f"Linked {source_id} --{edge_type}--> {target_id}")]


def handle_update(cfg: Config, args: dict):
    from mcp.types import TextContent

    from thinkweave.operations.notes import update_note

    note_id = args["id"]
    fm_updates = args.get("frontmatter")
    body_append = args.get("body_append", "")
    remove_tags = args.get("remove_tags")

    if not fm_updates and not body_append and not remove_tags:
        return [TextContent(type="text", text="Nothing to update. Provide frontmatter, body_append, or remove_tags.")]

    try:
        updated = update_note(
            cfg,
            note_id,
            frontmatter_updates=fm_updates,
            body_append=body_append,
            remove_tags=remove_tags,
        )
    except FileNotFoundError as e:
        # operations raises either "Note <id> not found" or "File missing for ..."
        # Map to the legacy MCP-text shapes for surface compatibility.
        msg = str(e)
        if msg.startswith("File missing for"):
            return [TextContent(type="text", text=f"File not found: {msg.split(': ', 1)[-1]}")]
        return [TextContent(type="text", text=f"Note {note_id} not found.")]

    parts = [f"Updated {updated.type.value} [{updated.id}]"]
    if fm_updates:
        parts.append(f"frontmatter: {list(fm_updates.keys())}")
    if remove_tags:
        parts.append(f"removed tags: {remove_tags}")
    if body_append:
        parts.append(f"appended {len(body_append)} chars")
    return [TextContent(type="text", text=", ".join(parts))]


def handle_unlink(cfg: Config, args: dict):
    from mcp.types import TextContent

    from thinkweave.operations.notes import unlink_notes

    source_id = args["source_id"]
    target_id = args["target_id"]
    edge_type = args["edge_type"]

    try:
        removed = unlink_notes(cfg, source_id, target_id, edge_type)
    except FileNotFoundError:
        return [TextContent(type="text", text=f"Source note {source_id} not found.")]
    if not removed:
        return [TextContent(type="text", text="No matching edge found.")]
    return [TextContent(type="text", text=f"Removed edge: {source_id} --{edge_type}--> {target_id}")]
