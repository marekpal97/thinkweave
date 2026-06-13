"""Unified hub abstraction — shared spine for concept and theme hubs.

Both concept hubs (``vault/concepts/topics/{concept}.md``) and theme hubs
(``vault/themes/{thm-XXXX}-{slug}.md``) share the same skeleton:

    # {title}

    ## Essence
    {slow-moving thesis, ≤500w}

    ## Catalyst log
    - YYYY-MM-DD · *flag[ ref]* — text — [[note-id]]

    ## Open questions          (theme only)

This module is the canonical home for parsing and rendering that skeleton:

- ``HubLogEntry`` — one log row (date, flag, ref, text, citations).
- ``Hub`` — in-memory parsed view (id, title, essence, log, open_questions).
- ``Hub.parse`` / ``Hub.render`` / ``Hub.append`` / ``Hub.render_dag`` —
  surface-agnostic ops.

Concept-hub and theme-hub modules layer their specialisations on top of
this (vocab id vs UUID id, lifecycle status, citation direction). After
this module lands, deleting it should break BOTH concept-hub and theme-hub
tests identically — that's the integration contract.

The on-disk grammar is unchanged from before. The only churn is the
section heading: concept hubs historically used ``## Learning log``;
both surfaces now write ``## Catalyst log``. ``migrate_hub_log_heading``
provides an idempotent rewrite for legacy concept hubs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from personal_mem.core.vault import parse_frontmatter

# ---------------------------------------------------------------------------
# Headings
# ---------------------------------------------------------------------------

ESSENCE_HEADING = "## Essence"
CATALYST_LOG_HEADING = "## Catalyst log"
LEGACY_LEARNING_LOG_HEADING = "## Learning log"
OPEN_QUESTIONS_HEADING = "## Open questions"

# Catalyst logs on major hubs run to hundreds of entries. Past this many
# thread anchors the renderer folds the *older* anchors into a collapsible
# ``<details>`` block so the page opens on the recent activity. The fold is
# purely visual — every entry stays in ``body_text`` (so the indexer still
# projects its citation into the edge graph) and round-trips through the
# parser unchanged. Never truncate; only collapse.
LOG_FOLD_THRESHOLD = 25

# Observational flag vocabulary. Same set on both surfaces — kept narrow on
# purpose, these are honest LLM observations the reader can verify, not a
# lifecycle state machine.
FLAG_NEW = "new"
FLAG_AGREES = "agrees"
FLAG_CONTRADICTS = "contradicts"
FLAG_EXTENDS = "extends"
ALLOWED_FLAGS = {FLAG_NEW, FLAG_AGREES, FLAG_CONTRADICTS, FLAG_EXTENDS}


# ---------------------------------------------------------------------------
# Log entry
# ---------------------------------------------------------------------------


# Entry pattern. Supports both flat and threaded log layouts:
#   `- 2026-01-15 · *new* — text — [[note-id]]`            (anchor)
#   `    - ↳ 2026-02-03 · *extends 2026-01-15* — … — …`    (child of anchor)
# The leading whitespace + arrow are decorative — the (date, flag, ref,
# text, citation) fields are recovered from the same regex regardless of
# nesting depth. The arrow `↳` (U+21B3) is optional; old flat logs still parse.
_ENTRY_RE = re.compile(
    r"^\s*-\s*"
    r"(?:↳\s*)?"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s*"
    r"·\s*"
    r"\*(?P<flag>\w+)(?:\s+(?P<ref>\d{4}-\d{2}-\d{2}))?\*\s*"
    r"(?:—|--|-)\s*"
    r"(?P<rest>.*)$"
)

# Captures both the link target (group 1) and the optional display alias
# (group 2). Catalyst-log citations are rendered path-based as
# ``[[full/path|note-id]]`` (the id lives in the display so it round-trips),
# but legacy logs carry bare ``[[note-id]]``; both must parse back to the id.
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")

# A note-id-shaped token: prefix + hex suffix (src-0032dd84, thm-aaaa1111,
# n-ade83929, dec-…, ses-…). Used to recover the citation id from either side
# of a piped wikilink — the side that looks like an id is the citation; the
# other side is a vault path. Falls back to the raw target when neither matches.
_NOTE_ID_RE = re.compile(r"^[a-z]+-[0-9a-f]{6,}$")


def build_id_path_map(db) -> dict[str, str]:
    """Map note id -> vault-relative path (sans ``.md``) for path-based links.

    A path wikilink resolves structurally in Obsidian by file location, so it
    never spawns a phantom stub — unlike a bare ``[[note-id]]`` that depends on
    the target's ``aliases:`` frontmatter being present *and* indexed. This is
    the same id->path resolution ``landing._id_path_map`` and the ``## See Also``
    materialiser use; the catalyst log shares it so every surface links the same
    durable way. ``db`` is any sqlite connection with a ``notes(id, path)`` table.
    """
    out: dict[str, str] = {}
    for r in db.execute("SELECT id, path FROM notes"):
        rel = str(r["path"] or "").replace("\\", "/")
        if rel.endswith(".md"):
            rel = rel[:-3]
        if rel:
            out[r["id"]] = rel
    return out


def build_id_title_map(db) -> dict[str, str]:
    """Map note id -> human title, for title-aliased catalyst-log citations.

    Mirrors ``build_id_path_map``; the two together let the renderer emit
    ``[[path|Title]]`` (durable target + legible alias). ``db`` is any sqlite
    connection with a ``notes(id, title)`` table. Empty/missing titles are
    skipped (the citation then falls back to displaying its id).
    """
    out: dict[str, str] = {}
    for r in db.execute("SELECT id, title FROM notes"):
        title = str(r["title"] or "").strip()
        if title:
            out[r["id"]] = title
    return out


def _clean_alias(text: str) -> str:
    """Sanitise a wikilink display alias.

    Aliases can't contain ``|`` (the wikilink field separator) or ``[`` / ``]``
    (the bracket delimiters), and newlines would break the single-line entry
    grammar. Collapse whitespace too so a multi-line title renders on one line.
    """
    return " ".join(
        text.replace("|", "/").replace("[", "(").replace("]", ")").split()
    )


def reflink(
    citation: str,
    idmap: dict[str, str] | None = None,
    title_map: dict[str, str] | None = None,
) -> str:
    """Render a catalyst-log citation as a wikilink.

    Path-based (``[[path|display]]``) when ``idmap`` resolves the id — the
    durable form that never spawns a phantom stub. ``display`` is the note's
    human title when ``title_map`` resolves it (so the reader sees *what* is
    cited, not an opaque ``n-ade83929``), falling back to the id otherwise.
    Falls back to bare ``[[note-id]]`` (alias resolution) when the path is
    unknown (e.g. a dangling citation to a deleted note). Empty citation ->
    empty string.

    Note the id stays the round-trip key: the parser recovers it from the path
    side via a ``path_to_id`` map (see ``_split_citation``), so swapping the
    display from id to title is lossless.
    """
    if not citation:
        return ""
    path = (idmap or {}).get(citation)
    if not path:
        return f"[[{citation}]]"
    display = (title_map or {}).get(citation) or citation
    return f"[[{path}|{_clean_alias(display)}]]"


@dataclass
class HubLogEntry:
    """A single entry in a hub's catalyst log.

    The ``citation`` field carries a single note id (the historical
    behaviour both surfaces relied on); ``citations`` is the same value
    promoted to a list for callers that want to reason about multiple refs
    in one entry. The two are kept consistent at construction time.
    """

    date: str  # YYYY-MM-DD
    flag: str  # one of ALLOWED_FLAGS
    ref: str = ""  # optional reference to another entry's date
    text: str = ""  # entry body text
    citation: str = ""  # primary cited note id (no brackets)

    @property
    def citations(self) -> list[str]:
        """List view of the entry's citations.

        Today this is one-element-or-empty; the list shape is preserved
        because both the spec calls for it and downstream callers want a
        forward-compatible signature when an entry one day cites multiple
        notes.
        """
        return [self.citation] if self.citation else []

    def render(
        self,
        *,
        depth: int = 0,
        idmap: dict[str, str] | None = None,
        title_map: dict[str, str] | None = None,
    ) -> str:
        """Render this entry as one markdown line.

        ``depth`` controls threading. Depth 0 is a top-level anchor
        (rendered as ``- {date} · *flag* — text — [[path|Title]]``); depth ≥ 1
        is a descendant rendered as a nested list item with a ``↳``
        cue (``    - ↳ {date} · *extends 2026-01-15* — text — [[path|Title]]``).
        Indentation is 4 spaces per level — the canonical markdown
        nested-list indent that Obsidian renders as a real sub-bullet.

        ``idmap`` (id -> vault-relative path) makes the citation a path-based
        wikilink that resolves structurally instead of via the fragile
        bare-alias form. ``title_map`` (id -> human title) makes the display
        alias the note's title so the reader can tell what is cited; without
        it the alias is the id. Omit both and the citation falls back to bare
        ``[[id]]`` (the legacy shape) — all forms round-trip through the parser.
        """
        flag_str = f"*{self.flag}*" if not self.ref else f"*{self.flag} {self.ref}*"
        link = reflink(self.citation, idmap, title_map)
        citation = f" — {link}" if link else ""
        if depth <= 0:
            prefix = "- "
        else:
            prefix = ("    " * depth) + "- ↳ "
        return f"{prefix}{self.date} · {flag_str} — {self.text}{citation}"


# ---------------------------------------------------------------------------
# Threading — derived view of the (date, flag, ref) DAG for rendering.
# ---------------------------------------------------------------------------


def thread_log(entries: list["HubLogEntry"]) -> list[tuple["HubLogEntry", int]]:
    """Order log entries into a threaded layout for rendering.

    Returns a list of ``(entry, depth)`` tuples. ``depth == 0`` is a
    thread anchor (a ``new`` entry, or a non-``new`` entry whose ``ref``
    doesn't match any earlier entry — orphaned by a prior linkage failure
    or curation pass). ``depth ≥ 1`` is a descendant indented under its
    predecessor.

    A child of entry E is any entry Y where ``Y.ref == E.date`` AND
    ``(Y.date, Y.citation) > (E.date, E.citation)``. When ``Y.ref``
    matches multiple same-day candidates, the first by ``(date, citation)``
    sort wins as the parent — deterministic, but not perfectly faithful
    to the model's original choice (we have no way to disambiguate).

    Anchors render in chronological order; within each thread, descendants
    are also chronological. The top-level reading flow is therefore the
    same chronology you'd get from a flat log; threading just adds vertical
    structure for the connected entries.
    """
    if not entries:
        return []

    sorted_entries = sorted(entries, key=lambda e: (e.date, e.citation))
    by_date: dict[str, list[HubLogEntry]] = {}
    for e in sorted_entries:
        by_date.setdefault(e.date, []).append(e)

    children_of: dict[int, list[HubLogEntry]] = {}
    parent_of: dict[int, HubLogEntry] = {}
    for e in sorted_entries:
        if e.flag == FLAG_NEW or not e.ref:
            continue
        candidates = by_date.get(e.ref, [])
        # Pick the earliest candidate that strictly precedes e.
        parent: HubLogEntry | None = None
        for c in candidates:
            if (c.date, c.citation) < (e.date, e.citation):
                parent = c
                break
        if parent is None:
            continue  # orphan — render as anchor
        children_of.setdefault(id(parent), []).append(e)
        parent_of[id(e)] = parent

    result: list[tuple[HubLogEntry, int]] = []

    def emit(entry: HubLogEntry, depth: int) -> None:
        result.append((entry, depth))
        for child in children_of.get(id(entry), []):
            emit(child, depth + 1)

    for entry in sorted_entries:
        if id(entry) in parent_of:
            continue  # rendered as a descendant of its parent
        emit(entry, 0)

    return result


def render_catalyst_log(
    entries: list["HubLogEntry"],
    *,
    idmap: dict[str, str] | None = None,
    title_map: dict[str, str] | None = None,
    threaded: bool = False,
    fold_threshold: int | None = LOG_FOLD_THRESHOLD,
) -> list[str]:
    """Render a catalyst log to markdown lines — the shared body for both surfaces.

    Threads (``thread_log``) when ``threaded``; otherwise renders flat in the
    given order. When the number of top-level *anchors* exceeds
    ``fold_threshold``, the older anchors (and their whole threads — a thread is
    never split across the boundary) are wrapped in a collapsible ``<details>``
    block so the page opens on the most recent ``fold_threshold`` anchors. The
    fold is purely visual: every entry stays in the markdown (and thus in the
    SQL edge graph) and re-parses identically. Pass ``fold_threshold=None`` to
    disable folding.

    Returns the lines for the section body (no heading). Empty log ->
    ``["*No entries yet.*"]``.
    """
    if not entries:
        return ["*No entries yet.*"]

    rows: list[tuple[HubLogEntry, int]] = (
        thread_log(entries) if threaded else [(e, 0) for e in entries]
    )

    anchor_rows = [i for i, (_, depth) in enumerate(rows) if depth == 0]
    n_anchors = len(anchor_rows)

    if fold_threshold and n_anchors > fold_threshold:
        # Keep the most recent `fold_threshold` anchors visible; the split lands
        # on an anchor boundary so threads stay whole on both sides.
        split = anchor_rows[n_anchors - fold_threshold]
        older, recent = rows[:split], rows[split:]
    else:
        older, recent = [], rows

    def _line(entry: HubLogEntry, depth: int) -> str:
        return entry.render(depth=depth, idmap=idmap, title_map=title_map)

    lines: list[str] = []
    if older:
        lines.append("<details>")
        lines.append(f"<summary>Earlier log ({len(older)} entries)</summary>")
        lines.append("")
        lines.extend(_line(e, d) for e, d in older)
        lines.append("</details>")
        lines.append("")
    lines.extend(_line(e, d) for e, d in recent)
    return lines


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------


@dataclass
class Hub:
    """Shared spine for concept hubs and theme hubs.

    ``id`` is the surface-specific identity (concept name like ``finance-regime``
    on a concept hub; UUID like ``thm-aaaa1111`` on a theme hub).

    ``open_questions`` is empty on concept hubs and present on theme hubs.
    Renderers omit the section when empty for concept hubs; theme hubs
    always include it even when empty (it's part of the authored skeleton).
    """

    id: str
    title: str
    essence: str = ""
    log: list[HubLogEntry] = field(default_factory=list)
    open_questions: str = ""
    # Original frontmatter + body, retained for surfaces that need to
    # reconstruct the full file faithfully (concept hubs preserve custom
    # frontmatter keys; theme hubs are authored-by-hand and the body
    # shape varies).
    frontmatter: dict = field(default_factory=dict)
    raw_body: str = ""
    path: Path | None = None

    # ---- I/O -------------------------------------------------------------

    @classmethod
    def parse(
        cls,
        path: Path,
        *,
        hub_id: str | None = None,
        path_to_id: dict[str, str] | None = None,
    ) -> "Hub":
        """Read a hub file from disk and parse the shared sections.

        Missing file → returns a Hub with empty essence/log. Malformed
        sections → best-effort parse; unrecognised entries are dropped
        silently. Tolerates both ``## Catalyst log`` (canonical) and
        ``## Learning log`` (legacy concept-hub heading).

        ``path_to_id`` (vault-relative-path-sans-.md -> note id) lets the
        parser recover the citation id from a title-aliased link
        (``[[path|Title]]``), where the id is no longer on the display side.
        Omit it and the parser still recovers ids from legacy ``[[path|id]]``
        and bare ``[[id]]`` links — so it's only required when reading hubs
        that have already been rewritten with title aliases.
        """
        if hub_id is None:
            hub_id = path.stem

        if not path.exists():
            return cls(id=hub_id, title=hub_id, path=path)

        text = path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)

        title = _extract_h1(body) or str(fm.get("title") or hub_id)

        essence = extract_section(body, ESSENCE_HEADING).strip()

        # Prefer canonical heading; fall back to legacy.
        log_body = extract_section(body, CATALYST_LOG_HEADING)
        if not log_body:
            log_body = extract_section(body, LEGACY_LEARNING_LOG_HEADING)
        log = parse_log_entries(log_body, path_to_id=path_to_id)

        open_q = extract_section(body, OPEN_QUESTIONS_HEADING).strip()

        return cls(
            id=hub_id,
            title=title,
            essence=essence,
            log=log,
            open_questions=open_q,
            frontmatter=fm,
            raw_body=body,
            path=path,
        )

    def render(
        self,
        *,
        include_open_questions: bool = False,
        threaded: bool = False,
        idmap: dict[str, str] | None = None,
        title_map: dict[str, str] | None = None,
        fold_threshold: int | None = LOG_FOLD_THRESHOLD,
    ) -> str:
        """Render the shared body skeleton.

        ``include_open_questions`` — concept hubs leave False (they don't
        carry the section); theme hubs pass True so the section is always
        rendered, even when empty (it's part of the authored skeleton).

        ``threaded`` — when True, the catalyst log is laid out as a
        threaded tree: ``new`` entries are top-level bullets and non-``new``
        entries indent under their predecessor with a ``↳`` cue. Order
        within each thread is chronological; anchors are also chronological
        at the top level. When False (default), the log renders as a flat
        chronological list — the historical layout. Both layouts round-trip
        through the parser without loss.

        This intentionally does not include frontmatter — the concept-hub
        renderer wraps this with its own frontmatter logic, and themes are
        created via ``VaultManager.create_note`` which adds frontmatter.
        """
        lines: list[str] = [f"# {self.title}", ""]

        lines.append(ESSENCE_HEADING)
        lines.append("")
        lines.append(self.essence.strip() if self.essence.strip() else "*No synthesis yet.*")
        lines.append("")

        lines.append(CATALYST_LOG_HEADING)
        lines.append("")
        lines.extend(
            render_catalyst_log(
                self.log,
                idmap=idmap,
                title_map=title_map,
                threaded=threaded,
                fold_threshold=fold_threshold,
            )
        )
        lines.append("")

        if include_open_questions:
            lines.append(OPEN_QUESTIONS_HEADING)
            lines.append("")
            if self.open_questions.strip():
                lines.append(self.open_questions.strip())
                lines.append("")

        return "\n".join(lines)

    # ---- Mutation --------------------------------------------------------

    def append(self, entry: HubLogEntry) -> bool:
        """Append a log entry. Idempotent on (citation, date, text).

        Returns True if appended, False if a duplicate (by citation match,
        consistent with the historical concept-hub dedup rule).
        """
        if entry.flag not in ALLOWED_FLAGS:
            return False
        if entry.citation:
            cited = {e.citation for e in self.log if e.citation}
            if entry.citation in cited:
                return False
        self.log.append(entry)
        return True

    # ---- Derived views ---------------------------------------------------

    @property
    def cited_ids(self) -> set[str]:
        """Set of note ids already cited on the hub."""
        return {e.citation for e in self.log if e.citation}

    def render_dag(self, *, kind: str = "log_entry") -> str:
        """Render the temporal DAG as a Mermaid block (no fences).

        ``kind`` selects the NodeKind for log entries; concept hubs use
        ``log_entry`` (default), theme hubs pass ``catalyst``.
        """
        # Local import: temporal lives in retrieval/, and we want hub.py
        # to stay leaf-ish for the synthesis layer.
        from personal_mem.retrieval.temporal import (
            entries_to_graph,
            render_mermaid,
        )

        graph = entries_to_graph(self.log, kind=kind)
        return render_mermaid(graph)


# ---------------------------------------------------------------------------
# Section / log primitives — the only place these live in the codebase.
# ---------------------------------------------------------------------------


def extract_section(body: str, heading: str) -> str:
    """Return text inside a markdown ``## Heading`` section.

    Reads from the heading up to the next ``##`` (any depth ≥2) or EOF.
    Returns empty string if the heading isn't present.
    """
    if heading not in body:
        return ""
    start = body.index(heading) + len(heading)
    rest = body[start:]
    m = re.search(r"\n##\s", rest)
    if m:
        return rest[: m.start()]
    return rest


def parse_log_entries(
    section_text: str, path_to_id: dict[str, str] | None = None
) -> list[HubLogEntry]:
    """Parse log entries out of a block of markdown.

    Supports multi-line entry text (continuation lines join into the same
    entry until the next ``- YYYY-MM-DD`` line). Returns the entries that
    pass flag validation; everything else is silently dropped.

    HTML fold decoration (``<details>`` / ``<summary>`` / their closing tags)
    is skipped — those lines are how ``render_catalyst_log`` collapses old
    entries, and must not be mistaken for entry continuation text.

    ``path_to_id`` is forwarded to ``_split_citation`` so title-aliased links
    (``[[path|Title]]``) recover their citation id from the path side.
    """
    entries: list[HubLogEntry] = []
    current_lines: list[str] = []
    current_header: dict | None = None

    def _flush() -> None:
        if current_header is None:
            return
        rest_text = " ".join(line.strip() for line in current_lines).strip()
        text, citation = _split_citation(rest_text, path_to_id)
        entries.append(
            HubLogEntry(
                date=current_header["date"],
                flag=current_header["flag"],
                ref=current_header.get("ref", "") or "",
                text=text.strip(" —-"),
                citation=citation,
            )
        )

    for line in section_text.splitlines():
        m = _ENTRY_RE.match(line)
        if m:
            _flush()
            current_header = {
                "date": m.group("date"),
                "flag": m.group("flag"),
                "ref": m.group("ref"),
            }
            current_lines = [m.group("rest")]
        elif line.lstrip().startswith("<"):
            # Fold decoration (<details>/<summary>/closing tags) — neither a
            # new entry nor continuation text. Skip so it never pollutes the
            # preceding entry's body on re-parse.
            continue
        elif current_header is not None and line.strip():
            current_lines.append(line)

    _flush()
    return [e for e in entries if e.flag in ALLOWED_FLAGS]


def parse_log_section(
    body: str, heading: str, path_to_id: dict[str, str] | None = None
) -> list[HubLogEntry]:
    """Extract a ``## ...`` section and parse it as log entries.

    Convenience wrapper used by both surfaces (concept hubs read
    ``## Catalyst log``; themes read the same heading on theme bodies).
    Empty list if the heading is absent or the section has no valid entries.
    ``path_to_id`` is forwarded to the parser for title-aliased citations.
    """
    return parse_log_entries(extract_section(body, heading), path_to_id=path_to_id)


def _split_citation(
    text: str, path_to_id: dict[str, str] | None = None
) -> tuple[str, str]:
    """Strip the final [[wikilink]] from an entry's rest-text.

    Returns ``(text_without_citation, citation_id)``. The citation is the
    *last* wikilink on the line; embedded wikilinks earlier in the body
    are left in place for the caller to handle (the concept-hub LLM path
    has a separate scrubber for stray inline wikilinks).

    Three citation shapes round-trip here:
      - ``[[path|note-id]]`` (legacy path-based) — id is the display side.
      - ``[[note-id]]`` (bare) — id is the target.
      - ``[[path|Title]]`` (title-aliased) — id is on neither side; recovered
        by resolving the path against ``path_to_id``.
    """
    matches = list(_WIKILINK_RE.finditer(text))
    if not matches:
        return text, ""
    last = matches[-1]
    target = (last.group(1) or "").strip()
    display = (last.group(2) or "").strip()
    if display and _NOTE_ID_RE.match(display):
        citation = display
    elif path_to_id and target in path_to_id:
        # Title-aliased link: the display is a human title, the id lives in
        # the path. Resolve it back so dedup/edges still key on the id.
        citation = path_to_id[target]
    elif _NOTE_ID_RE.match(target):
        citation = target
    else:
        citation = target
    stripped = (text[: last.start()] + text[last.end():]).rstrip(" —-")
    return stripped, citation


def _extract_h1(body: str) -> str:
    """Pull the first level-1 heading out of a body."""
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# ") and not line.startswith("## "):
            return line[2:].strip()
        if line.startswith("##"):
            break
    return ""


# ---------------------------------------------------------------------------
# Fold — merging one hub's log into another (concept merge / theme merge)
# ---------------------------------------------------------------------------

#: Frontmatter keys stamped on a hub whose log just absorbed another hub's
#: entries. Transient: the dream seam-link worker judges cross-parent entry
#: pairs (fold dates × the rest) and clears both keys when done. Two *flat*
#: keys (not one nested map) because the vault frontmatter parser is
#: deliberately flat — a nested dict renders but doesn't round-trip.
FOLD_PENDING_FROM_KEY = "fold_pending_from"
FOLD_PENDING_DATES_KEY = "fold_pending_dates"

#: Marker wrapping a folded-in essence stash inside ``## Essence`` — the
#: essence worker reconciles the two texts into one on its next pass.
FOLD_ESSENCE_MARKER = "<!-- folded-essence -->"

_ESSENCE_PLACEHOLDERS = (
    "*No synthesis yet.*",
    "_Awaiting first synthesis pass._",
)
_NORMALIZED_ESSENCE_PLACEHOLDERS = frozenset(
    p.strip("*_ ").rstrip(".").lower() for p in _ESSENCE_PLACEHOLDERS
)
#: Generic-stub length bound — every system-written stub is one short line
#: (the longest, ``render_theme_body_skeleton``'s ``_Replace with the
#: working thesis…_`` instruction, is ~170 chars). A real essence paragraph
#: is longer and/or multi-line, so it never trips the generic arm.
_PLACEHOLDER_MAX_CHARS = 200


def essence_is_placeholder(essence: str) -> bool:
    """True when the essence is empty or still a system-written stub.

    The single shared predicate for both hub families — the dream scan
    (``operations/dream.py``) and the landing catalog renderer
    (``synthesis/landing.py``) call it too, so the surfaces can't drift.

    Two arms: exact membership in the known stub strings
    (:data:`_ESSENCE_PLACEHOLDERS`, normalized), then a generic-stub
    check — ONE short line fully wrapped in a matching emphasis marker,
    the register every skeleton writer uses (``*No synthesis yet.*``,
    ``_Awaiting first synthesis pass._``, the theme skeleton's
    ``_Replace with the working thesis…_`` instruction). A real essence
    paragraph that merely opens with emphasis is NOT flagged: it is
    multi-line, longer than the stub bound, or doesn't end with the
    matching marker.
    """
    text = (essence or "").strip()
    if not text:
        return True
    normalized = text.strip("*_ ").rstrip(".").lower()
    if normalized in _NORMALIZED_ESSENCE_PLACEHOLDERS:
        return True
    # Generic stub: a single short emphasis-wrapped line.
    if "\n" in text or len(text) > _PLACEHOLDER_MAX_CHARS or len(text) < 2:
        return False
    return (text.startswith("_") and text.endswith("_")) or (
        text.startswith("*") and text.endswith("*")
    )


def _richer_entry(a: HubLogEntry, b: HubLogEntry) -> HubLogEntry:
    """Pick the more informative of two entries citing the same note.

    A non-``new`` flag carries linkage information; otherwise longer text
    wins (generic stubs like ``extend`` / ``cluster seed`` lose to a real
    distillation). Ties keep ``a`` (the winner hub's copy).
    """
    a_linked, b_linked = a.flag != FLAG_NEW, b.flag != FLAG_NEW
    if a_linked != b_linked:
        return a if a_linked else b
    return a if len(a.text or "") >= len(b.text or "") else b


def merge_log_entries(
    winner: list[HubLogEntry], loser: list[HubLogEntry]
) -> tuple[list[HubLogEntry], list[str]]:
    """Interleave two catalyst logs by date, deduping shared citations.

    Returns ``(merged_entries, fold_dates)`` — ``fold_dates`` are the dates
    of entries whose content came from the loser log (the seam-link pass
    judges those against the rest). Entries citing the same note collapse
    to the richer copy (:func:`_richer_entry`); near-dupe hubs logging the
    same source is expected, and a merged hub shouldn't say it twice.
    """
    by_citation: dict[str, int] = {}
    merged: list[HubLogEntry] = []
    from_loser: set[int] = set()

    for e in winner:
        if e.citation:
            by_citation[e.citation] = len(merged)
        merged.append(e)

    for e in loser:
        if e.citation and e.citation in by_citation:
            i = by_citation[e.citation]
            keep = _richer_entry(merged[i], e)
            if keep is e:
                merged[i] = e
                from_loser.add(id(e))
            continue
        if e.citation:
            by_citation[e.citation] = len(merged)
        from_loser.add(id(e))
        merged.append(e)

    merged.sort(key=lambda e: (e.date, e.citation))
    fold_dates = sorted({e.date for e in merged if id(e) in from_loser})
    return merged, fold_dates


def replace_section_body(body: str, heading: str, new_lines: list[str]) -> str:
    """Replace the contents of a ``## Heading`` section in a markdown body.

    The heading line stays; everything up to the next ``##`` (any depth ≥2)
    or EOF is swapped for ``new_lines``. Missing heading → section appended
    at the end of the body.
    """
    block = "\n".join([heading, ""] + new_lines + ["", ""])
    if heading not in body:
        return body.rstrip("\n") + "\n\n" + block
    start = body.index(heading)
    after = body[start + len(heading):]
    m = re.search(r"\n##\s", after)
    tail = after[m.start() + 1:] if m else ""
    return body[:start] + block + tail


def fold_hub_logs(
    winner_path: Path,
    loser_path: Path,
    *,
    loser_id: str | None = None,
    path_to_id: dict[str, str] | None = None,
    idmap: dict[str, str] | None = None,
    title_map: dict[str, str] | None = None,
) -> dict:
    """Fold ``loser_path``'s catalyst log (and essence) into ``winner_path``.

    The deterministic half of a hub merge — used for both concept-hub and
    theme merges (shared spine). Mutates the winner file in place:

    1. Log: interleave by date, dedup shared citations keeping the richer
       copy (:func:`merge_log_entries`), re-render threaded.
    2. Provenance: stamp :data:`FOLD_PENDING_FROM_KEY` /
       :data:`FOLD_PENDING_DATES_KEY` frontmatter so the dream seam-link
       worker knows which entry dates need cross-parent linkage judgment.
       Re-folding while a stamp is pending unions the dates.
    3. Essence: if both hubs carry a real essence, the loser's is stashed
       under :data:`FOLD_ESSENCE_MARKER` inside ``## Essence`` and the
       ``essence_updated`` stamp is cleared — the existing essence worker
       picks the hub up as a reconciliation candidate on its next cycle.
       A placeholder winner essence simply adopts the loser's (still
       clearing the stamp).

    Does NOT archive/delete the loser — callers orchestrate the tombstone
    (``archive_concept_hub`` / theme ``merged-into:`` status). Returns
    stats ``{folded, deduped, fold_dates, essence_stashed}``.
    """
    if loser_id is None:
        loser_id = loser_path.stem

    winner = Hub.parse(winner_path, path_to_id=path_to_id)
    loser = Hub.parse(loser_path, path_to_id=path_to_id)
    if not loser.log and essence_is_placeholder(loser.essence):
        return {"folded": 0, "deduped": 0, "fold_dates": [], "essence_stashed": False}

    n_before = len(winner.log)
    merged, fold_dates = merge_log_entries(winner.log, loser.log)
    deduped = n_before + len(loser.log) - len(merged)

    # Essence reconciliation routing.
    essence_stashed = False
    new_essence: str | None = None
    if not essence_is_placeholder(loser.essence):
        if essence_is_placeholder(winner.essence):
            new_essence = loser.essence.strip()
        else:
            new_essence = (
                winner.essence.strip()
                + f"\n\n{FOLD_ESSENCE_MARKER}\n"
                + f"> [!note]- Folded essence from `{loser_id}`\n"
                + "\n".join(
                    f"> {ln}" for ln in loser.essence.strip().splitlines()
                )
            )
            essence_stashed = True

    text = winner_path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)

    # Normalize a legacy-heading winner before section surgery.
    if CATALYST_LOG_HEADING not in body and LEGACY_LEARNING_LOG_HEADING in body:
        body = re.sub(
            r"(?m)^##\s+Learning log\s*$", CATALYST_LOG_HEADING, body
        )

    log_lines = render_catalyst_log(
        merged, idmap=idmap, title_map=title_map, threaded=True
    )
    body = replace_section_body(body, CATALYST_LOG_HEADING, log_lines)
    if new_essence is not None:
        body = replace_section_body(
            body, ESSENCE_HEADING, new_essence.splitlines()
        )
        fm.pop("essence_updated", None)

    prior_dates = fm.get(FOLD_PENDING_DATES_KEY) or []
    if not isinstance(prior_dates, list):
        prior_dates = [prior_dates]
    if prior_dates:
        # Union with an unprocessed earlier fold; keep the older `from`
        # label (the seam pass judges by dates, not by source label).
        fm[FOLD_PENDING_DATES_KEY] = sorted(set(prior_dates) | set(fold_dates))
        fm.setdefault(FOLD_PENDING_FROM_KEY, loser_id)
    elif fold_dates:
        fm[FOLD_PENDING_FROM_KEY] = loser_id
        fm[FOLD_PENDING_DATES_KEY] = fold_dates

    from personal_mem.core.vault import render_frontmatter

    winner_path.write_text(
        render_frontmatter(fm) + "\n" + body.lstrip("\n"), encoding="utf-8"
    )
    return {
        "folded": len(merged) - n_before + deduped,
        "deduped": deduped,
        "fold_dates": fold_dates,
        "essence_stashed": essence_stashed,
    }


def set_frontmatter_keys(path: Path, updates: dict) -> bool:
    """Set/replace top-level frontmatter keys on a markdown file in place.

    ``None`` values delete the key. Returns False when the file is missing.
    Used for the merge tombstone (``merged-into:``) and for clearing the
    :data:`FOLD_PENDING_FROM_KEY` / :data:`FOLD_PENDING_DATES_KEY` stamps
    after the seam-link pass.
    """
    if not path.exists():
        return False
    from personal_mem.core.vault import render_frontmatter

    fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    for k, v in updates.items():
        if v is None:
            fm.pop(k, None)
        else:
            fm[k] = v
    path.write_text(
        render_frontmatter(fm) + "\n" + body.lstrip("\n"), encoding="utf-8"
    )
    return True


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


def migrate_hub_log_heading(path: Path) -> bool:
    """Idempotently rename ``## Learning log`` → ``## Catalyst log``.

    No-op when:
    - the file is missing,
    - the file already has ``## Catalyst log`` (regardless of whether the
      legacy heading also appears — the canonical heading wins; we leave
      a leftover legacy heading alone because it's now ambiguous and
      better resolved by hand),
    - the file has neither heading.

    Returns True if the file was rewritten, False otherwise. Safe to call
    repeatedly: running twice on the same file mutates exactly once.
    """
    if not path.exists():
        return False

    text = path.read_text(encoding="utf-8")
    if CATALYST_LOG_HEADING in text:
        # Already migrated (or never needed migration). Idempotent no-op.
        return False
    if LEGACY_LEARNING_LOG_HEADING not in text:
        return False

    # Replace only the heading line; do not touch in-prose mentions of
    # "learning log" elsewhere in the body.
    new_text = re.sub(
        r"(?m)^##\s+Learning log\s*$",
        CATALYST_LOG_HEADING,
        text,
    )
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


# Bare note-id wikilink: ``[[src-0032dd84]]`` with no pipe and no path. Already
# path-based (``[[path|id]]``) or namespaced (``[[concepts/foo]]``) links carry
# a pipe or slash and are deliberately excluded, which makes the rewrite below
# idempotent.
_BARE_ID_LINK_RE = re.compile(r"\[\[([a-z]+-[0-9a-f]{6,})\]\]")


def migrate_bare_id_links(path: Path, idmap: dict[str, str]) -> int:
    """Rewrite bare ``[[note-id]]`` wikilinks to path-based ``[[path|note-id]]``.

    Heals any markdown file (hub catalyst logs and note/decision/source
    bodies alike) so id references resolve structurally instead of via the
    fragile bare-alias form (the phantom-stub bug). Only bare ids present in
    ``idmap`` are rewritten; unknown ids (e.g. dangling references to deleted
    notes) are left as-is. Idempotent — already-piped or namespaced links
    carry a pipe/slash and don't match the bare pattern.

    Returns the number of links rewritten in this file (0 if unchanged or
    missing).
    """
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8")
    count = 0

    def _sub(m: re.Match) -> str:
        nonlocal count
        note_id = m.group(1)
        dest = idmap.get(note_id)
        if not dest:
            return m.group(0)  # unknown id — leave the bare link untouched
        count += 1
        return f"[[{dest}|{note_id}]]"

    new_text = _BARE_ID_LINK_RE.sub(_sub, text)
    if count and new_text != text:
        path.write_text(new_text, encoding="utf-8")
    return count
