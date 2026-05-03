"""Argparse subcommand builders — primitive note ops, queries, hooks, intake.

Each ``add_*_subparser(sub)`` adds one or more subcommand parsers to the
shared ``sub = parser.add_subparsers(dest='command')`` so the
``build_parser`` function in ``parser.py`` stays a flat list of
``add_*_subparser`` calls.
"""

from __future__ import annotations

from personal_mem.core.schemas import EdgeType, NoteType


def add_note_subparsers(sub) -> None:
    p_add = sub.add_parser("add", help="Create a new note")
    p_add.add_argument("title", help="Note title")
    p_add.add_argument("--type", "-t", default="note", choices=[t.value for t in NoteType])
    p_add.add_argument("--project", "-p", default="")
    p_add.add_argument("--tags", default="", help="Comma-separated tags")
    p_add.add_argument("--body", "-b", default="", help="Note body (or pipe via stdin)")
    p_add.add_argument("--session", "-s", default="", help="Session ID to place note in")

    p_search = sub.add_parser("search", help="Search the vault")
    p_search.add_argument("query", nargs="?", default="")
    p_search.add_argument("--type", "-t", default="", help="Note type (or comma-separated list)")
    p_search.add_argument("--project", "-p", default="")
    p_search.add_argument("--tags", default="", help="Comma-separated tags")
    p_search.add_argument("--limit", "-n", type=int, default=10)
    p_search.add_argument("--concept", "-c", default="", help="Search by concept (comma-separated for multi)")
    p_search.add_argument(
        "--match-mode", default="any", choices=["any", "all"],
        help="With --concept: 'any' (union) or 'all' (intersection)",
    )
    p_search.add_argument(
        "--mode", default="fts", choices=["fts", "similar", "hybrid"],
        help="Search mode: fts (default), similar (semantic), hybrid (RRF fusion)",
    )
    p_search.add_argument("--semantic", action="store_true", help="Alias for --mode similar")

    p_decisions = sub.add_parser(
        "decisions", help="Query decisions — e.g. every decision that touched a file"
    )
    p_decisions.add_argument("--file", "-f", dest="file_path", default="", help="File path to filter by")
    p_decisions.add_argument("--project", "-p", default="")
    p_decisions.add_argument("--status", default="", help="Filter by status (accepted/proposed/deprecated/superseded)")
    p_decisions.add_argument("--limit", "-n", type=int, default=50)

    p_project = sub.add_parser(
        "project", help="Print a structured project snapshot (same payload as SessionStart hook)"
    )
    p_project.add_argument("name", help="Project slug")
    p_project.add_argument(
        "--sections", default="",
        help="Comma-separated section keys (default: all). "
             "Options: header,tools,sessions,state,backlog,decisions,probes,concepts,sources,footer",
    )
    p_project.add_argument("--budget", type=int, default=8000, help="Token budget (default 8000)")

    p_show = sub.add_parser("show", help="Display a note by ID")
    p_show.add_argument("id", help="Note ID")

    p_link = sub.add_parser("link", help="Create a relationship between notes")
    p_link.add_argument("source", help="Source note ID")
    p_link.add_argument("target", help="Target note ID")
    p_link.add_argument(
        "--type", "-t", default="relates_to", choices=[e.value for e in EdgeType]
    )

    p_graph = sub.add_parser("graph", help="Show local graph around a note")
    p_graph.add_argument("id", help="Center note ID")
    p_graph.add_argument("--depth", "-d", type=int, default=2)
    p_graph.add_argument("--format", "-f", default="text", choices=["text", "mermaid"])

    p_context = sub.add_parser("context", help="Get relevant notes for current context")
    p_context.add_argument("--project", "-p", default="")
    p_context.add_argument("--tags", default="", help="Comma-separated tags")
    p_context.add_argument("--query", "-q", default="")
    p_context.add_argument("--concepts", default="", help="Comma-separated concepts for concept-based retrieval")
    p_context.add_argument("--limit", "-n", type=int, default=5)

    p_backlog = sub.add_parser("backlog", help="List notes tagged 'todo'")
    p_backlog.add_argument("--project", "-p", default="", help="Filter by project")
    p_backlog.add_argument("--tag", default="todo", help="Tag to query (default: todo)")
    p_backlog.add_argument(
        "--hide-auto",
        action="store_true",
        help="Hide auto-extracted todos (those tagged with `auto`).",
    )

    p_update = sub.add_parser(
        "update",
        help="CLI parity for mem_update — minimal subset for headless flows.",
    )
    p_update.add_argument("note_id")
    p_update.add_argument(
        "--frontmatter", "-f", action="append", default=[],
        help="Repeatable: key=value frontmatter override",
    )
    p_update.add_argument(
        "--body-append", default="",
        help="Path to a file appended to the note body",
    )


