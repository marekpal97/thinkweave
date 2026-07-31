"""Import text files (podcast transcripts, articles) as source notes."""

from __future__ import annotations

from pathlib import Path

from thinkweave.acquisition.importers.common import index_imported_notes
from thinkweave.acquisition.sources import build_source_frontmatter
from thinkweave.core.config import Config, load_config
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager


def import_transcript(
    config: Config | None = None,
    file_path: Path = Path(),
    source_type: str = "article",
    project: str = "",
    title: str = "",
    url: str = "",
    authors: list[str] | None = None,
    tags: list[str] | None = None,
) -> Path:
    """Import a text file as a source note in the vault.

    Returns the path to the created note.
    """
    config = config or load_config()
    vm = VaultManager(config=config)
    vm.ensure_dirs()

    text = file_path.read_text(encoding="utf-8")

    # Derive title from first line if not provided
    if not title:
        first_line = text.split("\n")[0].strip()
        if first_line.startswith("#"):
            title = first_line.lstrip("#").strip()
        else:
            title = first_line[:60]
            if len(first_line) > 60:
                title += "..."

    extra_fm = build_source_frontmatter(
        source_type=source_type,
        title=title,
        url=url,
        authors=authors,
    )

    note_tags = tags or [source_type]

    path = vm.create_note(
        NoteType.SOURCE,
        title=title,
        body=text,
        project=project,
        tags=note_tags,
        extra_frontmatter=extra_fm,
    )

    # Index it (shared end-of-run bulk policy; see common.index_imported_notes).
    index_imported_notes(config, [path])

    return path
