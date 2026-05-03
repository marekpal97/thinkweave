"""Note operations — create / read / update / link.

Pure functions used by both CLI handlers and MCP tool implementations. The
`VaultManager` and `Indexer` classes still own the I/O; this module is the
single, narrow seam that both surfaces call into.
"""

from __future__ import annotations

from pathlib import Path

from personal_mem.core.config import Config
from personal_mem.core.indexer import EDGE_TYPE_TO_FIELD, Indexer
from personal_mem.core.schemas import NoteMeta, NoteType
from personal_mem.core.vault import VaultManager


def create_note(
    cfg: Config,
    *,
    note_type: NoteType,
    title: str,
    body: str = "",
    project: str = "",
    tags: list[str] | None = None,
    extra_frontmatter: dict | None = None,
    session_id: str = "",
    output_dir: Path | None = None,
) -> NoteMeta:
    """Create a note and incrementally index it. Returns the parsed NoteMeta."""
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()

    path = vm.create_note(
        note_type=note_type,
        title=title,
        body=body,
        project=project,
        tags=tags,
        extra_frontmatter=extra_frontmatter,
        session_id=session_id,
        output_dir=output_dir,
    )

    idx = Indexer(config=cfg)
    idx.index_file(path)
    idx.close()

    return vm.read_note(path)


def read_note(cfg: Config, note_id: str) -> tuple[NoteMeta | None, str | None]:
    """Read a note by id. Returns (NoteMeta, raw_text) — or (None, None) if missing."""
    from personal_mem.retrieval.search import Search

    s = Search(config=cfg)
    row = s.get_note_by_id(note_id)
    s.close()
    if not row:
        return None, None

    vm = VaultManager(config=cfg)
    full_path = vm.root / row["path"]
    if not full_path.exists():
        return vm.read_note(full_path) if False else None, None

    return vm.read_note(full_path), full_path.read_text(encoding="utf-8")


def update_note(
    cfg: Config,
    note_id: str,
    *,
    frontmatter_updates: dict | None = None,
    body_append: str = "",
    remove_tags: list[str] | None = None,
) -> NoteMeta:
    """Update a note's frontmatter / body. Re-indexes. Raises ValueError on bad input."""
    if not (frontmatter_updates or body_append or remove_tags):
        raise ValueError("Nothing to update.")

    idx = Indexer(config=cfg)
    row = idx.db.execute("SELECT path FROM notes WHERE id = ?", (note_id,)).fetchone()
    idx.close()
    if not row:
        raise FileNotFoundError(f"Note {note_id} not found")

    vm = VaultManager(config=cfg)
    path = vm.root / row["path"]
    if not path.exists():
        raise FileNotFoundError(f"File missing for {note_id}: {row['path']}")

    vm.update_note(
        path,
        frontmatter_updates=frontmatter_updates,
        body_append=body_append,
        remove_tags=remove_tags,
    )
    idx2 = Indexer(config=cfg)
    idx2.index_file(path)
    idx2.close()
    return vm.read_note(path)


def link_notes(cfg: Config, source_id: str, target_id: str, edge_type: str) -> None:
    """Add a typed edge from source to target."""
    idx = Indexer(config=cfg)
    src = idx.db.execute("SELECT path FROM notes WHERE id = ?", (source_id,)).fetchone()
    tgt = idx.db.execute("SELECT id FROM notes WHERE id = ?", (target_id,)).fetchone()
    if not src:
        idx.close()
        raise FileNotFoundError(f"Source note {source_id} not found")
    if not tgt:
        idx.close()
        raise FileNotFoundError(f"Target note {target_id} not found")

    vm = VaultManager(config=cfg)
    fm_field = EDGE_TYPE_TO_FIELD[edge_type]
    source_path = vm.root / src["path"]
    vm.update_note(source_path, frontmatter_updates={fm_field: [target_id]})
    idx.index_file(source_path)
    idx.close()


def unlink_notes(cfg: Config, source_id: str, target_id: str, edge_type: str) -> bool:
    """Remove a typed edge. Returns True if removed, False if no matching edge."""
    from personal_mem.core.vault import parse_frontmatter, render_frontmatter

    idx = Indexer(config=cfg)
    src = idx.db.execute("SELECT path FROM notes WHERE id = ?", (source_id,)).fetchone()
    if not src:
        idx.close()
        raise FileNotFoundError(f"Source note {source_id} not found")

    vm = VaultManager(config=cfg)
    source_path = vm.root / src["path"]
    note = vm.read_note(source_path)
    fm_field = EDGE_TYPE_TO_FIELD[edge_type]
    targets = note.frontmatter.get(fm_field, [])
    if isinstance(targets, str):
        targets = [targets] if targets else []
    if target_id not in targets:
        idx.close()
        return False

    new_targets = [t for t in targets if t != target_id]
    text = source_path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    if new_targets:
        fm[fm_field] = new_targets
    else:
        fm.pop(fm_field, None)
    source_path.write_text(render_frontmatter(fm) + "\n\n" + body, encoding="utf-8")
    idx.index_file(source_path)
    idx.close()
    return True
