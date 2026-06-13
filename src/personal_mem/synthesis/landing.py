"""Generate project landing documents.

These are materialized views over existing vault notes, excluded from
the index. The ``decisions`` and ``backlog`` ledgers are fully
auto-generated from data; ``state`` gathers context for LLM-assisted
narrative generation.

Filename defaults ship in
``personal_mem.sources.config.DEFAULT_CONFIG`` under the
``landing_files`` key and can be overridden per-vault in
``vault/config/sources.yaml``. Callers that need the *current* filename
set should use :func:`landing_filenames` /
:func:`landing_filename_set` rather than hardcoding any of the strings.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from personal_mem.core._utils import as_list
from personal_mem.core.config import Config


def _default_landing_filenames() -> dict[str, str]:
    """In-code defaults — pulled from
    :data:`personal_mem.sources.config.DEFAULT_CONFIG` so the framework
    has a single source of truth for landing-doc names."""
    from personal_mem.sources.config import DEFAULT_CONFIG

    return dict(DEFAULT_CONFIG["landing_files"])


# Module-level default mapping kept for back-compat. Mirrors
# ``DEFAULT_CONFIG['landing_files']``. User overrides flow through
# :func:`landing_filenames` below.
DEFAULT_LANDING_FILENAMES: dict[str, str] = _default_landing_filenames()


def landing_filenames(vault_root: Path | None = None) -> dict[str, str]:
    """Return the merged ``landing_files`` mapping for the given vault.

    Reads ``vault/config/sources.yaml`` if present and overlays user
    overrides on top of :data:`DEFAULT_LANDING_FILENAMES`. Missing
    vault root → defaults.

    The mapping always includes a ``research_focus`` key. The framework
    does not auto-generate the research-focus landing doc — it remains
    a user-maintained file. ``/discover`` reads it as ambient input but
    never writes it.
    """
    from personal_mem.sources.config import load_user_config

    merged = _default_landing_filenames()
    if vault_root is None:
        return merged
    user = load_user_config(vault_root).get("landing_files", {}) or {}
    for key, value in user.items():
        if isinstance(value, str) and value:
            merged[key] = value
    return merged


def landing_filename_set(vault_root: Path | None = None) -> set[str]:
    """Set of currently configured landing-doc filenames.

    Used by the indexer to skip auto-generated landing files. The set
    derives from :func:`landing_filenames` so it picks up user overrides.
    """
    return set(landing_filenames(vault_root).values())


# Backwards-compatible alias — preserves the old import path
# (``from personal_mem.synthesis.landing import LANDING_FILENAMES``).
# Resolves at import time using the in-code defaults; callers that need
# user-overridable filenames should call :func:`landing_filename_set`.
LANDING_FILENAMES = set(DEFAULT_LANDING_FILENAMES.values())


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


def _id_path_map(db) -> dict[str, str]:
    """Map note id -> vault-relative path (sans .md) for path-based wikilinks.

    Path links resolve structurally in Obsidian by file location, so they
    never spawn a phantom stub even on notes that predate the `aliases:`
    backfill. This is the durable form for every materialised link — the
    same structural shape concept-hub links use.
    """
    out: dict[str, str] = {}
    for r in db.execute("SELECT id, path FROM notes"):
        rel = str(r["path"] or "").replace("\\", "/")
        if rel.endswith(".md"):
            rel = rel[:-3]
        if rel:
            out[r["id"]] = rel
    return out


def _reflink(idmap: dict[str, str], note_id: str, display: str | None = None) -> str:
    """Path-based wikilink to a note, falling back to the bare id (alias)."""
    ref = idmap.get(note_id) or note_id
    if ref == note_id and display is None:
        return f"[[{note_id}]]"
    return f"[[{ref}|{display if display is not None else note_id}]]"


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
    """Generate the per-project decisions landing doc — table + Mermaid DAG."""
    db = _get_db(config)
    decisions = _query_decisions(db, project)
    edges = _query_edges(db, project)
    idmap = _id_path_map(db)
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
                f"| {_reflink(idmap, d['id'])} | {d['date']} | {d['title']} "
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
                f"| {_reflink(idmap, d['id'])} | {d['date']} | {d['title']} "
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
        for c in as_list(fm.get("concepts")):
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
    """Generate the per-project backlog landing doc — open items, stalled
    proposals, parked items."""
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

    idmap = _id_path_map(db)
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
                lines.append(f"- [ ] {item['title']} ({_reflink(idmap, item['id'])}){tag_str}")
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
            lines.append(f"| {_reflink(idmap, s['id'])} | {s['date']} | {s['title']} | {summary} |")
        lines.append("")

    # Parked items
    if parked:
        lines.append("## Parked")
        lines.append("Discussed and deliberately deferred or rejected.")
        lines.append("")
        for p in parked:
            reason = f" — {p['reason']}" if p["reason"] else ""
            lines.append(f"- {p['title']} ({_reflink(idmap, p['id'])}){reason}")
        lines.append("")

    return "\n".join(lines) + "\n"


def _gather_prompt_probes(
    config: Config,
    project: str,
    limit: int | None = None,
) -> list[dict]:
    """Walk a project's session JSONL buffers and return classified probes.

    Looks at archived per-session ``events.jsonl`` files plus any active
    ``.mem/buffer/<session>.jsonl`` files mapped to this project, lifts
    prompt events via ``extract.extract_prompts``, runs them through
    ``extract.classify_probe``, and returns the most recent ``limit``
    probes (default: config ``landing.open_probes_cap``, 20). Each entry
    is shaped like a ``probes`` row from the SQL path so the renderer can
    merge both sources.

    This deliberately does no SQL query — prompt events live in JSONL,
    not the index. Failures (missing dirs, bad JSON) degrade silently.
    """
    from personal_mem.core.events import classify_probe, extract_prompts

    if limit is None:
        limit = int(getattr(config, "landing_open_probes_cap", 20) or 20)

    project_root = config.vault_root / "projects" / project
    sessions_root = project_root / "sessions"

    candidates: list[tuple[datetime, dict]] = []
    if sessions_root.exists():
        # Iterate only the immediate session-folder children, then look for
        # events.jsonl. Pattern matches both `<id>-<date>/events.jsonl` and
        # `misc/events.jsonl` shapes.
        for sess_dir in sessions_root.iterdir():
            if not sess_dir.is_dir():
                continue
            events_file = sess_dir / "events.jsonl"
            if not events_file.exists():
                continue
            try:
                events = []
                for line in events_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                prompts = extract_prompts(events_file)
            except Exception:
                continue
            for p in prompts:
                if not classify_probe(p, events):
                    continue
                candidates.append((
                    p.ts,
                    {
                        "id": "",
                        "title": p.text[:160],
                        "date": p.ts.date().isoformat() if p.ts != datetime.min else "",
                        "session": sess_dir.name,
                        "source": "prompt",
                    },
                ))

    # Active (non-archived) buffers in .mem/buffer. We can't trivially map a
    # buffer's session UUID to a project, so we check the project of any
    # session note that already references the same source_session — if
    # missing, we conservatively skip rather than mislabel.
    buffer_root = config.mem_dir / "buffer"
    if buffer_root.exists():
        try:
            from personal_mem.core.schemas import NoteType
            from personal_mem.core.vault import VaultManager

            vm = VaultManager(config=config)
            session_to_project: dict[str, str] = {}
            for note in vm.list_notes(note_type=NoteType.SESSION, limit=200):
                src = note.frontmatter.get("source_session", "")
                if src:
                    session_to_project[str(src)] = note.project or ""
        except Exception:
            session_to_project = {}

        for buf_file in buffer_root.glob("*.jsonl"):
            session_uuid = buf_file.stem
            if session_to_project.get(session_uuid) not in (project, ""):
                # Skip buffers we know belong to a different project.
                if session_uuid in session_to_project:
                    continue
            try:
                events = []
                for line in buf_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                prompts = extract_prompts(buf_file)
            except Exception:
                continue
            for p in prompts:
                if not classify_probe(p, events):
                    continue
                candidates.append((
                    p.ts,
                    {
                        "id": "",
                        "title": p.text[:160],
                        "date": p.ts.date().isoformat() if p.ts != datetime.min else "",
                        "session": session_uuid,
                        "source": "prompt",
                    },
                ))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [row for _, row in candidates[:limit]]


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
    """Gather all data needed for the state-of-play landing doc.

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

    # Probes — two sources, merged:
    #   1. Notes tagged `probe` (manual override; see CLAUDE.md §3 Prompt
    #      lifecycle). Still load-bearing for back-compat, but no longer the
    #      primary signal.
    #   2. Classified prompts from session JSONL buffers via
    #      `_gather_prompt_probes` — the canonical Phase 4 E source.
    probes: list[dict] = []
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
                "source": "tag",
            })

    # Merge in classified prompt-probes. Dedup by lowercased title — when
    # the user explicitly tagged a prompt-derived note as `probe`, the SQL
    # row wins (it has a real id to link to).
    seen_titles = {p["title"].strip().lower() for p in probes if p.get("title")}
    for prompt_probe in _gather_prompt_probes(config, project):
        key = prompt_probe["title"].strip().lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        probes.append(prompt_probe)

    # Concept frequency
    concept_counts: dict[str, int] = defaultdict(int)
    for row in db.execute(
        "SELECT frontmatter FROM notes WHERE project = ?", (project,),
    ):
        fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
        for c in as_list(fm.get("concepts")):
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
    """Generate the data-driven state-of-play landing doc.

    When called via CLI, this produces a useful data-driven document.
    When called via MCP, the agent can enhance it with LLM narrative.
    """
    ctx = _gather_state_context(config, project)
    _db = _get_db(config)
    idmap = _id_path_map(_db)
    _db.close()
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
                lines.append(f"- {_reflink(idmap, d['id'])} **{d['title']}**{conf_str}")
                if d["summary"]:
                    lines.append(f"  {d['summary']}")
            lines.append("")
        else:
            decisions_doc = landing_filenames(config.vault_root)["decisions"]
            lines.append(
                f"All decisions have high confidence. See {decisions_doc} for the full ledger."
            )
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

    # Section 3: Open Probes — merges manual `probe`-tagged notes with
    # classified prompts from session JSONL buffers (Phase 4 E).
    probes = ctx["probes"]
    if probes:
        lines.append("## Open Probes")
        lines.append("")
        display_cap = int(
            getattr(config, "landing_probes_display_cap", 10) or 10
        )
        for p in probes[:display_cap]:
            session = p.get("session", "")
            sess_ref = f", {_reflink(idmap, session)}" if session else ""
            tag = ""
            if p.get("source") == "prompt":
                tag = " · *prompt*"
            elif p.get("source") == "tag":
                tag = " · *tagged*"
            lines.append(f"- \"{p['title']}\" ({p['date']}{sess_ref}){tag}")
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

    # Section 5: Recent Maintenance — links to the latest autonomous-run
    # reports (dream cycles + discover runs) so a user can see exactly
    # what each cycle did without digging through transcripts.
    from personal_mem.operations.reports import recent_reports

    reports = sorted(
        recent_reports(config, "dream", n=3)
        + recent_reports(config, "discover", n=3),
        key=lambda r: r["mtime"],
        reverse=True,
    )
    if reports:
        lines.append("## Recent Maintenance")
        lines.append(
            "Per-run reports from the autonomous cycles — dream "
            "(promotions, theme mints, essence rewrites) and discover "
            "(enqueues, stalled decisions, ontology proposals)."
        )
        lines.append("")
        for r in reports:
            rel = Path(r["path"]).relative_to(config.vault_root)
            lines.append(f"- [{r['run_id']}]({rel})")
        lines.append("")

    return "\n".join(lines) + "\n"


