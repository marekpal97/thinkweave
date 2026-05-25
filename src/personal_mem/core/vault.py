"""VaultManager — note CRUD, template rendering, wikilink resolution.

The vault is a directory of markdown files with YAML frontmatter.
This module handles reading, writing, and querying notes at the file level.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from personal_mem.core.config import Config, load_config
from personal_mem.core.schemas import NOTE_ID_PREFIXES, DecisionStatus, NoteMeta, NoteType
from personal_mem.sources import registry as source_registry

# --- YAML frontmatter parsing (inline, no PyYAML dependency) ---

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from a markdown string.

    Returns (frontmatter_dict, body_text).
    Handles flat key-value pairs and simple lists (- item).
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text

    raw_yaml, body = m.group(1), m.group(2)
    result: dict = {}
    current_key: str | None = None
    current_list: list[str] | None = None

    for line in raw_yaml.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # List item continuation
        if stripped.startswith("- ") and current_key is not None and current_list is not None:
            raw_item = stripped[2:].strip()
            if raw_item.startswith("{") and raw_item.endswith("}"):
                try:
                    current_list.append(json.loads(raw_item))
                except json.JSONDecodeError:
                    current_list.append(raw_item.strip("\"'"))
            else:
                current_list.append(raw_item.strip("\"'"))
            result[current_key] = current_list
            continue

        # Key-value pair
        if ":" in stripped:
            # Flush any pending list
            current_list = None

            colon_idx = stripped.index(":")
            key = stripped[:colon_idx].strip()
            value = stripped[colon_idx + 1 :].strip()

            if not value:
                # Could be start of a list or a nested map — set up for list
                current_key = key
                current_list = []
                result[key] = ""
                continue

            current_key = key

            # Inline list: [item1, item2]
            if value.startswith("[") and value.endswith("]"):
                items = value[1:-1]
                if items.strip():
                    result[key] = [
                        item.strip().strip("\"'") for item in items.split(",")
                    ]
                else:
                    result[key] = []
                current_list = None
                continue

            # Boolean
            if value.lower() in ("true", "yes"):
                result[key] = True
                continue
            if value.lower() in ("false", "no"):
                result[key] = False
                continue

            # Numeric
            try:
                result[key] = int(value)
                continue
            except ValueError:
                pass
            try:
                result[key] = float(value)
                continue
            except ValueError:
                pass

            # String (strip quotes)
            result[key] = value.strip("\"'")

    return result, body


def render_frontmatter(data: dict) -> str:
    """Render a dict as YAML frontmatter string (between --- delimiters)."""
    lines = ["---"]
    for key, value in data.items():
        if value is None or value == "":
            continue
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            elif len(value) <= 3 and all(isinstance(v, str) and "," not in v for v in value):
                # Inline list for short lists
                items = ", ".join(str(v) for v in value)
                lines.append(f"{key}: [{items}]")
            else:
                lines.append(f"{key}:")
                for item in value:
                    if isinstance(item, dict):
                        lines.append(f"  - {json.dumps(item, separators=(', ', ': '))}")
                    else:
                        lines.append(f"  - {item}")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, dict):
            lines.append(f"{key}:")
            for k, v in value.items():
                if v is not None and v != "":
                    lines.append(f"  {k}: {v}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        else:
            # Quote strings containing special chars
            s = str(value)
            if any(c in s for c in (":", "#", "[", "]", "{", "}")):
                lines.append(f'{key}: "{s}"')
            else:
                lines.append(f"{key}: {s}")
    lines.append("---")
    return "\n".join(lines)


def extract_wikilinks(text: str) -> list[str]:
    """Extract all [[wikilink]] targets from markdown body."""
    return _WIKILINK_RE.findall(text)


def strip_section(body: str, heading: str) -> str:
    """Remove a markdown section (heading + content until next ## heading or EOF)."""
    if heading not in body:
        return body
    before = body[: body.index(heading)]
    after_heading = body[body.index(heading) + len(heading) :]
    m = re.search(r"\n## ", after_heading)
    if m:
        after = after_heading[m.start() :]
    else:
        after = ""
    return (before.rstrip() + "\n" + after).strip() + "\n"


def content_hash(text: str) -> str:
    """SHA-256 hash of content for change detection."""
    return hashlib.sha256(text.encode()).hexdigest()


# --- VaultManager ---


class VaultManager:
    """Manages note CRUD operations in the Obsidian vault."""

    def __init__(self, config: Config | None = None):
        self.config = config or load_config()
        self.root = self.config.vault_root

    def ensure_dirs(self) -> None:
        """Create vault directory structure if it doesn't exist."""
        dirs = [
            self.root / ".mem",
            self.root / "projects",
            self.root / "daily",
            self.root / "sources",
            self.root / "sources" / "papers",
            self.root / "sources" / "repos",
            self.root / "sources" / "articles",
            self.root / "sources" / "conversations",
            self.root / "templates",
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)

    def generate_id(self, note_type: NoteType) -> str:
        """Generate a unique note ID with type prefix."""
        prefix = NOTE_ID_PREFIXES[note_type]
        short_uuid = uuid.uuid4().hex[:8]
        return f"{prefix}-{short_uuid}"

    def _note_dir(
        self,
        note_type: NoteType,
        project: str = "",
        output_dir: Path | None = None,
    ) -> Path:
        """Determine the directory for a note based on type and project.

        Args:
            output_dir: When provided, bypasses all routing and uses this
                directory directly (e.g. placing derived notes inside a
                session folder during extraction).
        """
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            return output_dir

        if note_type == NoteType.SOURCE:
            if project:
                d = self.root / "projects" / project / "sources"
                d.mkdir(parents=True, exist_ok=True)
                return d
            return self.root / "sources"

        if note_type == NoteType.THEME:
            # Themes are global narratives addressable from any project;
            # they never live under projects/{project}/. The `project:`
            # frontmatter field on a theme is informational (primary
            # stake), not a filing rule.
            d = self.root / "themes"
            d.mkdir(parents=True, exist_ok=True)
            return d

        if project:
            if note_type == NoteType.SESSION:
                d = self.root / "projects" / project / "sessions"
            else:
                # Notes and decisions live in session folders;
                # misc/ is the catch-all for standalone content.
                d = self.root / "projects" / project / "sessions" / "misc"
            d.mkdir(parents=True, exist_ok=True)
            return d

        if note_type == NoteType.SESSION:
            d = self.root / "projects" / "_unscoped" / "sessions"
            d.mkdir(parents=True, exist_ok=True)
            return d
        return self.root / "projects"

    # Source-type routing is declared in ``personal_mem.sources.registry``.
    # Adding a new source type means adding a SourceTypeSpec entry there and
    # writing a skill under commands/; no edits in this file are required.

    def _normalize_source_type(self, source_type: str) -> str:
        """Fold legacy aliases into the canonical source_type vocabulary.

        Consults the user-side overlay at
        ``<vault_root>/.mem/source_types.yaml`` first, then the in-code
        REGISTRY.
        """
        return source_registry.normalize(source_type, vault_root=self.root)

    def _source_bucket(self, source_type: str) -> str:
        """Return the bucket subfolder for a given source_type, or ''."""
        spec = source_registry.get_spec(source_type, vault_root=self.root)
        return spec.bucket if spec else ""

    def _sanitize_filename(self, title: str) -> str:
        """Convert a title to a safe filename slug."""
        slug = title.lower().strip()
        slug = re.sub(r"[^\w\s-]", "", slug)
        slug = re.sub(r"[\s_]+", "-", slug)
        slug = slug.strip("-")
        return slug[:80] if slug else "untitled"

    def _find_session_dir(self, project: str, session_id: str) -> Path:
        """Find or create a session folder by session note ID or source_session UUID.

        Searches by folder name prefix first, then falls back to checking
        source_session in session.md frontmatter. If no match is found,
        creates the folder eagerly so notes created mid-session land in
        the right place before the session note is written at wrap time.
        """
        sessions_dir = self.root / "projects" / project / "sessions"
        if sessions_dir.exists():
            for d in sessions_dir.iterdir():
                if not d.is_dir() or d.name == "misc":
                    continue
                # Direct prefix match (works for both ses-xxxx and UUID folder names)
                if d.name.startswith(session_id):
                    return d
                # Check source_session in session.md frontmatter
                sm = d / "session.md"
                if sm.exists():
                    try:
                        fm, _ = parse_frontmatter(sm.read_text(encoding="utf-8"))
                        if fm.get("source_session") == session_id:
                            return d
                    except Exception:
                        continue
        # Create eagerly — session.md will be added at wrap/stop time
        today = date.today().isoformat()
        session_dir = sessions_dir / f"{session_id}-{today}"
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir

    def _write_source_flat(self, target_dir: Path, slug: str) -> Path:
        """Flat layout — single file ``<slug>.md`` with file-level collision loop.

        Used by source types like ``conversation`` whose notes are
        self-contained summaries without raw companion content.
        """
        filepath = target_dir / f"{slug}.md"
        counter = 1
        while filepath.exists():
            filepath = target_dir / f"{slug}-{counter}.md"
            counter += 1
        return filepath

    def _write_source_folder(self, target_dir: Path, slug: str) -> Path:
        """Folder layout — ``<slug>/source.md`` with directory-level collision loop.

        The default layout for most source types. The slug subdirectory
        holds ``source.md`` plus any raw companion content (``raw.md``,
        ``paper.pdf``, ``snapshot.md``, ``assets/``) the skill writes
        alongside it.
        """
        source_subdir = target_dir / slug
        counter = 1
        while source_subdir.exists():
            source_subdir = target_dir / f"{slug}-{counter}"
            counter += 1
        source_subdir.mkdir(parents=True, exist_ok=True)
        return source_subdir / "source.md"

    def _write_source_author_folder(
        self, target_dir: Path, slug: str, fm: dict
    ) -> Path:
        """Author-nested folder layout — ``<author>/<slug>/source.md``.

        Used by substack (and similar newsletter sources) so each
        publication's corpus clusters under one folder. When ``author`` is
        missing or empty, falls back to the plain folder layout without the
        author level — tested by ``test_substack_missing_author_falls_back_flat``.
        """
        author = fm.get("author", "") or ""
        if not author:
            return self._write_source_folder(target_dir, slug)
        author_slug = self._sanitize_filename(author)
        author_dir = target_dir / author_slug
        author_dir.mkdir(parents=True, exist_ok=True)
        return self._write_source_folder(author_dir, slug)

    def create_note(
        self,
        note_type: NoteType,
        title: str,
        body: str = "",
        project: str = "",
        tags: list[str] | None = None,
        extra_frontmatter: dict | None = None,
        output_dir: Path | None = None,
        session_id: str = "",
    ) -> Path:
        """Create a new note file in the vault. Returns the file path.

        Args:
            output_dir: When provided, place the note in this directory
                instead of the default type-based location. Used by
                extraction to put derived notes inside a session folder.
            session_id: When provided, place the note in this session's
                folder instead of the default misc/ catch-all.
        """
        note_id = self.generate_id(note_type)
        today = datetime.now(timezone.utc).isoformat()
        project = project or self.config.default_project

        # Resolve session_id to output_dir if provided
        if session_id and not output_dir and project:
            resolved = self._find_session_dir(project, session_id)
            if resolved:
                output_dir = resolved

        # Build frontmatter
        fm: dict = {
            "type": note_type.value,
            "id": note_id,
            "date": today,
        }
        if tags:
            fm["tags"] = tags
        if project:
            fm["project"] = project

        # Type-specific defaults
        if note_type == NoteType.SESSION:
            fm["files_touched"] = []
            fm["context"] = {"prompt": "", "plan": {}}
        elif note_type == NoteType.DECISION:
            fm["status"] = DecisionStatus.PROPOSED.value
        elif note_type == NoteType.SOURCE:
            fm["source_type"] = ""
            fm["title"] = title
            fm["url"] = ""
            fm["authors"] = []

        if extra_frontmatter:
            fm.update(extra_frontmatter)

        # Obsidian resolves [[note-id]] wikilinks by filename or alias, never by
        # the frontmatter `id:` field. Notes are filed by slug, so without this
        # alias every [[n-XXX]] / [[dec-XXX]] / [[src-XXX]] click in a hub or
        # See-Also list would create a phantom file at vault root.
        existing_aliases = fm.get("aliases") or []
        if not isinstance(existing_aliases, list):
            existing_aliases = [existing_aliases]
        if note_id not in existing_aliases:
            fm["aliases"] = [note_id, *existing_aliases]
        else:
            fm["aliases"] = existing_aliases

        # Determine file path
        target_dir = self._note_dir(note_type, project, output_dir=output_dir)
        slug = self._sanitize_filename(title)

        if note_type == NoteType.SESSION:
            # Sessions get their own subdirectory: sessions/{id}-{date}/session.md
            # Check if an eagerly-created folder exists for this source_session
            source_session = (extra_frontmatter or {}).get("source_session", "")
            session_subdir = None
            if source_session and project:
                candidate = self._find_session_dir(project, source_session)
                # Only reuse if it doesn't already have a session.md
                if candidate and not (candidate / "session.md").exists():
                    session_subdir = candidate
            if not session_subdir:
                session_subdir = target_dir / f"{note_id}-{today}"
            session_subdir.mkdir(parents=True, exist_ok=True)
            filepath = session_subdir / "session.md"
        elif note_type == NoteType.SOURCE:
            # Normalise source_type (legacy aliases like github → repo) on
            # write so the on-disk vocabulary stays consistent.
            raw_source_type = fm.get("source_type", "") or ""
            source_type = source_registry.normalize(raw_source_type, vault_root=self.root)
            if source_type != raw_source_type:
                fm["source_type"] = source_type

            spec = source_registry.get_spec(source_type, vault_root=self.root)

            # output_dir is the extraction escape hatch — extracted sources
            # target a session folder directly and must bypass bucketing.
            if output_dir is None:
                bucket = spec.bucket if spec else ""
                if bucket:
                    target_dir = target_dir / bucket
                    target_dir.mkdir(parents=True, exist_ok=True)

            # Dispatch on declared layout. Unregistered types fall back to
            # the folder layout with whatever target_dir the caller already
            # selected (empty bucket or output_dir override).
            layout = spec.layout if spec else "folder"
            if layout == "flat":
                filepath = self._write_source_flat(target_dir, slug)
            elif layout == "author_folder":
                filepath = self._write_source_author_folder(target_dir, slug, fm)
            else:  # "folder"
                filepath = self._write_source_folder(target_dir, slug)
        else:
            filename = f"{slug}.md"
            filepath = target_dir / filename

            # Avoid collisions
            counter = 1
            while filepath.exists():
                filepath = target_dir / f"{slug}-{counter}.md"
                counter += 1

        # Render and write
        header = f"# {title}\n\n" if note_type != NoteType.SOURCE else ""
        content = render_frontmatter(fm) + "\n\n" + header + body
        filepath.write_text(content, encoding="utf-8")

        # Post-write hook: event-grain sources auto-float theme candidates.
        #
        # The CLAUDE.md contract — "Event-shaped types … post-ingest path
        # runs a deterministic cluster check (≥3 recent sources sharing
        # ≥2 concepts, no covering theme) and writes a candidate stub."
        # — was previously honoured only when `/drain` invoked the
        # `theme_scan` post-batch hook. Direct `mem_create` / `mem_extract`
        # / `/news` / `/capture` paths all bypassed it. This block closes
        # that gap.
        #
        # Conservative scope: only fires for NoteType.SOURCE with an
        # event-grain spec (substack, news). Failure must never poison
        # the create — scan_candidates touches the index and theme
        # filesystem; either can be transient.
        if note_type == NoteType.SOURCE:
            self._maybe_float_theme_candidate(filepath, fm)

        return filepath

    def _maybe_float_theme_candidate(self, filepath: Path, fm: dict) -> None:
        """Index the new event-grain source so ``detect_signals`` can see
        it on the next ``/dream`` scan.

        Before 2026-05-25 this also auto-fired ``scan_candidates``, which
        materialised candidate stubs with mechanical concept-pair slugs
        (e.g. ``geopolitics-thematic-investing``). That naming doomed
        every cluster at /dream's disambiguation test — capability-shaped
        names get archived. The auto-write is gone; cluster detection
        and LLM naming now happen together inside ``/dream`` (or the
        seed-once script), which composes a real label from the cluster
        + active themes. The reindex stays — without it the new source
        wouldn't be visible to ``mem_search`` or the next /dream cycle
        until the next bulk ``mem index`` run.

        Defensive try/except — a failure here must not surface as a
        create failure.
        """
        import logging

        source_type = fm.get("source_type", "") or ""
        if not source_type:
            return
        spec = source_registry.get_spec(source_type, vault_root=self.root)
        if spec is None or spec.temporal_grain != "event":
            return

        try:
            from personal_mem.core.indexer import Indexer

            idx = Indexer(config=self.config)
            try:
                idx.index_file(filepath)
            finally:
                idx.close()
        except Exception:
            logging.getLogger(__name__).exception(
                "incremental index after event-grain source create failed "
                "for %s; create succeeded",
                filepath,
            )

    def read_note(self, path: Path | str) -> NoteMeta:
        """Read and parse a note file into NoteMeta."""
        path = self._resolve_path(path)
        text = path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)

        # Extract title from first H1 or filename
        title = fm.get("title", "")
        if not title:
            for line in body.split("\n"):
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            if not title:
                title = path.stem

        note_type = NoteType(fm.get("type", "note"))
        tags = fm.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]

        return NoteMeta(
            id=fm.get("id", ""),
            type=note_type,
            title=title,
            path=str(path.relative_to(self.root)),
            date=str(fm.get("date", "")),
            project=fm.get("project", ""),
            tags=tags,
            frontmatter=fm,
            body=body,
        )

    def update_note(
        self,
        path: Path | str,
        frontmatter_updates: dict | None = None,
        body_append: str = "",
        remove_tags: list[str] | None = None,
    ) -> None:
        """Update a note's frontmatter and/or append to its body."""
        path = self._resolve_path(path)
        text = path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)

        if frontmatter_updates:
            for key, value in frontmatter_updates.items():
                if isinstance(value, list) and isinstance(fm.get(key), list):
                    # Merge lists, avoiding duplicates
                    existing = set(fm[key])
                    fm[key] = fm[key] + [v for v in value if v not in existing]
                else:
                    fm[key] = value

        if remove_tags:
            tags = fm.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",")]
            to_remove = set(remove_tags)
            fm["tags"] = [t for t in tags if t not in to_remove]

        if body_append:
            body = body.rstrip() + "\n\n" + body_append + "\n"

        content = render_frontmatter(fm) + "\n\n" + body
        path.write_text(content, encoding="utf-8")

    def list_notes(
        self,
        note_type: NoteType | None = None,
        project: str = "",
        tags: list[str] | None = None,
        limit: int = 50,
    ) -> list[NoteMeta]:
        """List notes matching filters by scanning vault markdown files."""
        results: list[NoteMeta] = []
        for md_file in self.root.rglob("*.md"):
            # Skip templates and hidden dirs (except .mem)
            rel = md_file.relative_to(self.root)
            parts = rel.parts
            if "templates" in parts or ".obsidian" in parts:
                continue

            try:
                note = self.read_note(md_file)
            except (ValueError, KeyError):
                continue

            if note_type and note.type != note_type:
                continue
            if project and note.project != project:
                continue
            if tags and not set(tags).issubset(set(note.tags)):
                continue

            results.append(note)
            if len(results) >= limit:
                break

        return results

    def resolve_wikilink(self, name: str) -> Path | None:
        """Find the note file matching a [[wikilink]] name."""
        slug = self._sanitize_filename(name)
        # Exact filename match
        for md_file in self.root.rglob("*.md"):
            if md_file.stem == slug or md_file.stem == name:
                return md_file
        # Alias match would require reading frontmatter — skip for now
        return None

    def _resolve_path(self, path: Path | str) -> Path:
        """Resolve a path that might be relative to vault root or absolute."""
        p = Path(path)
        if p.is_absolute():
            return p
        return self.root / p

    def get_all_md_files(self) -> list[Path]:
        """Get all markdown files in the vault (excluding templates, .obsidian, .archive)."""
        results = []
        for md_file in self.root.rglob("*.md"):
            rel = md_file.relative_to(self.root)
            parts = rel.parts
            if (
                "templates" in parts
                or ".obsidian" in parts
                or ".mem" in parts
                or ".archive" in parts
            ):
                continue
            results.append(md_file)
        return results
