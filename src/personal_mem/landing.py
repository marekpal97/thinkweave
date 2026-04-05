"""Generate project landing documents — DECISIONS.md, BACKLOG.md, STATE.md.

These are materialized views over existing vault notes, excluded from the
index. DECISIONS and BACKLOG are fully auto-generated from data. STATE
gathers context for LLM-assisted narrative generation.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from personal_mem.config import Config, load_config
from personal_mem.schemas import NoteType

# Landing doc filenames — excluded from indexer
LANDING_FILENAMES = {"DECISIONS.md", "BACKLOG.md", "STATE.md"}


def _get_db(config: Config):
    """Get a read-only connection to the index database."""
    import sqlite3

    db_path = config.index_db
    if not db_path.exists():
        raise FileNotFoundError(
            f"Index not found at {db_path}. Run `mem index` first."
        )
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    return db


def _extract_summary(frontmatter: dict, body: str) -> str:
    """Extract a one-sentence summary from a decision.

    Prefers the `summary` frontmatter field. Falls back to the first
    non-heading, non-empty sentence of the body.
    """
    summary = frontmatter.get("summary", "")
    if summary:
        return str(summary)

    # Fall back: first sentence from body, skipping headings
    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("---"):
            continue
        # Take first sentence (up to period, or entire line)
        dot = stripped.find(". ")
        if dot > 0:
            return stripped[: dot + 1]
        return stripped[:120]
    return ""


def _query_decisions(db, project: str) -> list[dict]:
    """Query all decisions for a project, ordered by date."""
    rows = db.execute(
        "SELECT id, title, date, frontmatter, body_text FROM notes "
        "WHERE type = 'decision' AND project = ? ORDER BY date",
        (project,),
    ).fetchall()

    decisions = []
    for row in rows:
        fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
        decisions.append({
            "id": row["id"],
            "title": row["title"],
            "date": row["date"] or "",
            "status": fm.get("status", "proposed"),
            "verdict": fm.get("verdict", ""),
            "confidence": fm.get("confidence", ""),
            "summary": _extract_summary(fm, row["body_text"] or ""),
            "supersedes": fm.get("supersedes", []),
            "builds_on": fm.get("builds_on", []),
            "frontmatter": fm,
        })
    return decisions


def _query_edges(db, project: str) -> list[dict]:
    """Query supersedes and builds_on edges between decisions in a project."""
    rows = db.execute(
        "SELECT e.source, e.target, e.edge_type "
        "FROM edges e "
        "JOIN notes s ON s.id = e.source AND s.type = 'decision' AND s.project = ? "
        "JOIN notes t ON t.id = e.target AND t.type = 'decision' AND t.project = ? "
        "WHERE e.edge_type IN ('supersedes', 'builds_on')",
        (project, project),
    ).fetchall()
    return [{"source": r["source"], "target": r["target"], "type": r["edge_type"]} for r in rows]


def decisions_ledger(config: Config, project: str) -> str:
    """Generate DECISIONS.md — decision table + Mermaid DAG."""
    db = _get_db(config)
    decisions = _query_decisions(db, project)
    edges = _query_edges(db, project)
    db.close()

    today = date.today().isoformat()
    lines = [
        f"# Decisions — {project}",
        f"*Auto-generated. Last updated: {today}*",
        "",
    ]

    if not decisions:
        lines.append("No decisions recorded yet.")
        return "\n".join(lines) + "\n"

    # Partition into active vs inactive
    active = [d for d in decisions if d["status"] in ("proposed", "accepted")]
    inactive = [d for d in decisions if d["status"] in ("deprecated", "superseded")]

    # Active decisions table
    if active:
        lines.append("## Active")
        lines.append("")
        lines.append("| ID | Date | Title | Status | Verdict | Summary |")
        lines.append("|---|---|---|---|---|---|")
        for d in active:
            verdict_str = ""
            if d["verdict"]:
                conf = f" ({d['confidence']})" if d["confidence"] else ""
                verdict_str = f"{d['verdict']}{conf}"
            summary = d["summary"].replace("|", "\\|")
            lines.append(
                f"| [[{d['id']}]] | {d['date']} | {d['title']} "
                f"| {d['status']} | {verdict_str} | {summary} |"
            )
        lines.append("")

    # Mermaid DAG
    if edges:
        lines.append("## Evolution Graph")
        lines.append("")
        lines.append("```mermaid")
        lines.append("graph TD")

        # Add node labels for all referenced decisions
        id_to_title = {d["id"]: d["title"] for d in decisions}
        rendered_nodes = set()
        for edge in edges:
            for nid in (edge["source"], edge["target"]):
                if nid not in rendered_nodes:
                    safe_title = id_to_title.get(nid, nid).replace('"', "'")
                    lines.append(f'  {nid}["{safe_title}"]')
                    rendered_nodes.add(nid)

        for edge in edges:
            if edge["type"] == "supersedes":
                lines.append(f"  {edge['target']} -.superseded.-> {edge['source']}")
            else:
                lines.append(f"  {edge['target']} --> {edge['source']}")

        lines.append("```")
        lines.append("")

    # Superseded/deprecated table
    if inactive:
        lines.append("## Superseded / Deprecated")
        lines.append("")
        lines.append("| ID | Date | Title | Status | Summary |")
        lines.append("|---|---|---|---|---|")
        for d in inactive:
            summary = d["summary"].replace("|", "\\|")
            lines.append(
                f"| [[{d['id']}]] | {d['date']} | {d['title']} "
                f"| {d['status']} | {summary} |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


def _group_by_concepts(notes: list[dict], db) -> dict[str, list[dict]]:
    """Group notes by shared concept clusters.

    Notes sharing 2+ concepts end up in the same group. The group label
    is the most frequent concept in that cluster.
    """
    if not notes:
        return {}

    # Build concept → notes mapping
    concept_to_notes: dict[str, list[str]] = defaultdict(list)
    note_by_id: dict[str, dict] = {}
    for note in notes:
        note_by_id[note["id"]] = note
        fm = note.get("frontmatter", {})
        concepts = fm.get("concepts", [])
        if isinstance(concepts, str):
            concepts = [c.strip() for c in concepts.split(",") if c.strip()]
        for c in concepts:
            concept_to_notes[c.lower()].append(note["id"])

    # Find clusters via shared concepts
    assigned: dict[str, str] = {}  # note_id → group_label
    for concept, note_ids in sorted(concept_to_notes.items(), key=lambda x: -len(x[1])):
        if len(note_ids) < 2:
            continue
        # Use this concept as group label if any note in it is unassigned
        for nid in note_ids:
            if nid not in assigned:
                assigned[nid] = concept

    # Build groups
    groups: dict[str, list[dict]] = defaultdict(list)
    for note in notes:
        label = assigned.get(note["id"], "Uncategorized")
        groups[label].append(note)

    return dict(groups)


def backlog_summary(config: Config, project: str) -> str:
    """Generate BACKLOG.md — open items, stalled proposals, parked items."""
    db = _get_db(config)
    today = date.today()
    stale_cutoff = (today - timedelta(days=7)).isoformat()

    # Query todo-tagged notes
    todo_notes = []
    for row in db.execute(
        "SELECT id, title, date, tags, frontmatter, body_text FROM notes "
        "WHERE project = ? ORDER BY date DESC",
        (project,),
    ):
        tags = json.loads(row["tags"]) if row["tags"] else []
        if "todo" not in tags:
            continue
        fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
        todo_notes.append({
            "id": row["id"],
            "title": row["title"],
            "date": row["date"] or "",
            "tags": [t for t in tags if t != "todo"],
            "frontmatter": fm,
        })

    # Query stalled proposed decisions (proposed + older than 7 days)
    stalled = []
    for row in db.execute(
        "SELECT id, title, date, frontmatter, body_text FROM notes "
        "WHERE type = 'decision' AND project = ? AND date < ? ORDER BY date",
        (project, stale_cutoff),
    ):
        fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
        if fm.get("status") == "proposed":
            stalled.append({
                "id": row["id"],
                "title": row["title"],
                "date": row["date"] or "",
                "summary": _extract_summary(fm, row["body_text"] or ""),
                "frontmatter": fm,
            })

    # Query parked-tagged notes
    parked = []
    for row in db.execute(
        "SELECT id, title, date, tags, body_text, frontmatter FROM notes "
        "WHERE project = ? ORDER BY date DESC",
        (project,),
    ):
        tags = json.loads(row["tags"]) if row["tags"] else []
        if "parked" not in tags:
            continue
        fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
        # Extract reason from body (first non-heading line)
        reason = _extract_summary(fm, row["body_text"] or "")
        parked.append({
            "id": row["id"],
            "title": row["title"],
            "date": row["date"] or "",
            "reason": reason,
        })

    db.close()

    today_str = today.isoformat()
    lines = [
        f"# Backlog — {project}",
        f"*Auto-generated. Last updated: {today_str}*",
        "",
    ]

    if not todo_notes and not stalled and not parked:
        lines.append("No open items, stalled proposals, or parked items.")
        return "\n".join(lines) + "\n"

    # Open items grouped by concept
    if todo_notes:
        lines.append("## Open")
        lines.append("")

        db2 = _get_db(config)
        groups = _group_by_concepts(todo_notes, db2)
        db2.close()

        if not groups:
            groups = {"Uncategorized": todo_notes}

        for label, items in sorted(groups.items()):
            lines.append(f"### {label.replace('-', ' ').title()}")
            for item in items:
                tag_str = ""
                if item["tags"]:
                    tag_str = f" — {', '.join(item['tags'])}"
                lines.append(f"- [ ] {item['title']} ([[{item['id']}]]){tag_str}")
            lines.append("")

    # Stalled proposals
    if stalled:
        lines.append("## Stalled Proposals")
        lines.append("Decisions proposed but never accepted (>7 days old).")
        lines.append("")
        lines.append("| ID | Date | Title | Summary |")
        lines.append("|---|---|---|---|")
        for s in stalled:
            summary = s["summary"].replace("|", "\\|")
            lines.append(f"| [[{s['id']}]] | {s['date']} | {s['title']} | {summary} |")
        lines.append("")

    # Parked items
    if parked:
        lines.append("## Parked")
        lines.append("Discussed and deliberately deferred or rejected.")
        lines.append("")
        for p in parked:
            reason = f" — {p['reason']}" if p["reason"] else ""
            lines.append(f"- {p['title']} ([[{p['id']}]]){reason}")
        lines.append("")

    return "\n".join(lines) + "\n"


def _shorten_path(filepath: str) -> str:
    """Shorten an absolute file path to a readable relative form.

    Tries to find 'src/' or 'tests/' as an anchor. Falls back to last 3 components.
    """
    parts = filepath.replace("\\", "/").split("/")
    for anchor in ("src", "tests", "commands", "docs"):
        if anchor in parts:
            idx = parts.index(anchor)
            return "/".join(parts[idx:])
    # Fallback: last 3 components
    return "/".join(parts[-3:]) if len(parts) > 3 else filepath


def _gather_state_context(config: Config, project: str) -> dict:
    """Gather all data needed for STATE.md generation.

    Returns a structured dict with decisions, sessions, probes, concepts,
    and file touch frequencies. This is the raw material for LLM narrative
    or data-driven template rendering.
    """
    db = _get_db(config)
    cutoff_14d = (date.today() - timedelta(days=14)).isoformat()

    # All decisions for project
    decisions = _query_decisions(db, project)

    # Recent sessions (last 14 days)
    recent_sessions = []
    for row in db.execute(
        "SELECT id, title, date, frontmatter FROM notes "
        "WHERE type = 'session' AND project = ? AND date >= ? ORDER BY date DESC",
        (project, cutoff_14d),
    ):
        fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
        recent_sessions.append({
            "id": row["id"],
            "title": row["title"],
            "date": row["date"],
            "files_touched": fm.get("files_touched", []),
            "commits": fm.get("commits", []),
            "test_runs": fm.get("test_runs", []),
        })

    # Probe-tagged notes (last 14 days)
    probes = []
    for row in db.execute(
        "SELECT id, title, date, tags, frontmatter FROM notes "
        "WHERE project = ? AND date >= ? ORDER BY date DESC",
        (project, cutoff_14d),
    ):
        tags = json.loads(row["tags"]) if row["tags"] else []
        if "probe" in tags:
            fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
            derived = fm.get("derived_from", [])
            if isinstance(derived, str):
                derived = [derived]
            probes.append({
                "id": row["id"],
                "title": row["title"],
                "date": row["date"],
                "session": derived[0] if derived else "",
            })

    # Concept frequency
    concept_counts: dict[str, int] = defaultdict(int)
    for row in db.execute(
        "SELECT frontmatter FROM notes WHERE project = ?", (project,),
    ):
        fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
        concepts = fm.get("concepts", [])
        if isinstance(concepts, str):
            concepts = [c.strip() for c in concepts.split(",") if c.strip()]
        for c in concepts:
            concept_counts[c.lower()] += 1

    # File touch frequency from sessions
    file_freq: dict[str, int] = defaultdict(int)
    for sess in recent_sessions:
        for f in sess["files_touched"]:
            file_freq[f] += 1

    # Build file → decisions map (which files have architectural decisions)
    file_decisions: dict[str, list[dict]] = defaultdict(list)
    for d in decisions:
        for fp in d["frontmatter"].get("file_paths", []):
            file_decisions[_shorten_path(fp)].append(d)

    db.close()

    return {
        "project": project,
        "decisions": decisions,
        "recent_sessions": recent_sessions,
        "probes": probes,
        "concept_counts": dict(concept_counts),
        "file_freq": dict(file_freq),
        "file_decisions": dict(file_decisions),
    }


def state_of_play(config: Config, project: str) -> str:
    """Generate STATE.md — data-driven version.

    When called via CLI, this produces a useful data-driven document.
    When called via MCP, the agent can enhance it with LLM narrative.
    """
    ctx = _gather_state_context(config, project)
    today = date.today().isoformat()

    lines = [
        f"# State of Play — {project}",
        f"*Last updated: {today}*",
        "",
    ]

    # Section 1: Key Files — structural overview
    file_decisions = ctx.get("file_decisions", {})
    file_freq = ctx["file_freq"]

    # Build a unified file importance map: files with decisions OR high touch frequency
    file_importance: dict[str, dict] = {}
    for fp, decs in file_decisions.items():
        file_importance[fp] = {
            "decisions": len(decs),
            "sessions": 0,
            "key_decision": decs[0]["title"] if decs else "",
        }

    # Merge in touch frequency (shortening paths to match)
    for fp, count in file_freq.items():
        short = _shorten_path(fp)
        if short in file_importance:
            file_importance[short]["sessions"] = count
        elif count >= 2:
            # Files touched in multiple sessions but no decisions — still noteworthy
            file_importance[short] = {
                "decisions": 0,
                "sessions": count,
                "key_decision": "",
            }

    if file_importance:
        # Sort: files with decisions first, then by session count
        sorted_files = sorted(
            file_importance.items(),
            key=lambda x: (-x[1]["decisions"], -x[1]["sessions"]),
        )

        lines.append("## Key Files")
        lines.append("")
        lines.append("| File | Decisions | Recent Sessions | Key Decision |")
        lines.append("|---|---|---|---|")
        for fp, info in sorted_files[:15]:
            dec_count = info["decisions"] or "—"
            sess_count = info["sessions"] or "—"
            key_dec = info["key_decision"]
            if len(key_dec) > 50:
                key_dec = key_dec[:47] + "..."
            lines.append(f"| {fp} | {dec_count} | {sess_count} | {key_dec} |")
        lines.append("")

    # Section 2: Decisions overview (for "Decisions Worth Understanding")
    decisions = ctx["decisions"]
    if decisions:
        # Highlight decisions worth inspecting
        worth_inspecting = [
            d for d in decisions
            if d["status"] in ("proposed", "accepted")
            and (not d["confidence"] or (isinstance(d["confidence"], (int, float)) and d["confidence"] < 0.7))
        ]

        lines.append("## Decisions Worth Understanding")
        lines.append("")
        if worth_inspecting:
            for d in worth_inspecting:
                conf_str = f" — confidence: {d['confidence']}" if d['confidence'] else " — not yet evaluated"
                lines.append(f"- [[{d['id']}]] **{d['title']}**{conf_str}")
                if d["summary"]:
                    lines.append(f"  {d['summary']}")
            lines.append("")
        else:
            lines.append("All decisions have high confidence. See DECISIONS.md for the full ledger.")
            lines.append("")

    # Section 2: What's been changing
    recent_sessions = ctx["recent_sessions"]
    file_freq = ctx["file_freq"]
    if recent_sessions:
        lines.append("## What's Been Changing")
        lines.append(f"*{len(recent_sessions)} sessions in the last 14 days*")
        lines.append("")

        if file_freq:
            # Top touched files
            sorted_files = sorted(file_freq.items(), key=lambda x: -x[1])[:10]
            lines.append("| File | Sessions |")
            lines.append("|---|---|")
            for f, count in sorted_files:
                lines.append(f"| {_shorten_path(f)} | {count} |")
            lines.append("")

        # Recent commits summary
        all_commits = []
        for sess in recent_sessions:
            for c in sess.get("commits", []):
                all_commits.append(c)
        if all_commits:
            lines.append(f"**{len(all_commits)} commits** across these sessions.")
            lines.append("")
    else:
        lines.append("## What's Been Changing")
        lines.append("No sessions in the last 14 days.")
        lines.append("")

    # Section 3: Probes / What You've Been Exploring
    probes = ctx["probes"]
    if probes:
        lines.append("## What You've Been Exploring")
        lines.append("")
        for p in probes[:10]:
            sess_ref = f", [[{p['session']}]]" if p["session"] else ""
            lines.append(f"- \"{p['title']}\" ({p['date']}{sess_ref})")
        lines.append("")

    # Section 4: Concept Landscape
    concept_counts = ctx["concept_counts"]
    if concept_counts:
        lines.append("## Concept Landscape")
        lines.append("")
        sorted_concepts = sorted(concept_counts.items(), key=lambda x: -x[1])[:15]
        concept_strs = [f"`{c}` ({n})" for c, n in sorted_concepts]
        lines.append(", ".join(concept_strs))
        lines.append("")

    return "\n".join(lines) + "\n"


def state_of_play_context(config: Config, project: str) -> str:
    """Return structured context for LLM-assisted STATE.md generation.

    Used by the MCP tool — the agent gets this context and writes the
    full narrative STATE.md using its judgment.
    """
    ctx = _gather_state_context(config, project)

    sections = []
    sections.append(f"# Context for STATE.md — {project}\n")
    sections.append("Use this data to write a STATE.md that tells the human what matters most.\n")

    # Format guidance for the LLM writing STATE.md
    sections.append("## Format Guidance\n")
    sections.append(
        "**Key Files → Reading Guide**: Don't list key files as a flat table. "
        "Write a guided reading order organized by data flow — how data enters, "
        "transforms, and exits the system. Group related files into numbered "
        "stages (e.g., '1. Start with the types', '2. Entry point'). For each "
        "file, write 1-2 sentences about what it does and why a reader should "
        "care. End with a brief mental model — a short ASCII pipeline or diagram "
        "showing how the stages connect. Skip files that aren't architecturally "
        "interesting.\n"
    )

    # Decisions
    decisions = ctx["decisions"]
    if decisions:
        sections.append("## Decisions")
        for d in decisions:
            verdict_str = f", verdict={d['verdict']}({d['confidence']})" if d["verdict"] else ""
            sections.append(f"- [{d['status']}] {d['title']} ({d['id']}, {d['date']}{verdict_str})")
            if d["summary"]:
                sections.append(f"  Summary: {d['summary']}")

    # File → decision map
    file_decisions = ctx.get("file_decisions", {})
    if file_decisions:
        sections.append("\n## Files With Architectural Decisions")
        sections.append("These files have decisions attached — they are structurally important.")
        for fp, decs in sorted(file_decisions.items(), key=lambda x: -len(x[1])):
            dec_titles = ", ".join(d["title"] for d in decs)
            sections.append(f"  {fp} ({len(decs)} decisions): {dec_titles}")

    # Recent sessions
    sessions = ctx["recent_sessions"]
    if sessions:
        sections.append("\n## Recent Sessions (14 days)")
        for s in sessions:
            files_str = f", files: {', '.join(_shorten_path(f) for f in s['files_touched'][:5])}" if s["files_touched"] else ""
            sections.append(f"- {s['date']} — {s['title']} ({s['id']}{files_str})")

    # File frequency
    if ctx["file_freq"]:
        sections.append("\n## File Touch Frequency (14 days)")
        for f, count in sorted(ctx["file_freq"].items(), key=lambda x: -x[1])[:15]:
            sections.append(f"  {count}x {_shorten_path(f)}")

    # Probes
    if ctx["probes"]:
        sections.append("\n## Recent Probes (user questions)")
        for p in ctx["probes"]:
            sections.append(f"- \"{p['title']}\" ({p['date']})")

    # Concepts
    if ctx["concept_counts"]:
        sections.append("\n## Concept Frequency")
        for c, n in sorted(ctx["concept_counts"].items(), key=lambda x: -x[1])[:20]:
            sections.append(f"  {n:3d}  {c}")

    return "\n".join(sections) + "\n"


def generate_all(config: Config, project: str) -> dict[str, str]:
    """Generate all 3 landing documents. Returns {filename: content}."""
    return {
        "DECISIONS.md": decisions_ledger(config, project),
        "BACKLOG.md": backlog_summary(config, project),
        "STATE.md": state_of_play(config, project),
    }


def write_landing_docs(
    config: Config,
    project: str,
    docs: str = "all",
) -> dict[str, Path]:
    """Generate and write landing documents to the project directory.

    Args:
        docs: Which docs to generate — "all", "decisions", "backlog", "state"

    Returns:
        Dict of {filename: written_path}.
    """
    project_dir = config.vault_root / "projects" / project
    project_dir.mkdir(parents=True, exist_ok=True)

    generators = {
        "decisions": ("DECISIONS.md", decisions_ledger),
        "backlog": ("BACKLOG.md", backlog_summary),
        "state": ("STATE.md", state_of_play),
    }

    if docs == "all":
        to_generate = list(generators.keys())
    elif docs in generators:
        to_generate = [docs]
    else:
        raise ValueError(f"Unknown doc type: {docs}. Use: all, decisions, backlog, state")

    written: dict[str, Path] = {}
    for key in to_generate:
        filename, gen_fn = generators[key]
        content = gen_fn(config, project)
        path = project_dir / filename
        path.write_text(content, encoding="utf-8")
        written[filename] = path

    return written