def state_of_play_context(config: Config, project: str) -> str:
    """Return structured context for the LLM-assisted state-of-play doc.

    Used by the MCP tool — the agent gets this context and writes the
    full narrative landing doc using its judgment.
    """
    ctx = _gather_state_context(config, project)
    state_doc = landing_filenames(config.vault_root)["state"]

    sections = []
    sections.append(f"# Context for {state_doc} — {project}\n")
    sections.append(
        f"Use this data to write a {state_doc} that tells the human what matters most.\n"
    )

    # Format guidance for the LLM writing the state-of-play landing doc
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

    # Probes — merged from `probe`-tagged notes + classified prompt events.
    if ctx["probes"]:
        sections.append("\n## Recent Probes (user questions)")
        for p in ctx["probes"]:
            origin = p.get("source", "tag")
            sections.append(f"- \"{p['title']}\" ({p['date']}, source={origin})")

    # Concepts
    if ctx["concept_counts"]:
        sections.append("\n## Concept Frequency")
        for c, n in sorted(ctx["concept_counts"].items(), key=lambda x: -x[1])[:20]:
            sections.append(f"  {n:3d}  {c}")

    # Recent maintenance (dream reports). Surfaces the latest 3 cycles so
    # the user can click through to see exactly what each one did.
    from personal_mem.operations.dream import recent_dream_reports

    reports = recent_dream_reports(config, n=3)
    if reports:
        sections.append("\n## Recent Maintenance")
        sections.append(
            "Per-cycle dream reports — concept/theme promotions, "
            "candidates archived, status changes, essence rewrites."
        )
        for r in reports:
            rel = Path(r["path"]).relative_to(config.vault_root)
            sections.append(f"- [{r['cycle_id']}]({rel})")

    return "\n".join(sections) + "\n"


