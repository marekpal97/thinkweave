"""Minimal enums and dataclasses for personal_mem note types and edges."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class NoteType(str, Enum):
    NOTE = "note"
    SESSION = "session"
    DECISION = "decision"
    SOURCE = "source"
    THEME = "theme"


class EdgeType(str, Enum):
    BUILDS_ON = "builds_on"
    DERIVED_FROM = "derived_from"
    SUPERSEDES = "supersedes"
    IMPLEMENTS = "implements"
    RELATES_TO = "relates_to"
    CITES = "cites"


class DecisionStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    DEPRECATED = "deprecated"
    SUPERSEDED = "superseded"


class DecisionOutcome(str, Enum):
    KEPT = "kept"
    SUPERSEDED = "superseded"
    REVERTED = "reverted"
    UNKNOWN = "unknown"


@dataclass
class NoteMeta:
    """Parsed frontmatter of a vault note."""

    id: str
    type: NoteType
    title: str
    path: str  # relative to vault root
    date: str = ""
    project: str = ""
    tags: list[str] = field(default_factory=list)
    frontmatter: dict = field(default_factory=dict)  # raw frontmatter dict
    body: str = ""

    @property
    def prefix(self) -> str:
        return self.id.split("-")[0] if "-" in self.id else ""


# ID prefixes per type
NOTE_ID_PREFIXES: dict[NoteType, str] = {
    NoteType.NOTE: "n",
    NoteType.SESSION: "ses",
    NoteType.DECISION: "dec",
    NoteType.SOURCE: "src",
    NoteType.THEME: "thm",
}
