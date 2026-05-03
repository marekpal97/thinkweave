"""``mem index`` / ``stats`` / ``doctor`` / ``connect`` (legacy alias) /
``enrich`` / ``import``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from personal_mem.core.config import load_config


def cmd_index(args: argparse.Namespace) -> None:
    from personal_mem.core.indexer import Indexer
    from personal_mem.core.vault import VaultManager

    cfg = load_config()
    idx = Indexer(config=cfg)

    VaultManager(config=cfg).ensure_dirs()

    if args.full:
        from personal_mem.synthesis.concept_hub import migrate_concept_hub_headings

        migrated = migrate_concept_hub_headings(cfg)
        if migrated:
            print(f"Migrated {migrated} concept hub(s) from `## Learning log` to `## Catalyst log`.")

    stats = idx.rebuild(full=args.full)
    print(f"Indexed: {stats['indexed']}, Skipped: {stats['skipped']}, "
          f"Removed: {stats['removed']}, Edges: {stats['edges']}")

    if args.embed:
        try:
            from personal_mem.core.embeddings import EmbeddingSearch
            es = EmbeddingSearch(config=cfg)
            embed_stats = es.compute_all()
            print(f"Embeddings: {embed_stats['computed']} computed, {embed_stats['skipped']} cached")
        except ImportError:
            print("Embeddings require: pip install personal-mem[embeddings]")

    if getattr(args, "materialize_links", False):
        cstats = idx.materialize_links(max_links=getattr(args, "max_links", 5))
        print(
            f"Materialize: {cstats['notes_updated']} note(s) updated, "
            f"{cstats['notes_skipped']} skipped, "
            f"{cstats['links_written']} link(s) written."
        )
        fstats = idx.rebuild(full=False)
        print(f"  Reindex edges: {fstats['edges']}")

    idx.close()


def cmd_stats(args: argparse.Namespace) -> None:
    from personal_mem.core.indexer import Indexer

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
    """Run vault coherence checks (read-only by default).

    With ``--migrate``, runs idempotent one-shot data migrations from
    ``operations/migrations.py`` (e.g. ``todo+research`` → queue) before
    printing the report.
    """
    from personal_mem.synthesis.concepts import doctor_report, format_doctor_report

    cfg = load_config()
    if not cfg.index_db.exists():
        print(f"Index not found at {cfg.index_db}. Run `mem index` first.")
        sys.exit(1)

    if getattr(args, "migrate", False):
        from personal_mem.operations.migrations import migrate_todo_research_to_queue

        moved = migrate_todo_research_to_queue(cfg.vault_root)
        print(f"migrate_todo_research_to_queue: {moved} note(s) moved to queues")

    report = doctor_report(cfg)
    print(format_doctor_report(report))


def cmd_connect(args: argparse.Namespace) -> None:
    """[DEPRECATED] Use `mem index --materialize-links` instead.

    Phase 4 C: this command is folded into `mem index`. Alias kept for one
    release; will be removed.
    """
    print(
        "deprecated: use `mem index --materialize-links` "
        "(alias kept for one release).",
        file=sys.stderr,
    )
    from personal_mem.core.indexer import Indexer

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


def cmd_enrich(args: argparse.Namespace) -> None:
    """LLM-assisted concept enrichment for notes missing concepts."""
    from personal_mem.core.indexer import Indexer
    from personal_mem.enrich import enrich

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
            from personal_mem.core.indexer import Indexer as Idx2
            idx2 = Idx2(config=cfg)
            cstats = idx2.materialize_links(max_links=5)
            print(f"  Updated: {cstats['notes_updated']}, Links: {cstats['links_written']}")
            idx2.close()

            print("\nFinal reindex to pick up new wikilinks...")
            idx3 = Indexer(config=cfg)
            fstats = idx3.rebuild(full=False)
            print(f"  Edges: {fstats['edges']}")
            idx3.close()


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
