"""``weave add`` / ``show`` / ``link`` / ``search`` / ``context`` / ``update``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from thinkweave.core.config import load_config
from thinkweave.core.schemas import NoteType


def cmd_add(args: argparse.Namespace) -> None:
    from thinkweave.operations.notes import create_note
    cfg = load_config()
    body = args.body or (sys.stdin.read() if not sys.stdin.isatty() else "")
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
    fm_kvs = getattr(args, "frontmatter", []) or []
    extra_fm = dict(_parse_fm_token(kv) for kv in fm_kvs) if fm_kvs else None
    result = create_note(
        cfg, note_type=NoteType(args.type), title=args.title, body=body,
        project=args.project or cfg.default_project, tags=tags, session_id=args.session,
        extra_frontmatter=extra_fm,
    )
    note = result.note
    verb = "Exists" if result.existed else "Created"
    print(f"{verb} {note.type.value} [{note.id}] at {(cfg.vault_root / note.path).relative_to(cfg.vault_root)}")


def cmd_search(args: argparse.Namespace) -> None:
    from thinkweave.operations import search as ops_search

    cfg = load_config()

    mode = args.mode
    if args.semantic and mode == "fts":
        mode = "similar"

    type_arg: str | list[str] = args.type
    if args.type and "," in args.type:
        type_arg = [t.strip() for t in args.type.split(",") if t.strip()]

    # Concept lookup is a distinct retrieval operation (search_by_concept),
    # not part of the fts/similar/hybrid seam wired through operations.search,
    # so it keeps its own Search instance until an ops wrapper exists.
    if args.concept:
        from thinkweave.retrieval.search import Search

        concept_list = [c.strip() for c in args.concept.split(",") if c.strip()]
        s = Search(config=cfg)
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
        results = ops_search.query_similar(
            cfg, args.query, note_type=type_arg, project=args.project, limit=args.limit
        )
        if not results:
            print(
                "No semantic results. If embeddings aren't set up yet, run "
                "`weave index --embed` with OPENAI_API_KEY set."
            )
            return
    elif mode == "hybrid":
        results = ops_search.query_hybrid(
            cfg, args.query, note_type=type_arg, project=args.project, limit=args.limit
        )
    else:
        results = ops_search.query_fts(
            cfg,
            args.query,
            note_type=type_arg,
            project=args.project,
            tags=tags,
            limit=args.limit,
        )

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


def cmd_show(args: argparse.Namespace) -> None:
    from thinkweave.core.vault import VaultManager
    from thinkweave.retrieval.search import Search

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
        print(f"Type: {note['type']}")
        print(f"Title: {note['title']}")
        print(f"Project: {note['project']}")
        print(f"Date: {note['date']}")
        print(f"Tags: {note['tags']}")
        print(f"\n{note['body_text']}")


def cmd_link(args: argparse.Namespace) -> None:
    from thinkweave.core.indexer import EDGE_TYPE_TO_FIELD, Indexer
    from thinkweave.core.vault import VaultManager

    cfg = load_config()
    idx = Indexer(config=cfg)
    vm = VaultManager(config=cfg)

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

    fm_field = EDGE_TYPE_TO_FIELD[args.type]
    vm.update_note(
        vm.root / src["path"],
        frontmatter_updates={fm_field: [args.target]},
    )

    idx.index_file(vm.root / src["path"])
    idx.close()
    print(f"Linked {args.source} --{args.type}--> {args.target}")


def cmd_context(args: argparse.Namespace) -> None:
    from thinkweave.operations.search import query_context

    cfg = load_config()
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
    concepts = [c.strip() for c in args.concepts.split(",") if c.strip()] if args.concepts else None

    results = query_context(
        cfg,
        project=args.project,
        tags=tags,
        query=args.query,
        concepts=concepts,
        limit=args.limit,
    )

    if not results:
        print("No context available.")
        return

    for r in results:
        tag_str = f" [{', '.join(r.tags)}]" if r.tags else ""
        print(f"  [{r.type}] {r.title} ({r.id}){tag_str}")


def _parse_fm_token(kv: str) -> tuple[str, object]:
    if "=" not in kv:
        print(f"Bad --frontmatter token (need key=value): {kv}")
        sys.exit(1)
    key, val = kv.split("=", 1)
    # Structured values: a value that looks like JSON ([...] / {...}) is
    # parsed as JSON, so list-of-dict fields (e.g. a decision's
    # prediction_history) survive the CLI round-trip instead of being
    # comma-split into broken strings. Malformed JSON falls through to the
    # legacy string handling.
    if val[:1] in ("[", "{"):
        try:
            return key, json.loads(val)
        except json.JSONDecodeError:
            pass
    if val.lower() in ("true", "false"):
        return key, val.lower() == "true"
    if "," in val:
        return key, [v.strip() for v in val.split(",") if v.strip()]
    return key, val


def cmd_update(args: argparse.Namespace) -> None:
    """CLI parity for weave_update — set frontmatter, append body."""
    from thinkweave.operations.notes import update_note
    cfg = load_config()
    fm_updates = dict(_parse_fm_token(kv) for kv in args.frontmatter)
    # --frontmatter-json: a whole updates dict from a file (or '-' = stdin).
    # The headless-worker path — writing a temp JSON file and passing its
    # path avoids every layer of shell quoting, which matters doubly under
    # Windows Git Bash. Explicit -f tokens win on key collision.
    if getattr(args, "frontmatter_json", None):
        raw = (
            sys.stdin.read()
            if args.frontmatter_json == "-"
            else Path(args.frontmatter_json).expanduser().read_text(encoding="utf-8")
        )
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"Bad --frontmatter-json payload: {e}")
            sys.exit(1)
        if not isinstance(loaded, dict):
            print("Bad --frontmatter-json payload: expected a JSON object")
            sys.exit(1)
        fm_updates = {**loaded, **fm_updates}
    body_append = Path(args.body_append).expanduser().read_text(encoding="utf-8") if args.body_append else ""
    try:
        note = update_note(cfg, args.note_id, frontmatter_updates=fm_updates or None, body_append=body_append)
    except (FileNotFoundError, ValueError) as e:
        print(str(e))
        sys.exit(1)
    print(f"Updated {args.note_id} ({(cfg.vault_root / note.path).relative_to(cfg.vault_root)})")


def cmd_decisions(args: argparse.Namespace) -> None:
    """Query decisions — primary use: ``weave decisions --file <path>``."""
    from thinkweave.retrieval.search import Search

    if not args.file_path:
        print("Usage: weave decisions --file <path> [--project X] [--status accepted]")
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
    from thinkweave.retrieval.context import build_project_context

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


def cmd_backlog(args: argparse.Namespace) -> None:
    from thinkweave.retrieval.search import Search
    from thinkweave.acquisition.sources import all_specs
    from thinkweave.acquisition.sources.queue import Queue

    cfg = load_config()
    s = Search(config=cfg)

    results = s.search(
        query="",
        project=args.project,
        tags=[args.tag],
        limit=50,
    )
    s.close()

    hide_auto = getattr(args, "hide_auto", False)
    if hide_auto:
        results = [r for r in results if "auto" not in (r.tags or [])]

    queue_rows: list[tuple[str, str, str, str]] = []  # (slug, id, title, url)
    if args.tag == "todo":
        seen: set[str] = set()
        for spec in all_specs():
            seen.add(spec.slug)
            q = Queue.for_source_type(spec.slug, cfg.vault_root)
            for item in q.peek(10_000):
                if item.get("claimed"):
                    continue
                queue_rows.append((
                    spec.slug,
                    str(item.get("id", "")),
                    str(item.get("title") or item.get("url") or "(no title)"),
                    str(item.get("url", "")),
                ))
        queues_root = cfg.vault_root / ".weave" / "queues"
        if queues_root.exists():
            for child in sorted(queues_root.glob("*.jsonl")):
                if child.stem in seen:
                    continue
                q = Queue.for_source_type(child.stem, cfg.vault_root)
                for item in q.peek(10_000):
                    if item.get("claimed"):
                        continue
                    queue_rows.append((
                        child.stem,
                        str(item.get("id", "")),
                        str(item.get("title") or item.get("url") or "(no title)"),
                        str(item.get("url", "")),
                    ))

    if not results and not queue_rows:
        print(f"No notes tagged '{args.tag}'.")
        return

    by_project: dict[str, list] = {}
    for r in results:
        proj = r.project or "(unscoped)"
        by_project.setdefault(proj, []).append(r)

    for proj, notes in sorted(by_project.items()):
        print(f"\n{proj}:")
        for r in notes:
            tag_str = f" [{', '.join(t for t in r.tags if t != args.tag)}]" if len(r.tags) > 1 else ""
            auto_marker = " [auto]" if "auto" in (r.tags or []) else ""
            print(f"  [{r.type}] {r.title} ({r.id}) {r.date}{tag_str}{auto_marker}")

    if queue_rows:
        print("\n[queued]:")
        for slug, qid, title, url in queue_rows:
            url_part = f"  {url}" if url else ""
            print(f"  [{slug}] {title} ({qid}){url_part}")