def add_index_subparsers(sub) -> None:
    p_index = sub.add_parser("index", help="Rebuild the SQLite index")
    p_index.add_argument("--full", action="store_true", help="Full rebuild (drop and recreate)")
    p_index.add_argument("--embed", action="store_true", help="Compute embeddings via API")
    p_index.add_argument(
        "--materialize-links",
        action="store_true",
        help="After indexing, write SQLite edges as wikilinks (## See Also) for Obsidian.",
    )
    p_index.add_argument(
        "--max-links", type=int, default=5, help="With --materialize-links: max links per note (default: 5)"
    )

    sub.add_parser("stats", help="Show vault statistics")

    sub.add_parser(
        "mcp",
        help=(
            "Run the personal_mem MCP server over stdio. "
            "Used by Claude Code plugin registration."
        ),
    )

    p_doctor = sub.add_parser(
        "doctor",
        help=(
            "Coherence linter: tag/concept overlap, unknown tags, "
            "dead vocabulary. Advisory — never modifies the vault."
        ),
    )
    p_doctor.add_argument(
        "--migrate",
        action="store_true",
        help=(
            "Run idempotent one-shot data migrations (e.g. todo+research → "
            "queue) before printing the report."
        ),
    )

    p_connect = sub.add_parser(
        "connect",
        help="Materialize SQLite edges as wikilinks (## See Also) for Obsidian graph",
    )
    p_connect.add_argument("--max-links", type=int, default=5, help="Max links per note (default: 5)")
    p_connect.add_argument("--dry-run", action="store_true", help="Show stats without writing files")

    p_enrich = sub.add_parser(
        "enrich",
        help="LLM-assisted concept assignment for notes missing concepts (uses gpt-5-mini)",
    )
    p_enrich.add_argument("--project", "-p", default="", help="Scope to one project")
    p_enrich.add_argument(
        "--type", "-t", dest="note_types", default="",
        help="Comma-separated types to enrich (default: session,note,decision,source)",
    )
    p_enrich.add_argument("--limit", "-n", type=int, default=0, help="Max notes to process (0=all)")
    p_enrich.add_argument(
        "--force", action="store_true",
        help="Re-enrich notes that already have concepts",
    )
    p_enrich.add_argument("--dry-run", action="store_true", help="Show what would be done")
    p_enrich.add_argument(
        "--reindex", action="store_true", default=True,
        help="Rebuild index after enrichment (default: true)",
    )
    p_enrich.add_argument("--no-reindex", dest="reindex", action="store_false")
    p_enrich.add_argument(
        "--connect", action="store_true", default=True,
        help="Re-run mem connect after reindex (default: true)",
    )
    p_enrich.add_argument("--no-connect", dest="connect", action="store_false")

    p_import = sub.add_parser("import", help="Import from external sources")
    p_import.add_argument("source", choices=["claude-mem", "file", "chatgpt", "messenger"])
    p_import.add_argument("path", nargs="?", default="", help="File path (for 'file'/'chatgpt' source)")
    p_import.add_argument("--source-type", default="article", help="Source type for file import")
    p_import.add_argument("--project", "-p", default="")
    p_import.add_argument("--dry-run", action="store_true", help="Show what would be imported")
    p_import.add_argument("--db-path", default="", help="Path to claude-mem database")
    p_import.add_argument("--limit", type=int, default=0, help="Max conversations to import (chatgpt)")
    p_import.add_argument("--since", default="", help="Import conversations from this date (YYYY-MM-DD)")
    p_import.add_argument("--until", default="", help="Import conversations until this date (YYYY-MM-DD)")
    p_import.add_argument("--no-resolve", action="store_true", help="Skip Facebook URL resolution (messenger)")


