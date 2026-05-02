"""CLI entry point for personal_mem.

Usage: mem <command> [options]
All subcommands use argparse (no external dependencies).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from personal_mem.config import load_config
from personal_mem.schemas import EdgeType, NoteType


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mem",
        description="Obsidian-native universal memory layer",
    )
    sub = parser.add_subparsers(dest="command")

    # --- mem add ---
    p_add = sub.add_parser("add", help="Create a new note")
    p_add.add_argument("title", help="Note title")
    p_add.add_argument("--type", "-t", default="note", choices=[t.value for t in NoteType])
    p_add.add_argument("--project", "-p", default="")
    p_add.add_argument("--tags", default="", help="Comma-separated tags")
    p_add.add_argument("--body", "-b", default="", help="Note body (or pipe via stdin)")
    p_add.add_argument("--session", "-s", default="", help="Session ID to place note in")

    # --- mem search ---
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

    # --- mem decisions --file ---
    p_decisions = sub.add_parser(
        "decisions", help="Query decisions — e.g. every decision that touched a file"
    )
    p_decisions.add_argument("--file", "-f", dest="file_path", default="", help="File path to filter by")
    p_decisions.add_argument("--project", "-p", default="")
    p_decisions.add_argument("--status", default="", help="Filter by status (accepted/proposed/deprecated/superseded)")
    p_decisions.add_argument("--limit", "-n", type=int, default=50)

    # --- mem project ---
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

    # --- mem show ---
    p_show = sub.add_parser("show", help="Display a note by ID")
    p_show.add_argument("id", help="Note ID")

    # --- mem link ---
    p_link = sub.add_parser("link", help="Create a relationship between notes")
    p_link.add_argument("source", help="Source note ID")
    p_link.add_argument("target", help="Target note ID")
    p_link.add_argument(
        "--type", "-t", default="relates_to", choices=[e.value for e in EdgeType]
    )

    # --- mem graph ---
    p_graph = sub.add_parser("graph", help="Show local graph around a note")
    p_graph.add_argument("id", help="Center note ID")
    p_graph.add_argument("--depth", "-d", type=int, default=2)
    p_graph.add_argument("--format", "-f", default="text", choices=["text", "mermaid"])

    # --- mem index ---
    p_index = sub.add_parser("index", help="Rebuild the SQLite index")
    p_index.add_argument("--full", action="store_true", help="Full rebuild (drop and recreate)")
    p_index.add_argument("--embed", action="store_true", help="Compute embeddings via API")

    # --- mem import ---
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

    # --- mem context ---
    p_context = sub.add_parser("context", help="Get relevant notes for current context")
    p_context.add_argument("--project", "-p", default="")
    p_context.add_argument("--tags", default="", help="Comma-separated tags")
    p_context.add_argument("--query", "-q", default="")
    p_context.add_argument("--concepts", default="", help="Comma-separated concepts for concept-based retrieval")
    p_context.add_argument("--limit", "-n", type=int, default=5)

    # --- mem stats ---
    sub.add_parser("stats", help="Show vault statistics")

    # --- mem doctor ---
    sub.add_parser(
        "doctor",
        help=(
            "Coherence linter: tag/concept overlap, unknown tags, "
            "dead vocabulary. Advisory — never modifies the vault."
        ),
    )

    # --- mem flow ---
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

    # --- mem hooks ---
    p_hooks = sub.add_parser("hooks", help="Manage Claude Code hooks")
    hooks_sub = p_hooks.add_subparsers(dest="hooks_action")
    p_install = hooks_sub.add_parser("install", help="Install hooks")
    p_install.add_argument("--project", "-p", default="")
    hooks_sub.add_parser("uninstall", help="Uninstall hooks")
    p_hooks_status = hooks_sub.add_parser("status", help="Show recent hook errors")
    p_hooks_status.add_argument("--limit", "-n", type=int, default=20, help="Number of lines to show")

    # --- mem intake ---
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

    # --- mem backlog ---
    p_backlog = sub.add_parser("backlog", help="List notes tagged 'todo'")
    p_backlog.add_argument("--project", "-p", default="", help="Filter by project")
    p_backlog.add_argument("--tag", default="todo", help="Tag to query (default: todo)")

    # --- mem concepts ---
    p_concepts = sub.add_parser("concepts", help="List, drift, merge, prune concepts")
    concepts_sub = p_concepts.add_subparsers(dest="concepts_action")
    p_concepts_list = concepts_sub.add_parser("list", help="List all concepts with counts")
    p_concepts_list.add_argument("--prefix", default="", help="Filter by prefix")
    p_concepts_list.add_argument("--min-count", type=int, default=1, help="Minimum note count")
    p_merge = concepts_sub.add_parser("merge", help="Merge one concept into another")
    p_merge.add_argument("from_concept", help="Concept to rename/remove")
    p_merge.add_argument("to_concept", help="Canonical concept to merge into")
    p_prune = concepts_sub.add_parser("prune", help="Remove low-count concepts from notes")
    p_prune.add_argument("--dry-run", action="store_true", help="Show what would be pruned")
    p_concepts_hubs = concepts_sub.add_parser(
        "hubs", help="Generate or prune Obsidian hub pages"
    )
    p_concepts_hubs.add_argument(
        "--prune",
        action="store_true",
        help=(
            "Find and delete orphan hub pages (concepts with zero vault "
            "notes that aren't in ontology.yaml). Read-only without --apply."
        ),
    )
    p_concepts_hubs.add_argument(
        "--apply",
        action="store_true",
        help="With --prune, actually delete the orphans (otherwise list only).",
    )
    p_drift = concepts_sub.add_parser(
        "drift",
        help="Advisory drift report (near-dupes, new ontology candidates, stale hubs)",
    )
    p_drift.add_argument("--project", "-p", default="", help="Optional project scope")
    p_drift.add_argument("--threshold", type=int, default=5, help="Min count for candidates")
    p_drift.add_argument("--max-items", type=int, default=5, help="Max per category")
    p_drift.add_argument(
        "--hubs",
        action="store_true",
        help=(
            "Also surface redundant-hub candidates: pairs of concept hubs "
            "with overlapping essence content (Jaccard pre-filter; LLM "
            "judgment lives in /mem-resolve-concepts)."
        ),
    )
    p_drift.add_argument(
        "--hub-jaccard",
        type=float,
        default=0.4,
        help="Minimum Jaccard similarity for hub-pair candidates (default: 0.4)",
    )
    p_notes = concepts_sub.add_parser("notes", help="List notes for a specific concept")
    p_notes.add_argument("concept", help="Concept to search for")
    p_notes.add_argument("--project", "-p", default="", help="Filter by project")

    # --- mem hubs ---
    p_hubs = sub.add_parser(
        "hubs",
        help="Concept hub pages — plan, run (backfill), and status",
    )
    hubs_sub = p_hubs.add_subparsers(dest="hubs_action")

    p_hubs_plan = hubs_sub.add_parser(
        "plan", help="Walk the vault and write a JSON plan for hub backfill"
    )
    p_hubs_plan.add_argument(
        "--out", default="", help="Plan output path (default: .mem/hubs_plan.json)"
    )
    p_hubs_plan.add_argument("--concept", default="", help="Restrict to one concept")
    p_hubs_plan.add_argument("--project", default="", help="Restrict to one project")
    p_hubs_plan.add_argument("--note-type", default="", help="Restrict to one note type")
    p_hubs_plan.add_argument(
        "--limit-notes",
        type=int,
        default=0,
        help="Cap unprocessed notes per concept (0 = no cap)",
    )
    p_hubs_plan.add_argument(
        "--limit-concepts",
        type=int,
        default=0,
        help="Cap total concepts in the plan (0 = no cap)",
    )

    p_hubs_run = hubs_sub.add_parser(
        "run",
        help="Execute a backfill plan via the OpenAI SDK + Batches API",
    )
    p_hubs_run.add_argument(
        "--plan", default="", help="Path to plan JSON (default: .mem/hubs_plan.json)"
    )
    p_hubs_run.add_argument(
        "--model",
        default="gpt-5-mini",
        help="OpenAI model to use (default: gpt-5-mini)",
    )
    p_hubs_run.add_argument(
        "--max-tokens",
        type=int,
        default=1024,
        help="Max output tokens per request (default: 1024)",
    )
    p_hubs_run.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        help="Seconds between batch status polls (default: 30)",
    )
    p_hubs_run.add_argument(
        "--max-input-tokens",
        type=int,
        default=4_500_000,
        help=(
            "Cap enqueued input tokens per batch (default: 4,500,000, safely "
            "under OpenAI's 5M gpt-5-mini org limit). 0 = no cap. Requests "
            "past the cap are deferred to a subsequent run."
        ),
    )
    p_hubs_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Build requests and print the first one, but don't submit to the API",
    )

    p_hubs_status = hubs_sub.add_parser(
        "status",
        help="Show processed state per concept (cited vs total)",
    )
    p_hubs_status.add_argument("--concept", default="", help="Restrict to one concept")

    p_hubs_repair = hubs_sub.add_parser(
        "repair",
        help=(
            "Retroactively fix hub log entries: swap backfill dates for the "
            "cited note's real date, strip duplicated inline wikilink citations."
        ),
    )
    p_hubs_repair.add_argument("--concept", default="", help="Restrict to one concept")
    p_hubs_repair.add_argument(
        "--dry-run",
        action="store_true",
        help="Report changes per hub without writing",
    )

    p_hubs_link = hubs_sub.add_parser(
        "link",
        help=(
            "Temporal-DAG linkage pass: rewrite flat `new` flags into "
            "agrees/contradicts/extends relationships via gpt-5-mini Batches API."
        ),
    )
    p_hubs_link.add_argument("--concept", default="", help="Restrict to one concept")
    p_hubs_link.add_argument(
        "--model",
        default="gpt-5-mini",
        help="OpenAI model to use (default: gpt-5-mini)",
    )
    p_hubs_link.add_argument(
        "--max-tokens",
        type=int,
        default=2048,
        help="Max output tokens per request (default: 2048; linkage responses are longer than per-note extractions)",
    )
    p_hubs_link.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        help="Seconds between batch status polls (default: 30)",
    )
    p_hubs_link.add_argument(
        "--max-input-tokens",
        type=int,
        default=4_500_000,
        help="Cap enqueued input tokens per batch (default: 4,500,000, under OpenAI's 5M org limit). 0 = no cap.",
    )
    p_hubs_link.add_argument(
        "--min-entries",
        type=int,
        default=2,
        help="Skip hubs with fewer than N entries (default: 2)",
    )
    p_hubs_link.add_argument(
        "--dry-run",
        action="store_true",
        help="Build requests and print the first one, but don't submit to the API",
    )

    # --- mem landing ---
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

    # --- mem init ---
    sub.add_parser("init", help="Initialize a new vault")

    # --- mem prune-orphans ---
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

    # --- mem enrich ---
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

    # --- mem connect ---
    p_connect = sub.add_parser(
        "connect",
        help="Materialize SQLite edges as wikilinks (## See Also) for Obsidian graph",
    )
    p_connect.add_argument("--max-links", type=int, default=5, help="Max links per note (default: 5)")
    p_connect.add_argument("--dry-run", action="store_true", help="Show stats without writing files")

    # --- mem sources ---
    p_sources = sub.add_parser(
        "sources",
        help="List and inspect registered source types",
    )
    sources_sub = p_sources.add_subparsers(dest="sources_action")
    sources_sub.add_parser("list", help="List all registered source types")
    p_sources_show = sources_sub.add_parser(
        "show", help="Show full spec for a source type"
    )
    p_sources_show.add_argument("slug", help="Source type slug (e.g. paper, substack)")

    # --- mem skill ---
    p_skill = sub.add_parser(
        "skill",
        help="List, inspect, and run skills from commands/",
    )
    skill_sub = p_skill.add_subparsers(dest="skill_action")
    skill_sub.add_parser("list", help="List all skills with their frontmatter")
    p_skill_show = skill_sub.add_parser("show", help="Show a skill's frontmatter + head")
    p_skill_show.add_argument("name", help="Skill name (without .md)")

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Dispatch
    commands = {
        "add": cmd_add,
        "backlog": cmd_backlog,
        "concepts": cmd_concepts,
        "decisions": cmd_decisions,
        "hubs": cmd_hubs,
        "landing": cmd_landing,
        "project": cmd_project,
        "prune-orphans": cmd_prune_orphans,
        "enrich": cmd_enrich,
        "connect": cmd_connect,
        "search": cmd_search,
        "show": cmd_show,
        "link": cmd_link,
        "graph": cmd_graph,
        "index": cmd_index,
        "import": cmd_import,
        "context": cmd_context,
        "stats": cmd_stats,
        "doctor": cmd_doctor,
        "flow": cmd_flow,
        "hooks": cmd_hooks,
        "init": cmd_init,
        "intake": cmd_intake,
        "sources": cmd_sources,
        "skill": cmd_skill,
    }
    commands[args.command](args)


def cmd_backlog(args: argparse.Namespace) -> None:
    from personal_mem.search import Search

    cfg = load_config()
    s = Search(config=cfg)

    results = s.search(
        query="",
        project=args.project,
        tags=[args.tag],
        limit=50,
    )
    s.close()

    if not results:
        print(f"No notes tagged '{args.tag}'.")
        return

    # Group by project
    by_project: dict[str, list] = {}
    for r in results:
        proj = r.project or "(unscoped)"
        by_project.setdefault(proj, []).append(r)

    for proj, notes in sorted(by_project.items()):
        print(f"\n{proj}:")
        for r in notes:
            tag_str = f" [{', '.join(t for t in r.tags if t != args.tag)}]" if len(r.tags) > 1 else ""
            print(f"  [{r.type}] {r.title} ({r.id}) {r.date}{tag_str}")


def cmd_landing(args: argparse.Namespace) -> None:
    from personal_mem.landing import write_landing_docs

    cfg = load_config()
    project = args.project or cfg.default_project

    # Themes is global — doesn't need a project. Other docs do.
    if args.doc != "themes" and not project:
        print("Project name required. Use --project or set PERSONAL_MEM_PROJECT.")
        sys.exit(1)

    written = write_landing_docs(cfg, project, docs=args.doc)
    for filename, path in written.items():
        print(f"  {filename} → {path.relative_to(cfg.vault_root)}")
    print(f"Generated {len(written)} landing document(s).")


def cmd_hubs(args: argparse.Namespace) -> None:
    """Concept hub page management — plan, run, status."""
    cfg = load_config()
    action = args.hubs_action or "status"

    if action == "plan":
        _hubs_plan(cfg, args)
    elif action == "run":
        _hubs_run(cfg, args)
    elif action == "status":
        _hubs_status(cfg, args)
    elif action == "repair":
        _hubs_repair(cfg, args)
    elif action == "link":
        _hubs_link(cfg, args)
    else:
        print(f"Unknown hubs action: {action}")
        sys.exit(1)


def _hubs_plan(cfg, args: argparse.Namespace) -> None:
    from personal_mem.hubs import build_plan, plan_to_dict

    plans = build_plan(
        cfg,
        project=args.project,
        note_type=args.note_type,
        concept_filter=args.concept,
        limit_notes_per_concept=args.limit_notes,
        limit_concepts=args.limit_concepts,
    )

    payload = plan_to_dict(plans)
    out_path = Path(args.out) if args.out else (cfg.mem_dir / "hubs_plan.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"Plan: {out_path}")
    print(f"  concepts: {payload['total_concepts']}")
    print(f"  unprocessed notes: {payload['total_notes']}")
    print(f"  est input tokens: {payload['est_input_tokens']:,}")
    if not plans:
        print("  (nothing to process — all hubs are caught up)")
        return
    print("\n  Top concepts by unprocessed note count:")
    for p in plans[:10]:
        dom = f" [{', '.join(p.domains)}]" if p.domains else ""
        print(f"    {len(p.unprocessed_notes):4d}  {p.concept}{dom}")


def _hubs_run(cfg, args: argparse.Namespace) -> None:
    from personal_mem.hubs import (
        LogEntry,
        append_log_entries,
        build_extraction_user_prompt,
        concept_hub_path,
        parse_concept_hub,
        parse_llm_response,
        HUB_EXTRACTION_SYSTEM,
    )
    from personal_mem.indexer import Indexer

    plan_path = Path(args.plan) if args.plan else (cfg.mem_dir / "hubs_plan.json")
    if not plan_path.exists():
        print(f"Plan file not found: {plan_path}")
        print("Run `mem hubs plan` first.")
        sys.exit(1)

    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    concept_plans = payload.get("concepts", [])
    if not concept_plans:
        print("Plan is empty.")
        return

    # Build requests per concept — one LLM call per (concept, note) pair.
    # Within a concept, the system prompt is stable (current essence + recent
    # entries), so we mark it cacheable.
    from personal_mem.vault import VaultManager

    vm = VaultManager(config=cfg)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build requests list first so we can preview / dry-run
    requests_to_send = []  # [(concept, note_id, system_prompt, user_prompt)]

    for cp in concept_plans:
        concept = cp["concept"]
        hub_path = concept_hub_path(cfg, concept)
        hub = parse_concept_hub(hub_path, concept=concept)

        for note_entry in cp["unprocessed_notes"]:
            note_path = vm.root / note_entry["path"]
            if not note_path.exists():
                continue
            note_text = note_path.read_text(encoding="utf-8")
            # Strip frontmatter for the body input
            from personal_mem.vault import parse_frontmatter
            _, body = parse_frontmatter(note_text)

            user_prompt = build_extraction_user_prompt(
                concept=concept,
                essence=hub.essence,
                recent_entries=hub.log_entries,
                note_id=note_entry["id"],
                note_type=note_entry.get("type", "note"),
                project=note_entry.get("project", ""),
                date=note_entry.get("date", ""),
                title=note_entry.get("title", ""),
                body=body,
            )
            # Use the source note's own date so the learning log carries
            # real temporal structure (when the artifact was learned, not
            # when this backfill ran). Fall back to today only if the plan
            # entry is missing a date.
            note_date = (note_entry.get("date") or today)[:10]
            requests_to_send.append(
                {
                    "concept": concept,
                    "note_id": note_entry["id"],
                    "note_date": note_date,
                    "system": HUB_EXTRACTION_SYSTEM,
                    "user": user_prompt,
                    "cache_key": concept,
                }
            )

    print(f"Built {len(requests_to_send)} request(s) across {len(concept_plans)} concept(s).")

    if args.dry_run:
        print("\n--- DRY RUN: first request preview ---")
        if requests_to_send:
            r = requests_to_send[0]
            print(f"concept: {r['concept']}")
            print(f"note_id: {r['note_id']}")
            print(f"system: {len(r['system'])} chars")
            print(f"user: {len(r['user'])} chars")
            print("\n--- user prompt (first 800 chars) ---")
            print(r["user"][:800])
        return

    # Import OpenAI SDK lazily so the optional dependency is only
    # required when actually running.
    try:
        from openai import OpenAI
    except ImportError:
        print(
            "mem hubs run requires the OpenAI SDK.\n"
            "Install with: uv add --optional hubs openai  "
            "(or `pip install openai`)"
        )
        sys.exit(1)

    from personal_mem.enrich import load_openai_api_key

    api_key = load_openai_api_key()
    if not api_key:
        print(
            "OPENAI_API_KEY is not set (neither in env nor in the project .env)."
            " Export it or add OPENAI_API_KEY=sk-... to the repo .env."
        )
        sys.exit(1)
    os.environ["OPENAI_API_KEY"] = api_key

    client = OpenAI()

    # Sort requests by concept so batch rows for the same concept are
    # contiguous — maximises OpenAI's automatic prompt-cache hits on the
    # shared system prompt + hub state (cached for 5-10 min once seen).
    sorted_requests = sorted(
        requests_to_send,
        key=lambda r: (r["cache_key"], r["note_id"]),
    )

    # Cap total enqueued tokens to stay under the OpenAI org limit. Tokens
    # estimated as chars / 4 — matches the heuristic used in build_plan.
    if args.max_input_tokens > 0:
        budget = args.max_input_tokens
        capped: list[dict] = []
        total_tokens = 0
        for r in sorted_requests:
            est = (len(r["system"]) + len(r["user"])) // 4
            if total_tokens + est > budget:
                break
            capped.append(r)
            total_tokens += est
        deferred = len(sorted_requests) - len(capped)
        if deferred > 0:
            print(
                f"Capping at {len(capped)} request(s) (~{total_tokens:,} input "
                f"tokens) to stay under --max-input-tokens={budget:,}. "
                f"{deferred} request(s) deferred — rerun `mem hubs plan` + "
                f"`mem hubs run` after this batch completes."
            )
        sorted_requests = capped

    # Build the JSONL batch file. One line per request; OpenAI Batches
    # expects the raw /v1/chat/completions body under the `body` key.
    # custom_id → (concept, note_id, note_date) — note_date is threaded
    # through so parse_llm_response stamps each entry with the source
    # note's date instead of a uniform backfill date.
    id_to_key: dict[str, tuple[str, str, str]] = {}
    jsonl_lines: list[str] = []
    for i, r in enumerate(sorted_requests):
        custom_id = f"req-{i:05d}"
        id_to_key[custom_id] = (r["concept"], r["note_id"], r["note_date"])
        body = {
            "model": args.model,
            "max_completion_tokens": args.max_tokens,
            "messages": [
                {"role": "system", "content": r["system"]},
                {"role": "user", "content": r["user"]},
            ],
        }
        jsonl_lines.append(
            json.dumps({
                "custom_id": custom_id,
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": body,
            })
        )

    batch_input_path = cfg.mem_dir / "hubs_batch_input.jsonl"
    batch_input_path.parent.mkdir(parents=True, exist_ok=True)
    batch_input_path.write_text("\n".join(jsonl_lines) + "\n", encoding="utf-8")
    print(f"Wrote batch input: {batch_input_path} ({len(jsonl_lines)} line(s))")

    print(f"Uploading batch input to OpenAI Files API...")
    with batch_input_path.open("rb") as f:
        input_file = client.files.create(file=f, purpose="batch")
    print(f"Input file ID: {input_file.id}")

    print(f"Submitting batch of {len(jsonl_lines)} request(s) to {args.model}...")
    batch = client.batches.create(
        input_file_id=input_file.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
        metadata={"source": "personal-mem.hubs"},
    )
    print(f"Batch ID: {batch.id}")
    (cfg.mem_dir / "hubs_last_run").write_text(
        json.dumps({"batch_id": batch.id, "input_file_id": input_file.id}, indent=2),
        encoding="utf-8",
    )
    print("Polling for completion (typically minutes for small batches, up to 24h for large)...")

    import time as _time
    terminal_statuses = {"completed", "failed", "expired", "cancelled"}
    while True:
        batch = client.batches.retrieve(batch.id)
        counts = batch.request_counts
        print(
            f"  status={batch.status} "
            f"completed={counts.completed if counts else 0} "
            f"failed={counts.failed if counts else 0} "
            f"total={counts.total if counts else 0}"
        )
        if batch.status in terminal_statuses:
            break
        _time.sleep(args.poll_interval)

    if batch.status != "completed":
        print(f"Batch did not complete cleanly: status={batch.status}")
        if batch.errors:
            print(f"Errors: {batch.errors}")
        sys.exit(1)

    if not batch.output_file_id:
        print("Batch completed but has no output_file_id.")
        sys.exit(1)

    print(f"Downloading results from output file {batch.output_file_id}...")
    output_content = client.files.content(batch.output_file_id).text

    # Parse results and apply entries
    applied = 0
    essence_flagged: set[str] = set()
    for line in output_content.splitlines():
        if not line.strip():
            continue
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        custom_id = result.get("custom_id", "")
        concept, note_id, note_date = id_to_key.get(custom_id, ("", "", ""))
        if not concept:
            continue
        if result.get("error"):
            continue
        response = result.get("response", {})
        if response.get("status_code") != 200:
            continue
        body = response.get("body", {})
        choices = body.get("choices", [])
        if not choices:
            continue
        raw = choices[0].get("message", {}).get("content", "")
        if not raw:
            continue
        entries, needs_essence = parse_llm_response(
            raw, note_id=note_id, run_date=note_date or today
        )
        if entries:
            append_log_entries(cfg, concept, entries)
            applied += len(entries)
        if needs_essence:
            essence_flagged.add(concept)

    print(f"\nApplied {applied} new log entries.")
    if essence_flagged:
        print(f"Essence revision flagged for {len(essence_flagged)} concept(s):")
        for c in sorted(essence_flagged):
            print(f"  {c}")
        print("Run /mem-resolve-concepts to review flagged essences.")

    # Reindex touched hub pages incrementally — best-effort against SQLite
    # lock contention from a live MCP server.
    import sqlite3 as _sqlite3

    idx = Indexer(config=cfg)
    touched_concepts = {c for c, _, _ in id_to_key.values()}
    reindex_failures = 0
    for concept in touched_concepts:
        path = concept_hub_path(cfg, concept)
        if not path.exists():
            continue
        try:
            idx.index_file(path)
        except _sqlite3.OperationalError as e:
            reindex_failures += 1
            if reindex_failures == 1:
                print(f"  warning: reindex hit SQLite contention ({e}); continuing")
    idx.close()
    print(f"Reindexed {len(touched_concepts) - reindex_failures} of {len(touched_concepts)} hub page(s).")
    if reindex_failures:
        print(
            f"  {reindex_failures} hub(s) couldn't be reindexed due to DB "
            f"contention. Run `uv run mem index` once the contending process "
            f"releases the lock."
        )


def _hubs_status(cfg, args: argparse.Namespace) -> None:
    from personal_mem.hubs import (
        all_concepts_in_vault,
        concept_hub_path,
        parse_concept_hub,
    )

    counts = all_concepts_in_vault(cfg)
    if args.concept:
        counts = {c: n for c, n in counts.items() if c == args.concept.lower()}
    if not counts:
        print("No concepts found in the vault index.")
        return

    rows: list[tuple[str, int, int, int]] = []
    for concept, total in sorted(counts.items(), key=lambda x: -x[1]):
        hub = parse_concept_hub(concept_hub_path(cfg, concept), concept=concept)
        cited = len(hub.cited_ids)
        unprocessed = total - cited
        rows.append((concept, total, cited, unprocessed))

    print(f"{'concept':<40} {'total':>6} {'cited':>6} {'todo':>6}")
    print("-" * 62)
    for concept, total, cited, todo in rows:
        print(f"{concept:<40} {total:>6} {cited:>6} {todo:>6}")
    print(f"\n{len(rows)} concept(s), {sum(r[3] for r in rows)} unprocessed note-citations total.")


def _hubs_repair(cfg, args: argparse.Namespace) -> None:
    """Retroactive fix: swap backfill dates for source-note dates, strip
    duplicated inline wikilink citations. No LLM calls.
    """
    from personal_mem.hubs import (
        parse_concept_hub,
        topics_dir,
        write_concept_hub,
        _strip_inline_wikilinks,
    )
    from personal_mem.indexer import Indexer

    topics = topics_dir(cfg)
    if not topics.exists():
        print(f"No concept-hub topics directory at {topics}.")
        return

    # Build id → YYYY-MM-DD map from the SQLite index in one pass.
    idx = Indexer(config=cfg)
    id_to_date: dict[str, str] = {}
    for row in idx.db.execute("SELECT id, date FROM notes WHERE date IS NOT NULL AND date != ''"):
        id_to_date[row["id"]] = str(row["date"])[:10]
    idx.close()

    hub_files = sorted(topics.glob("*.md"))
    if args.concept:
        target = args.concept.lower()
        hub_files = [p for p in hub_files if p.stem == target]

    changed_hubs = 0
    changed_entries = 0
    citation_cleanups = 0
    date_updates = 0

    for hub_path in hub_files:
        hub = parse_concept_hub(hub_path)
        if not hub.log_entries:
            continue
        dirty = False
        for entry in hub.log_entries:
            new_date = id_to_date.get(entry.citation, entry.date)
            new_text = _strip_inline_wikilinks(entry.text) if entry.text else entry.text
            if new_date != entry.date:
                entry.date = new_date
                date_updates += 1
                dirty = True
            if new_text != entry.text:
                entry.text = new_text
                citation_cleanups += 1
                dirty = True
        if dirty:
            changed_hubs += 1
            changed_entries += sum(
                1 for e in hub.log_entries
                if id_to_date.get(e.citation, e.date) == e.date
            )
            if args.dry_run:
                print(f"[dry-run] would rewrite {hub_path.name}")
            else:
                write_concept_hub(hub)

    print(
        f"Repaired {changed_hubs} hub(s) — "
        f"{date_updates} date swap(s), {citation_cleanups} citation cleanup(s)."
    )
    if args.dry_run:
        print("(dry-run: no files written)")
        return

    # Reindex touched hub files so the FTS body reflects the new lines.
    # Best-effort: a live MCP server can hold a conflicting SQLite writer
    # lock. If that happens we log and move on — the vault file content is
    # already correct; the user can rerun `mem index` after the contending
    # process releases its lock.
    import sqlite3 as _sqlite3

    idx = Indexer(config=cfg)
    reindex_failures = 0
    for hub_path in hub_files:
        if not hub_path.exists():
            continue
        try:
            idx.index_file(hub_path)
        except _sqlite3.OperationalError as e:
            reindex_failures += 1
            if reindex_failures == 1:
                print(f"  warning: reindex hit SQLite contention ({e}); continuing")
    idx.close()
    if reindex_failures:
        print(
            f"  {reindex_failures} hub(s) couldn't be reindexed due to DB "
            f"contention. Run `uv run mem index` once the contending process "
            f"releases the lock."
        )


def _hubs_link(cfg, args: argparse.Namespace) -> None:
    """Temporal-DAG linkage: rewrite flat `new` flags based on chronological
    relationships between entries on the same hub. One LLM request per hub
    via the OpenAI Batches API.
    """
    from personal_mem.hubs import (
        ALLOWED_FLAGS,
        LogEntry,
        concept_hub_path,
        parse_concept_hub,
        topics_dir,
        write_concept_hub,
    )
    from personal_mem.indexer import Indexer

    topics = topics_dir(cfg)
    hub_files = sorted(topics.glob("*.md"))
    if args.concept:
        target = args.concept.lower()
        hub_files = [p for p in hub_files if p.stem == target]

    # Collect hubs that have enough entries to bother with.
    work: list[tuple[str, list[LogEntry], str]] = []  # (concept, entries_chrono, essence)
    for hub_path in hub_files:
        hub = parse_concept_hub(hub_path)
        if len(hub.log_entries) < args.min_entries:
            continue
        entries_sorted = sorted(hub.log_entries, key=lambda e: (e.date, e.citation))
        work.append((hub.concept, entries_sorted, hub.essence))

    if not work:
        print(f"No hubs with ≥{args.min_entries} entries found.")
        return

    print(f"Building linkage requests for {len(work)} hub(s)...")

    system_prompt = _HUB_LINKAGE_SYSTEM
    requests_to_send: list[dict] = []
    for concept, entries, essence in work:
        user_prompt = _build_linkage_user_prompt(concept, essence, entries)
        requests_to_send.append({
            "concept": concept,
            "system": system_prompt,
            "user": user_prompt,
            "entry_count": len(entries),
        })

    print(f"Built {len(requests_to_send)} request(s).")

    if args.dry_run:
        print("\n--- DRY RUN: first request preview ---")
        r = requests_to_send[0]
        print(f"concept: {r['concept']}  entries: {r['entry_count']}")
        print(f"system: {len(r['system'])} chars  user: {len(r['user'])} chars")
        print("\n--- user prompt (first 1200 chars) ---")
        print(r["user"][:1200])
        return

    # Cap input tokens against OpenAI's org limit.
    if args.max_input_tokens > 0:
        budget = args.max_input_tokens
        capped: list[dict] = []
        total_tokens = 0
        for r in requests_to_send:
            est = (len(r["system"]) + len(r["user"])) // 4
            if total_tokens + est > budget:
                break
            capped.append(r)
            total_tokens += est
        if len(capped) < len(requests_to_send):
            deferred = len(requests_to_send) - len(capped)
            print(
                f"Capping at {len(capped)} hub(s) (~{total_tokens:,} input tokens); "
                f"{deferred} deferred to a subsequent run."
            )
        requests_to_send = capped

    try:
        from openai import OpenAI
    except ImportError:
        print(
            "mem hubs link requires the OpenAI SDK.\n"
            "Install with: uv add --optional hubs openai"
        )
        sys.exit(1)

    from personal_mem.enrich import load_openai_api_key

    api_key = load_openai_api_key()
    if not api_key:
        print("OPENAI_API_KEY is not set.")
        sys.exit(1)
    os.environ["OPENAI_API_KEY"] = api_key
    client = OpenAI()

    # custom_id → concept; we re-read the hub at apply time to avoid
    # stale entry lists if another process edited the file in between.
    id_to_concept: dict[str, str] = {}
    jsonl_lines: list[str] = []
    for i, r in enumerate(requests_to_send):
        custom_id = f"link-{i:05d}"
        id_to_concept[custom_id] = r["concept"]
        body = {
            "model": args.model,
            "max_completion_tokens": args.max_tokens,
            "messages": [
                {"role": "system", "content": r["system"]},
                {"role": "user", "content": r["user"]},
            ],
            "response_format": {"type": "json_object"},
        }
        jsonl_lines.append(json.dumps({
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": body,
        }))

    batch_input_path = cfg.mem_dir / "hubs_link_input.jsonl"
    batch_input_path.parent.mkdir(parents=True, exist_ok=True)
    batch_input_path.write_text("\n".join(jsonl_lines) + "\n", encoding="utf-8")
    print(f"Wrote batch input: {batch_input_path} ({len(jsonl_lines)} line(s))")

    with batch_input_path.open("rb") as f:
        input_file = client.files.create(file=f, purpose="batch")
    print(f"Input file ID: {input_file.id}")

    batch = client.batches.create(
        input_file_id=input_file.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
        metadata={"source": "personal-mem.hubs-link"},
    )
    print(f"Batch ID: {batch.id}")
    (cfg.mem_dir / "hubs_last_link_run").write_text(
        json.dumps({"batch_id": batch.id, "input_file_id": input_file.id}, indent=2),
        encoding="utf-8",
    )

    import time as _time
    terminal_statuses = {"completed", "failed", "expired", "cancelled"}
    while True:
        batch = client.batches.retrieve(batch.id)
        counts = batch.request_counts
        print(
            f"  status={batch.status} "
            f"completed={counts.completed if counts else 0} "
            f"failed={counts.failed if counts else 0} "
            f"total={counts.total if counts else 0}"
        )
        if batch.status in terminal_statuses:
            break
        _time.sleep(args.poll_interval)

    if batch.status != "completed" or not batch.output_file_id:
        print(f"Batch did not complete cleanly: status={batch.status}")
        if batch.errors:
            print(f"Errors: {batch.errors}")
        sys.exit(1)

    output_content = client.files.content(batch.output_file_id).text

    applied_hubs = 0
    applied_entries = 0
    for line in output_content.splitlines():
        if not line.strip():
            continue
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        custom_id = result.get("custom_id", "")
        concept = id_to_concept.get(custom_id, "")
        if not concept or result.get("error"):
            continue
        response = result.get("response", {})
        if response.get("status_code") != 200:
            continue
        raw = (
            response.get("body", {})
            .get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        if not raw:
            continue
        revisions = _parse_linkage_response(raw)
        if not revisions:
            continue

        hub_path = concept_hub_path(cfg, concept)
        hub = parse_concept_hub(hub_path, concept=concept)
        entries_sorted = sorted(hub.log_entries, key=lambda e: (e.date, e.citation))
        if len(revisions) != len(entries_sorted):
            # Length mismatch — skip rather than misalign.
            continue

        any_change = False
        for entry, rev in zip(entries_sorted, revisions):
            new_flag, new_ref = _validate_linkage_revision(
                entry_date=entry.date,
                flag=str(rev.get("flag", "new")).lower(),
                ref=str(rev.get("ref") or "").strip(),
            )
            if new_flag is None:
                continue
            if new_flag != entry.flag or new_ref != entry.ref:
                entry.flag = new_flag
                entry.ref = new_ref
                any_change = True
                applied_entries += 1

        if any_change:
            # Preserve existing order on disk — write_concept_hub renders
            # hub.log_entries as-is, so we only commit changed metadata.
            hub.log_entries = sorted(hub.log_entries, key=lambda e: (e.date, e.citation))
            write_concept_hub(hub)
            applied_hubs += 1

    print(f"\nApplied linkage revisions to {applied_hubs} hub(s), {applied_entries} entries updated.")

    # Best-effort reindex — see _hubs_repair for the SQLite contention
    # rationale.
    import sqlite3 as _sqlite3

    idx = Indexer(config=cfg)
    reindex_failures = 0
    for concept in set(id_to_concept.values()):
        p = concept_hub_path(cfg, concept)
        if not p.exists():
            continue
        try:
            idx.index_file(p)
        except _sqlite3.OperationalError as e:
            reindex_failures += 1
            if reindex_failures == 1:
                print(f"  warning: reindex hit SQLite contention ({e}); continuing")
    idx.close()
    if reindex_failures:
        print(
            f"  {reindex_failures} hub(s) couldn't be reindexed. "
            f"Run `uv run mem index` to catch up."
        )


def _validate_linkage_revision(
    entry_date: str, flag: str, ref: str
) -> tuple[str | None, str]:
    """Validate a single linkage revision against the temporal-DAG contract.

    The contract: an entry's `ref` must point to a STRICTLY EARLIER entry
    (`ref < entry_date`). Flags `extends` and `contradicts` REQUIRE a
    valid ref; `agrees` accepts an empty ref; `new` MUST have an empty
    ref. The LLM has historically inverted subject/object on this task —
    parser-side validation is the load-bearing rule.

    Returns ``(flag, ref)`` for valid revisions. If the flag itself is
    unknown, returns ``(None, "")`` so the caller skips the revision.
    Otherwise the helper repairs invalid combinations:

    - ``new`` + any ref → drop the ref
    - non-YYYY-MM-DD ref → drop the ref
    - ``ref >= entry_date`` → drop the ref. If the flag REQUIRED a ref
      (``extends`` / ``contradicts``), downgrade the flag to ``new``
      since the structural relationship is no longer expressible.
    - ``agrees`` with an invalid ref → keep ``agrees``, ref empty.
    """
    from personal_mem.hubs import ALLOWED_FLAGS

    if flag not in ALLOWED_FLAGS:
        return None, ""

    if flag == "new":
        return "new", ""

    if ref and not re.match(r"^\d{4}-\d{2}-\d{2}$", ref):
        ref = ""

    if ref and ref >= entry_date:
        ref = ""

    if not ref and flag in {"extends", "contradicts"}:
        # These flags REQUIRE a ref. Without one, the relationship is
        # not expressible — fall back to "new".
        flag = "new"

    return flag, ref


_HUB_LINKAGE_SYSTEM = """You are revising a learning log for one concept in a personal knowledge vault. Each entry is a distilled learning artifact captured from a note. Entries are listed oldest-first; the entry's date is its line prefix.

