"""``mem_create`` / ``mem_read`` / ``mem_update`` / ``mem_link`` / ``mem_unlink``.

Note CRUD + edge management. Handlers are thin wrappers; the heavy
lifting lives in ``personal_mem.core.vault`` (``VaultManager``) and
``personal_mem.core.indexer`` (``Indexer``). Schemas are returned by
:func:`tool_schemas` so the server can register them in one pass.
"""

from __future__ import annotations

import json

from personal_mem.core.config import Config
from personal_mem.core.schemas import EdgeType, NoteType


def tool_schemas() -> list:
    from mcp.types import Tool

    return [
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
    ]


def handle_create(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.core.indexer import Indexer
    from personal_mem.core.vault import VaultManager
    from personal_mem.synthesis.concepts import split_concepts_by_ontology

    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    note_type = NoteType(args["type"])

    # Strict policy: anything in `concepts:` that isn't in the merged
    # ontology gets routed to `proposed_concepts:`. Caller intent is
    # preserved (proposed terms stay proposed; canonical-but-unknown
    # terms move into the proposed queue for /mem-resolve-concepts).
    extra_frontmatter = dict(args.get("frontmatter") or {})
    if "concepts" in extra_frontmatter or "proposed_concepts" in extra_frontmatter:
        canonical, proposed = split_concepts_by_ontology(
            extra_frontmatter.get("concepts"),
            proposed=extra_frontmatter.get("proposed_concepts"),
        )
        if canonical:
            extra_frontmatter["concepts"] = canonical
        else:
            extra_frontmatter.pop("concepts", None)
        if proposed:
            extra_frontmatter["proposed_concepts"] = proposed
        else:
            extra_frontmatter.pop("proposed_concepts", None)

    path = vm.create_note(
        note_type=note_type,
        title=args["title"],
        body=args.get("body", ""),
        project=args.get("project", ""),
        tags=args.get("tags"),
        extra_frontmatter=extra_frontmatter or None,
        session_id=args.get("session_id", ""),
    )

    idx = Indexer(config=cfg)
    idx.index_file(path)
    idx.close()

    note = vm.read_note(path)
    msg = f"Created {note.type.value} [{note.id}] at {note.path}"
    if note_type == NoteType.SOURCE:
        msg += f"\nSource directory: {path.parent}"
    return [TextContent(type="text", text=msg)]


def handle_read(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.core.vault import VaultManager
    from personal_mem.retrieval.search import Search

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

    from personal_mem.core.indexer import EDGE_TYPE_TO_FIELD, Indexer
    from personal_mem.core.vault import VaultManager

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
    return [TextContent(type="text", text=f"Linked {source_id} --{edge_type}--> {target_id}")]


def handle_update(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.core.indexer import Indexer
    from personal_mem.core.vault import VaultManager
    from personal_mem.retrieval.search import Search

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


def handle_unlink(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.core.indexer import EDGE_TYPE_TO_FIELD, Indexer
    from personal_mem.core.vault import VaultManager, parse_frontmatter, render_frontmatter

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
    return [TextContent(type="text", text=f"Removed edge: {source_id} --{edge_type}--> {target_id}")]