def generate_all(config: Config, project: str) -> dict[str, str]:
    """Generate all 3 landing documents. Returns {filename: content}.

    Filenames respect ``vault/.mem/sources.yaml: landing_files:`` so a
    user who renamed any of the standard names sees their key in the
    returned mapping.
    """
    names = landing_filenames(config.vault_root)
    return {
        names["decisions"]: decisions_ledger(config, project),
        names["backlog"]: backlog_summary(config, project),
        names["state"]: state_of_play(config, project),
    }


def _query_themes(db) -> list[dict]:
    """Query all themes (global). Themes live at vault/themes/, not per-project.

    Returns themes ordered by date desc. Each entry includes:

    - ``implements_count``: how many decisions cite the theme via an
      ``implements`` edge.
    - ``decisions``: list of {id, title, implements_catalyst} dicts for
      those decisions. Used by the inline temporal DAG renderer.
    - ``catalyst_entries``: parsed catalyst-log entries.
    - ``last_catalyst``: the most recent catalyst date.
    """
    from personal_mem.synthesis.hub import ESSENCE_HEADING, extract_section
    from personal_mem.synthesis.theme_hub import parse_theme_catalyst_log

    rows = db.execute(
        "SELECT id, title, date, project, frontmatter, body_text "
        "FROM notes WHERE type = 'theme' ORDER BY date DESC"
    ).fetchall()

    themes: list[dict] = []
    for row in rows:
        fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}

        # Decisions implementing this theme. Pull title + frontmatter so
        # we can read implements_catalyst (if set) without a second pass.
        decision_rows = db.execute(
            "SELECT s.id, s.title, s.frontmatter "
            "FROM edges e "
            "JOIN notes s ON s.id = e.source AND s.type = 'decision' "
            "WHERE e.target = ? AND e.edge_type = 'implements'",
            (row["id"],),
        ).fetchall()

        decisions: list[dict] = []
        for d in decision_rows:
            d_fm = json.loads(d["frontmatter"]) if d["frontmatter"] else {}
            decisions.append({
                "id": d["id"],
                "title": d["title"] or d_fm.get("title", ""),
                "implements_catalyst": str(
                    d_fm.get("implements_catalyst", "")
                ),
            })

        body_text = row["body_text"] or ""
        catalyst_entries = parse_theme_catalyst_log(body_text)
        last_catalyst = _last_catalyst_date(body_text)
        essence = extract_section(body_text, ESSENCE_HEADING).strip()

        themes.append({
            "id": row["id"],
            "title": row["title"] or fm.get("title", ""),
            "date": row["date"] or "",
            "project": row["project"] or fm.get("project", ""),
            "status": fm.get("status", "active"),
            "concepts": fm.get("concepts", []),
            "relates_to": fm.get("relates_to", []),
            "parent": str(fm.get("parent") or ""),
            "essence": essence,
            "implements_count": len(decisions),
            "decisions": decisions,
            "catalyst_entries": catalyst_entries,
            "last_catalyst": last_catalyst,
            "frontmatter": fm,
        })
    return themes