For each entry E in the list, decide what E does relative to the entries that appear BEFORE it (the entries with strictly earlier dates):
- "new" — E introduces something not present in any earlier entry.
- "agrees" — E reinforces, restates, or confirms a claim from an earlier entry.
- "contradicts" — E directly conflicts with an earlier entry.
- "extends" — E elaborates on, refines, or adds a corollary to an earlier entry.

E is the SUBJECT. The earlier entry is the OBJECT. The verb describes what E does to the earlier entry — never what the earlier entry does to E. The first entry in the list (no earlier entries exist) MUST be flagged "new".

Rules for the ref field:
- flag "new" → ref MUST be empty.
- flag "agrees" → ref is optional (empty if no single earlier entry to cite; otherwise the date of that earlier entry).
- flag "contradicts" → ref is REQUIRED and must be the date of an earlier entry.
- flag "extends" → ref is REQUIRED and must be the date of an earlier entry.

The ref date MUST be strictly less than E's date. If you cannot find an earlier entry that fits, flag E as "new". Never cite a future or same-day entry. Never invent dates — only cite dates that appear in the input.

Be conservative: default to "new" unless the relationship is clear.

Return a single JSON object of the form:
  {"entries": [{"flag": "...", "ref": "YYYY-MM-DD or empty"}, ...]}
