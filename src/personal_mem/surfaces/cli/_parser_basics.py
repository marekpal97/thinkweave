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
    p_add.add_argument(
        "--frontmatter", "-f", action="append", default=[],
        help=(
            "Extra frontmatter key=value (repeatable). For source notes, set "
            "source_type and outlet here so SourceTypeSpec layout routing "
            "applies — e.g. -f source_type=news -f outlet=wolf-street. "
            "Without this, source notes land at sources/<slug>/source.md "
            "instead of sources/<bucket>/<author>/<slug>/source.md."
        ),
    )

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
        "--only-new",
        action="store_true",
        help=(
            "With --embed: only embed notes whose updated_at is newer than "
            "the most recent cached embedding (the keep-warm cron path). "
            "Falls back to a full scan on an empty embeddings table."
        ),
    )
    p_index.add_argument(
        "--since",
        default="",
        help=(
            "With --embed: alternative cutoff for --only-new — embed notes "
            "whose updated_at > <ISO timestamp> (e.g. 2026-05-01). "
            "Overrides the derived cutoff when both are passed."
        ),
    )
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
            "dead vocabulary. Advisory — never modifies the vault. "
            "Use --mcp for MCP-wiring diagnostics, --all for both."
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
    p_doctor.add_argument(
        "--fix-phantoms",
        action="store_true",
        help=(
            "Delete zero-byte n-*/dec-*/src-* phantom files at vault root "
            "(unresolved-wikilink residue). Safe; never touches non-empty files."
        ),
    )
    p_doctor.add_argument(
        "--mcp",
        action="store_true",
        help=(
            "Run MCP-registration diagnostics only: which scopes declare "
            "personal-mem, whether the launcher resolves, env-var sanity. "
            "Exits non-zero on FAIL."
        ),
    )
    p_doctor.add_argument(
        "--all",
        action="store_true",
        help="Run vault coherence + MCP diagnostics together.",
    )
    p_doctor.add_argument(
        "--isolation",
        action="store_true",
        help=(
            "Append an isolation diagnostic: notes with no graph edges, "
            "broken down by type / concept-count bucket / project, plus "
            "10 examples. Opt-in because output is verbose; useful before "
            "running `mem enrich` to characterise what's reachable."
        ),
    )

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
        "--via",
        choices=["inline", "batch"],
        default=None,
        help=(
            "Execution route. 'batch' = wrapper async fan-out (needs an "
            "API key); 'inline' = /enrich-notes CC skill (uses the running "
            "model, no provider key). Default: auto (batch when key + "
            ">200 items, else inline)."
        ),
    )
    p_enrich.add_argument(
        "--reindex", action="store_true", default=True,
        help="Rebuild index after enrichment (default: true)",
    )
    p_enrich.add_argument("--no-reindex", dest="reindex", action="store_false")
    p_enrich.add_argument(
        "--connect", action="store_true", default=True,
        help="Materialize SQLite edges as wikilinks after reindex (default: true)",
    )
    p_enrich.add_argument("--no-connect", dest="connect", action="store_false")

    p_import = sub.add_parser("import", help="Import from external sources")
    p_import.add_argument(
        "source",
        choices=["claude-code", "claude-history", "file", "chatgpt", "messenger"],
    )
    p_import.add_argument("path", nargs="?", default="", help="File path (for 'file'/'chatgpt' source)")
    p_import.add_argument("--source-type", default="article", help="Source type for file import")
    p_import.add_argument("--project", "-p", default="")
    p_import.add_argument("--dry-run", action="store_true", help="Show what would be imported")
    p_import.add_argument("--db-path", default="", help="Path to claude-mem database")
    p_import.add_argument(
        "--cc-root",
        default="",
        help="Override Claude Code projects root (default ~/.claude/projects)",
    )
    p_import.add_argument(
        "--enrich",
        action="store_true",
        help=(
            "claude-code: enrich previously-materialized sessions with "
            "decisions/insights (does not re-materialize)."
        ),
    )
    p_import.add_argument(
        "--via",
        choices=["inline", "batch"],
        default=None,
        help=(
            "Execution route for claude-code --enrich and chatgpt imports. "
            "'batch' = wrapper async fan-out; 'inline' = CC skill "
            "(/seed-enrich for claude-code-enrich, /import-chatgpt for "
            "chatgpt). Default: auto via choose_route()."
        ),
    )
    p_import.add_argument(
        "--enrich-limit",
        type=int,
        default=0,
        help="Cap how many pending sessions to enrich in this batch (0 = all).",
    )
    p_import.add_argument(
        "--enrich-model",
        default="claude-haiku-4-5-20251001",
        help="Claude model for batch enrichment.",
    )
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

    p_schedule = sub.add_parser(
        "schedule",
        help="Install recurring jobs (vault/config/scheduling.yaml) onto the "
        "host scheduler — crontab on Linux/macOS, Task Scheduler on Windows.",
    )
    sched_sub = p_schedule.add_subparsers(dest="schedule_action")
    sched_sub.add_parser("list", help="List scheduled jobs + the resolved backend.")
    p_sched_install = sched_sub.add_parser(
        "install", help="Render + install the jobs onto the native scheduler."
    )
    p_sched_uninstall = sched_sub.add_parser(
        "uninstall", help="Remove personal-mem's scheduled jobs."
    )
    for p in (p_sched_install, p_sched_uninstall):
        p.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change without touching the scheduler.",
        )
        p.add_argument(
            "--only",
            default="",
            help="Comma-separated job names to act on (default: all).",
        )

    p_hooks = sub.add_parser("hooks", help="Manage Claude Code hooks")
    hooks_sub = p_hooks.add_subparsers(dest="hooks_action")
    p_install = hooks_sub.add_parser("install", help="Install hooks")
    p_install.add_argument("--project", "-p", default="")
    p_install.add_argument(
        "--scope",
        choices=("project", "user"),
        default="project",
        help=(
            "project (default): write to <project>/.claude/settings.local.json; "
            "user: write to ~/.claude/settings.json (fires in every CC session)"
        ),
    )
    p_install.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned settings.json diff without writing.",
    )
    p_uninstall = hooks_sub.add_parser("uninstall", help="Uninstall hooks")
    p_uninstall.add_argument("--project", "-p", default="")
    p_uninstall.add_argument(
        "--scope",
        choices=("project", "user"),
        default="project",
        help="Settings scope to remove hooks from (mirror of install).",
    )
    p_uninstall.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned settings.json diff without writing.",
    )
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

    p_install = sub.add_parser(
        "install",
        help=(
            "Machine-scope setup: verify console scripts and register the "
            "personal-mem MCP server in ~/.claude.json (idempotent)."
        ),
    )
    p_install.add_argument(
        "--vault",
        default=None,
        help=(
            "Default PERSONAL_MEM_VAULT to embed in the MCP entry's env "
            "(optional — leave unset to inherit from shell)."
        ),
    )
    p_install.add_argument(
        "--yes", "-y", action="store_true",
        help="Proceed without prompting on create or overwrite.",
    )
    p_install.add_argument(
        "--no-claude-md", action="store_true",
        help=(
            "Skip the small personal_mem block normally appended to "
            "~/.claude/CLAUDE.md (a persistent nudge to prefer mem_* tools "
            "over filesystem search). MCP registration still happens."
        ),
    )

    p_uninstall = sub.add_parser(
        "uninstall",
        help=(
            "Reverse `mem install` — remove the MCP entry from "
            "~/.claude.json, the personal_mem block from "
            "~/.claude/CLAUDE.md, and any leftover pause marker. "
            "Hooks, vault, plugin manifest, and cron jobs are untouched."
        ),
    )
    p_uninstall.add_argument(
        "--yes", "-y", action="store_true",
        help="Proceed without prompting (otherwise prints a preview and exits).",
    )

    p_pause = sub.add_parser(
        "pause",
        help=(
            "Temporarily disable personal_mem (remove user-scope hooks, MCP "
            "entry, and CLAUDE.md block). Vault untouched. Reversed by `mem resume`."
        ),
    )
    p_pause.add_argument(
        "--status", action="store_true",
        help="Report whether personal_mem is currently paused and exit.",
    )

    sub.add_parser(
        "resume",
        help="Restore personal_mem touchpoints removed by `mem pause`.",
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

    p_wrap_finalize = sub.add_parser(
        "wrap-finalize",
        help=(
            "Deterministic tail of /mem-wrap: prune orphans → index → judge "
            "extracted decisions → refresh DECISIONS/BACKLOG → concept-drift "
            "advisory, all in one process. Run after mem_extract has written "
            "the session's insights/decisions."
        ),
    )
    p_wrap_finalize.add_argument(
        "session_id", help="Session note ID (ses-...) that was just extracted"
    )
    p_wrap_finalize.add_argument(
        "--project", "-p", default="",
        help="Project (defaults to PERSONAL_MEM_PROJECT)",
    )
    p_wrap_finalize.add_argument(
        "--json", action="store_true",
        help="Emit a JSON summary on stdout (for headless flows)",
    )
    p_wrap_finalize.add_argument(
        "--no-prune", action="store_true",
        help="Skip the orphan-prune step",
    )

    p_spend = sub.add_parser(
        "spend",
        help=(
            "Report LLM spend. With a session id: Layer A (Claude turns, read "
            "from the native transcript) + Layer B (mem internal ops). With "
            "--since/--until: a date-range rollup over all runs + headless ops."
        ),
    )
    p_spend.add_argument(
        "session_id", nargs="?", default="",
        help="Session id (ses-… / Claude UUID) for a single-session report",
    )
    p_spend.add_argument(
        "--project", "-p", default="",
        help="Project (defaults to PERSONAL_MEM_PROJECT) — locates Layer-B events",
    )
    p_spend.add_argument("--since", default="", help="Range start (YYYY-MM-DD)")
    p_spend.add_argument("--until", default="", help="Range end (YYYY-MM-DD)")
    p_spend.add_argument(
        "--ops-only",
        action="store_true",
        help=(
            "Range mode only: report just personal_mem's own operating cost — "
            "all Layer-B internal ops plus Layer-A turns of mem-skill runs "
            "(/dream, /drain, /mem-wrap, …), excluding interactive coding."
        ),
    )
    p_spend.add_argument(
        "--json", action="store_true", help="Emit the summary as JSON"
    )

    p_judge = sub.add_parser(
        "judge",
        help=(
            "Prediction-verdict pipeline. Drain the rejudge queue (emit "
            "JSON worklist for /judge-prediction), list pending decisions, "
            "or manually rejudge a single decision."
        ),
    )
    judge_actions = p_judge.add_mutually_exclusive_group(required=False)
    judge_actions.add_argument(
        "--drain", action="store_true",
        help=(
            "Drain the supersession-triggered rejudge queue, merge in "
            "cron-style pending_due stragglers, emit a JSON worklist on "
            "stdout for /judge-prediction to consume."
        ),
    )
    judge_actions.add_argument(
        "--rejudge", metavar="DEC_ID", default="",
        help=(
            "Enqueue DEC_ID for re-judgment (source=manual) and shell to "
            "`claude -p \"/judge-prediction --decision DEC_ID\"`. Inherits "
            "stdio; exits with the subprocess's return code."
        ),
    )
    judge_actions.add_argument(
        "--list-pending", action="store_true",
        help=(
            "Read-only: print decision ids whose prediction_match == "
            "'pending', one per line on stdout. Use --json for a JSON array."
        ),
    )
    p_judge.add_argument(
        "--max", type=int, default=20,
        help="Cap worklist size for --drain (default: 20)",
    )
    p_judge.add_argument(
        "--json", action="store_true",
        help=(
            "Emit JSON output. Implied by --drain (always JSON). Optional "
            "for --list-pending (default is plain text, one id per line)."
        ),
    )

    p_rlvr = sub.add_parser(
        "rlvr",
        help=(
            "RLVR (decision-context RL) data export. One row per decision "
            "joining frontmatter + body citations + context_served. "
            "Schema lives in operations/rlvr_export.RLVRRow."
        ),
    )
    rlvr_sub = p_rlvr.add_subparsers(dest="rlvr_action")
    p_rlvr_export = rlvr_sub.add_parser(
        "export",
        help="Stream RLVR rows as JSONL on stdout (one decision per line).",
    )
    p_rlvr_export.add_argument(
        "--project", "-p", default="",
        help="Filter to a single project (default: all projects).",
    )
    p_rlvr_export.add_argument(
        "--since", default="",
        help="Earliest decision date (YYYY-MM-DD, inclusive).",
    )
    p_rlvr_export.add_argument(
        "--until", default="",
        help="Latest decision date (YYYY-MM-DD, inclusive).",
    )
    p_rlvr_export.add_argument(
        "--committed-only", action="store_true",
        help="Skip rows whose decision was not committed.",
    )
    p_rlvr_export.add_argument(
        "--explode-history", action="store_true",
        help=(
            "Emit one row per prediction-history entry instead of one row "
            "per decision. Each row carries per-entry match/judged_at/reason "
            "and a 0-based entry_index. Decisions without history still emit "
            "exactly one row."
        ),
    )
    p_rlvr_export.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print a 'N rows emitted' summary on stderr after streaming.",
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
    p_sources_scaffold.add_argument(
        "--skill-target",
        choices=["user", "repo", "none"],
        default="user",
        help=(
            "Where to write the skill file. 'user' = ~/.claude/commands/ "
            "(machine-global, default — works across all projects). "
            "'repo' = personal_mem/commands/ (legacy; for contributor "
            "use). 'none' = skip skill creation entirely."
        ),
    )

    p_skill = sub.add_parser(
        "skill",
        help="List, inspect, and run skills from commands/",
    )
    skill_sub = p_skill.add_subparsers(dest="skill_action")
    skill_sub.add_parser("list", help="List all skills with their frontmatter")
    p_skill_show = skill_sub.add_parser("show", help="Show a skill's frontmatter + head")
    p_skill_show.add_argument("name", help="Skill name (without .md)")

    p_news_stats = sub.add_parser(
        "news-stats",
        help=(
            "Per-outlet drain stats from vault/.mem/queues/_processed/. "
            "Surfaces drop/accept/fail rates so you can prune the "
            "news_feeds.yaml registry on evidence."
        ),
    )
    p_news_stats.add_argument(
        "--days", type=int, default=30,
        help="Window size in days (default: 30)",
    )
    p_news_stats.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of the formatted table.",
    )

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

    p_dream = sub.add_parser(
        "dream",
        help=(
            "Periodic vault-hygiene cycle (the backbone of /dream). "
            "Two actions: scan (read-only action plan) and apply "
            "(execute an LLM-judged plan with one index rebuild + "
            "maintenance.jsonl log)."
        ),
    )
    dream_sub = p_dream.add_subparsers(dest="dream_action")

    p_dream_scan = dream_sub.add_parser(
        "scan",
        help=(
            "Read-only scan: drift, promotion candidates, theme "
            "cluster signals (mint/extend), recent probe pressure. "
            "Emit as table or JSON."
        ),
    )
    p_dream_scan.add_argument("--project", "-p", default="")
    p_dream_scan.add_argument(
        "--promotion-cap", type=int, default=20,
        help="Max promotion candidates to surface per cycle (default: 20)",
    )
    p_dream_scan.add_argument(
        "--promotion-threshold", type=int, default=5,
        help="Min proposed-concept count for promotion eligibility (default: 5)",
    )
    p_dream_scan.add_argument(
        "--json", action="store_true",
        help="Emit raw JSON for skill/headless consumption",
    )

    p_dream_apply = dream_sub.add_parser(
        "apply",
        help=(
            "Execute a dream-cycle plan. Reads JSON from --plan path "
            "(or '-' for stdin). One index rebuild + one log line."
        ),
    )
    p_dream_apply.add_argument(
        "--plan", required=True,
        help="Path to JSON plan file, or '-' to read from stdin",
    )
    p_dream_apply.add_argument("--project", "-p", default="")
    p_dream_apply.add_argument(
        "--dry-run", action="store_true",
        help="Parse and validate the plan; report what would apply; do not write.",
    )
    p_dream_apply.add_argument(
        "--json", action="store_true",
        help="Emit raw JSON result on stdout",
    )
    # Strict plan-fragment validation: unknown top-level / sub-keys abort
    # instead of silently no-opping. Default ON — the orchestrator should
    # see worker drift loudly. Pass --no-strict for legacy plans where any
    # single drift shouldn't kill the cycle.
    p_dream_apply.add_argument(
        "--strict", dest="strict", action="store_true", default=True,
        help=(
            "Abort on unknown plan keys or item sub-keys (default ON). "
            "Catches worker-fragment drift like ``add_source_ids`` for "
            "``source_ids`` that would otherwise silently no-op."
        ),
    )
    p_dream_apply.add_argument(
        "--no-strict", dest="strict", action="store_false",
        help=(
            "Surface unknown plan keys as errors on the result but still "
            "run apply. Use for legacy plans where individual drift "
            "shouldn't abort the cycle."
        ),
    )

    p_dream_tasks = dream_sub.add_parser(
        "tasks",
        help=(
            "Enumerate the subagent tasks the /dream orchestrator should "
            "spawn for one phase. Consumes the scan JSON (optionally piped "
            "in from `mem dream scan --json`), filters the dream_tasks "
            "REGISTRY by phase + has_signal, and emits one JSON list of "
            "{surface_key, worker_name, plan_keys, depends_on} entries."
        ),
    )
    p_dream_tasks.add_argument(
        "--phase", type=int, choices=(1, 2), required=True,
        help="Phase to enumerate (1=synthesis, 2=composition/consumption)",
    )
    p_dream_tasks.add_argument(
        "--scan", default=None,
        help=(
            "Path to a scan JSON payload (output of `mem dream scan --json`). "
            "If omitted, runs scan(cfg) fresh."
        ),
    )
    p_dream_tasks.add_argument(
        "--apply-result", dest="apply_result", default=None,
        help=(
            "Path to a DreamCycleResult JSON payload. Required by some "
            "phase-2 workers in future revisions; pass-through for v1."
        ),
    )
    p_dream_tasks.add_argument("--project", "-p", default="")
    p_dream_tasks.add_argument(
        "--json", action="store_true",
        help="Emit raw JSON for skill/headless consumption",
    )

    # --- C24: CLI parity for MCP-only tools -------------------------------
    p_unlink = sub.add_parser(
        "unlink",
        help="Remove a typed edge between two notes (CLI parity for mem_unlink).",
    )
    p_unlink.add_argument("source", help="Source note ID")
    p_unlink.add_argument("target", help="Target note ID")
    p_unlink.add_argument(
        "--type", "-t", default="relates_to",
        choices=[e.value for e in EdgeType],
    )

    p_timeline = sub.add_parser(
        "timeline",
        help=(
            "Chronological session+decision window (CLI parity for mem_timeline). "
            "Without --project: cross-project ranking by activity."
        ),
    )
    p_timeline.add_argument("--project", "-p", default="")
    p_timeline.add_argument("--days", "-d", type=int, default=7)
    p_timeline.add_argument("--json", action="store_true")

    p_snap = sub.add_parser(
        "project-snapshot",
        help=(
            "Re-fetch the SessionStart context payload for a project "
            "(CLI parity for mem_project_snapshot)."
        ),
    )
    p_snap.add_argument("project", help="Project name")
    p_snap.add_argument(
        "--sections", default="",
        help="Comma-separated section names to include (omit for default).",
    )
    p_snap.add_argument(
        "--budget-tokens", type=int, default=0,
        help="Token budget (0 = default).",
    )

    p_prompts = sub.add_parser(
        "prompts",
        help=(
            "List user prompts captured by the UserPromptSubmit hook "
            "(CLI parity for mem_prompts)."
        ),
    )
    p_prompts.add_argument("--project", "-p", default="")
    p_prompts.add_argument(
        "--since", default="",
        help="Earliest ISO date/datetime (YYYY-MM-DD).",
    )
    p_prompts.add_argument("--limit", "-n", type=int, default=50)
    p_prompts.add_argument(
        "--classified-as", default="",
        help="Filter by classification (e.g. 'probe').",
    )
    p_prompts.add_argument("--json", action="store_true")
