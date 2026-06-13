"""Drop-folder intake primitives — enumerate + archive.

Used by ``/substack`` and any future drop-folder importer (``/email``,
``/podcasts``, …). Pure stdlib; no vault state involved.

Design rationale: these are pure mechanical helpers. LLM-decided work
(frontmatter parsing, brief writing, concept mapping) stays in the skill,
where Claude has the necessary judgment. Image backfill is intentionally
NOT here either — it's substack-CDN-specific (curl against
``substackcdn.com``, format-detection, path rewriting) and belongs with
the skill that knows about that platform. What lives here is exactly
what's portable across drop-folder importers: walking an inbox and
archiving processed entries.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

EntryKind = Literal["flat", "folder"]

# Companion-dir suffixes recognised next to a flat ``.md`` entry.
# ``-images`` is the MarkDownload convention; ``_assets`` shows up with
# some Obsidian Web Clipper variants. Order matters only in that the
# first hit wins.
_COMPANION_SUFFIXES: tuple[str, ...] = ("-images", "_assets")


@dataclass(frozen=True)
class InboxEntry:
    """One actionable item in a drop-folder inbox.

    Attributes:
        path: Absolute path to the ``.md`` file (flat) or directory (folder).
        kind: ``"flat"`` for a single ``.md`` file, ``"folder"`` for a bundle
            directory containing at least one ``.md``.
        companion_dir: For ``flat`` entries, the resolved
            ``<stem>-images/`` or ``<stem>_assets/`` sibling directory if
            present; ``None`` otherwise. Always ``None`` for ``folder``
            entries — folder bundles already contain their own assets.
    """

    path: Path
    kind: EntryKind
    companion_dir: Path | None


def enumerate_inbox(
    inbox: Path,
    *,
    archive_name: str = "_processed",
) -> list[InboxEntry]:
    """List actionable entries in ``inbox``, sorted alphabetically by name.

    Skips:
        - The archive folder (default ``_processed``).
        - Dotfiles / dot-directories.
        - Loose top-level files that are not ``.md``.
        - Top-level directories that contain no ``.md`` file.

    Args:
        inbox: Path to the inbox root.
        archive_name: Name of the archive sub-directory to skip.

    Returns:
        A possibly-empty list of :class:`InboxEntry`. Returns an empty list
        if ``inbox`` does not exist or is not a directory (matches the
        ``ls ... 2>/dev/null`` semantics the skill used previously).
    """

    if not inbox.exists() or not inbox.is_dir():
        return []

    entries: list[InboxEntry] = []
    children = sorted(inbox.iterdir(), key=lambda p: p.name)

    # Pre-compute the set of names that look like companion directories so
    # we can elide them from the top-level enumeration even if the .md
    # alphabetises after them.
    companion_names: set[str] = set()
    for child in children:
        if not child.is_file() or child.suffix != ".md":
            continue
        companion = _resolve_companion(child)
        if companion is not None:
            companion_names.add(companion.name)

    for child in children:
        name = child.name

        if name.startswith("."):
            continue
        if name == archive_name:
            continue
        if name in companion_names:
            continue

        if child.is_file():
            if child.suffix != ".md":
                continue
            entries.append(
                InboxEntry(
                    path=child.resolve(),
                    kind="flat",
                    companion_dir=_resolve_companion(child),
                )
            )
            continue

        if child.is_dir():
            if not _has_markdown(child):
                continue
            entries.append(
                InboxEntry(
                    path=child.resolve(),
                    kind="folder",
                    companion_dir=None,
                )
            )

    return entries


def archive_to_processed(
    entry_path: Path,
    inbox_root: Path,
    *,
    today: date | None = None,
) -> Path:
    """Move ``entry_path`` (and its companion dir, if any) into the dated
    archive folder beneath ``inbox_root``.

    The destination is ``<inbox_root>/_processed/<YYYY-MM-DD>/``. The dated
    folder is created on demand. On name collision inside the dated folder,
    the moved entry's basename is suffixed with ``-1``, ``-2``, … to keep
    the operation idempotent — re-running after a partial failure must
    never overwrite an existing archive.

    For flat entries, any sibling companion directory (``<stem>-images/``
    or ``<stem>_assets/``) is moved alongside, sharing the same suffix
    that was applied to the primary file (so the wikilink between them is
    preserved).

    Args:
        entry_path: Absolute path to the file or directory to archive.
        inbox_root: Inbox root; ``entry_path`` must live directly inside it.
        today: Optional date override (mostly for tests). Defaults to the
            local current date.

    Returns:
        The final archive path of the moved entry.

    Raises:
        FileNotFoundError: If ``entry_path`` does not exist.
        ValueError: If ``entry_path`` is not a direct child of ``inbox_root``.
    """

    entry = entry_path
    if not entry.exists():
        raise FileNotFoundError(f"Inbox entry does not exist: {entry}")

    entry_resolved = entry.resolve()
    inbox_resolved = inbox_root.resolve()

    if entry_resolved.parent != inbox_resolved:
        raise ValueError(
            f"Entry {entry_resolved} is not a direct child of inbox {inbox_resolved}"
        )

    today = today or date.today()
    dated_dir = inbox_resolved / "_processed" / today.isoformat()
    dated_dir.mkdir(parents=True, exist_ok=True)

    # Companion lookup before any move, so the rename suffix is stable.
    companion = None
    if entry_resolved.is_file() and entry_resolved.suffix == ".md":
        companion = _resolve_companion(entry_resolved)

    final_name, suffix = _allocate_unique_name(dated_dir, entry_resolved.name)
    final_path = dated_dir / final_name
    shutil.move(str(entry_resolved), str(final_path))

    if companion is not None and companion.exists():
        # Mirror the suffix onto the companion so paired names stay aligned
        # with the primary entry. If the mirrored name happens to clash
        # too, fall back to fresh suffix allocation on the companion side.
        companion_target_name = _apply_suffix(companion.name, suffix)
        if (dated_dir / companion_target_name).exists():
            companion_target_name, _ = _allocate_unique_name(
                dated_dir, companion.name
            )
        shutil.move(str(companion), str(dated_dir / companion_target_name))

    return final_path


# ---------------------------------------------------------------------------
# helpers


def _resolve_companion(md_file: Path) -> Path | None:
    """Return the ``<stem>-images/`` or ``<stem>_assets/`` sibling of an
    ``.md`` entry, if one exists as a directory. ``None`` otherwise.
    """
    stem = md_file.stem
    parent = md_file.parent
    for suffix in _COMPANION_SUFFIXES:
        candidate = parent / f"{stem}{suffix}"
        if candidate.is_dir():
            return candidate
    return None


def _has_markdown(directory: Path) -> bool:
    """True if ``directory`` contains at least one ``.md`` file at the top
    level. Does not recurse — folder bundles keep their markdown alongside
    their assets.
    """
    try:
        for child in directory.iterdir():
            if child.is_file() and child.suffix == ".md":
                return True
    except OSError:
        return False
    return False


def _allocate_unique_name(target_dir: Path, desired_name: str) -> tuple[str, int]:
    """Return a name that does not yet exist inside ``target_dir``.

    If ``desired_name`` is free, returns it unchanged with suffix ``0``.
    Otherwise tries ``<stem>-1<ext>``, ``<stem>-2<ext>``, … and returns the
    first free name plus the numeric suffix that was applied. For
    directories (no extension) the suffix is appended to the bare name.
    """
    if not (target_dir / desired_name).exists():
        return desired_name, 0

    stem, ext = _split_name(desired_name)
    n = 1
    while True:
        candidate = f"{stem}-{n}{ext}"
        if not (target_dir / candidate).exists():
            return candidate, n
        n += 1


def _apply_suffix(name: str, suffix: int) -> str:
    """Apply numeric ``suffix`` to ``name`` the same way
    :func:`_allocate_unique_name` would. ``suffix=0`` returns ``name``
    unchanged.
    """
    if suffix == 0:
        return name
    stem, ext = _split_name(name)
    return f"{stem}-{suffix}{ext}"


def _split_name(name: str) -> tuple[str, str]:
    """Split a basename into ``(stem, ext)``. For names without an
    extension (directories), ``ext`` is empty.
    """
    p = Path(name)
    if p.suffix:
        return p.stem, p.suffix
    return name, ""
