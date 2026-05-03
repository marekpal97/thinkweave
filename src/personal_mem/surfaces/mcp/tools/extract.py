"""``mem_extract`` / ``mem_judge`` / ``mem_landing`` / ``mem_enrich``.

Synthesis-layer tools: pull insights/decisions out of a session, judge
decisions against structural evidence, regenerate landing docs, and run
LLM-driven concept enrichment over notes missing concepts.
"""

from __future__ import annotations

import re
from pathlib import Path

from personal_mem.core.config import Config


def _parse_candidate_insights(body: str) -> list[dict]:
    """Parse ## Candidate Insights section into structured insights.

    Each blank-line-separated block becomes an insight.
    First non-empty line of a block is the title; rest is the body.
    """
    insights: list[dict] = []
    in_section = False
    current_block: list[str] = []

    for line in body.split("\n"):
        if line.strip().startswith("## Candidate Insights"):
            in_section = True
            continue
        if in_section and line.strip().startswith("## "):
            break
        if in_section:
            stripped = line.strip()
            if not stripped and current_block:
                _flush_insight(current_block, insights)
                current_block = []
            elif stripped:
                current_block.append(line)

    if current_block:
        _flush_insight(current_block, insights)

    return insights


def _flush_insight(lines: list[str], insights: list[dict]) -> None:
    """Convert a block of lines into an insight dict."""
    if not lines:
        return
    title_line = lines[0].strip().lstrip("-#*").strip()
    title_line = re.sub(r"^★\s*Insight[─ ]*", "", title_line).strip()
    if not title_line or all(c in "─-=" for c in title_line):
        for line in lines[1:]:
            candidate = line.strip().lstrip("-#*").strip()
            candidate = re.sub(r"^★\s*Insight[─ ]*", "", candidate).strip()
            if candidate and not all(c in "─-=" for c in candidate):
                title_line = candidate
                break
    if not title_line:
        return
    body = "\n".join(lines[1:]).strip() if len(lines) > 1 else title_line
    body = re.sub(r"\n[─-]{3,}\s*$", "", body).strip()
    insights.append({"title": title_line, "body": body or title_line})


def _append_to_section(path: Path, section_header: str, content: str) -> None:
    """Append content under a specific section in a markdown file."""
    text = path.read_text(encoding="utf-8")
    if section_header in text:
        idx = text.index(section_header) + len(section_header)
        nl = text.index("\n", idx)
        text = text[: nl + 1] + content + "\n" + text[nl + 1 :]
    else:
        text = text.rstrip() + f"\n\n{section_header}\n{content}\n"
    path.write_text(text, encoding="utf-8")


def _strip_section(body: str, heading: str) -> str:
    """Remove a markdown section — delegates to vault.strip_section."""
    from personal_mem.core.vault import strip_section

    return strip_section(body, heading)


def _build_decision_body(rationale: str, title: str, outcome: str) -> str:
    """Wrap a rationale in ``## Context`` / ``## Decision`` / ``## Consequences``."""
    rationale = (rationale or "").lstrip()
    rationale = re.sub(
        r"^##\s+Context\s*\n+", "", rationale, count=1, flags=re.IGNORECASE
    )
    has_decision_hdr = bool(
        re.search(r"^##\s+Decision\b", rationale, flags=re.MULTILINE | re.IGNORECASE)
    )
    has_consequences_hdr = bool(
        re.search(
            r"^##\s+Consequences\b", rationale, flags=re.MULTILINE | re.IGNORECASE
        )
    )
    if has_decision_hdr:
        body = f"## Context\n\n{rationale}"
    else:
        body = f"## Context\n\n{rationale}\n\n## Decision\n\n{title}"
    if outcome == "abandoned" and not has_consequences_hdr:
        body += "\n\n## Consequences\n\nApproach was abandoned."
    return body


def tool_schemas() -> list:
    from personal_mem.surfaces.mcp.tools._extract_schemas import (
        tool_schemas as _schemas,
    )

    return _schemas()


