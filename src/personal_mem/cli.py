"""CLI entry point for personal_mem.

Usage: mem <command> [options]
All subcommands use argparse (no external dependencies).
"""

from __future__ import annotations

import argparse
import json
import sys
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
    p_search.add_argument("--type", "-t", default="")
    p_search.add_argument("--project", "-p", default="")
    p_search.add_argument("--tags", default="", help="Comma-separated tags")
    p_search.add_argument("--limit", "-n", type=int, default=10)
    p_search.add_argument("--semantic", action="store_true", help="Use semantic search")

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
    p_import.add_argument("source", choices=["claude-mem", "hive", "file"])
    p_import.add_argument("path", nargs="?", default="", help="File path (for 'file' source)")
    p_import.add_argument("--source-type", default="article", help="Source type for file import")
    p_import.add_argument("--project", "-p", default="")

    # --- mem context ---
    p_context = sub.add_parser("context", help="Get relevant notes for current context")
    p_context.add_argument("--project", "-p", default="")
    p_context.add_argument("--tags", default="", help="Comma-separated tags")
    p_context.add_argument("--query", "-q", default="")
    p_context.add_argument("--limit", "-n", type=int, default=5)

    # --- mem stats ---
    sub.add_parser("stats", help="Show vault statistics")

    # --- mem hooks ---
    p_hooks = sub.add_parser("hooks", help="Manage Claude Code hooks")
    hooks_sub = p_hooks.add_subparsers(dest="hooks_action")
    p_install = hooks_sub.add_parser("install", help="Install hooks")
    p_install.add_argument("--project", "-p", default="")
    hooks_sub.add_parser("uninstall", help="Uninstall hooks")

    # --- mem backlog ---
    p_backlog = sub.add_parser("backlog", help="List notes tagged 'todo'")
    p_backlog.add_argument("--project", "-p", default="", help="Filter by project")
    p_backlog.add_argument("--tag", default="todo", help="Tag to query (default: todo)")

    # --- mem concepts ---
    p_concepts = sub.add_parser("concepts", help="List, tighten, or merge concepts")
    concepts_sub = p_concepts.add_subparsers(dest="concepts_action")
    p_concepts_list = concepts_sub.add_parser("list", help="List all concepts with counts")
    p_concepts_list.add_argument("--prefix", default="", help="Filter by prefix")
    p_concepts_list.add_argument("--min-count", type=int, default=1, help="Minimum note count")
    concepts_sub.add_parser("tighten", help="Find near-duplicate concepts")
    p_merge = concepts_sub.add_parser("merge", help="Merge one concept into another")
    p_merge.add_argument("from_concept", help="Concept to rename/remove")
    p_merge.add_argument("to_concept", help="Canonical concept to merge into")

    # --- mem landing ---
    p_landing = sub.add_parser("landing", help="Generate project landing documents")
    p_landing.add_argument("--project", "-p", default="", help="Project name")
    p_landing.add_argument(
        "--doc", "-d", default="all",
        choices=["all", "decisions", "backlog", "state"],
        help="Which document(s) to generate (default: all)",
    )

    # --- mem init ---
    sub.add_parser("init", help="Initialize a new vault")

    # --- mem restructure ---
    p_restructure = sub.add_parser(
        "restructure", help="Move flat vault files into sessions/notes/decisions subdirectories"
    )
    p_restructure.add_argument("--dry-run", action="store_true", help="Show what would move")

    # --- mem migrate ---
    p_migrate = sub.add_parser(
        "migrate", help="Bulk-update frontmatter across vault notes"
    )
    p_migrate.add_argument("--type", "-t", default="", help="Filter: only notes of this type")
    p_migrate.add_argument("--project", "-p", default="", help="Filter: only this project")
    p_migrate.add_argument("--tags", default="", help="Filter: comma-separated tags")
    p_migrate.add_argument("--set", dest="set_fields", action="append", default=[],
                           help="Set a frontmatter field: --set key=value (repeatable)")
    p_migrate.add_argument("--rename-tag", nargs=2, metavar=("OLD", "NEW"),
                           help="Rename a tag across all matching notes")
    p_migrate.add_argument("--rename-type", nargs=2, metavar=("OLD", "NEW"),
                           help="Rename a note type across all matching notes")
    p_migrate.add_argument("--dry-run", action="store_true", help="Show what would change")

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Dispatch
    commands = {
        "add": cmd_add,
        "backlog": cmd_backlog,
        "concepts": cmd_concepts,
        "landing": cmd_landing,
        "migrate": cmd_migrate,
        "restructure": cmd_restructure,
        "search": cmd_search,
        "show": cmd_show,
        "link": cmd_link,
        "graph": cmd_graph,
        "index": cmd_index,
        "import": cmd_import,
        "context": cmd_context,
        "stats": cmd_stats,
        "hooks": cmd_hooks,
        "init": cmd_init,
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
    if not project:
        print("Project name required. Use --project or set PERSONAL_MEM_PROJECT.")
        sys.exit(1)

    written = write_landing_docs(cfg, project, docs=args.doc)
    for filename, path in written.items():
        print(f"  {filename} → {path.relative_to(cfg.vault_root)}")
    print(f"Generated {len(written)} landing document(s).")


def cmd_concepts(args: argparse.Namespace) -> None:
    from personal_mem.concepts import (
        find_near_duplicates,
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

    elif action == "tighten":
        idx = Indexer(config=cfg)
        concept_counts = get_all_concepts(idx.db)
        idx.close()

        if not concept_counts:
            print("No concepts in vault.")
            return

        duplicates = find_near_duplicates(list(concept_counts.keys()))
        if not duplicates:
            print(f"No near-duplicates found among {len(concept_counts)} concepts.")
            return

        print(f"Found {len(duplicates)} potential duplicate(s):\n")
        for a, b, reason in duplicates:
            count_a = concept_counts.get(a, 0)
            count_b = concept_counts.get(b, 0)
            print(f"  {a} ({count_a}) ↔ {b} ({count_b})  — {reason}")
        print(f"\nTo merge: mem concepts merge <from> <to>")

    elif action == "merge":
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

        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()

        print(f"Merged '{from_c}' → '{to_c}': {changed} notes updated. Alias saved. Index rebuilt.")


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

    if args.semantic:
        _cmd_search_semantic(args, cfg)
        return

    s = Search(config=cfg)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None

    results = s.search(
        query=args.query,
        note_type=args.type,
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
        from personal_mem.importers.claude_mem import import_claude_mem

        stats = import_claude_mem(cfg)
        print(f"Imported {stats['imported']} notes from claude-mem, {stats['skipped']} skipped")

    elif args.source == "hive":
        from personal_mem.importers.hive_insights import import_hive_insights

        stats = import_hive_insights(cfg, project=args.project)
        print(f"Imported {stats['imported']} notes from hive, {stats['skipped']} skipped")

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


def cmd_context(args: argparse.Namespace) -> None:
    from personal_mem.search import Search

    cfg = load_config()
    s = Search(config=cfg)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None

    results = s.get_context(
        project=args.project,
        tags=tags,
        query=args.query,
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


def cmd_restructure(args: argparse.Namespace) -> None:
    """Move flat vault files into sessions/notes/decisions subdirectories.

    Also merges 'personal-mem' into 'personal_mem' if both exist.
    """
    import shutil

    from personal_mem.vault import VaultManager, parse_frontmatter, render_frontmatter

    cfg = load_config()
    vm = VaultManager(config=cfg)
    projects_dir = vm.root / "projects"
    dry_run = args.dry_run
    moved = 0

    if not projects_dir.exists():
        print("No projects directory found.")
        return

    # Phase 1: Merge personal-mem → personal_mem (if both exist)
    hyphen_dir = projects_dir / "personal-mem"
    underscore_dir = projects_dir / "personal_mem"
    if hyphen_dir.exists() and hyphen_dir.is_dir():
        print(f"\nMerging personal-mem → personal_mem:")
        for md_file in hyphen_dir.glob("*.md"):
            text = md_file.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(text)
            note_type = fm.get("type", "note")

            # Update project field
            if fm.get("project") == "personal-mem":
                fm["project"] = "personal_mem"
                text = render_frontmatter(fm) + "\n\n" + body

            # Determine destination
            if note_type == "session":
                note_id = fm.get("id", "unknown")
                date_str = fm.get("date", "unknown")
                dest_dir = underscore_dir / "sessions" / f"{note_id}-{date_str}"
            elif note_type == "source":
                dest_dir = underscore_dir / "sources"
            else:
                # Notes and decisions go to sessions/misc
                dest_dir = underscore_dir / "sessions" / "misc"

            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_name = "session.md" if note_type == "session" else md_file.name
            dest = dest_dir / dest_name

            if dry_run:
                print(f"  Would move: {md_file.name} → {dest.relative_to(vm.root)}")
            else:
                dest.write_text(text, encoding="utf-8")
                md_file.unlink()
                print(f"  Moved: {md_file.name} → {dest.relative_to(vm.root)}")
            moved += 1

        if not dry_run and not any(hyphen_dir.iterdir()):
            hyphen_dir.rmdir()
            print(f"  Removed empty directory: personal-mem/")

    # Phase 2: Restructure each project's flat files into session folders
    for proj_dir in sorted(projects_dir.iterdir()):
        if not proj_dir.is_dir():
            continue
        # Skip the hyphen directory if it was merged in phase 1
        if proj_dir == hyphen_dir:
            continue

        flat_files = list(proj_dir.glob("*.md"))
        if not flat_files:
            continue

        project_name = proj_dir.name
        print(f"\nRestructuring {project_name}/:")

        for md_file in flat_files:
            text = md_file.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(text)
            note_type = fm.get("type", "note")

            if note_type == "session":
                note_id = fm.get("id", "unknown")
                date_str = fm.get("date", "unknown")
                dest_dir = proj_dir / "sessions" / f"{note_id}-{date_str}"
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / "session.md"
            elif note_type == "source":
                # Sources already have their own directory
                continue
            else:
                # Notes and decisions go to sessions/misc
                dest_dir = proj_dir / "sessions" / "misc"
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / md_file.name

            if dry_run:
                print(f"  Would move: {md_file.name} → {dest.relative_to(proj_dir)}")
            else:
                shutil.move(str(md_file), str(dest))
                print(f"  Moved: {md_file.name} → {dest.relative_to(proj_dir)}")
            moved += 1

    # Phase 3: Consolidate notes/ and decisions/ dirs into session folders
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        notes_dir = proj_dir / "notes"
        decisions_dir = proj_dir / "decisions"
        sessions_dir = proj_dir / "sessions"

        for subdir in [notes_dir, decisions_dir]:
            if not subdir.exists():
                continue
            for md_file in list(subdir.glob("*.md")):
                text = md_file.read_text(encoding="utf-8")
                fm, _ = parse_frontmatter(text)
                derived = fm.get("derived_from", [])
                if isinstance(derived, str):
                    derived = [derived]

                # Derived notes go to their parent session folder
                placed = False
                if derived:
                    session_id = derived[0]
                    for ses_dir in sessions_dir.iterdir() if sessions_dir.exists() else []:
                        if ses_dir.is_dir() and ses_dir.name.startswith(session_id):
                            dest = ses_dir / md_file.name
                            if dry_run:
                                print(f"  Would move: {subdir.name}/{md_file.name} → {dest.relative_to(proj_dir)}")
                            else:
                                shutil.move(str(md_file), str(dest))
                                print(f"  Moved derived {md_file.name} → {dest.relative_to(proj_dir)}")
                            moved += 1
                            placed = True
                            break

                # Standalone notes go to sessions/misc
                if not placed:
                    misc_dir = sessions_dir / "misc"
                    misc_dir.mkdir(parents=True, exist_ok=True)
                    dest = misc_dir / md_file.name
                    if dry_run:
                        print(f"  Would move: {subdir.name}/{md_file.name} → {dest.relative_to(proj_dir)}")
                    else:
                        shutil.move(str(md_file), str(dest))
                        print(f"  Moved {md_file.name} → {dest.relative_to(proj_dir)}")
                    moved += 1

            # Clean up empty directories
            if not dry_run and subdir.exists() and not any(subdir.iterdir()):
                subdir.rmdir()
                print(f"  Removed empty directory: {subdir.name}/")

    # Phase 4: Rebuild index
    if moved > 0 and not dry_run:
        from personal_mem.indexer import Indexer

        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()
        print(f"\nIndex rebuilt.")

    action = "Would move" if dry_run else "Moved"
    print(f"\n{action} {moved} files total.")


def cmd_migrate(args: argparse.Namespace) -> None:
    """Bulk-update frontmatter across vault notes.

    Examples:
        mem migrate --rename-tag gotcha pitfall
        mem migrate --rename-type note knowledge
        mem migrate --type note --set confidence=0.5
        mem migrate --project old-name --set project=new-name
    """
    from personal_mem.indexer import Indexer
    from personal_mem.vault import VaultManager, parse_frontmatter, render_frontmatter

    cfg = load_config()
    vm = VaultManager(config=cfg)

    # Collect all markdown files
    md_files = vm.get_all_md_files()

    filter_type = args.type
    filter_project = args.project
    filter_tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []

    changed = 0
    skipped = 0

    for md_file in md_files:
        text = md_file.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)

        if not fm:
            skipped += 1
            continue

        # Apply filters
        if filter_type and fm.get("type", "") != filter_type:
            skipped += 1
            continue
        if filter_project and fm.get("project", "") != filter_project:
            skipped += 1
            continue
        if filter_tags:
            note_tags = fm.get("tags", [])
            if isinstance(note_tags, str):
                note_tags = [t.strip() for t in note_tags.split(",")]
            if not set(filter_tags).issubset(set(note_tags)):
                skipped += 1
                continue

        modified = False

        # --rename-tag OLD NEW
        if args.rename_tag:
            old_tag, new_tag = args.rename_tag
            tags = fm.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",")]
            if old_tag in tags:
                tags = [new_tag if t == old_tag else t for t in tags]
                fm["tags"] = tags
                modified = True

        # --rename-type OLD NEW
        if args.rename_type:
            old_type, new_type = args.rename_type
            if fm.get("type") == old_type:
                fm["type"] = new_type
                modified = True

        # --set key=value (repeatable)
        for field_spec in args.set_fields:
            if "=" not in field_spec:
                print(f"Invalid --set format: {field_spec} (expected key=value)")
                sys.exit(1)
            key, value = field_spec.split("=", 1)
            key = key.strip()
            value = value.strip()

            # Auto-detect lists
            if value.startswith("[") and value.endswith("]"):
                value = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]

            fm[key] = value
            modified = True

        if not modified:
            skipped += 1
            continue

        if args.dry_run:
            rel = md_file.relative_to(vm.root)
            print(f"  Would update: {rel}")
            changed += 1
            continue

        # Write back
        new_content = render_frontmatter(fm) + "\n\n" + body
        md_file.write_text(new_content, encoding="utf-8")
        changed += 1

    # Rebuild index after changes
    if changed > 0 and not args.dry_run:
        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()

    action = "Would update" if args.dry_run else "Updated"
    print(f"{action} {changed} notes, skipped {skipped}")
