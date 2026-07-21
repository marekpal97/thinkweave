"""Import observations and sessions from claude-mem SQLite database.

Reads ~/.claude-mem/claude-mem.db and converts:
  - observations → notes (or decisions, based on type)
  - session_summaries → session notes
  - observations are placed inside their parent session's folder

One-time migration with idempotency via manifest file.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from thinkweave.acquisition.importers.common import ImportManifest, index_imported_notes
from thinkweave.core.config import Config, load_config
from thinkweave.core.schemas import DecisionStatus, NoteType
from thinkweave.core.vault import VaultManager

_DEFAULT_CLAUDE_MEM_DB = Path.home() / ".claude-mem" / "claude-mem.db"
_MANIFEST_NAME = "claude_mem_migration.json"

# ── Project name normalization ──────────────────────────────────────

PROJECT_MAP: dict[str, str] = {
    # Real projects — keep as-is
    "thinkmesh_neural": "thinkmesh_neural",
    "options_engine": "options_engine",
    "personal_finance_assistant": "personal_finance_assistant",
    "code_graph": "code_graph",
    "research_assistant": "research_assistant",
    # thinkmesh is the original project; thinkmesh_neural is a separate spinoff
    "thinkmesh": "thinkmesh",
    # Normalize variations
    ".claude": "_claude_config",
    "research": "research_assistant",
    "marek": "_personal",
    # Date-coded / manual / spawn / agent → automated
    "MAR-21": "_automated",
    "MAR-22": "_automated",
    "MAR-23": "_automated",
    "MAR-24": "_automated",
    "MAR-25": "_automated",
    "MAR-26": "_automated",
    "MAR-27": "_automated",
    "MAR-28": "_automated",
    "manual-001": "_automated",
    "manual-002": "_automated",
    "spawn-001": "_automated",
    "agent-a999c174": "_automated",
    "python_projects": "_automated",
    # Empty string
    "": "_unscoped",
}

# claude-mem meta-concepts that map to useful tags
META_CONCEPT_TO_TAG: dict[str, str] = {
    "gotcha": "gotcha",
    "pattern": "pattern",
    "trade-off": "trade-off",
}


# ── Helpers ─────────────────────────────────────────────────────────


def _parse_json_list(value: str | None) -> list[str]:
    """Parse a JSON array string, returning [] on failure."""
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _content_hash(narrative: str, facts_json: str) -> str:
    """Dedup key from narrative + raw facts JSON."""
    combined = (narrative or "") + (facts_json or "")
    return hashlib.sha256(combined.encode()).hexdigest()


def normalize_project(raw: str) -> str:
    """Map a claude-mem project name to a normalized vault project."""
    raw = (raw or "").strip()
    if raw in PROJECT_MAP:
        return PROJECT_MAP[raw]
    # Unknown project — keep as-is (will create a new project folder)
    return raw if raw else "_unscoped"


def _observation_tags(obs_type: str, concepts_json: str) -> list[str]:
    """Build tag list from observation type + meta-concepts."""
    tags: list[str] = []
    # observation type becomes a tag (except 'decision' — that maps to NoteType)
    if obs_type and obs_type != "decision":
        tags.append(obs_type)
    # Map useful meta-concepts to tags
    for concept in _parse_json_list(concepts_json):
        tag = META_CONCEPT_TO_TAG.get(concept)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


# ── Body builders ───────────────────────────────────────────────────


def build_observation_body(
    subtitle: str,
    narrative: str,
    facts_json: str,
    files_read_json: str,
    files_modified_json: str,
) -> str:
    """Build markdown body for a note-type observation."""
    parts: list[str] = []

    if subtitle:
        parts.append(subtitle)
        parts.append("")

    if narrative:
        parts.append("## Narrative")
        parts.append("")
        parts.append(narrative)
        parts.append("")

    facts = _parse_json_list(facts_json)
    if facts:
        parts.append("## Key Facts")
        parts.append("")
        for fact in facts:
            parts.append(f"- {fact}")
        parts.append("")

    files_r = _parse_json_list(files_read_json)
    files_m = _parse_json_list(files_modified_json)
    if files_r or files_m:
        parts.append("## Files")
        parts.append("")
        if files_r:
            parts.append(f"**Read**: {', '.join(files_r)}")
        if files_m:
            parts.append(f"**Modified**: {', '.join(files_m)}")
        parts.append("")

    return "\n".join(parts).rstrip()


def build_decision_body(
    subtitle: str,
    narrative: str,
    facts_json: str,
) -> str:
    """Build markdown body for a decision-type observation."""
    parts: list[str] = []

    if narrative:
        parts.append("## Context")
        parts.append("")
        parts.append(narrative)
        parts.append("")

    if subtitle:
        parts.append("## Decision")
        parts.append("")
        parts.append(subtitle)
        parts.append("")

    facts = _parse_json_list(facts_json)
    if facts:
        parts.append("## Key Facts")
        parts.append("")
        for fact in facts:
            parts.append(f"- {fact}")
        parts.append("")

    return "\n".join(parts).rstrip()


def build_session_body(summary: dict) -> str:
    """Build markdown body from a session_summaries row dict."""
    sections = [
        ("Request", "request"),
        ("Investigated", "investigated"),
        ("Learned", "learned"),
        ("Completed", "completed"),
        ("Next Steps", "next_steps"),
        ("Notes", "notes"),
    ]
    parts: list[str] = []
    for heading, key in sections:
        value = (summary.get(key) or "").strip()
        if value:
            parts.append(f"## {heading}")
            parts.append("")
            parts.append(value)
            parts.append("")

    return "\n".join(parts).rstrip()


# ── Data loading ────────────────────────────────────────────────────


def _load_observations(conn: sqlite3.Connection) -> list[dict]:
    """Load all observations as dicts."""
    rows = conn.execute(
        "SELECT * FROM observations ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]


def _load_session_summaries(conn: sqlite3.Connection) -> dict[str, dict]:
    """Load the latest session summary per memory_session_id."""
    rows = conn.execute(
        """SELECT * FROM session_summaries
           ORDER BY memory_session_id, created_at DESC"""
    ).fetchall()
    summaries: dict[str, dict] = {}
    for row in rows:
        sid = row["memory_session_id"]
        if sid not in summaries:
            summaries[sid] = dict(row)
    return summaries


def _build_session_map(
    observations: list[dict],
    summaries: dict[str, dict],
) -> dict[str, dict]:
    """Group observations by session; merge with summaries.

    Returns: {memory_session_id: {summary: dict|None, observations: [dict], project: str, earliest_date: str}}
    """
    sessions: dict[str, dict] = {}

    # Seed from observations
    for obs in observations:
        sid = obs["memory_session_id"]
        if sid not in sessions:
            sessions[sid] = {
                "summary": summaries.get(sid),
                "observations": [],
                "project": "",
                "earliest_date": obs["created_at"],
            }
        sessions[sid]["observations"].append(obs)
        # Use the first observation's date as earliest
        if obs["created_at"] < sessions[sid]["earliest_date"]:
            sessions[sid]["earliest_date"] = obs["created_at"]

    # Add summary-only sessions (no observations)
    for sid, summary in summaries.items():
        if sid not in sessions:
            sessions[sid] = {
                "summary": summary,
                "observations": [],
                "project": "",
                "earliest_date": summary["created_at"],
            }

    # Determine project for each session
    for sid, data in sessions.items():
        # Prefer the summary's project, then first observation's project
        raw_project = ""
        if data["summary"]:
            raw_project = data["summary"].get("project", "") or ""
        if not raw_project and data["observations"]:
            raw_project = data["observations"][0].get("project", "") or ""
        data["project"] = normalize_project(raw_project)

    return sessions


# ── Deduplication ───────────────────────────────────────────────────


def _deduplicate_observations(observations: list[dict]) -> list[dict]:
    """Remove duplicate observations (same narrative+facts within session)."""
    seen: set[str] = set()
    unique: list[str] = []
    removed = 0
    for obs in observations:
        h = _content_hash(obs.get("narrative", ""), obs.get("facts", ""))
        if h in seen:
            removed += 1
            continue
        seen.add(h)
        unique.append(obs)
    return unique


# ── Main import ─────────────────────────────────────────────────────


def import_claude_history(
    config: Config | None = None,
    db_path: Path | None = None,
    project_filter: str = "",
    dry_run: bool = False,
) -> dict:
    """Import observations and sessions from claude-mem into the vault.

    Args:
        config: Vault config (loaded from defaults if None).
        db_path: Path to claude-mem.db (defaults to ~/.claude-mem/claude-mem.db).
        project_filter: Only import sessions matching this (normalized) project name.
        dry_run: If True, print what would be imported without writing files.

    Returns:
        Stats dict: {sessions, notes, decisions, skipped, errors, deduped}.
    """
    config = config or load_config()
    db_path = db_path or _DEFAULT_CLAUDE_MEM_DB

    if not db_path.exists():
        return {"error": f"Database not found: {db_path}"}

    vm = VaultManager(config=config)
    vm.ensure_dirs()

    stats = {
        "sessions": 0,
        "notes": 0,
        "decisions": 0,
        "skipped": 0,
        "errors": 0,
        "deduped": 0,
    }

    # Load data from claude-mem
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        observations = _load_observations(conn)
        summaries = _load_session_summaries(conn)
    except sqlite3.OperationalError as e:
        conn.close()
        return {"error": f"Could not read claude-mem database: {e}"}

    conn.close()

    # Build session map
    session_map = _build_session_map(observations, summaries)

    # Load manifest for idempotency. Dry runs use a throwaway empty ledger so
    # counts reflect a from-scratch import regardless of prior on-disk state.
    manifest = (
        ImportManifest(config.weave_dir / _MANIFEST_NAME)
        if dry_run
        else ImportManifest.load(config.weave_dir, _MANIFEST_NAME)
    )
    imported_ids = manifest.ids
    written_paths: list[Path] = []

    # Dry-run stats accumulator
    dry_stats: dict[str, dict[str, int]] = {}  # project -> {type: count}

    # Sort sessions by earliest date
    sorted_sessions = sorted(session_map.items(), key=lambda x: x[1]["earliest_date"])

    for session_uuid, session_data in sorted_sessions:
        project = session_data["project"]

        # Apply project filter
        if project_filter and project != project_filter:
            stats["skipped"] += 1 + len(session_data["observations"])
            continue

        session_source_key = f"session-{session_uuid}"

        if dry_run:
            # Accumulate dry-run stats
            proj_stats = dry_stats.setdefault(project, {"session": 0, "note": 0, "decision": 0})
            proj_stats["session"] += 1
            for obs in session_data["observations"]:
                obs_type = obs.get("type", "discovery")
                if obs_type == "decision":
                    proj_stats["decision"] += 1
                else:
                    proj_stats["note"] += 1
            continue

        # ── Create session note ──

        if session_source_key in imported_ids:
            session_note_id = imported_ids[session_source_key]
            stats["skipped"] += 1
        else:
            summary = session_data["summary"]
            earliest = session_data["earliest_date"]

            # Aggregate files_touched from observations
            files_touched: list[str] = []
            seen_files: set[str] = set()
            for obs in session_data["observations"]:
                for field in ("files_read", "files_modified"):
                    for f in _parse_json_list(obs.get(field, "")):
                        if f and f not in seen_files:
                            files_touched.append(f)
                            seen_files.add(f)

            # Build session body
            if summary:
                session_title = f"Session {earliest[:10]}"
                session_body = build_session_body(summary)
            else:
                session_title = f"Session {earliest[:10]}"
                obs_count = len(session_data["observations"])
                session_body = f"Imported session ({obs_count} observation{'s' if obs_count != 1 else ''}, no summary available)."

            try:
                session_path = vm.create_note(
                    note_type=NoteType.SESSION,
                    title=session_title,
                    body=session_body,
                    project=project,
                    extra_frontmatter={
                        "date": earliest,
                        "source_session": session_uuid,
                        "imported_from": "claude-mem",
                        "source_id": session_source_key,
                        "files_touched": files_touched,
                        "processed": True,
                    },
                )
                session_note = vm.read_note(session_path)
                session_note_id = session_note.id
                imported_ids[session_source_key] = session_note_id
                written_paths.append(session_path)
                stats["sessions"] += 1
            except Exception as e:
                print(f"  ERROR creating session {session_uuid}: {e}")
                stats["errors"] += 1
                # Skip all observations in this session
                stats["skipped"] += len(session_data["observations"])
                continue

        # ── Create observation notes inside session folder ──
        # Use source_session UUID for folder lookup: the session folder
        # is named {source_session}-{date}, so _find_session_dir matches
        # by prefix on the UUID, not the vault-generated note ID.

        deduped_obs = _deduplicate_observations(session_data["observations"])
        stats["deduped"] += len(session_data["observations"]) - len(deduped_obs)

        for obs in deduped_obs:
            obs_source_key = f"obs-{obs['id']}"
            if obs_source_key in imported_ids:
                stats["skipped"] += 1
                continue

            obs_type = obs.get("type", "discovery")
            title = obs.get("title", "Untitled observation")
            subtitle = obs.get("subtitle", "") or ""
            narrative = obs.get("narrative", "") or ""
            facts_json = obs.get("facts", "[]") or "[]"
            files_read_json = obs.get("files_read", "[]") or "[]"
            files_modified_json = obs.get("files_modified", "[]") or "[]"
            concepts_json = obs.get("concepts", "[]") or "[]"
            created_at = obs.get("created_at", "")

            if obs_type == "decision":
                note_type = NoteType.DECISION
                body = build_decision_body(subtitle, narrative, facts_json)
                tags = _observation_tags(obs_type, concepts_json)
                extra_fm: dict = {
                    "date": created_at,
                    "status": DecisionStatus.ACCEPTED.value,
                    "imported_from": "claude-mem",
                    "source_id": obs_source_key,
                    "derived_from": [session_note_id],
                    "summary": subtitle[:200] if subtitle else title[:200],
                }
                file_paths = _parse_json_list(files_modified_json)
                if file_paths:
                    extra_fm["file_paths"] = file_paths
            else:
                note_type = NoteType.NOTE
                body = build_observation_body(
                    subtitle, narrative, facts_json,
                    files_read_json, files_modified_json,
                )
                tags = _observation_tags(obs_type, concepts_json)
                extra_fm = {
                    "date": created_at,
                    "imported_from": "claude-mem",
                    "source_id": obs_source_key,
                    "derived_from": [session_note_id],
                }

            try:
                path = vm.create_note(
                    note_type=note_type,
                    title=title,
                    body=body,
                    project=project,
                    tags=tags,
                    extra_frontmatter=extra_fm,
                    session_id=session_uuid,
                )
                imported_ids[obs_source_key] = vm.read_note(path).id
                written_paths.append(path)
                if obs_type == "decision":
                    stats["decisions"] += 1
                else:
                    stats["notes"] += 1
            except Exception as e:
                print(f"  ERROR importing obs {obs['id']} ({title[:50]}): {e}")
                stats["errors"] += 1

    if dry_run:
        # Print dry-run report
        total_sessions = 0
        total_notes = 0
        total_decisions = 0
        print("\n── Dry Run Report ──────────────────────────────────\n")
        for proj in sorted(dry_stats):
            ps = dry_stats[proj]
            total_sessions += ps["session"]
            total_notes += ps["note"]
            total_decisions += ps["decision"]
            total = ps["session"] + ps["note"] + ps["decision"]
            print(f"  {proj:30s}  {ps['session']:3d} sessions  {ps['note']:4d} notes  {ps['decision']:3d} decisions  ({total} total)")
        print(f"\n  {'TOTAL':30s}  {total_sessions:3d} sessions  {total_notes:4d} notes  {total_decisions:3d} decisions")
        print(f"\n  Total notes to create: {total_sessions + total_notes + total_decisions}")
        stats["sessions"] = total_sessions
        stats["notes"] = total_notes
        stats["decisions"] = total_decisions
        return stats

    # Save manifest
    manifest.set_meta(
        completed_at=datetime.now(timezone.utc).isoformat(),
        source_db=str(db_path),
    )
    manifest.save()

    # Index everything written this run in one pass (shared policy). This can
    # be thousands of notes; index_imported_notes touches only the written
    # paths (O(imported), not O(vault)) — see common.index_imported_notes.
    print("  Indexing imported notes...")
    idx_stats = index_imported_notes(config, written_paths)
    stats["indexed"] = idx_stats.get("indexed", 0)

    return stats