def _last_catalyst_date(body: str) -> str:
    """Return the most recent date that appears at the start of a line in
    the ``## Catalyst log`` section, or empty string if none found.

    Best-effort, schema-tolerant: just looks for an ISO-ish date prefix on
    each line within the section. The full temporal-DAG parser lives in
    ``temporal.py`` (Workstream C).
    """
    in_log = False
    dates: list[str] = []
    date_re = re.compile(r"^\s*[-*]?\s*(\d{4}-\d{2}-\d{2})\b")
    for line in body.split("\n"):
        if line.strip().startswith("## "):
            in_log = "catalyst" in line.lower()
            continue
        if not in_log:
            continue
        m = date_re.match(line)
        if m:
            dates.append(m.group(1))
    return max(dates) if dates else ""


def themes_ledger(config: Config) -> str:
    """Generate the global themes landing doc.

    Lists active themes (table), dormant themes (collapsed), resolved
    themes (collapsed). Each theme row links to its full page where the
    Essence and Catalyst log live. The per-theme temporal DAG (Workstream
    C) renders on the theme page itself, not here.
    """
    db = _get_db(config)
    themes = _query_themes(db)
    idmap = _id_path_map(db)
    db.close()

    today = date.today().isoformat()
    lines = [
        "# Themes",
        f"*Auto-generated. Last updated: {today}*",
        "",
        "Themes are global narratives — temporal stories cited by sources, "
        "decisions, and notes from any project. Concepts they cite are "
        "invariants (e.g. `finance-regime`); the timed catalysts live "
        "inside each theme's `## Catalyst log` section.",
        "",
    ]

    if not themes:
        lines.append("No themes recorded yet.")
        return "\n".join(lines) + "\n"

    active = [t for t in themes if t["status"] == "active"]
    dormant = [t for t in themes if t["status"] == "dormant"]
    resolved = [t for t in themes if t["status"] == "resolved"]
    merged = [t for t in themes if str(t["status"]).startswith("merged-into")]
    other = [
        t for t in themes
        if t["status"] not in ("active", "dormant", "resolved")
        and not str(t["status"]).startswith("merged-into")
    ]

    def _row(t: dict, depth: int = 0) -> str:
        prefix = "↳ " * depth
        # Use the real indexed path, not a path reconstructed from the
        # title-slug — a title whose slug differs from the on-disk filename
        # would otherwise spawn a phantom theme stub.
        link = f"{prefix}{_reflink(idmap, t['id'], t['title'])}"
        proj = t["project"] or "—"
        # Catalyst log dates are bare YYYY-MM-DD; the index `date` column
        # carries an ISO timestamp — trim the time portion for display.
        raw_last = t["last_catalyst"] or t["date"] or ""
        last = raw_last.split("T", 1)[0] if raw_last else "—"
        impls = t["implements_count"]
        return f"| {link} | {proj} | {last} | {impls} |"

    if active:
        lines.append(f"## Active ({len(active)})")
        lines.append("")
        lines.append("| Theme | Project | Last catalyst | # decisions |")
        lines.append("|---|---|---|---|")
        for t, depth in _hierarchical_order(active):
            lines.append(_row(t, depth))
        lines.append("")

        # Catalog — compact essence + concepts per active theme. Two
        # audiences: the human reader who wants more than a table row,
        # and the news triage helper which slurps this section as the
        # cached system context for Haiku verdicts. The format is stable
        # markdown — `### {title}` heading + bullet block + blockquote
        # essence — so triage can locate the section by heading and pass
        # it whole.
        lines.append(f"## Catalog (active)")
        lines.append("")
        lines.append(
            "*Compact view of active themes — used by news triage and as "
            "a quick read of what's being tracked. Generated from each "
            "theme's frontmatter and `## Essence` section.*"
        )
        lines.append("")
        for t, depth in _hierarchical_order(active):
            lines.append(_catalog_card(t, depth, by_id={x["id"]: x for x in active}, idmap=idmap))
            lines.append("")

        # Per-theme temporal DAG — inlined Mermaid diagram for any theme
        # that has either catalyst-log linkage or pinned decisions to
        # render. Themes with only `new` flags and no decisions get no
        # diagram.
        from personal_mem.retrieval.temporal import (
            entries_to_graph,
            render_evolution_section,
        )

        for t in active:
            graph = entries_to_graph(
                t["catalyst_entries"],
                decisions=t["decisions"] or None,
                kind="catalyst",
            )
            if graph.is_empty() or (not graph.edges and not t["decisions"]):
                continue
            section = render_evolution_section(
                graph, heading=f"### {t['title']}"
            )
            if section:
                lines.append(section)
                lines.append("")

    for label, group in (("Dormant", dormant), ("Resolved", resolved)):
        if group:
            lines.append(f"<details><summary>{label} ({len(group)})</summary>")
            lines.append("")
            lines.append("| Theme | Project | Last catalyst | # decisions |")
            lines.append("|---|---|---|---|")
            for t, depth in _hierarchical_order(group):
                lines.append(_row(t, depth))
            lines.append("")
            lines.append("</details>")
            lines.append("")

    if merged:
        lines.append(f"<details><summary>Merged ({len(merged)})</summary>")
        lines.append("")
        for t in merged:
            target = str(t["status"]).split(":", 1)[-1] if ":" in str(t["status"]) else "?"
            lines.append(f"- {t['title']} → `{target}`")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    if other:
        lines.append(f"<details><summary>Other status ({len(other)})</summary>")
        lines.append("")
        for t in other:
            lines.append(f"- {t['title']} — `{t['status']}`")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines) + "\n"


