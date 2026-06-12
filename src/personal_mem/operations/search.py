"""Search / retrieval operations.

Wraps the `retrieval.search.Search` class behind narrow callables. Both the
CLI search handler and the `mem_search` MCP tool delegate here.

Also hosts cross-cutting read-only retrievals over primitives that live
outside the SQLite index — e.g. :func:`query_prompts`, which walks
session JSONL buffers populated by the UserPromptSubmit hook (Phase 4 E).
"""

from __future__ import annotations

from datetime import datetime

from personal_mem.core.config import Config


def query_fts(
    cfg: Config,
    query: str,
    *,
    note_type: str | list[str] = "",
    project: str = "",
    tags: list[str] | None = None,
    concepts: list[str] | None = None,
    since: str = "",
    until: str = "",
    limit: int = 10,
):
    from personal_mem.retrieval.search import Search

    s = Search(config=cfg)
    try:
        return s.search(
            query=query,
            note_type=note_type,
            project=project,
            tags=tags,
            concepts=concepts,
            since=since,
            until=until,
            limit=limit,
        )
    finally:
        s.close()


def query_similar(
    cfg: Config,
    query: str,
    *,
    note_type: str | list[str] = "",
    project: str = "",
    limit: int = 10,
):
    from personal_mem.retrieval.search import Search

    s = Search(config=cfg)
    try:
        return s.similar(query, project=project, note_type=note_type, limit=limit)
    finally:
        s.close()


def query_hybrid(
    cfg: Config,
    query: str,
    *,
    note_type: str | list[str] = "",
    project: str = "",
    limit: int = 10,
):
    from personal_mem.retrieval.search import Search

    s = Search(config=cfg)
    try:
        return s.hybrid_search(query, project=project, note_type=note_type, limit=limit)
    finally:
        s.close()


def query_context(
    cfg: Config,
    *,
    project: str = "",
    tags: list[str] | None = None,
    query: str = "",
    concepts: list[str] | None = None,
    note_type: str = "",
    since: str = "",
    until: str = "",
    limit: int = 5,
):
    from personal_mem.retrieval.search import Search

    s = Search(config=cfg)
    try:
        return s.get_context(
            project=project,
            tags=tags,
            query=query,
            concepts=concepts,
            note_type=note_type,
            since=since,
            until=until,
            limit=limit,
        )
    finally:
        s.close()


def _serialize_prompt(p) -> dict:
    return {
        "ts": p.ts.isoformat() if p.ts != datetime.min else "",
        "text": p.text,
        "session_id": p.session_id,
        "project": p.project,
        "cwd": p.cwd,
        "classification": p.classification,
    }


def _passes_since(p, since: str | None) -> bool:
    if not since:
        return True
    try:
        cutoff = datetime.fromisoformat(since)
    except (ValueError, TypeError):
        return True
    if p.ts == datetime.min:
        return False
    if cutoff.tzinfo is None and p.ts.tzinfo is not None:
        return p.ts.replace(tzinfo=None) >= cutoff
    if cutoff.tzinfo is not None and p.ts.tzinfo is None:
        return p.ts >= cutoff.replace(tzinfo=None)
    return p.ts >= cutoff


def _project_buffer_session_ids(cfg: Config, project: str) -> set[str]:
    """Return Claude Code session UUIDs whose session note maps to ``project``.

    Used to scope active ``.mem/buffer/<uuid>.jsonl`` files so we never
    bleed prompts across projects.

    Goes through the SQLite index (one query). The pre-2026-06-09 variant
    crawled the vault via ``VaultManager.list_notes`` — which reads every
    note file to filter by type — and ``recent_probe_pressure``'s
    vault-wide scope multiplied that by the project count: ~88k file
    reads / 6+ minutes per dream scan on a /mnt/c WSL vault. The vault
    crawl remains as the fallback for index-less vaults.
    """
    out: set[str] = set()

    # Fast path: the index. Session notes are indexed with their full
    # frontmatter blob; source_session is a plain key on it. Trust the
    # index whenever it knows about ANY session note — only a vault whose
    # index has never seen a session (fresh install, index not yet built)
    # falls through to the legacy walk.
    try:
        from personal_mem.core.indexer import Indexer

        idx = Indexer(config=cfg)
        try:
            n_sessions = idx.db.execute(
                "SELECT COUNT(*) FROM notes WHERE type = 'session'"
            ).fetchone()[0]
            rows = idx.db.execute(
                "SELECT json_extract(frontmatter, '$.source_session') "
                "  FROM notes WHERE type = 'session' AND project = ?",
                (project,),
            ).fetchall()
        finally:
            idx.close()
        if n_sessions:
            for row in rows:
                if row[0]:
                    out.add(str(row[0]))
            return out
    except Exception:
        pass

    # Fallback: index unavailable → bounded vault walk (legacy path).
    try:
        from personal_mem.core.schemas import NoteType
        from personal_mem.core.vault import VaultManager

        vm = VaultManager(config=cfg)
        for note in vm.list_notes(note_type=NoteType.SESSION, limit=500):
            if note.project != project:
                continue
            src = note.frontmatter.get("source_session", "")
            if src:
                out.add(str(src))
    except Exception:
        pass
    return out


def query_prompts(
    cfg: Config,
    project: str,
    since: str | None = None,
    limit: int = 50,
    classified_as: str | None = None,
) -> list[dict]:
    """Return user prompts captured for ``project`` as serialized dicts.

    Reads two sources, ordered by ``ts`` descending:

    1. Archived ``vault/projects/<project>/sessions/*/events.jsonl``
    2. Active ``.mem/buffer/<session_uuid>.jsonl`` files whose owning
       Claude Code session maps to a session note in this project.

    Args:
        cfg: vault config (drives roots).
        project: project to scope to. Required.
        since: optional ISO date / datetime cutoff (inclusive).
        limit: max items returned after sorting by recency.
        classified_as: optional classification filter (e.g. ``"probe"``);
            keeps only prompts whose ``Prompt.classification`` matches.

    Returns:
        List of dicts with ``ts``, ``text``, ``session_id``, ``project``,
        ``cwd``, ``classification``. Read-only. Phase 4 H's ``/discover``
        consumes this to prioritise gap-analysis on what the user has
        actually been asking.
    """
    from personal_mem.core.events import extract_prompts

    if not project:
        return []

    out = []

    sessions_root = cfg.vault_root / "projects" / project / "sessions"
    if sessions_root.exists():
        for sess_dir in sessions_root.iterdir():
            if not sess_dir.is_dir():
                continue
            events_file = sess_dir / "events.jsonl"
            if not events_file.exists():
                continue
            for prompt in extract_prompts(events_file):
                if not _passes_since(prompt, since):
                    continue
                if classified_as and prompt.classification != classified_as:
                    continue
                if prompt.project is None:
                    prompt.project = project
                out.append(prompt)

    buffer_root = cfg.mem_dir / "buffer"
    if buffer_root.exists():
        project_uuids = _project_buffer_session_ids(cfg, project)
        for buf_file in buffer_root.glob("*.jsonl"):
            if buf_file.stem not in project_uuids:
                continue
            for prompt in extract_prompts(buf_file):
                if not _passes_since(prompt, since):
                    continue
                if classified_as and prompt.classification != classified_as:
                    continue
                if prompt.project is None:
                    prompt.project = project
                out.append(prompt)

    out.sort(key=lambda p: p.ts, reverse=True)
    if limit and limit > 0:
        out = out[:limit]
    return [_serialize_prompt(p) for p in out]
