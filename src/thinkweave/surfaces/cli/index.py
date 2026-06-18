"""``weave index`` / ``stats`` / ``doctor`` / ``import``.

The ``weave connect`` deprecation alias (folded into ``weave index
--materialize-links``) was removed 2026-05-21; agents should call the
canonical form directly.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from thinkweave.core.config import load_config


def cmd_index(args: argparse.Namespace) -> None:
    from thinkweave.core.indexer import Indexer
    from thinkweave.core.vault import VaultManager

    cfg = load_config()
    idx = Indexer(config=cfg)

    VaultManager(config=cfg).ensure_dirs()

    if args.full:
        from thinkweave.synthesis.concept_hub import migrate_concept_hub_headings

        migrated = migrate_concept_hub_headings(cfg)
        if migrated:
            print(f"Migrated {migrated} concept hub(s) from `## Learning log` to `## Catalyst log`.")

    stats = idx.rebuild(full=args.full)

    if args.full:
        # Heal existing flat theme catalyst logs to the threaded temporal-DAG
        # layout concept hubs use. Runs after rebuild so the id->path/title maps
        # reflect current layout; idempotent (already-threaded themes skip).
        from thinkweave.synthesis.theme_hub import refold_theme_catalyst_logs

        rethreaded = refold_theme_catalyst_logs(cfg)
        if rethreaded:
            print(f"Re-threaded {rethreaded} theme catalyst log(s) to the indented DAG layout.")
    print(f"Indexed: {stats['indexed']}, Skipped: {stats['skipped']}, "
          f"Removed: {stats['removed']}, Edges: {stats['edges']}")

    if args.full:
        # Heal bare [[note-id]] wikilinks to path-based links vault-wide
        # (hub catalyst logs AND note/decision/source bodies) so clicking a
        # reference resolves structurally instead of spawning a phantom stub.
        # Runs after rebuild so the id->path map reflects current file layout;
        # idempotent (only bare id-shaped links present in the index match).
        from thinkweave.synthesis.hub import (
            build_id_path_map,
            migrate_bare_id_links,
        )

        idmap = build_id_path_map(idx.db)
        links_healed = 0
        files_touched = 0
        for p in cfg.vault_root.rglob("*.md"):
            n = migrate_bare_id_links(p, idmap)
            if n:
                links_healed += n
                files_touched += 1
        if links_healed:
            print(
                f"Healed {links_healed} bare wikilink(s) → path-based "
                f"across {files_touched} file(s)."
            )

    if args.embed:
        try:
            from thinkweave.core.embeddings import EmbeddingSearch
            es = EmbeddingSearch(config=cfg)
            if getattr(args, "reset", False):
                removed = es.clear()
                print(
                    f"Embeddings cache reset: {removed} vector(s) cleared "
                    f"(full re-embed follows)."
                )
            only_new = bool(getattr(args, "only_new", False))
            since = getattr(args, "since", "") or ""
            embed_stats = es.compute_all(only_new=only_new, since=since)
            if embed_stats.get("cutoff"):
                print(
                    f"Embeddings (incremental — cutoff {embed_stats['cutoff']}): "
                    f"{embed_stats['computed']} computed, "
                    f"{embed_stats['skipped']} cached, "
                    f"{embed_stats['scanned']} scanned"
                )
            else:
                print(
                    f"Embeddings: {embed_stats['computed']} computed, "
                    f"{embed_stats['skipped']} cached"
                )
        except ImportError:
            print("Embeddings require: pip install thinkweave[embeddings]")

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
    from thinkweave.core.indexer import Indexer

    print("[deprecated] `weave stats` will be removed; use `weave doctor` for vault health.")
    print()

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


def _embedding_posture_lines(cfg) -> list[str]:
    """Human-readable embedding posture for ``weave doctor``.

    Reports the configured provider/model, whether the required key is
    reachable, and the cache size — then, when no key is present on the
    OpenAI path, points at the free local fallback. The goal: make the
    silent "similarity degraded to BM25/FTS" state visible, and give a
    keyless user a concrete free path instead of a dead end.
    """
    from thinkweave.core.api_config import embeddings_config, load_api_config

    emb = embeddings_config(load_api_config(cfg.vault_root))
    provider, model = emb["provider"], emb["model"]
    lines = ["Embedding posture:", f"  provider/model : {provider} / {model}"]

    show_hint = False
    if provider == "openai":
        from thinkweave.core.api_keys import get_provider_key

        key_present = bool(get_provider_key("openai"))
        lines.append(f"  api key        : OPENAI_API_KEY {'present' if key_present else 'MISSING'}")
        show_hint = not key_present
    elif provider == "litellm":
        lines.append("  api key        : provider-specific (LiteLLM env vars)")
    else:  # sentence_transformer / local
        lines.append("  api key        : not required (local, free)")

    n = 0
    if cfg.embeddings_db.exists():
        import sqlite3

        try:
            db = sqlite3.connect(str(cfg.embeddings_db))
            n = int(db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])
            db.close()
        except sqlite3.Error:
            n = 0
    lines.append(f"  cache          : {n} vector(s) at {cfg.embeddings_db}")

    if show_hint:
        lines += [
            "  ⚠ No OPENAI_API_KEY → semantic/hybrid search is OFF; retrieval falls",
            "    back to BM25 keyword (FTS), which always works. For free semantic",
            "    search with no key (local):",
            "      pip install thinkweave[embeddings-local]",
            "      set  embeddings.provider: sentence_transformer  in vault/config/api.yaml",
            "      run  weave index --embed --reset",
        ]
    elif n == 0:
        lines.append("  ⚠ Cache empty → run: weave index --embed")
    return lines


def cmd_doctor(args: argparse.Namespace) -> None:
    """Run coherence + MCP-wiring checks (read-only by default).

    Flag matrix:
      bare        → vault coherence only (legacy default)
      --mcp       → MCP-registration diagnostics only
      --all       → both
      --migrate / --fix-phantoms only meaningful with the vault path

    With ``--migrate``, runs idempotent one-shot data migrations from
    ``operations/migrations.py`` (e.g. ``todo+research`` → queue) before
    printing the report. With ``--fix-phantoms``, deletes the zero-byte
    phantom files surfaced by the report.

    Exits non-zero if any selected check fails.
    """
    from thinkweave.surfaces.cli.mcp_doctor import run_mcp_doctor

    mcp_mode = bool(getattr(args, "mcp", False))
    all_mode = bool(getattr(args, "all", False))
    # Default: vault-only. --mcp = MCP-only. --all = both.
    do_vault = all_mode or not mcp_mode
    do_mcp = all_mode or mcp_mode

    exit_code = 0

    if do_vault:
        from thinkweave.synthesis.concepts import doctor_report, format_doctor_report

        cfg = load_config()
        if not cfg.index_db.exists():
            print(f"Index not found at {cfg.index_db}. Run `weave index` first.")
            sys.exit(1)

        if getattr(args, "migrate", False):
            from thinkweave.operations.migrations import (
                migrate_dormant_themes_to_resolved,
                migrate_todo_research_to_queue,
            )

            moved = migrate_todo_research_to_queue(cfg.vault_root)
            print(f"migrate_todo_research_to_queue: {moved} note(s) moved to queues")
            flipped = migrate_dormant_themes_to_resolved(cfg.vault_root)
            print(f"migrate_dormant_themes_to_resolved: {flipped} theme(s) flipped")

        include_isolation = bool(getattr(args, "isolation", False))
        report = doctor_report(cfg, include_isolation=include_isolation)

        if getattr(args, "fix_phantoms", False):
            phantoms = report.get("phantom_note_files", [])
            for path in phantoms:
                try:
                    path.unlink()
                except OSError as exc:
                    print(f"  ! could not delete {path}: {exc}")
            print(f"fix-phantoms: deleted {len(phantoms)} zero-byte file(s)")
            # Re-run after deletion so the printed report reflects the new state.
            report = doctor_report(cfg, include_isolation=include_isolation)

        print(format_doctor_report(report))

        print()
        for line in _embedding_posture_lines(cfg):
            print(line)

    if do_mcp:
        if do_vault:
            print()  # visual separator
        result = run_mcp_doctor()
        if not result.passed:
            exit_code = 1

    if exit_code != 0:
        sys.exit(exit_code)


def _count_chatgpt_conversations(path: Path, limit: int) -> int:
    """Cheap pre-flight conversation count for the route picker.

    ChatGPT's ``conversations.json`` is a JSON array; we don't need to
    parse the full bodies — just count list entries.
    """
    import json
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if not isinstance(data, list):
        return 0
    n = len(data)
    if limit > 0:
        n = min(n, limit)
    return n


def cmd_import(args: argparse.Namespace) -> None:
    cfg = load_config()

    if args.source == "claude-code":
        from thinkweave.onboarding.claude_code_seed import (
            DEFAULT_CC_PROJECTS_ROOT,
            import_claude_code,
        )

        if getattr(args, "enrich", False):
            from thinkweave.onboarding.enrich_batch import (
                find_pending_sessions,
                run_enrichment_batch,
            )
            from thinkweave.operations._backfill_route import choose_route

            # --dry-run lists the pending set (the inline /seed-enrich skill
            # parses it); route selection only governs real execution.
            if args.dry_run:
                run_enrichment_batch(
                    cfg, project_filter=args.project, limit=args.enrich_limit, dry_run=True
                )
                return

            n_pending = len(
                find_pending_sessions(
                    cfg, project_filter=args.project, limit=args.enrich_limit
                )
            )
            if n_pending == 0:
                print("No pending claude-code sessions found. Nothing to synthesise.")
                return

            decision = choose_route(via=getattr(args, "via", None), n_items=n_pending)
            if decision.route == "inline":
                print(
                    f"Inline session synthesis ({n_pending} pending session(s); "
                    f"{decision.reason}).\n"
                    f"  Run:  /seed-enrich\n"
                    f"  (synthesises each session via the running model, no "
                    f"provider key required)."
                )
                return

            run_enrichment_batch(
                cfg,
                project_filter=args.project,
                model=args.enrich_model or None,
                limit=args.enrich_limit,
                dry_run=False,
            )
            return

        root = Path(args.cc_root) if args.cc_root else DEFAULT_CC_PROJECTS_ROOT
        # --sample-only is the CLI shorthand for --limit 50, newest-first.
        # Explicit --limit wins if both are passed.
        effective_limit = args.limit if args.limit else (50 if getattr(args, "sample_only", False) else 0)
        stats = import_claude_code(
            cfg,
            project_filter=args.project,
            dry_run=args.dry_run,
            claude_projects_root=root,
            since=args.since,
            limit=effective_limit,
        )
        label = "Would materialize" if args.dry_run else "Materialized"
        print(
            f"{label}: {stats['materialized']} session(s) across "
            f"{len(stats['per_project'])} project(s).\n"
            f"  discovered={stats['discovered']}  "
            f"skipped_no_content={stats['skipped_no_content']}  "
            f"skipped_filter={stats['skipped_filter']}  "
            f"skipped_already_imported={stats['skipped_already_imported']}\n"
        )
        if stats["per_project"]:
            print("  per-project breakdown:")
            for proj, counts in sorted(
                stats["per_project"].items(),
                key=lambda kv: -kv[1]["materialized"],
            ):
                print(
                    f"    {counts['materialized']:>4}  {proj}"
                    f"  (of {counts['discovered']} discovered)"
                )
        if stats["errors"]:
            print(f"\n  errors ({len(stats['errors'])}):")
            for err in stats["errors"][:10]:
                print(f"    {err}")
        if args.dry_run:
            print("\n(Dry run — re-run without --dry-run to materialize.)")
        return

    if args.source == "claude-history":
        from pathlib import Path as _Path

        from thinkweave.acquisition.importers.claude_history import import_claude_history

        db_path = _Path(args.db_path) if args.db_path else None
        stats = import_claude_history(
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
            print("File path required. Usage: weave import chatgpt <path-to-conversations.json>")
            sys.exit(1)

        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        # B2: --via {inline,batch} route. The importer's summarisation
        # step is the LLM-bearing part; inline routes to /import-chatgpt
        # (CC skill); batch keeps the existing path which now flows through
        # agent_client.
        from thinkweave.operations._backfill_route import choose_route

        # Cheap probe of conversation count via the importer's index.
        try:
            n_conversations = _count_chatgpt_conversations(Path(args.path), args.limit)
        except OSError:
            n_conversations = 0
        decision = choose_route(
            via=getattr(args, "via", None),
            n_items=n_conversations,
        )
        if decision.route == "inline":
            print(
                f"Inline ChatGPT import ({n_conversations} conversation(s); "
                f"{decision.reason}).\n"
                f"  Run:  /import-chatgpt {args.path}\n"
                f"  (skill walks conversations via the running model, no "
                f"provider key required)."
            )
            return

        from thinkweave.acquisition.importers.chatgpt import import_chatgpt

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
        from thinkweave.acquisition.importers.transcript import import_transcript

        path = import_transcript(
            cfg,
            file_path=Path(args.path),
            source_type=args.source_type,
            project=args.project,
        )
        print(f"Imported source note at {path}")

    elif args.source == "messenger":
        if not args.path:
            print("File path required. Usage: weave import messenger <path-to-export.json>")
            sys.exit(1)

        from thinkweave.acquisition.importers.messenger import import_messenger

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