The array length must EXACTLY match the input. Preserve input order.
"""


def _build_linkage_user_prompt(concept: str, essence: str, entries: list) -> str:
    essence_text = essence.strip() or "*No synthesis yet.*"
    lines = [f"Concept: `{concept}`", "", f"Essence:\n{essence_text}", "", "Entries (chronological):"]
    for i, e in enumerate(entries, start=1):
        lines.append(f"{i}. {e.date} — {e.text}")
    lines.append("")
    lines.append("Output JSON only.")
    return "\n".join(lines)


def _parse_linkage_response(raw: str) -> list[dict]:
    """Parse the linkage LLM response into a list of {flag, ref} dicts.

    Tolerates code-fenced JSON. Returns [] on any parse failure.
    """
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def cmd_concepts(args: argparse.Namespace) -> None:
    from personal_mem.concepts import (
        get_all_concepts,
        load_aliases,
        merge_concept_in_notes,
        save_aliases,
    )
    from personal_mem.indexer import Indexer

    cfg = load_config()

    action = args.concepts_action
    if not action:
        # Default to list
        action = "list"

    if action == "list":
        idx = Indexer(config=cfg)
        concept_counts = get_all_concepts(idx.db)
        idx.close()

        prefix = args.prefix.lower() if hasattr(args, "prefix") else ""
        min_count = args.min_count if hasattr(args, "min_count") else 1

        filtered = sorted(
            ((c, n) for c, n in concept_counts.items()
             if n >= min_count and c.startswith(prefix)),
            key=lambda x: (-x[1], x[0]),
        )
        if not filtered:
            print("No concepts found.")
            return
        print(f"Concepts ({len(filtered)} total):\n")
        for concept, count in filtered:
            print(f"  {count:3d}  {concept}")

    elif action == "merge":
        from personal_mem.concepts import delete_concept_hub

        from_c = args.from_concept.lower()
        to_c = args.to_concept.lower()
        if from_c == to_c:
            print("from and to concepts are the same.")
            return

        changed = merge_concept_in_notes(cfg.vault_root, from_c, to_c)

        aliases = load_aliases(cfg)
        existing = aliases.get(to_c, [])
        if from_c not in existing:
            existing.append(from_c)
        if from_c in aliases:
            for old in aliases.pop(from_c):
                if old != to_c and old not in existing:
                    existing.append(old)
        aliases[to_c] = existing
        save_aliases(cfg, aliases)

        # Remove the renamed concept's hub page so it doesn't linger as a
        # stale ledger. Safe even if the file never existed.
        hub_removed = delete_concept_hub(cfg, from_c)

        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()

        suffix = " Stale hub removed." if hub_removed else ""
        print(
            f"Merged '{from_c}' → '{to_c}': {changed} notes updated. "
            f"Alias saved. Index rebuilt.{suffix}"
        )

    elif action == "prune":
        from personal_mem.concepts import build_keep_set, load_ontology, prune_concepts

        ontology = load_ontology()
        if not ontology:
            print("No ontology.yaml found.")
            return

        keep_set = build_keep_set(ontology)
        print(f"Ontology defines {len(keep_set)} concepts across {len(ontology)} domains.")

        if args.dry_run:
            # Count what would be pruned
            from personal_mem.vault import VaultManager, parse_frontmatter
            vm = VaultManager(config=cfg)
            would_remove = 0
            would_modify = 0
            for md_file in vm.root.rglob("*.md"):
                text = md_file.read_text(encoding="utf-8")
                fm, _ = parse_frontmatter(text)
                if not fm:
                    continue
                concepts = fm.get("concepts", [])
                if isinstance(concepts, str):
                    concepts = [c.strip() for c in concepts.split(",") if c.strip()]
                removed = sum(1 for c in concepts if c.lower() not in keep_set)
                if removed:
                    would_modify += 1
                    would_remove += removed
            print(f"Would modify {would_modify} files, removing {would_remove} concepts.")
            return

        stats = prune_concepts(cfg.vault_root, keep_set)
        print(f"Pruned {stats['concepts_removed']} concepts from {stats['files_modified']} files.")

        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()
        print("Index rebuilt.")

    elif action == "notes":
        from personal_mem.search import Search

        s = Search(config=cfg)
        concept = args.concept.lower()
        project = args.project if hasattr(args, "project") else ""
        results = s.search_by_concept(concept, project=project, limit=50)
        s.close()

        if not results:
            print(f"No notes with concept '{concept}'.")
            return

        print(f"Notes with concept '{concept}' ({len(results)}):\n")
        for r in results:
            tag_str = f" [{', '.join(r.tags)}]" if r.tags else ""
            proj_str = f" | {r.project}" if r.project else ""
            print(f"  [{r.type}] {r.title} ({r.id}){tag_str}{proj_str}")

    elif action == "hubs":
        from personal_mem.concepts import (
            add_hub_wikilinks,
            find_orphan_hubs,
            generate_concept_hub_skeletons,
            generate_domain_hubs,
            hubs_marker_path,
            load_ontology,
        )

        # --prune is mutually exclusive with the regenerate flow.
        if getattr(args, "prune", False):
            orphans = find_orphan_hubs(cfg)
            if not orphans:
                print("No orphan hubs.")
                return

            print(f"Orphan hubs ({len(orphans)}):")
            for concept, path in orphans:
                rel = path.relative_to(cfg.vault_root)
                print(f"  {concept} → {rel}")

            if not getattr(args, "apply", False):
                print(
                    "\nDry run. Re-run with --apply to delete these files."
                )
                return

            for _, path in orphans:
                path.unlink()
            print(f"\nDeleted {len(orphans)} orphan hub(s).")
            idx = Indexer(config=cfg)
            idx.rebuild(full=False)
            idx.close()
            return

        ontology = load_ontology()
        if not ontology:
            print("No ontology.yaml found.")
            return

        domain_hubs = generate_domain_hubs(cfg, ontology)
        print(f"Generated {len(domain_hubs)} domain hub(s) in vault/concepts/:")
        for domain, path in sorted(domain_hubs.items()):
            print(f"  {domain} → {path.name}")

        concept_hubs = generate_concept_hub_skeletons(cfg, ontology)
        print(
            f"\nEnsured {len(concept_hubs)} concept hub skeleton(s) in "
            "vault/concepts/topics/ (existing files preserved)."
        )

        modified = add_hub_wikilinks(cfg, ontology)
        print(f"\nAdded domain wikilinks to {modified} notes.")

        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()
        print("Index rebuilt.")

        # Touch the marker so drift_report knows hubs are fresh
        marker = hubs_marker_path(cfg)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()

    elif action == "drift":
        from personal_mem.concepts import (
            drift_report,
            find_redundant_hub_candidates,
            format_drift_report,
        )

        report = drift_report(
            cfg,
            project=args.project,
            threshold=args.threshold,
            max_items=args.max_items,
        )
        print(format_drift_report(report))

        if getattr(args, "hubs", False):
            jaccard = getattr(args, "hub_jaccard", 0.4)
            candidates = find_redundant_hub_candidates(cfg, min_jaccard=jaccard)
            print()
            if not candidates:
                print(
                    f"No redundant-hub candidates (Jaccard ≥ {jaccard:.2f})."
                )
            else:
                print(
                    f"Redundant-hub candidates (Jaccard ≥ {jaccard:.2f}): "
                    f"{len(candidates)} pair(s)"
                )
                for a, b, score in candidates[:args.max_items]:
                    print(
                        f"  {a} ↔ {b}  (Jaccard {score:.2f}) — "
                        f"review via `/mem-resolve-concepts`"
                    )


def cmd_init(args: argparse.Namespace) -> None:
    from personal_mem.vault import VaultManager

    cfg = load_config()
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    print(f"Vault initialized at {cfg.vault_root}")


def cmd_add(args: argparse.Namespace) -> None:
    from personal_mem.vault import VaultManager
    from personal_mem.indexer import Indexer

    cfg = load_config()
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()

    note_type = NoteType(args.type)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
    project = args.project or cfg.default_project

    # Read body from stdin if not provided and stdin is not a terminal
    body = args.body
    if not body and not sys.stdin.isatty():
        body = sys.stdin.read()

    path = vm.create_note(
        note_type=note_type,
        title=args.title,
        body=body,
        project=project,
        tags=tags,
        session_id=args.session,
    )

    # Incremental index
    idx = Indexer(config=cfg)
    idx.index_file(path)
    idx.close()

    note = vm.read_note(path)
    print(f"Created {note.type.value} [{note.id}] at {path.relative_to(cfg.vault_root)}")


def cmd_search(args: argparse.Namespace) -> None:
    from personal_mem.search import Search

    cfg = load_config()

    # --semantic is an alias for --mode similar (back-compat)
    mode = args.mode
    if args.semantic and mode == "fts":
        mode = "similar"

    s = Search(config=cfg)

    # Normalize type filter: accept comma-separated for list support
    type_arg: str | list[str] = args.type
    if args.type and "," in args.type:
        type_arg = [t.strip() for t in args.type.split(",") if t.strip()]

    # Concept-based search (overrides text search)
    if args.concept:
        concept_list = [c.strip() for c in args.concept.split(",") if c.strip()]
        results = s.search_by_concept(
            concept=concept_list if len(concept_list) > 1 else concept_list[0],
            project=args.project,
            note_type=type_arg,
            limit=args.limit,
            match_mode=args.match_mode,
        )
        s.close()

        label = (
            concept_list[0]
            if len(concept_list) == 1
            else f"{len(concept_list)} concepts ({args.match_mode})"
        )
        if not results:
            print(f"No notes with {label}.")
            return

        print(f"Notes with {label} ({len(results)}):\n")
        for r in results:
            tag_str = f" [{', '.join(r.tags)}]" if r.tags else ""
            print(f"  [{r.type}] {r.title} ({r.id}){tag_str}")
            if r.project:
                print(f"    project: {r.project}")
            print()
        return

    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None

    if mode == "similar":
        results = s.similar(
            args.query, project=args.project, note_type=type_arg, limit=args.limit
        )
        if not results:
            s.close()
            print(
                "No semantic results. If embeddings aren't set up yet, run "
                "`mem index --embed` with OPENAI_API_KEY set."
            )
            return
    elif mode == "hybrid":
        results = s.hybrid_search(
            args.query, project=args.project, note_type=type_arg, limit=args.limit
        )
    else:
        results = s.search(
            query=args.query,
            note_type=type_arg,
            project=args.project,
            tags=tags,
            limit=args.limit,
        )
    s.close()

    if not results:
        print("No results found.")
        return

    for r in results:
        tag_str = f" [{', '.join(r.tags)}]" if r.tags else ""
        print(f"  [{r.type}] {r.title} ({r.id}){tag_str}")
        if r.snippet:
            print(f"    {r.snippet}")
        if r.project:
            print(f"    project: {r.project}")
        print()


def _cmd_search_semantic(args: argparse.Namespace, cfg) -> None:
    try:
        from personal_mem.embeddings import EmbeddingSearch
    except ImportError:
        print("Semantic search requires: pip install personal-mem[embeddings]")
        sys.exit(1)

    es = EmbeddingSearch(config=cfg)
    results = es.search(args.query, limit=args.limit)

    if not results:
        print("No results found.")
        return

    for note_id, score in results:
        from personal_mem.search import Search
        s = Search(config=cfg)
        note = s.get_note_by_id(note_id)
        s.close()
        if note:
            print(f"  [{note['type']}] {note['title']} ({note_id}) score={score:.3f}")


def cmd_show(args: argparse.Namespace) -> None:
    from personal_mem.search import Search
    from personal_mem.vault import VaultManager

    cfg = load_config()
    s = Search(config=cfg)
    note = s.get_note_by_id(args.id)
    s.close()

    if not note:
        print(f"Note {args.id} not found.")
        sys.exit(1)

    vm = VaultManager(config=cfg)
    full_path = vm.root / note["path"]
    if full_path.exists():
        print(full_path.read_text(encoding="utf-8"))
    else:
        # Fallback: print from index
        print(f"Type: {note['type']}")
        print(f"Title: {note['title']}")
        print(f"Project: {note['project']}")
        print(f"Date: {note['date']}")
        print(f"Tags: {note['tags']}")
        print(f"\n{note['body_text']}")


def cmd_link(args: argparse.Namespace) -> None:
    from personal_mem.indexer import EDGE_TYPE_TO_FIELD, Indexer
    from personal_mem.vault import VaultManager

    cfg = load_config()
    idx = Indexer(config=cfg)
    vm = VaultManager(config=cfg)

    # Verify both notes exist and get paths
    src = idx.db.execute("SELECT id, path FROM notes WHERE id = ?", (args.source,)).fetchone()
    tgt = idx.db.execute("SELECT id, path FROM notes WHERE id = ?", (args.target,)).fetchone()

    if not src:
        print(f"Source note {args.source} not found.")
        idx.close()
        sys.exit(1)
    if not tgt:
        print(f"Target note {args.target} not found.")
        idx.close()
        sys.exit(1)

    # Write edge into source note's frontmatter
    fm_field = EDGE_TYPE_TO_FIELD[args.type]
    vm.update_note(
        vm.root / src["path"],
        frontmatter_updates={fm_field: [args.target]},
    )

    # Re-index so the edge appears immediately
    idx.index_file(vm.root / src["path"])
    idx.close()
    print(f"Linked {args.source} --{args.type}--> {args.target}")


def cmd_graph(args: argparse.Namespace) -> None:
    from personal_mem.search import Search

    cfg = load_config()
    s = Search(config=cfg)

    if args.format == "mermaid":
        print(s.render_graph_mermaid(args.id, depth=args.depth))
    else:
        print(s.render_graph_text(args.id, depth=args.depth))
    s.close()


def cmd_enrich(args: argparse.Namespace) -> None:
    """LLM-assisted concept enrichment for notes missing concepts."""
    from personal_mem.enrich import enrich
    from personal_mem.indexer import Indexer

    cfg = load_config()

    note_types = (
        [t.strip() for t in args.note_types.split(",") if t.strip()]
        if args.note_types
        else ["session", "note", "decision", "source"]
    )

    prefix = "[dry run] " if args.dry_run else ""
    type_str = ",".join(note_types)
    print(f"{prefix}Enriching {type_str} notes"
          + (f" in project '{args.project}'" if args.project else " (all projects)")
          + (f" (limit {args.limit})" if args.limit else "")
          + "...")

    def progress(current, total, title):
        pct = current * 100 // max(total, 1)
        print(f"  [{pct:3d}%] batch at note {current}/{total}: {title[:50]}")

    stats = enrich(
        cfg,
        project=args.project,
        note_types=note_types,
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        progress_cb=progress,
    )

    print(
        f"\n{prefix}Done — enriched: {stats['enriched']}, "
        f"skipped: {stats['skipped']}, "
        f"errors: {stats['errors']}, "
        f"concepts assigned: {stats['new_concepts']}"
    )

    if not args.dry_run and stats["enriched"] > 0:
        if args.reindex:
            print("\nRebuilding index...")
            idx = Indexer(config=cfg)
            istats = idx.rebuild(full=True)
            print(f"  Indexed: {istats['indexed']}, Edges: {istats['edges']}")
            idx.close()

        if args.connect:
            print("\nMaterializing links for Obsidian...")
            from personal_mem.indexer import Indexer as Idx2
            idx2 = Idx2(config=cfg)
            cstats = idx2.materialize_links(max_links=5)
            print(f"  Updated: {cstats['notes_updated']}, Links: {cstats['links_written']}")
            idx2.close()

            print("\nFinal reindex to pick up new wikilinks...")
            idx3 = Indexer(config=cfg)
            fstats = idx3.rebuild(full=False)
            print(f"  Edges: {fstats['edges']}")
            idx3.close()


def cmd_connect(args: argparse.Namespace) -> None:
    """Materialize SQLite edges as wikilinks in markdown for Obsidian."""
    from personal_mem.indexer import Indexer

    cfg = load_config()
    idx = Indexer(config=cfg)

    stats = idx.materialize_links(max_links=args.max_links, dry_run=args.dry_run)
    prefix = "[dry run] " if args.dry_run else ""
    print(
        f"{prefix}Updated: {stats['notes_updated']}, "
        f"Skipped: {stats['notes_skipped']}, "
        f"Links written: {stats['links_written']}"
    )
    if not args.dry_run:
        print("Re-run `mem index` to update the index with new wikilinks.")
    idx.close()


def cmd_index(args: argparse.Namespace) -> None:
    from personal_mem.indexer import Indexer

    cfg = load_config()
    idx = Indexer(config=cfg)

    # Ensure vault dirs exist
    from personal_mem.vault import VaultManager
    VaultManager(config=cfg).ensure_dirs()

    stats = idx.rebuild(full=args.full)
    print(f"Indexed: {stats['indexed']}, Skipped: {stats['skipped']}, "
          f"Removed: {stats['removed']}, Edges: {stats['edges']}")

    if args.embed:
        try:
            from personal_mem.embeddings import EmbeddingSearch
            es = EmbeddingSearch(config=cfg)
            embed_stats = es.compute_all()
            print(f"Embeddings: {embed_stats['computed']} computed, {embed_stats['skipped']} cached")
        except ImportError:
            print("Embeddings require: pip install personal-mem[embeddings]")

    idx.close()


def cmd_import(args: argparse.Namespace) -> None:
    cfg = load_config()

    if args.source == "claude-mem":
        from pathlib import Path as _Path

        from personal_mem.importers.claude_mem import import_claude_mem

        db_path = _Path(args.db_path) if args.db_path else None
        stats = import_claude_mem(
            cfg,
            db_path=db_path,
            project_filter=args.project,
            dry_run=args.dry_run,
        )
        if "error" in stats:
            print(f"Error: {stats['error']}")
            sys.exit(1)
        if not args.dry_run:
            print(
                f"Imported: {stats['sessions']} sessions, "
                f"{stats['notes']} notes, {stats['decisions']} decisions"
            )
            if stats.get("deduped"):
                print(f"  Deduped: {stats['deduped']}")
            if stats.get("skipped"):
                print(f"  Skipped (already imported): {stats['skipped']}")
            if stats.get("errors"):
                print(f"  Errors: {stats['errors']}")

    elif args.source == "chatgpt":
        if not args.path:
            print("File path required. Usage: mem import chatgpt <path-to-conversations.json>")
            sys.exit(1)

        # Load .env if python-dotenv is available
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        from personal_mem.importers.chatgpt import import_chatgpt

        stats = import_chatgpt(
            cfg,
            conversations_path=Path(args.path),
            dry_run=args.dry_run,
            limit=args.limit,
            since=args.since,
            until=args.until,
        )
        if "error" in stats:
            print(f"Error: {stats['error']}")
            sys.exit(1)
        if not args.dry_run:
            print(
                f"\nDone: {stats['imported']} imported, "
                f"{stats['skipped']} skipped, {stats['errors']} errors"
            )

    elif args.source == "file":
        if not args.path:
            print("File path required for 'file' import.")
            sys.exit(1)
        from personal_mem.importers.transcript import import_transcript

        path = import_transcript(
            cfg,
            file_path=Path(args.path),
            source_type=args.source_type,
            project=args.project,
        )
        print(f"Imported source note at {path}")

    elif args.source == "messenger":
        if not args.path:
            print("File path required. Usage: mem import messenger <path-to-export.json>")
            sys.exit(1)

        from personal_mem.importers.messenger import import_messenger

        stats = import_messenger(
            cfg,
            json_path=Path(args.path),
            dry_run=args.dry_run,
            resolve=not args.no_resolve,
            since=args.since,
            until=args.until,
        )
        if "error" in stats:
            print(f"Error: {stats['error']}")
            sys.exit(1)


def cmd_context(args: argparse.Namespace) -> None:
    from personal_mem.search import Search

    cfg = load_config()
    s = Search(config=cfg)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
    concepts = [c.strip() for c in args.concepts.split(",") if c.strip()] if args.concepts else None

    results = s.get_context(
        project=args.project,
        tags=tags,
        query=args.query,
        concepts=concepts,
        limit=args.limit,
    )
    s.close()

    if not results:
        print("No context available.")
        return

    for r in results:
        tag_str = f" [{', '.join(r.tags)}]" if r.tags else ""
        print(f"  [{r.type}] {r.title} ({r.id}){tag_str}")


def cmd_stats(args: argparse.Namespace) -> None:
    from personal_mem.indexer import Indexer

    cfg = load_config()
    idx = Indexer(config=cfg)
    stats = idx.get_stats()
    idx.close()

    print(f"Vault: {cfg.vault_root}")
    print(f"Index: {cfg.index_db}")
    print()
    for key, value in sorted(stats.items()):
        label = key.replace("_", " ").title()
        print(f"  {label}: {value}")


def cmd_doctor(args: argparse.Namespace) -> None:
    """Run vault coherence checks (read-only)."""
    from personal_mem.concepts import doctor_report, format_doctor_report

    cfg = load_config()
    if not cfg.index_db.exists():
        print(f"Index not found at {cfg.index_db}. Run `mem index` first.")
        sys.exit(1)

    report = doctor_report(cfg)
    print(format_doctor_report(report))


def cmd_flow(args: argparse.Namespace) -> None:
    """Run a named workflow pipeline."""
    from personal_mem.flows import flows_path, load_flows, run_flow

    cfg = load_config()
    flows = load_flows(cfg)

    action = args.flow_action or "list"

    if action == "list":
        if not flows:
            print(f"No flows defined. Create {flows_path(cfg)} to add one.")
            return
        print(f"Flows ({len(flows)}):\n")
        for name, spec in sorted(flows.items()):
            desc = spec.description or "(no description)"
            print(f"  {name:24s} {desc}")
        return

    if action == "show":
        if args.name not in flows:
            print(f"Unknown flow: {args.name}")
            sys.exit(1)
        spec = flows[args.name]
        print(f"{spec.name}: {spec.description}")
        print(f"  on_error: {spec.on_error}")
        if spec.log:
            print(f"  log: {spec.log}")
        for i, stage in enumerate(spec.stages):
            print(f"  stage {i + 1}: {stage.run}")
            if stage.sleep:
                print(f"    sleep {stage.sleep}s")
        return

    if action == "run":
        if args.name not in flows:
            print(f"Unknown flow: {args.name}")
            sys.exit(1)
        code = run_flow(flows[args.name], dry_run=args.dry_run)
        sys.exit(code if not args.dry_run else 0)


def cmd_intake(args: argparse.Namespace) -> None:
    """Drop-folder intake helpers — enumerate / archive.

    Intentionally narrow: just enumeration + archival. LLM-driven work
    (frontmatter parsing, brief writing, concept mapping) stays in the
    skill that calls these. Image backfill is platform-specific and lives
    with each importer skill.
    """
    from personal_mem.sources.intake import (
        archive_to_processed,
        enumerate_inbox,
    )

    action = args.intake_action
    if not action:
        print("Usage: mem intake enumerate <path> | archive <entry> --inbox <root>")
        sys.exit(1)

    if action == "enumerate":
        inbox = Path(args.path).expanduser()
        entries = enumerate_inbox(inbox, archive_name=args.archive_name)
        payload = [
            {
                "path": str(e.path),
                "kind": e.kind,
                "companion_dir": str(e.companion_dir) if e.companion_dir else None,
            }
            for e in entries
        ]
        print(json.dumps(payload))
        return

    if action == "archive":
        entry = Path(args.entry).expanduser()
        inbox_root = Path(args.inbox).expanduser()
        try:
            final_path = archive_to_processed(entry, inbox_root)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(2)
        print(str(final_path))
        return

    print(f"Unknown intake action: {action}", file=sys.stderr)
    sys.exit(1)


def cmd_hooks(args: argparse.Namespace) -> None:
    if not args.hooks_action:
        print("Usage: mem hooks install|uninstall")
        sys.exit(1)

    if args.hooks_action == "install":
        from personal_mem.hooks.install import install_hooks

        project = args.project if hasattr(args, "project") else ""
        install_hooks(project_dir=project)
    elif args.hooks_action == "uninstall":
        from personal_mem.hooks.install import uninstall_hooks

        uninstall_hooks()
    elif args.hooks_action == "status":
        cfg = load_config()
        log_path = cfg.mem_dir / "hooks.log"
        if not log_path.exists():
            print("No hook errors recorded.")
            return
        lines = log_path.read_text(encoding="utf-8").splitlines()
        limit = args.limit if hasattr(args, "limit") else 20
        recent = lines[-limit:] if len(lines) > limit else lines
        if not recent:
            print("Hook log is empty.")
        else:
            print(f"Last {len(recent)} lines from {log_path}:\n")
            for line in recent:
                print(line)


def cmd_decisions(args: argparse.Namespace) -> None:
    """Query decisions — primary use: ``mem decisions --file <path>``."""
    from personal_mem.search import Search

    if not args.file_path:
        print("Usage: mem decisions --file <path> [--project X] [--status accepted]")
        return

    cfg = load_config()
    s = Search(config=cfg)
    results = s.search_decisions_by_file(
        args.file_path,
        project=args.project,
        status=args.status,
        limit=args.limit,
    )
    s.close()

    if not results:
        print(f"No decisions found touching {args.file_path}.")
        print("(Path must match exactly as stored in decision frontmatter.)")
        return

    print(f"Decisions touching {args.file_path} ({len(results)}):\n")
    for r in results:
        print(f"  [{r.id}] {r.title}")
        if r.project:
            print(f"    project: {r.project}  date: {r.date}")
        print()


def cmd_project(args: argparse.Namespace) -> None:
    """Print a structured project snapshot — same payload as the SessionStart hook."""
    from personal_mem.context import build_project_context

    cfg = load_config()
    sections = None
    if args.sections:
        sections = [s.strip() for s in args.sections.split(",") if s.strip()]
    payload = build_project_context(
        cfg,
        args.name,
        sections=sections,
        budget_tokens=args.budget,
    )
    print(payload)


def cmd_prune_orphans(args: argparse.Namespace) -> None:
    """Delete orphan session folders under the vault.

    Safety: defaults to dry-run unless ``--yes`` is passed. An orphan is a
    session folder with no derived notes/decisions, no real events.jsonl
    (< 500 bytes), empty ``files_touched``, empty ``commits``, older than
    ``--min-age`` seconds, and NOT the currently running session.
    """
    from personal_mem.prune import find_orphans, prune_orphans

    cfg = load_config()
    project = args.project or cfg.default_project or ""
    dry_run = not args.yes  # default dry-run; explicit --yes to commit

    orphans = find_orphans(
        cfg,
        project=project,
        min_age_seconds=args.min_age,
    )

    if not orphans:
        print("No orphan sessions found.")
        return

    label = "Would delete" if dry_run else "Deleting"
    scope = f" in project '{project}'" if project else ""
    print(f"{label} {len(orphans)} orphan session folder(s){scope}:\n")
    for p in orphans[:30]:
        print(f"  {p.relative_to(cfg.vault_root)}")
    if len(orphans) > 30:
        print(f"  ... and {len(orphans) - 30} more")

    result = prune_orphans(orphans, dry_run=dry_run)
    mb = result.freed_bytes / (1024 * 1024)
    print(
        f"\n{'Would free' if dry_run else 'Freed'}: {mb:.1f} MB across "
        f"{len(orphans)} folders."
    )

    if dry_run:
        print("\n(Dry run — re-run with --yes to actually delete.)")
        return

    # After real delete, drop the stale rows from the index so searches /
    # landing docs / SessionStart don't keep surfacing deleted sessions.
    try:
        from personal_mem.indexer import Indexer

        idx = Indexer(config=cfg)
        removed = 0
        for session_dir in orphans:
            prefix = str(session_dir.relative_to(cfg.vault_root))
            removed += _remove_notes_by_path_prefix(idx, prefix)
        idx.db.commit()
        idx.close()
        print(f"Removed {removed} index row(s).")
    except Exception as e:
        print(f"Warning: index cleanup failed — run `mem index --full` to rebuild. ({e})")


def _remove_notes_by_path_prefix(idx, prefix: str) -> int:
    """Drop notes whose path starts with ``prefix`` from every index table."""
    rows = idx.db.execute(
        "SELECT id FROM notes WHERE path LIKE ?", (prefix + "%",)
    ).fetchall()
    note_ids = [r["id"] for r in rows]
    if not note_ids:
        return 0

    placeholders = ",".join("?" for _ in note_ids)
    idx.db.execute(
        f"DELETE FROM notes_fts WHERE id IN ({placeholders})", note_ids
    )
    idx.db.execute(
        f"DELETE FROM note_concepts WHERE note_id IN ({placeholders})", note_ids
    )
    idx.db.execute(
        f"DELETE FROM edges WHERE source IN ({placeholders}) "
        f"OR target IN ({placeholders})",
        note_ids + note_ids,
    )
    idx.db.execute(f"DELETE FROM notes WHERE id IN ({placeholders})", note_ids)
    return len(note_ids)


# ---------------------------------------------------------------------------
# mem sources — inspect the source-type registry
# ---------------------------------------------------------------------------


def cmd_sources(args: argparse.Namespace) -> None:
    from personal_mem.sources import all_specs, get_spec

    action = getattr(args, "sources_action", None) or "list"

    if action == "list":
        specs = all_specs()
        if not specs:
            print("No source types registered.")
            return
        print(f"{'SLUG':<14} {'BUCKET':<14} {'LAYOUT':<15} {'SKILLS':<24} DESCRIPTION")
        print("-" * 100)
        for spec in specs:
            skills = ", ".join(spec.skills) if spec.skills else "—"
            print(
                f"{spec.slug:<14} {spec.bucket:<14} {spec.layout:<15} "
                f"{skills:<24} {spec.description}"
            )
        print()
        print(
            "To add a new source type: edit src/personal_mem/sources/registry.py "
            "and copy commands/_source_template.md."
        )
        return

    if action == "show":
        slug = args.slug
        spec = get_spec(slug)
        if spec is None:
            print(f"No registered source type for '{slug}'.")
            print("Unregistered types still work — they land in sources/<slug>/source.md.")
            sys.exit(1)
        print(f"# {spec.slug}")
        print(f"bucket:       {spec.bucket}")
        print(f"layout:       {spec.layout}")
        print(f"aliases:      {', '.join(spec.aliases) if spec.aliases else '—'}")
        print(f"skills:       {', '.join(spec.skills) if spec.skills else '—'}")
        print(f"description:  {spec.description}")
        # Cross-reference: walk commands/ and list skills whose frontmatter
        # claims this source_type.
        skills_found = _skills_for_source_type(spec.slug)
        if skills_found:
            print()
            print("skill files handling this type:")
            for name, desc in skills_found:
                print(f"  /{name:<20} {desc}")
        return

    # No action given → default to list
    cmd_sources(argparse.Namespace(sources_action="list"))


# ---------------------------------------------------------------------------
# mem skill — inspect and run skills from commands/
# ---------------------------------------------------------------------------


def cmd_skill(args: argparse.Namespace) -> None:
    action = getattr(args, "skill_action", None) or "list"

    if action == "list":
        skills = _load_all_skills()
        if not skills:
            print("No skills found in commands/.")
            return
        print(f"{'NAME':<22} {'SOURCE_TYPE':<24} {'CAPABILITIES':<22} DESCRIPTION")
        print("-" * 110)
        for skill in skills:
            st = _format_list_field(skill["fm"].get("source_type"))
            caps = _format_list_field(skill["fm"].get("capabilities"))
            desc = skill["fm"].get("description", "").strip().replace("\n", " ")
            print(f"{skill['name']:<22} {st:<24} {caps:<22} {desc}")
        return

    if action == "show":
        skill = _load_skill(args.name)
        if skill is None:
            print(f"No skill found at commands/{args.name}.md")
            sys.exit(1)
        fm = skill["fm"]
        print(f"# /{skill['name']}")
        for key in ("source_type", "capabilities", "tools", "description"):
            if key in fm:
                val = fm[key]
                if isinstance(val, list):
                    print(f"{key}:")
                    for item in val:
                        print(f"  - {item}")
                else:
                    print(f"{key}: {val}")
        print()
        print("--- head (first 30 lines of body) ---")
        body_lines = skill["body"].splitlines()
        for line in body_lines[:30]:
            print(line)
        if len(body_lines) > 30:
            print(f"... ({len(body_lines) - 30} more lines)")
        return

    # No action given → default to list
    cmd_skill(argparse.Namespace(skill_action="list"))


def _commands_dir() -> Path:
    """Return the commands/ directory shipped with the package."""
    # cli.py lives at src/personal_mem/cli.py; commands/ is at the repo root.
    return Path(__file__).resolve().parent.parent.parent / "commands"


def _load_skill(name: str) -> dict | None:
    """Load a single skill file by name. Returns None if not found."""
    from personal_mem.vault import parse_frontmatter

    path = _commands_dir() / f"{name}.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    return {"name": name, "path": path, "fm": fm, "body": body}


def _load_all_skills() -> list[dict]:
    """Return every skill in commands/ (excluding files starting with _)."""
    cmd_dir = _commands_dir()
    if not cmd_dir.exists():
        return []
    out = []
    for path in sorted(cmd_dir.glob("*.md")):
        if path.name.startswith("_"):
            continue
        skill = _load_skill(path.stem)
        if skill is not None:
            out.append(skill)
    return out


def _skills_for_source_type(slug: str) -> list[tuple[str, str]]:
    """Return (name, description) for each skill whose frontmatter claims this source_type."""
    out = []
    for skill in _load_all_skills():
        st = skill["fm"].get("source_type")
        types = st if isinstance(st, list) else [st] if st else []
        if slug in types:
            desc = skill["fm"].get("description", "").strip().replace("\n", " ")
            out.append((skill["name"], desc))
    return out


def _format_list_field(value) -> str:
    """Render a list-or-scalar frontmatter field for the CLI table."""
    if value is None or value == "":
        return "—"
    if isinstance(value, list):
        if not value:
            return "—"
        return ",".join(str(v) for v in value)
    return str(value)
