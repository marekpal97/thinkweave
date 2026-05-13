"""Session-extraction operation — the heavy lift behind ``mem_extract``.

Walks a session note, materializes insights as derived notes, materializes
decisions (with auto-flip of superseded decisions), runs the auto-todo pass,
emits dedup suggestions, and finally archives the JSONL event buffer into
the session folder. Pure business logic — returns a structured result; the
MCP / CLI surfaces format the human-readable report.

Imports `core/`, `retrieval/`, `synthesis/`, `sources/`, and the root-level
``personal_mem.extract`` module — never `surfaces/`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from personal_mem.core.buffer import archive_buffer
from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteMeta, NoteType
from personal_mem.core.vault import (
    VaultManager,
    parse_frontmatter,
    render_frontmatter,
    strip_section,
)
from personal_mem.retrieval.search import Search
from personal_mem.synthesis.concepts import (
    build_keep_set,
    load_ontology,
    split_concepts_by_ontology,
)


@dataclass
class ExtractOutcome:
    """Structured result from :func:`extract_session`."""

    session_id: str
    summary: str = ""
    created_notes: list[NoteMeta] = field(default_factory=list)
    created_decisions: list[NoteMeta] = field(default_factory=list)
    created_todos: list[NoteMeta] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    processed_at: str = ""
    error: str = ""
    skipped_reason: str = ""

    @property
    def all_created(self) -> list[NoteMeta]:
        return self.created_notes + self.created_decisions + self.created_todos


# ---------------------------------------------------------------------------
# Insight parsing helpers (kept here, not in core/, because they speak the
# session note's "## Candidate Insights" markdown convention)
# ---------------------------------------------------------------------------


def parse_candidate_insights(body: str) -> list[dict]:
    """Parse ``## Candidate Insights`` blocks into ``{title, body}`` dicts."""
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
    text = path.read_text(encoding="utf-8")
    if section_header in text:
        idx = text.index(section_header) + len(section_header)
        nl = text.index("\n", idx)
        text = text[: nl + 1] + content + "\n" + text[nl + 1 :]
    else:
        text = text.rstrip() + f"\n\n{section_header}\n{content}\n"
    path.write_text(text, encoding="utf-8")


def _build_decision_body(rationale: str, title: str, outcome: str) -> str:
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


# ---------------------------------------------------------------------------
# Main operation
# ---------------------------------------------------------------------------


