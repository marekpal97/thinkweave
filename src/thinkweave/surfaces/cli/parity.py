"""C24 — CLI parity for MCP-only tools.

Four subcommands wrapping ``weave_unlink``, ``weave_timeline``,
``weave_project_snapshot``, and ``weave_prompts``. Used by headless flows
and shell pipelines where the MCP surface isn't reachable.

Each handler calls the underlying operations/retrieval function
directly (not the MCP-side wrapper) so the optional ``mcp`` dep isn't
required for CLI parity.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

from thinkweave.core.config import load_config


def cmd_unlink(args: argparse.Namespace) -> None:
    from thinkweave.operations.notes import unlink_notes

    cfg = load_config()
    try:
        ok = unlink_notes(cfg, args.source, args.target, args.type)
    except FileNotFoundError as e:
        # unlink_notes raises when the source note is missing from the index.
        # Surface the message and exit 1 so the shell sees a failure.
        print(str(e))
        sys.exit(1)
    if ok:
        print(f"Unlinked {args.source} --{args.type}--> {args.target}")
    else:
        print(
            f"Edge not found: {args.source} --{args.type}--> {args.target}"
        )
        sys.exit(1)


def cmd_timeline(args: argparse.Namespace) -> None:
    from thinkweave.core.schemas import NoteType
    from thinkweave.core.vault import VaultManager
    from thinkweave.retrieval.search import Search

    cfg = load_config()
    project = args.project or ""
    days = max(1, int(args.days))

    s = Search(config=cfg)
    if not project:
        ranking = s.get_cross_project_activity(days=days)
        s.close()
        if args.json:
            print(json.dumps(ranking, indent=2))
            return
        if not ranking:
            print(f"No session or decision activity in the last {days} days.")
            return
        print(
            f"Cross-project activity (last {days} days, "
            f"{len(ranking)} projects)\n"
        )
        for entry in ranking:
            latest = (entry.get("latest_date") or "")[:10] or "?"
            print(
                f"- {entry['project']} — {entry['sessions']} sessions, "
                f"{entry['decisions']} decisions (latest: {latest})"
            )
        return

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    vm = VaultManager(config=cfg)
    sessions = [
        n for n in vm.list_notes(note_type=NoteType.SESSION, limit=100)
        if n.project == project and n.date >= cutoff
    ]
    sessions.sort(key=lambda n: n.date)
    s.close()

    if args.json:
        payload = [
            {
                "id": n.id,
                "title": n.title,
                "date": n.date,
                "files_touched": n.frontmatter.get("files_touched", []),
                "commits": n.frontmatter.get("commits", []),
                "processed": n.frontmatter.get("processed", False),
            }
            for n in sessions
        ]
        print(json.dumps(payload, indent=2))
        return

    if not sessions:
        print(
            f"No sessions found for project '{project}' in the last {days} days."
        )
        return
    print(
        f"Timeline: {project} (last {days} days, {len(sessions)} sessions)\n"
    )
    for sess in sessions:
        fm = sess.frontmatter
        print(f"## {sess.date} — {sess.title} ({sess.id})")
        if fm.get("commits"):
            for c in fm["commits"]:
                print(f"Commit: {c.get('hash', '?')} \"{c.get('message', '')}\"")
        print()


def cmd_project_snapshot(args: argparse.Namespace) -> None:
    from thinkweave.retrieval.context import build_project_context

    cfg = load_config()
    sections = (
        [s.strip() for s in args.sections.split(",") if s.strip()]
        if args.sections else None
    )
    kwargs = {}
    if sections:
        kwargs["sections"] = sections
    if args.budget_tokens:
        kwargs["budget_tokens"] = args.budget_tokens
    text = build_project_context(cfg, project=args.project, **kwargs)
    print(text)


def cmd_prompts(args: argparse.Namespace) -> None:
    from thinkweave.operations.search import query_prompts

    cfg = load_config()
    project = args.project or cfg.default_project
    if not project:
        print("error: --project required (or set default_project in config.toml)")
        sys.exit(1)
    rows = query_prompts(
        cfg,
        project=project,
        since=args.since or None,
        limit=int(args.limit),
        classified_as=args.classified_as or None,
    )
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print(f"No prompts for project '{project}'.")
        return
    for r in rows:
        ts = (r.get("ts") or "")[:19] or "?"
        cls = r.get("classification")
        cls_tag = f" [{cls}]" if cls else ""
        text = (r.get("text") or "").replace("\n", " ")[:120]
        print(f"{ts}{cls_tag}  {text}")