def _slug_for_link(title: str) -> str:
    """Mirror VaultManager._sanitize_filename for wikilink generation."""
    slug = title.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug.strip("-")
    return slug[:80] if slug else "untitled"


def _catalog_card(t: dict, depth: int, by_id: dict[str, dict], idmap: dict[str, str] | None = None) -> str:
    """Render one active theme as a markdown sub-block for the Catalog section.

    Stable shape so the triage helper can parse-by-heading:

        ### {title}
        - **id:** `thm-XXXX`
        - **parent:** `thm-YYYY` (or "(top-level)")
        - **concepts:** `c1`, `c2`, `c3`
        - **last catalyst:** YYYY-MM-DD
        > {essence excerpt — first ~400 chars}

    Children are rendered identically — depth signals nesting only via
    the parent line, not via heading level (kept at H3 throughout so
    Obsidian's outline view is flat-readable).
    """
    title = t["title"] or t["id"]
    parent_id = t.get("parent") or ""
    parent_line = (
        f"- **parent:** {_reflink(idmap or {}, parent_id, parent_id)} ({by_id[parent_id]['title']})"
        if parent_id and parent_id in by_id
        else "- **parent:** _(top-level)_"
    )
    concepts = t.get("concepts") or []
    concepts_line = (
        "- **concepts:** "
        + (", ".join(f"`{c}`" for c in concepts) if concepts else "_(none yet)_")
    )
    raw_last = t.get("last_catalyst") or t.get("date") or ""
    last = raw_last.split("T", 1)[0] if raw_last else "—"
    essence = (t.get("essence") or "").strip()
    essence_excerpt = _truncate_essence(essence, max_chars=400)

    parts = [
        f"### {title}",
        f"- **id:** `{t['id']}`",
        parent_line,
        concepts_line,
        f"- **last catalyst:** {last}",
    ]
    if essence_excerpt:
        parts.append("")
        # Blockquote-style so the triage helper can strip `> ` markers
        # cleanly when serialising into the Haiku prompt.
        for line in essence_excerpt.split("\n"):
            parts.append(f"> {line}" if line else ">")
    return "\n".join(parts)