def handle_extract(cfg: Config, args: dict):
    from datetime import date

    from mcp.types import TextContent

    from personal_mem.core.indexer import Indexer
    from personal_mem.core.schemas import NoteType
    from personal_mem.core.vault import VaultManager, parse_frontmatter, render_frontmatter
    from personal_mem.retrieval.search import Search

    session_id = args["session_id"]
    force = args.get("force", False)

    s = Search(config=cfg)
    session_row = s.get_note_by_id(session_id)
    s.close()

    if session_row and session_row["type"] != "session":
        return [TextContent(
            type="text",
            text=f"Note {session_id} is type '{session_row['type']}', not 'session'.",
        )]

    vm = VaultManager(config=cfg)

    if not session_row:
        project = args.get("project", "") or cfg.default_project
        summary = args.get("summary", "")
        title = summary[:60] if summary else "conversation"
        session_path = vm.create_note(
            note_type=NoteType.SESSION,
            title=title,
            body="## Summary\n\n## Events\n",
            project=project,
            extra_frontmatter={"source_session": session_id},
        )
        idx = Indexer(config=cfg)
        idx.index_file(session_path)
        idx.close()
    else:
        session_path = vm.root / session_row["path"]
        if not session_path.exists():
            return [TextContent(type="text", text=f"Session file not found: {session_row['path']}")]

    session_note = vm.read_note(session_path)

    if session_note.frontmatter.get("processed") and not force:
        processed_at = session_note.frontmatter.get("processed_at", "unknown date")
        return [TextContent(
            type="text",
            text=f"Session {session_id} already processed on {processed_at}. Use force=true to re-extract.",
        )]

    project = session_note.project

    plan_path = args.get("plan_path", "")
    plan_summary = args.get("plan_summary", "")
    if plan_path or plan_summary:
        plan_ctx = {}
        if plan_path:
            plan_ctx["path"] = plan_path
        if plan_summary:
            plan_ctx["summary"] = plan_summary
        vm.update_note(
            session_path,
            frontmatter_updates={"context": {"plan": plan_ctx}},
        )

    session_dir = session_path.parent
    idx = Indexer(config=cfg)
    for md_file in session_dir.glob("*.md"):
        if md_file.name == "session.md":
            continue
        try:
            fm, _ = parse_frontmatter(md_file.read_text(encoding="utf-8"))
            derived = fm.get("derived_from", [])
            if isinstance(derived, str):
                derived = [derived]
            if session_id in derived or session_note.id in derived:
                rel = str(md_file.relative_to(vm.root))
                idx._remove_by_path(rel)
                md_file.unlink()
        except Exception:
            continue

    insights = args.get("insights")
    if not insights:
        insights = _parse_candidate_insights(session_note.body)

    insights = insights[:3]

    created = []
    created_decisions = []
    for insight in insights:
        title = insight["title"]
        body = insight["body"]
        tags = insight.get("tags", [])
        concepts = insight.get("concepts", [])

        extra_fm: dict = {"derived_from": [session_id]}
        if concepts:
            extra_fm["concepts"] = concepts

        path = vm.create_note(
            note_type=NoteType.NOTE,
            title=title,
            body=body,
            project=project,
            tags=tags,
            extra_frontmatter=extra_fm,
            output_dir=session_path.parent,
        )
        idx.index_file(path)
        note = vm.read_note(path)
        created.append(note)

    decisions = args.get("decisions", [])
    for dec in decisions:
        outcome = dec.get("outcome", "committed")
        status = {
            "committed": "accepted",
            "abandoned": "proposed",
            "partial": "proposed",
        }.get(outcome, "proposed")

        extra_fm: dict = {
            "status": status,
            "committed": outcome == "committed",
            "source_session": session_id,
            "derived_from": [session_id],
        }
        if dec.get("file_paths"):
            extra_fm["file_paths"] = dec["file_paths"]
        if dec.get("concepts"):
            extra_fm["concepts"] = dec["concepts"]
        if dec.get("supersedes"):
            extra_fm["supersedes"] = dec["supersedes"]
        if dec.get("cites"):
            extra_fm["cites"] = dec["cites"]
        if dec.get("plan_ref"):
            extra_fm["plan_ref"] = dec["plan_ref"]
        if dec.get("summary"):
            extra_fm["summary"] = dec["summary"]

        dec_body = _build_decision_body(
            dec.get("rationale", ""), dec["title"], outcome
        )

        path = vm.create_note(
            note_type=NoteType.DECISION,
            title=dec["title"],
            body=dec_body,
            project=project,
            tags=dec.get("tags", []),
            extra_frontmatter=extra_fm,
            output_dir=session_path.parent,
        )
        idx.index_file(path)
        dec_note = vm.read_note(path)
        created_decisions.append(dec_note)

        for target_id in dec.get("supersedes", []) or []:
            try:
                row = idx.db.execute(
                    "SELECT path FROM notes WHERE id = ?", (target_id,)
                ).fetchone()
                if row is None:
                    continue
                target_path = vm.root / row["path"]
                if not target_path.exists():
                    continue
                vm.update_note(
                    target_path,
                    frontmatter_updates={"status": "superseded"},
                )
                idx.index_file(target_path)
            except Exception:
                continue

    session_commits = session_note.frontmatter.get("commits", [])
    if created_decisions and session_commits:
        for dec_note in created_decisions:
            dec_files = dec_note.frontmatter.get("file_paths", [])
            if not dec_files:
                continue
            dec_basenames = {Path(fp).name for fp in dec_files}
            matched_hashes = []
            for commit in session_commits:
                commit_files = commit.get("files", [])
                commit_hash = commit.get("hash", "")
                if not commit_hash or not commit_files:
                    continue
                commit_basenames = {Path(f).name for f in commit_files}
                if dec_basenames & commit_basenames:
                    matched_hashes.append(commit_hash)
            if matched_hashes:
                vm.update_note(
                    vm.root / dec_note.path,
                    frontmatter_updates={"commit_refs": matched_hashes},
                )

    suggestions = []
    try:
        if created_decisions:
            from personal_mem.synthesis.concepts import get_all_concepts, suggest_similar

            all_concepts = get_all_concepts(idx.db)
            existing_list = list(all_concepts.keys())

            s = Search(config=cfg)
            for dec_note in created_decisions:
                dec_concepts = dec_note.frontmatter.get("concepts", [])
                if dec_concepts:
                    for concept in dec_concepts:
                        similar = suggest_similar(concept, existing_list)
                        for sim in similar:
                            if sim != concept.lower():
                                suggestions.append(
                                    f"  ⚠ Concept '{concept}' is similar to existing "
                                    f"'{sim}' ({all_concepts.get(sim, 0)} notes). "
                                    f"Consider mem_concepts_merge if they mean the same thing."
                                )
                    for concept in dec_concepts[:3]:
                        results = s.search(query=concept, note_type="source", limit=3)
                        for r in results:
                            suggestions.append(
                                f"  Tip: {dec_note.id} shares concept '{concept}' with "
                                f"source {r.title} ({r.id}). Consider mem_link with cites."
                            )
            s.close()
    except Exception:
        pass

    created_todos = []
    try:
        from personal_mem.sources import load_user_config

        cfg_merged = load_user_config(cfg.vault_root)
        if cfg_merged.get("auto_todo_extraction", True):
            from personal_mem.extract import extract_todos

            scan_chunks = [session_note.body]
            for ins in insights:
                scan_chunks.append(ins.get("body", ""))
            for dec in decisions:
                scan_chunks.append(dec.get("rationale", ""))
            combined = "\n".join(c for c in scan_chunks if c)
            todos = extract_todos(combined, source_session_id=session_id)

            seen_titles = set()
            for todo in todos:
                title = todo.text.strip()
                if not title or title.lower() in seen_titles:
                    continue
                seen_titles.add(title.lower())
                extra_fm = {
                    "derived_from": [session_id],
                    "auto_extracted": True,
                }
                path = vm.create_note(
                    note_type=NoteType.NOTE,
                    title=title[:80],
                    body=f"Auto-extracted TODO: {title}",
                    project=project,
                    tags=["todo", "auto"],
                    extra_frontmatter=extra_fm,
                    output_dir=session_path.parent,
                )
                idx.index_file(path)
                created_todos.append(vm.read_note(path))
    except Exception:
        pass

    summary_text = args.get("summary", "")
    all_created = created + created_decisions + created_todos
    if not summary_text and all_created:
        note_titles = ", ".join(n.title for n in created) if created else ""
        dec_titles = ", ".join(n.title for n in created_decisions) if created_decisions else ""
        parts = []
        if note_titles:
            parts.append(f"{len(created)} notes: {note_titles}")
        if dec_titles:
            parts.append(f"{len(created_decisions)} decisions: {dec_titles}")
        summary_text = f"Extracted {'; '.join(parts)}."

    if summary_text:
        if force and "## Summary" in session_note.body:
            cur_text = session_path.read_text(encoding="utf-8")
            cur_fm, cur_body = parse_frontmatter(cur_text)
            cur_body = _strip_section(cur_body, "## Summary")
            cur_body = cur_body.rstrip() + f"\n\n## Summary\n{summary_text}\n"
            session_path.write_text(
                render_frontmatter(cur_fm) + "\n\n" + cur_body, encoding="utf-8"
            )
        elif "## Summary" in session_note.body:
            _append_to_section(session_path, "## Summary", summary_text)
        else:
            vm.update_note(session_path, body_append=f"## Summary\n{summary_text}")

    today = date.today().isoformat()
    fm_updates: dict = {"processed": True, "processed_at": today}
    if session_note.frontmatter.get("auto_extracted"):
        fm_updates["auto_extracted"] = False
    vm.update_note(
        session_path,
        frontmatter_updates=fm_updates,
    )

    session_text = session_path.read_text(encoding="utf-8")
    fm_part, body_part = parse_frontmatter(session_text)
    cleaned_body = _strip_section(body_part, "## Events")
    cleaned_body = _strip_section(cleaned_body, "## Candidate Insights")
    if cleaned_body != body_part:
        content = render_frontmatter(fm_part) + "\n\n" + cleaned_body
        session_path.write_text(content, encoding="utf-8")

    idx.index_file(session_path)
    idx.close()

    report_lines = [f"Extracted from session {session_id}:"]
    if summary_text:
        report_lines.append(f"Summary: {summary_text}")
    for note in created:
        report_lines.append(
            f"  Created [{note.type.value}] {note.title} ({note.id}) derived_from={session_id}"
        )
    for dec_note in created_decisions:
        outcome_str = dec_note.frontmatter.get("committed", False)
        report_lines.append(
            f"  Created [{dec_note.type.value}] {dec_note.title} ({dec_note.id}) "
            f"status={dec_note.frontmatter.get('status')}, committed={outcome_str}"
        )
    for todo_note in created_todos:
        report_lines.append(
            f"  Auto-todo [{todo_note.type.value}] {todo_note.title} "
            f"({todo_note.id}) tags=[todo, auto]"
        )
    if not all_created:
        report_lines.append("  No insights or decisions extracted.")
    for s in suggestions[:5]:
        report_lines.append(s)

    try:
        from personal_mem.surfaces.hooks.handler import archive_buffer
        source_session = session_note.frontmatter.get("source_session", session_id)
        archive_buffer(cfg.mem_dir, source_session, session_path.parent)
    except Exception:
        pass

    report_lines.append(f"Session marked processed={today}")
    return [TextContent(type="text", text="\n".join(report_lines))]


