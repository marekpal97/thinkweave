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


# Entry pattern: `- 2026-01-15 · *new* — text text — [[note-id]]`
# Flag may be `*new*` or `*contradicts 2026-01-15*` etc.
# Citation may be missing; if present it's the final `[[...]]` wikilink.
_ENTRY_RE = re.compile(
    r"^\s*-\s*"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s*"
    r"·\s*"
    r"\*(?P<flag>\w+)(?:\s+(?P<ref>\d{4}-\d{2}-\d{2}))?\*\s*"
    r"(?:—|--|-)\s*"
    r"(?P<rest>.*)$"
)

_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


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

    def render(self) -> str:
        flag_str = f"*{self.flag}*" if not self.ref else f"*{self.flag} {self.ref}*"
        citation = f" — [[{self.citation}]]" if self.citation else ""
        return f"- {self.date} · {flag_str} — {self.text}{citation}"


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------


@dataclass
class Hub:
    """Shared spine for concept hubs and theme hubs.

    ``id`` is the surface-specific identity (concept name like ``finance/regime``
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
    def parse(cls, path: Path, *, hub_id: str | None = None) -> "Hub":
        """Read a hub file from disk and parse the shared sections.

        Missing file → returns a Hub with empty essence/log. Malformed
        sections → best-effort parse; unrecognised entries are dropped
        silently. Tolerates both ``## Catalyst log`` (canonical) and
        ``## Learning log`` (legacy concept-hub heading).
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
        log = parse_log_entries(log_body)

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

    def render(self, *, include_open_questions: bool = False) -> str:
        """Render the shared body skeleton.

        Concept hubs leave ``include_open_questions`` False (they don't
        carry the section); theme hubs pass True so the section is always
        rendered, even when empty (it's part of the authored skeleton).

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
        if self.log:
            for entry in self.log:
                lines.append(entry.render())
        else:
            lines.append("*No entries yet.*")
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


def parse_log_entries(section_text: str) -> list[HubLogEntry]:
    """Parse log entries out of a block of markdown.

    Supports multi-line entry text (continuation lines join into the same
    entry until the next ``- YYYY-MM-DD`` line). Returns the entries that
    pass flag validation; everything else is silently dropped.
    """
    entries: list[HubLogEntry] = []
    current_lines: list[str] = []
    current_header: dict | None = None

    def _flush() -> None:
        if current_header is None:
            return
        rest_text = " ".join(line.strip() for line in current_lines).strip()
        text, citation = _split_citation(rest_text)
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
        elif current_header is not None and line.strip():
            current_lines.append(line)

    _flush()
    return [e for e in entries if e.flag in ALLOWED_FLAGS]


def parse_log_section(body: str, heading: str) -> list[HubLogEntry]:
    """Extract a ``## ...`` section and parse it as log entries.

    Convenience wrapper used by both surfaces (concept hubs read
    ``## Catalyst log``; themes read the same heading on theme bodies).
    Empty list if the heading is absent or the section has no valid entries.
    """
    return parse_log_entries(extract_section(body, heading))


def _split_citation(text: str) -> tuple[str, str]:
    """Strip the final [[wikilink]] from an entry's rest-text.

    Returns ``(text_without_citation, citation_id)``. The citation is the
    *last* wikilink on the line; embedded wikilinks earlier in the body
    are left in place for the caller to handle (the concept-hub LLM path
    has a separate scrubber for stray inline wikilinks).
    """
    matches = list(_WIKILINK_RE.finditer(text))
    if not matches:
        return text, ""
    last = matches[-1]
    citation = last.group(1).strip()
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
