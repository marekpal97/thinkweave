"""Graph operations — unified `walk(filter=...)` over typed edges.

Folds three previously separate MCP tools into one filter-dispatched walk:
- ``filter='source_lens'`` (was mem_source_lens)
- ``filter='decisions_for_file'`` (was mem_decisions_for_file)
- ``filter='concept_walk'`` (was mem_concept_search)
- ``filter=''`` (default — render local graph from a center note)
"""

from __future__ import annotations

from personal_mem.core.config import Config


def walk(
    cfg: Config,
    *,
    filter: str = "",
    note_id: str = "",
    depth: int = 2,
    edge_types: list[str] | None = None,
    note_type: str = "",
    project: str = "",
    file_path: str = "",
    source_id: str = "",
    concepts: list[str] | None = None,
    status: str = "",
    match_mode: str = "any",
    min_matches: int = 0,
    since: str = "",
    until: str = "",
    limit: int = 50,
):
    """Unified graph walk.

    Returns a structure shaped per filter:
    - ``''`` (default): rendered text via Search.render_graph_text, OR a list
      of `RelatedNode` when projection filters are present.
    - ``'source_lens'``: dict from `Search.get_source_lens`.
    - ``'decisions_for_file'``: list of `SearchResult`.
    - ``'concept_walk'``: list of `SearchResult` from concept-set ops.
    """
    from personal_mem.retrieval.search import Search

    s = Search(config=cfg)
    try:
        if filter == "source_lens":
            return s.get_source_lens(source_id, limit=limit)

        if filter == "decisions_for_file":
            return s.search_decisions_by_file(
                file_path, project=project, status=status, limit=limit
            )

        if filter == "concept_walk":
            return s.search_by_concept(
                concept=concepts or [],
                project=project,
                note_type=note_type,
                limit=limit,
                match_mode=match_mode,
                min_matches=min_matches,
                since=since,
                until=until,
            )

        # Default — local graph walk
        if not note_type and not project:
            return s.render_graph_text(note_id, depth=depth)

        return s.get_related(
            note_id,
            depth=depth,
            edge_types=edge_types,
            note_type=note_type,
            project=project,
        )
    finally:
        s.close()


def render_text(cfg: Config, note_id: str, *, depth: int = 2) -> str:
    from personal_mem.retrieval.search import Search

    s = Search(config=cfg)
    try:
        return s.render_graph_text(note_id, depth=depth)
    finally:
        s.close()


def render_mermaid(cfg: Config, note_id: str, *, depth: int = 2) -> str:
    from personal_mem.retrieval.search import Search

    s = Search(config=cfg)
    try:
        return s.render_graph_mermaid(note_id, depth=depth)
    finally:
        s.close()