def _truncate_essence(essence: str, *, max_chars: int = 400) -> str:
    """Compress an essence paragraph to <= max_chars, breaking on a
    sentence boundary when feasible. Skeleton placeholder text (italic
    underscores prefacing the body) is dropped entirely — those notes
    have no real essence yet and shouldn't pollute the catalog.
    """
    from personal_mem.synthesis.hub import essence_is_placeholder

    text = essence.strip()
    # Skeleton placeholder check — shared predicate (synthesis/hub.py)
    # with the dream scan; stub essences shouldn't pollute the catalog.
    if essence_is_placeholder(text):
        return ""
    if len(text) <= max_chars:
        return text
    # Try to break at a sentence boundary within the budget.
    cutoff = text.rfind(". ", 0, max_chars)
    if cutoff > max_chars // 2:
        return text[: cutoff + 1] + "…"
    # Fall back to nearest word boundary.
    cutoff = text.rfind(" ", 0, max_chars)
    if cutoff > max_chars // 2:
        return text[:cutoff] + "…"
    return text[:max_chars] + "…"


def _hierarchical_order(themes: list[dict]) -> list[tuple[dict, int]]:
    """Order themes as a parent → children tree, depth-first.

    Themes are nodes; ``parent`` frontmatter is the only edge. Themes whose
    ``parent`` is empty *or* points outside the input set are roots
    (rendered at depth 0). Children appear immediately after their parent
    at depth+1. Multi-parent themes are not supported — the first parent
    wins and any deeper cycle is broken on second visit.

    Stable: roots preserve the input order; siblings preserve input order
    among themselves. Disconnected children (parent unknown / not in
    list) get promoted to roots so nothing is dropped.
    """
    by_id = {t["id"]: t for t in themes}
    children_of: dict[str, list[dict]] = {}
    for t in themes:
        p = t.get("parent") or ""
        if p and p in by_id:
            children_of.setdefault(p, []).append(t)

    out: list[tuple[dict, int]] = []
    visited: set[str] = set()

    def _walk(t: dict, depth: int) -> None:
        if t["id"] in visited:
            return
        visited.add(t["id"])
        out.append((t, depth))
        for child in children_of.get(t["id"], []):
            _walk(child, depth + 1)

    for t in themes:
        p = t.get("parent") or ""
        if p and p in by_id:
            continue
        _walk(t, 0)

    # Pick up any disconnected children whose parent reference points
    # outside the visible group (e.g., dormant parent, active child).
    for t in themes:
        if t["id"] not in visited:
            _walk(t, 0)

    return out