def add_admin_subparsers(sub) -> None:
    p_flow = sub.add_parser(
        "flow",
        help="Run named workflow pipelines defined in vault/.mem/flows.yaml",
    )
    flow_sub = p_flow.add_subparsers(dest="flow_action")
    flow_sub.add_parser("list", help="List all named flows.")
    p_flow_show = flow_sub.add_parser("show", help="Print a flow's stages.")
    p_flow_show.add_argument("name", help="Flow name")
    p_flow_run = flow_sub.add_parser("run", help="Execute a flow.")
    p_flow_run.add_argument("name", help="Flow name")
    p_flow_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Print resolved invocations without executing them.",
    )

    p_hooks = sub.add_parser("hooks", help="Manage Claude Code hooks")
    hooks_sub = p_hooks.add_subparsers(dest="hooks_action")
    p_install = hooks_sub.add_parser("install", help="Install hooks")
    p_install.add_argument("--project", "-p", default="")
    hooks_sub.add_parser("uninstall", help="Uninstall hooks")
    p_hooks_status = hooks_sub.add_parser("status", help="Show recent hook errors")
    p_hooks_status.add_argument("--limit", "-n", type=int, default=20, help="Number of lines to show")

    p_intake = sub.add_parser(
        "intake",
        help="Drop-folder intake helpers (enumerate / archive) shared by /substack, /email, ...",
    )
    intake_sub = p_intake.add_subparsers(dest="intake_action")

    p_intake_enum = intake_sub.add_parser(
        "enumerate", help="List inbox entries as JSON on stdout"
    )
    p_intake_enum.add_argument("path", help="Inbox directory")
    p_intake_enum.add_argument(
        "--archive-name",
        default="_processed",
        help="Name of the archive folder to skip (default: _processed)",
    )

    p_intake_arch = intake_sub.add_parser(
        "archive", help="Move an entry into <inbox>/_processed/<YYYY-MM-DD>/"
    )
    p_intake_arch.add_argument("entry", help="Entry path (file or folder) to archive")
    p_intake_arch.add_argument(
        "--inbox", required=True, help="Inbox root (entry must be a direct child)"
    )

    sub.add_parser("init", help="Initialize a new vault")

    p_prune_orphans = sub.add_parser(
        "prune-orphans",
        help="Delete empty/abandoned session folders (no derived notes, no events, no commits)",
    )
    p_prune_orphans.add_argument("--project", "-p", default="", help="Scope to one project")
    p_prune_orphans.add_argument(
        "--dry-run", action="store_true", help="Report what would be deleted without deleting"
    )
    p_prune_orphans.add_argument(
        "--yes", "-y", action="store_true", help="Commit the deletion (default: dry-run)"
    )
    p_prune_orphans.add_argument(
        "--min-age",
        type=int,
        default=3600,
        help="Minimum session age in seconds to be eligible (default: 3600)",
    )

    p_sources = sub.add_parser(
        "sources",
        help="List, inspect, and scaffold source types",
    )
    sources_sub = p_sources.add_subparsers(dest="sources_action")
    sources_sub.add_parser("list", help="List all registered source types")
    p_sources_show = sources_sub.add_parser(
        "show", help="Show full spec for a source type"
    )
    p_sources_show.add_argument("slug", help="Source type slug (e.g. paper, substack)")

    p_sources_scaffold = sources_sub.add_parser(
        "scaffold",
        help=(
            "Register a new source type without editing Python. Writes a "
            "SourceTypeSpec entry to <vault>/.mem/source_types.yaml, a "
            "skill at commands/<slug>.md, and a config block to "
            "vault_templates/.mem/sources.yaml."
        ),
    )
    p_sources_scaffold.add_argument("slug", help="Canonical source_type slug (e.g. podcast, email)")
    p_sources_scaffold.add_argument(
        "--bucket",
        required=True,
        help="Subfolder under vault/sources/ (e.g. podcasts, emails)",
    )
    p_sources_scaffold.add_argument(
        "--layout",
        required=True,
        choices=["flat", "folder", "author_folder"],
        help="On-disk routing pattern",
    )
    p_sources_scaffold.add_argument(
        "--description", default="", help="One-liner shown by `mem sources list`"
    )
    p_sources_scaffold.add_argument(
        "--aliases",
        default="",
        help="Comma-separated legacy slugs that should fold into the new slug on write",
    )

    p_skill = sub.add_parser(
        "skill",
        help="List, inspect, and run skills from commands/",
    )
    skill_sub = p_skill.add_subparsers(dest="skill_action")
    skill_sub.add_parser("list", help="List all skills with their frontmatter")
    p_skill_show = skill_sub.add_parser("show", help="Show a skill's frontmatter + head")
    p_skill_show.add_argument("name", help="Skill name (without .md)")

    p_landing = sub.add_parser("landing", help="Generate landing documents")
    p_landing.add_argument(
        "--project", "-p", default="",
        help="Project name (ignored for global docs like 'themes')",
    )
    p_landing.add_argument(
        "--doc", "-d", default="all",
        choices=["all", "decisions", "backlog", "state", "themes"],
        help="Which document(s) to generate (default: all)",
    )