def extract_session(
    cfg: Config,
    *,
    session_id: str,
    force: bool = False,
    project: str = "",
    summary: str = "",
    insights: list[dict] | None = None,
    decisions: list[dict] | None = None,
    plan_path: str = "",
    plan_summary: str = "",
) -> ExtractOutcome:
    """Extract insights, decisions, and todos from a session note.

    On success returns an ``ExtractOutcome`` whose lists describe what was
    created. ``error`` / ``skipped_reason`` are set in failure / no-op cases
    so the surface can format the right message.
    """
    outcome = ExtractOutcome(session_id=session_id)

    s = Search(config=cfg)
    session_row = s.get_note_by_id(session_id)
    s.close()

    if session_row and session_row["type"] != "session":
        outcome.error = (
            f"Note {session_id} is type '{session_row['type']}', not 'session'."
        )
        return outcome

    vm = VaultManager(config=cfg)

    if not session_row:
        proj = project or cfg.default_project
        title = (summary or "")[:60] or "conversation"
        session_path = vm.create_note(
            note_type=NoteType.SESSION,
            title=title,
            body="## Summary\n\n## Events\n",
            project=proj,
            extra_frontmatter={"source_session": session_id},
        )
        idx = Indexer(config=cfg)
        idx.index_file(session_path)
        idx.close()
    else:
        session_path = vm.root / session_row["path"]
        if not session_path.exists():
            outcome.error = f"Session file not found: {session_row['path']}"
            return outcome

    session_note = vm.read_note(session_path)

    if session_note.frontmatter.get("processed") and not force:
        processed_at = session_note.frontmatter.get("processed_at", "unknown date")
        outcome.skipped_reason = (
            f"Session {session_id} already processed on {processed_at}. "
            "Use force=true to re-extract."
        )
        return outcome

    proj = session_note.project

    if plan_path or plan_summary:
        plan_ctx: dict = {}
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

    insights_in = insights if insights else parse_candidate_insights(session_note.body)
    insights_in = insights_in[:3]
    decisions_in = decisions or []

    # Strict creation policy: terms not in the merged ontology (seed +
    # vault override) cannot reach canonical `concepts:` from extraction.
    # They flow into `proposed_concepts:` and surface in
    # /mem-resolve-concepts at count >= DRIFT_COUNT_THRESHOLD for review.
    ontology_keep = build_keep_set(load_ontology())

    for insight in insights_in:
        title = insight["title"]
        body = insight["body"]
        tags = insight.get("tags", [])
        canonical, proposed = split_concepts_by_ontology(
            insight.get("concepts", []),
            proposed=insight.get("proposed_concepts", []),
            ontology_keep=ontology_keep,
        )
        extra_fm: dict = {"derived_from": [session_id]}
        if canonical:
            extra_fm["concepts"] = canonical
        if proposed:
            extra_fm["proposed_concepts"] = proposed
        path = vm.create_note(
            note_type=NoteType.NOTE,
            title=title,
            body=body,
            project=proj,
            tags=tags,
            extra_frontmatter=extra_fm,
            output_dir=session_path.parent,
        )
        idx.index_file(path)
        outcome.created_notes.append(vm.read_note(path))

    for dec in decisions_in:
        outcome_value = dec.get("outcome", "committed")
        status = {
            "committed": "accepted",
            "abandoned": "proposed",
            "partial": "proposed",
        }.get(outcome_value, "proposed")
        extra_fm = {
            "status": status,
            "committed": outcome_value == "committed",
            "source_session": session_id,
            "derived_from": [session_id],
        }
        if dec.get("file_paths"):
            extra_fm["file_paths"] = dec["file_paths"]
        dec_canonical, dec_proposed = split_concepts_by_ontology(
            dec.get("concepts", []),
            proposed=dec.get("proposed_concepts", []),
            ontology_keep=ontology_keep,
        )
        if dec_canonical:
            extra_fm["concepts"] = dec_canonical
        if dec_proposed:
            extra_fm["proposed_concepts"] = dec_proposed
        if dec.get("supersedes"):
            extra_fm["supersedes"] = dec["supersedes"]
        if dec.get("cites"):
            extra_fm["cites"] = dec["cites"]
        if dec.get("plan_ref"):
            extra_fm["plan_ref"] = dec["plan_ref"]
        if dec.get("summary"):
            extra_fm["summary"] = dec["summary"]
        if dec.get("predicted_outcome"):
            # Optional forward-looking text the wrap composer (or caller)
            # writes when recording the decision. Judged later by
            # synthesis/judge.py:_evaluate_prediction_match — feeds the
            # `prediction.match` column in the RLVR export.
            extra_fm["predicted_outcome"] = dec["predicted_outcome"]
        dec_body = _build_decision_body(
            dec.get("rationale", ""), dec["title"], outcome_value
        )
        path = vm.create_note(
            note_type=NoteType.DECISION,
            title=dec["title"],
            body=dec_body,
            project=proj,
            tags=dec.get("tags", []),
            extra_frontmatter=extra_fm,
            output_dir=session_path.parent,
        )
        idx.index_file(path)
        outcome.created_decisions.append(vm.read_note(path))

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
    if outcome.created_decisions and session_commits:
        for dec_note in outcome.created_decisions:
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

    try:
        if outcome.created_decisions:
            from personal_mem.synthesis.concepts import (
                get_all_concepts,
                suggest_similar,
            )

            all_concepts = get_all_concepts(idx.db)
            existing_list = list(all_concepts.keys())
            s2 = Search(config=cfg)
            for dec_note in outcome.created_decisions:
                dec_concepts = dec_note.frontmatter.get("concepts", [])
                if dec_concepts:
                    for concept in dec_concepts:
                        similar = suggest_similar(concept, existing_list)
                        for sim in similar:
                            if sim != concept.lower():
                                outcome.suggestions.append(
                                    f"  ⚠ Concept '{concept}' is similar to existing "
                                    f"'{sim}' ({all_concepts.get(sim, 0)} notes). "
                                    f"Consider mem_concepts_merge if they mean the same thing."
                                )
                    for concept in dec_concepts[:3]:
                        results = s2.search(query=concept, note_type="source", limit=3)
                        for r in results:
                            outcome.suggestions.append(
                                f"  Tip: {dec_note.id} shares concept '{concept}' with "
                                f"source {r.title} ({r.id}). Consider mem_link with cites."
                            )
            s2.close()
    except Exception:
        pass

    try:
        from personal_mem.sources import load_user_config

        cfg_merged = load_user_config(cfg.vault_root)
        if cfg_merged.get("auto_todo_extraction", True):
            from personal_mem.extract import extract_todos

            scan_chunks = [session_note.body]
            for ins in insights_in:
                scan_chunks.append(ins.get("body", ""))
            for dec in decisions_in:
                scan_chunks.append(dec.get("rationale", ""))
            combined = "\n".join(c for c in scan_chunks if c)
            todos = extract_todos(combined, source_session_id=session_id)

            seen_titles: set[str] = set()
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
                    project=proj,
                    tags=["todo", "auto"],
                    extra_frontmatter=extra_fm,
                    output_dir=session_path.parent,
                )
                idx.index_file(path)
                outcome.created_todos.append(vm.read_note(path))
    except Exception:
        pass

    summary_text = summary
    if not summary_text and outcome.all_created:
        note_titles = ", ".join(n.title for n in outcome.created_notes)
        dec_titles = ", ".join(n.title for n in outcome.created_decisions)
        parts = []
        if note_titles:
            parts.append(f"{len(outcome.created_notes)} notes: {note_titles}")
        if dec_titles:
            parts.append(f"{len(outcome.created_decisions)} decisions: {dec_titles}")
        summary_text = f"Extracted {'; '.join(parts)}."
    outcome.summary = summary_text

    if summary_text:
        if force and "## Summary" in session_note.body:
            cur_text = session_path.read_text(encoding="utf-8")
            cur_fm, cur_body = parse_frontmatter(cur_text)
            cur_body = strip_section(cur_body, "## Summary")
            cur_body = cur_body.rstrip() + f"\n\n## Summary\n{summary_text}\n"
            session_path.write_text(
                render_frontmatter(cur_fm) + "\n\n" + cur_body, encoding="utf-8"
            )
        elif "## Summary" in session_note.body:
            _append_to_section(session_path, "## Summary", summary_text)
        else:
            vm.update_note(session_path, body_append=f"## Summary\n{summary_text}")

    today = date.today().isoformat()
    outcome.processed_at = today
    fm_updates: dict = {"processed": True, "processed_at": today}
    if session_note.frontmatter.get("auto_extracted"):
        fm_updates["auto_extracted"] = False
    vm.update_note(
        session_path,
        frontmatter_updates=fm_updates,
    )

    session_text = session_path.read_text(encoding="utf-8")
    fm_part, body_part = parse_frontmatter(session_text)
    cleaned_body = strip_section(body_part, "## Events")
    cleaned_body = strip_section(cleaned_body, "## Candidate Insights")
    if cleaned_body != body_part:
        content = render_frontmatter(fm_part) + "\n\n" + cleaned_body
        session_path.write_text(content, encoding="utf-8")

    idx.index_file(session_path)
    idx.close()

    try:
        source_session = session_note.frontmatter.get("source_session", session_id)
        archive_buffer(cfg.mem_dir, source_session, session_path.parent)
    except Exception:
        pass

    return outcome
