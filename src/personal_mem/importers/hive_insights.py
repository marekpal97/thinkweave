"""Import insights from hive_swarm knowledge store.

Reads .hive/knowledge/ session learnings and insights.
"""

from __future__ import annotations

import json
from pathlib import Path

from personal_mem.config import Config, load_config
from personal_mem.indexer import Indexer
from personal_mem.schemas import NoteType
from personal_mem.vault import VaultManager, content_hash


def import_hive_insights(
    config: Config | None = None,
    hive_dir: Path | None = None,
    project: str = "",
) -> dict:
    """Import insights from hive_swarm knowledge store.

    Looks for session learnings and insights in .hive/knowledge/.
    """
    config = config or load_config()

    # Search common locations for hive knowledge
    search_paths = []
    if hive_dir:
        search_paths.append(hive_dir)
    search_paths.extend([
        Path.home() / ".hive" / "knowledge",
        Path.cwd() / ".hive" / "knowledge",
    ])

    vm = VaultManager(config=config)
    vm.ensure_dirs()
    idx = Indexer(config=config)

    stats = {"imported": 0, "skipped": 0}

    # Track existing content
    existing_hashes: set[str] = set()
    for note in vm.list_notes(limit=10000):
        existing_hashes.add(content_hash(note.body))

    for knowledge_dir in search_paths:
        if not knowledge_dir.exists():
            continue

        # Import session learnings
        for learnings_file in knowledge_dir.glob("**/session_learnings.md"):
            _import_learnings_file(
                learnings_file, vm, idx, existing_hashes, stats, project, config
            )

        # Import from sessions.jsonl
        sessions_file = knowledge_dir / "sessions.jsonl"
        if sessions_file.exists():
            _import_sessions_jsonl(
                sessions_file, vm, idx, existing_hashes, stats, project, config
            )

    idx.close()
    return stats


def _import_learnings_file(
    path: Path,
    vm: VaultManager,
    idx: Indexer,
    existing_hashes: set[str],
    stats: dict,
    project: str,
    config: Config,
) -> None:
    text = path.read_text(encoding="utf-8")
    chash = content_hash(text)

    if chash in existing_hashes:
        stats["skipped"] += 1
        return

    # Use parent directory name as context
    session_dir = path.parent.name
    title = f"Hive session learnings — {session_dir}"

    note_path = vm.create_note(
        NoteType.NOTE,
        title=title,
        body=text,
        project=project,
        tags=["hive-swarm", "session-learnings"],
        extra_frontmatter={"imported_from": "hive_swarm"},
    )

    idx.index_file(note_path)
    existing_hashes.add(chash)
    stats["imported"] += 1


def _import_sessions_jsonl(
    path: Path,
    vm: VaultManager,
    idx: Indexer,
    existing_hashes: set[str],
    stats: dict,
    project: str,
    config: Config,
) -> None:
    for line in path.read_text(encoding="utf-8").strip().split("\n"):
        if not line.strip():
            continue
        try:
            session = json.loads(line)
        except json.JSONDecodeError:
            continue

        summary = session.get("summary", "")
        if not summary:
            continue

        chash = content_hash(summary)
        if chash in existing_hashes:
            stats["skipped"] += 1
            continue

        session_id = session.get("session_id", "unknown")
        title = f"Hive session — {session_id[:12]}"

        note_path = vm.create_note(
            NoteType.NOTE,
            title=title,
            body=summary,
            project=project,
            tags=["hive-swarm", "session-summary"],
            extra_frontmatter={"imported_from": "hive_swarm", "hive_session_id": session_id},
        )

        idx.index_file(note_path)
        existing_hashes.add(chash)
        stats["imported"] += 1