def handle_judge(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.core.indexer import Indexer
    from personal_mem.core.schemas import NoteMeta  # noqa: F401
    from personal_mem.core.vault import VaultManager
    from personal_mem.retrieval.search import Search
    from personal_mem.synthesis.judge import evaluate_decision, find_decisions

    vm = VaultManager(config=cfg)
    s = Search(config=cfg)
    idx = Indexer(config=cfg)

    target_decisions: list = []

    if args.get("decision_id"):
        row = s.get_note_by_id(args["decision_id"])
        if row and row["type"] == "decision":
            note = vm.read_note(vm.root / row["path"])
            target_decisions.append(note)
    elif args.get("session_id"):
        target_decisions = find_decisions(
            idx.db, vm, session_id=args["session_id"]
        )
    elif args.get("project"):
        target_decisions = find_decisions(
            idx.db, vm, project=args["project"]
        )

    if not target_decisions:
        idx.close()
        s.close()
        return [TextContent(type="text", text="No decisions found to evaluate.")]

    all_decisions = find_decisions(idx.db, vm)
    idx.close()

    results = []
    for dec in target_decisions:
        session_id = dec.frontmatter.get("source_session", "")
        session_meta = None
        if session_id:
            session_row = s.get_note_by_id(session_id)
            if session_row:
                session_meta = vm.read_note(vm.root / session_row["path"])

        result = evaluate_decision(dec, all_decisions, session_meta)

        fm_updates: dict = {
            "verdict": result["verdict"],
            "confidence": result["confidence"],
            "judged_at": result["judged_at"],
        }
        if result["blame_lines"] >= 0:
            fm_updates["blame_lines"] = result["blame_lines"]
        if result.get("commit_refs"):
            fm_updates["commit_refs"] = result["commit_refs"]
            if not dec.frontmatter.get("committed"):
                fm_updates["committed"] = True
        vm.update_note(
            vm.root / dec.path,
            frontmatter_updates=fm_updates,
        )
        idx = Indexer(config=cfg)
        idx.index_file(vm.root / dec.path)
        idx.close()

        status_map = {
            "kept": "accepted",
            "superseded": "superseded",
            "reverted": "deprecated",
        }
        new_status = status_map.get(result["verdict"])
        if new_status and new_status != dec.frontmatter.get("status"):
            vm.update_note(
                vm.root / dec.path,
                frontmatter_updates={"status": new_status},
            )

        results.append(
            f"  {dec.id} ({dec.title}): {result['verdict']} "
            f"(confidence={result['confidence']}) — {result['evidence']}"
        )

    s.close()
    lines = [f"Evaluated {len(results)} decisions:"] + results
    return [TextContent(type="text", text="\n".join(lines))]


def handle_landing(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.synthesis.landing import (
        state_of_play_context,
        write_landing_docs,
    )

    project = args.get("project", "")
    doc = args.get("doc", "all")
    state_context = args.get("state_context", False)

    if state_context:
        if not project:
            return [TextContent(
                type="text",
                text="state_context=true requires a project argument.",
            )]
        context_text = state_of_play_context(cfg, project)
        return [TextContent(type="text", text=context_text)]

    if doc != "themes" and not project:
        return [TextContent(
            type="text",
            text=(
                "Project argument required for doc="
                f"{doc!r} (only doc='themes' is global)."
            ),
        )]

    written = write_landing_docs(cfg, project, docs=doc)
    scope = "global vault" if doc == "themes" else project
    lines = [f"Generated landing documents for {scope}:"]
    for filename, path in written.items():
        lines.append(f"  {filename} → {path.relative_to(cfg.vault_root)}")
    return [TextContent(type="text", text="\n".join(lines))]


def handle_enrich(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.core.indexer import Indexer
    from personal_mem.enrich import enrich

    note_types = args.get("note_types") or ["session", "note", "decision", "source"]
    stats = enrich(
        cfg,
        project=args.get("project", ""),
        note_types=note_types,
        limit=args.get("limit", 0),
        force=args.get("force", False),
        dry_run=args.get("dry_run", False),
    )

    dry = args.get("dry_run", False)
    lines = [
        f"{'[dry run] ' if dry else ''}Concept enrichment complete:",
        f"  enriched: {stats['enriched']}",
        f"  skipped: {stats['skipped']}",
        f"  errors: {stats['errors']}",
        f"  concepts assigned: {stats['new_concepts']}",
    ]

    if not dry and stats["enriched"] > 0:
        idx = Indexer(config=cfg)
        istats = idx.rebuild(full=True)
        cstats = idx.materialize_links(max_links=5)
        idx.rebuild(full=False)
        idx.close()
        lines += [
            f"\nReindexed: {istats['edges']} edges",
            f"Materialized: {cstats['links_written']} wikilinks into {cstats['notes_updated']} notes",
        ]

    return [TextContent(type="text", text="\n".join(lines))]