def write_landing_docs(
    config: Config,
    project: str,
    docs: str = "all",
    landing_filenames_override: dict[str, str] | None = None,
) -> dict[str, Path]:
    """Generate and write landing documents.

    Project-scoped docs (decisions, backlog, state) land in
    ``projects/{project}/``. The global themes doc lands at the vault
    root — passing ``docs="themes"`` ignores the ``project`` argument
    since themes are global. ``docs="all"`` writes every doc,
    project-scoped + global.

    Args:
        docs: Which docs to generate — ``all``, ``decisions``,
            ``backlog``, ``state``, ``themes``.
        landing_filenames_override: Optional explicit mapping that
            replaces the resolved-from-config filenames. Keys: any
            subset of ``decisions``/``backlog``/``state``/``themes``/
            ``research_focus``. Missing keys fall back to the resolved
            user/default names. ``None`` (the default) reads
            ``vault/.mem/sources.yaml`` via :func:`landing_filenames`.

    Returns:
        Dict of {filename: written_path}.
    """
    project_dir = config.vault_root / "projects" / project
    project_dir.mkdir(parents=True, exist_ok=True)

    names = landing_filenames(config.vault_root)
    if landing_filenames_override:
        for key, value in landing_filenames_override.items():
            if isinstance(value, str) and value:
                names[key] = value
    project_generators = {
        "decisions": (names["decisions"], decisions_ledger),
        "backlog": (names["backlog"], backlog_summary),
        "state": (names["state"], state_of_play),
    }
    global_generators = {
        "themes": (names["themes"], themes_ledger),
    }

    valid = set(project_generators) | set(global_generators) | {"all"}
    if docs not in valid:
        raise ValueError(
            f"Unknown doc type: {docs}. Use: all, decisions, backlog, state, themes"
        )

    if docs == "all":
        project_keys = list(project_generators)
        global_keys = list(global_generators)
    elif docs in project_generators:
        project_keys, global_keys = [docs], []
    else:
        project_keys, global_keys = [], [docs]

    written: dict[str, Path] = {}
    for key in project_keys:
        filename, gen_fn = project_generators[key]
        content = gen_fn(config, project)
        path = project_dir / filename
        path.write_text(content, encoding="utf-8")
        written[filename] = path

    for key in global_keys:
        filename, gen_fn = global_generators[key]
        content = gen_fn(config)  # global generators take no project arg
        path = config.vault_root / filename
        path.write_text(content, encoding="utf-8")
        written[filename] = path

    return written
