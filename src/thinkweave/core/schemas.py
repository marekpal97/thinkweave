"""Minimal enums and dataclasses for thinkweave note types and edges."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class NoteType(str, Enum):
    NOTE = "note"
    SESSION = "session"
    DECISION = "decision"
    SOURCE = "source"
    THEME = "theme"
    # Knowledge-first daily summary written by ``dream-digest-worker``
    # (phase 2 of ``/dream``). Post-2026-06-07 grain split: files land
    # vault-global at ``vault/digests/YYYY-MM-DD-<grain>.md``, with
    # ``grain ∈ {"concept", "event"}`` — one per non-empty knowledge slice.
    # Queryable via ``weave_search(type='digest')`` and ``weave list_notes`` —
    # the SQLite indexer treats it like any other note type (uniform
    # ``fm.get('type','note')`` read in ``Indexer.index_file``).
    DIGEST = "digest"


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
    NoteType.DIGEST: "dig",
}


# Canonical set of frontmatter keys whose value must be a list. Used by
# ``core/vault.py::render_frontmatter`` (and the early-coercion guard in
# ``VaultManager.create_note``) as a write-time backstop: when a caller
# passes a JSON-shaped string (e.g. ``"['liqudty']"``) or a bare scalar
# for one of these fields, we coerce it to a real list rather than (a)
# letting the string get iterated char-by-char by a downstream consumer,
# or (b) letting it round-trip through YAML as a stringified value.
#
# The bug this set defends against was the 2026-06-07 char-by-char
# ``proposed_concepts: ['[', 'l', 'i', 'q', 'u', 'd', 't', 'y', ']']``
# pollution found on four news source notes: the news-writer subagent
# occasionally JSON-stringified its frontmatter list arguments before
# the MCP call, and ``split_concepts_by_ontology`` iterated the string
# as characters.
LIST_FRONTMATTER_KEYS: frozenset[str] = frozenset({
    # Concept + tag vocabulary
    "concepts",
    "proposed_concepts",
    "tags",
    "aliases",
    "authors",
    # Edge declarations (typed graph references)
    "relates_to",
    "derived_from",
    "builds_on",
    "supersedes",
    "implements",
    "cites",
    # Git + file tracking (decisions, sessions) — always list[str]
    "file_paths",
    "files_touched",
    "commit_refs",
    "commits",
    # Test + verdict logs — list[dict]
    "test_runs",
    "prediction_history",
})
