"""Concept hub pages — Essence + Catalyst log synthesis layer.

A *concept hub* is a markdown page at ``vault/concepts/topics/{concept}.md``
that distills what the user knows about a concept across all notes in the
vault, regardless of note type or project. It has two sections:

- **Essence** (~500 words): slow-moving working mental model, LLM-revised
  rarely, off the hot path.
- **Catalyst log**: append-only list of learning artifacts extracted from
  individual notes, each citing its source note via a ``[[note-id]]``
  wikilink. Entries carry an observational flag (``new``, ``agrees``,
  ``contradicts``, ``extends``) honestly describing their relationship to
  prior entries — no validated lifecycle.

(Historically the section was titled ``## Learning log``; ``synthesis.hub.
migrate_hub_log_heading`` is the idempotent rename to the unified
``## Catalyst log`` shared with theme hubs.)

This module is the *shared core* used by both execution paths:

- ``mem hubs plan`` / ``mem hubs run`` — bulk backfill via the OpenAI SDK
  and Batches API with gpt-5-mini (see ``cli.py``).
- ``/update-hubs`` skill — daily incremental via inline Claude Code.

Both paths use the same diff model: **the hub page itself is the processed
ledger**. To find notes that still need to contribute a learning artifact
for a given concept, we query ``note_concepts`` in SQLite for all notes
tagged with that concept, then subtract the set of note IDs already cited
in the hub page's catalyst log. No frontmatter mutation on source notes.

No LLM calls live in this module — it parses, diffs, and writes. The LLM
work happens in the caller (CLI or skill). Parsing/rendering of the
shared ``## Essence`` + ``## Catalyst log`` skeleton is delegated to
``synthesis.hub`` so concept hubs and theme hubs share one spine.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from personal_mem.core.config import Config
from personal_mem.core.vault import parse_frontmatter
from personal_mem.synthesis.hub import (
    ALLOWED_FLAGS,
    CATALYST_LOG_HEADING,
    ESSENCE_HEADING,
    FLAG_AGREES,
    FLAG_CONTRADICTS,
    FLAG_EXTENDS,
    FLAG_NEW,
    Hub,
    HubLogEntry,
    extract_section,
    migrate_hub_log_heading,
    parse_log_entries,
    parse_log_section,
)

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

HUBS_DIRNAME = "concepts"
TOPICS_DIRNAME = "topics"

# Re-export for backwards compatibility — the heading is now declared in hub.py.
LEARNING_LOG_HEADING = CATALYST_LOG_HEADING


def topics_dir(config: Config) -> Path:
    """Directory that holds concept hubs (one file per concept)."""
    return config.vault_root / HUBS_DIRNAME / TOPICS_DIRNAME


def concept_hub_path(config: Config, concept: str) -> Path:
    """Filesystem path for a concept hub. No side effects."""
    safe = _slugify_concept(concept)
    return topics_dir(config) / f"{safe}.md"


def _slugify_concept(concept: str) -> str:
    """Concept slug — lowercase, kebab-case already assumed; strip filesystem chars."""
    s = concept.strip().lower()
    # Concepts are kebab-case by convention; guard against stray chars anyway.
    s = re.sub(r"[^a-z0-9\-]+", "-", s)
    return s.strip("-") or "unnamed"


# ---------------------------------------------------------------------------
# Parsed hub representation
# ---------------------------------------------------------------------------


# Backwards-compatible alias. Existing call-sites import ``LogEntry`` from
# this module; the underlying type now lives in ``synthesis.hub``.
LogEntry = HubLogEntry


@dataclass
class ConceptHub:
    """In-memory representation of a parsed concept hub page.

    Thin wrapper around ``Hub``: adds the vocab-keyed ``concept`` field
    and the ``raw_body`` retention used by writers. Delegates parsing and
    rendering to the shared spine.

    ``raw_body`` is the original body (no frontmatter) so writers can
    reconstruct the file faithfully, only replacing the two managed
    sections.
    """

    concept: str
    path: Path
    frontmatter: dict = field(default_factory=dict)
    essence: str = ""  # raw essence content (markdown body of the Essence section)
    log_entries: list[HubLogEntry] = field(default_factory=list)
    raw_body: str = ""  # original body as read from disk, for diagnostics

    @property
    def cited_ids(self) -> set[str]:
        """Set of note IDs already referenced on this hub."""
        return {e.citation for e in self.log_entries if e.citation}


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def parse_concept_hub(path: Path, concept: str | None = None) -> ConceptHub:
    """Read a concept hub file from disk and parse it.

    Missing file → returns a ConceptHub with empty essence/log. Malformed
    sections → best-effort parse, invalid entries are skipped silently
    (they'll be ignored when computing the cited-set, which simply means
    the next run may re-cite the same note — harmless).

    Tolerates both the canonical ``## Catalyst log`` and the legacy
    ``## Learning log`` heading; ``migrate_hub_log_heading`` rewrites the
    file to canonical form on first index run.
    """
    if concept is None:
        concept = path.stem

    if not path.exists():
        return ConceptHub(concept=concept, path=path)

    hub = Hub.parse(path, hub_id=concept)
    return ConceptHub(
        concept=concept,
        path=path,
        frontmatter=hub.frontmatter,
        essence=hub.essence,
        log_entries=hub.log,
        raw_body=hub.raw_body,
    )


def parse_log_section_entries(body: str, heading: str) -> list[HubLogEntry]:
    """Public helper: extract a ``## ...`` section and parse it as log entries.

    Used by themes.py to read a theme's ``## Catalyst log`` with the same
    grammar as concept hubs. Empty list if the heading is absent or the
    section has no valid entries.
    """
    return parse_log_section(body, heading)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def render_concept_hub(hub: ConceptHub, *, domains: list[str] | None = None) -> str:
    """Serialize a ConceptHub back to markdown with preserved frontmatter metadata.

    Frontmatter is refreshed (type, concept, domains, updated) but any
    previously-saved custom keys are preserved.
    """
    now = datetime.now(timezone.utc).isoformat()
    fm = dict(hub.frontmatter)
    fm["type"] = "concept-hub"
    fm["concept"] = hub.concept
    if domains:
        fm["domains"] = sorted(set(domains))
    fm["updated"] = now

    from personal_mem.core.vault import render_frontmatter

    lines = [render_frontmatter(fm), "", f"# {hub.concept}", ""]
    if domains:
        from personal_mem.synthesis.concepts import _domain_label

        dlist = " · ".join(
            f"[[concepts/{d}|{_domain_label(d)}]]"
            for d in sorted(set(domains))
        )
        lines.append(f"*Domains: {dlist}*")
        lines.append("")

    lines.append(ESSENCE_HEADING)
    lines.append("")
    if hub.essence.strip():
        lines.append(hub.essence.strip())
    else:
        lines.append("*No synthesis yet.*")
    lines.append("")

    lines.append(CATALYST_LOG_HEADING)
    lines.append("")
    if hub.log_entries:
        from personal_mem.synthesis.hub import thread_log

        for entry, depth in thread_log(hub.log_entries):
            lines.append(entry.render(depth=depth))
    else:
        lines.append("*No entries yet.*")
    lines.append("")

    # No Mermaid `## Evolution` block — the threaded log above already
    # exposes the DAG structure typographically. Mermaid was unreadable
    # past ~30 entries and produced churny diffs on every append; the
    # threaded markdown renders natively in Obsidian, scales to the largest
    # hubs, and shows append-only diffs cleanly.

    return "\n".join(lines)


def write_concept_hub(hub: ConceptHub, *, domains: list[str] | None = None) -> Path:
    """Write a concept hub to disk, creating parent dirs if needed."""
    hub.path.parent.mkdir(parents=True, exist_ok=True)
    hub.path.write_text(render_concept_hub(hub, domains=domains), encoding="utf-8")
    return hub.path


def append_log_entries(
    config: Config,
    concept: str,
    new_entries: list[HubLogEntry],
    *,
    domains: list[str] | None = None,
) -> Path:
    """Append new catalyst-log entries to a concept hub, preserving the essence.

    Loads the existing hub (or creates a new one), appends entries that
    aren't already cited, and writes the file back. Safe to call when the
    hub doesn't exist yet.

    Returns the hub path.
    """
    path = concept_hub_path(config, concept)
    hub = parse_concept_hub(path, concept=concept)
    cited = hub.cited_ids
    for entry in new_entries:
        if entry.citation and entry.citation in cited:
            continue
        if entry.flag not in ALLOWED_FLAGS:
            continue
        hub.log_entries.append(entry)
        if entry.citation:
            cited.add(entry.citation)
    return write_concept_hub(hub, domains=domains)


def ensure_concept_hub_skeleton(
    config: Config,
    concept: str,
    *,
    domains: list[str] | None = None,
) -> Path:
    """Create an empty concept-hub skeleton if one doesn't exist yet.

    Never overwrites existing content. Used by ``generate_concept_hub_skeletons``
    so every ontology concept has a hub to append against.
    """
    path = concept_hub_path(config, concept)
    if path.exists():
        return path
    hub = ConceptHub(concept=concept, path=path)
    return write_concept_hub(hub, domains=domains)


# ---------------------------------------------------------------------------
# Migration helpers — exposed at this surface for `mem index --full`.
# ---------------------------------------------------------------------------


def migrate_concept_hub_headings(config: Config) -> int:
    """Walk ``vault/concepts/topics/`` and rewrite legacy log headings.

    Idempotent: a second run is a no-op. Returns the number of files
    rewritten on this invocation. Wired into ``mem index --full`` so the
    rename happens once per vault without a separate command.
    """
    topics = topics_dir(config)
    if not topics.exists():
        return 0
    count = 0
    for path in topics.glob("*.md"):
        if migrate_hub_log_heading(path):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Diff — which notes still need to contribute to a concept hub?
# ---------------------------------------------------------------------------


def _open_index_db(config: Config) -> sqlite3.Connection:
    db = sqlite3.connect(str(config.index_db))
    db.row_factory = sqlite3.Row
    return db


@dataclass
class NoteRef:
    """A note that's tagged with a concept and may need processing for it."""

    id: str
    type: str
    title: str
    project: str
    date: str
    path: str  # relative to vault_root
    body_chars: int  # body_text length, used by the planner to estimate cost


def notes_for_concept(
    config: Config,
    concept: str,
    *,
    project: str = "",
    note_type: str = "",
) -> list[NoteRef]:
    """Query the SQLite index for all notes tagged with a concept.

    Cross-project, cross-note-type by default. Pass ``project`` / ``note_type``
    to narrow. Concept name should already be in canonical form (caller is
    responsible for alias resolution).

    Excludes hub pages themselves (``concept-hub``, ``domain-hub``) — those
    are navigation/synthesis artifacts, not source material.
    """
    if not config.index_db.exists():
        return []

    concept_lower = concept.strip().lower()
    sql = """
        SELECT n.id, n.type, n.title, n.project, n.date, n.path,
               COALESCE(length(n.body_text), 0) AS body_chars
        FROM note_concepts nc
        JOIN notes n ON n.id = nc.note_id
        WHERE nc.concept = ?
          AND n.type NOT IN ('concept-hub', 'domain-hub')
    """
    params: list = [concept_lower]
    if project:
        sql += " AND n.project = ?"
        params.append(project)
    if note_type:
        sql += " AND n.type = ?"
        params.append(note_type)
    sql += " ORDER BY n.date ASC, n.id ASC"

    db = _open_index_db(config)
    try:
        rows = db.execute(sql, params).fetchall()
    finally:
        db.close()

    return [
        NoteRef(
            id=row["id"],
            type=row["type"],
            title=row["title"] or "",
            project=row["project"] or "",
            date=row["date"] or "",
            path=row["path"] or "",
            body_chars=row["body_chars"] or 0,
        )
        for row in rows
    ]


def unprocessed_notes_for_concept(
    config: Config,
    concept: str,
    *,
    project: str = "",
    note_type: str = "",
) -> list[NoteRef]:
    """Return notes tagged with ``concept`` that are not yet cited on its hub.

    This is *the* diff. Same logic for backfill and daily runs. If the hub
    doesn't exist yet, every tagged note is unprocessed.
    """
    all_notes = notes_for_concept(
        config, concept, project=project, note_type=note_type
    )
    hub = parse_concept_hub(concept_hub_path(config, concept), concept=concept)
    cited = hub.cited_ids
    return [n for n in all_notes if n.id not in cited]


# ---------------------------------------------------------------------------
# Planner — used by `mem hubs plan`
# ---------------------------------------------------------------------------


def all_concepts_in_vault(config: Config) -> dict[str, int]:
    """Return {concept: note_count} from the SQLite index.

    Concept names are already canonical because ``indexer._sync_concepts``
    resolves aliases at index time. Excludes hub pages from the count —
    they shouldn't contribute to their own "unprocessed notes" tallies.
    """
    if not config.index_db.exists():
        return {}
    db = _open_index_db(config)
    try:
        rows = db.execute(
            """
            SELECT nc.concept, COUNT(*) as cnt
            FROM note_concepts nc
            JOIN notes n ON n.id = nc.note_id
            WHERE n.type NOT IN ('concept-hub', 'domain-hub')
            GROUP BY nc.concept
            """
        ).fetchall()
    finally:
        db.close()
    return {row["concept"]: row["cnt"] for row in rows}


@dataclass
class ConceptPlan:
    """Backfill plan for a single concept."""

    concept: str
    domains: list[str]
    total_notes: int
    unprocessed_notes: list[NoteRef]
    est_input_chars: int  # sum of note bodies — for rough token estimation


def build_plan(
    config: Config,
    *,
    project: str = "",
    note_type: str = "",
    concept_filter: str = "",
    limit_notes_per_concept: int = 0,
    limit_concepts: int = 0,
) -> list[ConceptPlan]:
    """Walk the vault and build a list of ConceptPlan entries.

    Ordered by unprocessed-note count descending (process the heaviest
    concepts first — they're where the most synthesis leverage is).

    Read-only: no LLM calls, no writes.
    """
    from personal_mem.synthesis.concepts import concept_to_domains, load_ontology

    ontology = load_ontology()
    c2d = concept_to_domains(ontology)

    concept_counts = all_concepts_in_vault(config)
    if concept_filter:
        concept_counts = {
            c: n for c, n in concept_counts.items() if c == concept_filter.lower()
        }

    plans: list[ConceptPlan] = []
    for concept, total in concept_counts.items():
        unprocessed = unprocessed_notes_for_concept(
            config, concept, project=project, note_type=note_type
        )
        if not unprocessed:
            continue
        if limit_notes_per_concept > 0:
            unprocessed = unprocessed[:limit_notes_per_concept]
        est_chars = sum(n.body_chars for n in unprocessed)
        plans.append(
            ConceptPlan(
                concept=concept,
                domains=c2d.get(concept, []),
                total_notes=total,
                unprocessed_notes=unprocessed,
                est_input_chars=est_chars,
            )
        )

    # Heaviest concepts first
    plans.sort(key=lambda p: len(p.unprocessed_notes), reverse=True)
    if limit_concepts > 0:
        plans = plans[:limit_concepts]
    return plans


# ---------------------------------------------------------------------------
# LLM contract — shared by CLI backfill and /update-hubs skill
# ---------------------------------------------------------------------------

HUB_EXTRACTION_SYSTEM = """You are extracting learning artifacts for a single concept hub in a personal knowledge vault. The user maintains a hub page per concept with two sections:

1. **Essence** — a short (≤500 word) working mental model of the concept. Slow-moving. Only flag for revision when a source genuinely shifts the model.
2. **Catalyst log** — an append-only list of discrete learning artifacts, each citing a vault note via a [[note-id]] wikilink.

Your job for each note you see: read it, and decide what (if anything) it contributes to this concept's catalyst log.

**The bar is high.** An artifact must be **durable** — something a future user, browsing this hub a year from now, would still want to be reminded of.

Passes the bar:
- a non-obvious technique
- a counterintuitive framing or mental model
- a load-bearing constraint (the kind of thing whose violation breaks something downstream)
- a hard-won gotcha (the kind of thing that cost time to learn)
- a strong reference (a paper, repo, or page worth coming back to)
- a real decision-changing data point

Does NOT pass:
- routine operations ("ran the test suite", "synced the lockfile")
- commodity facts ("pytest fixtures exist", "X has a CLI")
- session ephemera ("19/19 tests green", "build took 7s")
- a near-duplicate of an existing entry shown in the recent log — even if it comes from a different note. Look at the recent entries before deciding; if your candidate artifact substantially restates one already there, return empty.

**Most notes contribute 0–1 artifacts.** A genuinely rich note may warrant 2; the cap is 3 but you should rarely reach it. When in doubt, return empty — a sparse log of strong artifacts beats a dense log of weak ones.

**Flag assignment is best-effort here.** A separate linkage pass runs over the full chronological log later and rewrites the flags into a temporal DAG. So default to `new` and don't overthink it. Use `agrees` / `extends` / `contradicts` only when the connection to a specific listed log entry is obvious from your reading of this note alone; in that case, include `ref` = that entry's exact date.

Entry text must be **short** (1–3 sentences, max ~200 chars). Distilled, not summarized. Terse artifact statements, not paraphrases of the note.

Return a JSON object. No prose outside the JSON. Format:

```
{
  "essence_revision_needed": false,
  "entries": [
    {"flag": "new", "ref": null, "text": "Short artifact text here."},
    {"flag": "contradicts", "ref": "2026-01-15", "text": "Counter-claim."}
  ]
}
```

Set `essence_revision_needed: true` only if this note contains something that would genuinely change the essence's working mental model. Do not set it for incremental additions — those belong in the log. Essence revision is rare."""


HUB_EXTRACTION_USER_TEMPLATE = """Concept: **{concept}**

## Current hub state

### Essence
{essence}

### Recent catalyst log entries
{recent_entries}

## Note to process

- id: `{note_id}`
- type: `{note_type}`
- project: `{project}`
- date: `{date}`
- title: {title}

### Note body

{body}

---

Extract 0–3 learning artifacts for the concept `{concept}` from this note. Return JSON only."""


def build_extraction_user_prompt(
    *,
    concept: str,
    essence: str,
    recent_entries: list[HubLogEntry],
    note_id: str,
    note_type: str,
    project: str,
    date: str,
    title: str,
    body: str,
    recent_limit: int = 25,
    full_log_threshold: int = 50,
) -> str:
    """Render the user prompt for extracting artifacts from a single note.

    When the existing log is small (≤``full_log_threshold`` entries) the
    full log is shown so the model can see every potential predecessor.
    Past that, only the most recent ``recent_limit`` entries are shown to
    cap prompt size — a defensible compromise since the linkage pass
    sees the full log later anyway.
    """
    essence_text = essence.strip() or "*No synthesis yet.*"
    if not recent_entries:
        recent = []
    elif len(recent_entries) <= full_log_threshold:
        recent = recent_entries
    else:
        recent = recent_entries[-recent_limit:]
    if recent:
        recent_text = "\n".join(e.render() for e in recent)
    else:
        recent_text = "*No entries yet.*"
    return HUB_EXTRACTION_USER_TEMPLATE.format(
        concept=concept,
        essence=essence_text,
        recent_entries=recent_text,
        note_id=note_id,
        note_type=note_type or "note",
        project=project or "(none)",
        date=date[:10] if date else "",
        title=title,
        body=body,
    )


def parse_llm_response(
    raw: str,
    *,
    note_id: str,
    run_date: str,
) -> tuple[list[HubLogEntry], bool]:
    """Parse a structured LLM response into HubLogEntry objects.

    Returns (entries, essence_revision_needed). Tolerates JSON wrapped
    in ```json ... ``` code fences. Rejects entries with unknown flags
    or missing text.

    ``run_date`` is the date stamped on every returned entry — callers
    should pass the *source note's* date (YYYY-MM-DD) so the log becomes
    a true temporal record of when each artifact was learned. Passing a
    uniform backfill date flattens the log into a single point in time
    and loses temporal structure.

    ``note_id`` is the wikilink citation appended to every entry.

    Any `[[...]]` wikilinks embedded in the LLM's artifact text are
    stripped, since the citation is appended separately. Without this
    step the rendered line carries duplicated `[[note-id]] — [[note-id]]`
    tails whenever the LLM quoted the citation inline.
    """
    import json as _json

    text = raw.strip()
    if text.startswith("```"):
        # Strip code fences (```json ... ```)
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        data = _json.loads(text)
    except _json.JSONDecodeError:
        return [], False

    if not isinstance(data, dict):
        return [], False

    essence_flag = bool(data.get("essence_revision_needed", False))
    raw_entries = data.get("entries", [])
    if not isinstance(raw_entries, list):
        return [], essence_flag

    entries: list[HubLogEntry] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        flag = str(item.get("flag", "")).lower()
        if flag not in ALLOWED_FLAGS:
            continue
        text_val = str(item.get("text", "")).strip()
        if not text_val:
            continue
        text_val = _strip_inline_wikilinks(text_val)
        if not text_val:
            # Entry was entirely a wikilink citation — nothing useful left.
            continue
        ref = item.get("ref") or ""
        entries.append(
            HubLogEntry(
                date=run_date,
                flag=flag,
                ref=str(ref) if ref else "",
                text=text_val,
                citation=note_id,
            )
        )
    return entries, essence_flag


# Matches a `[[...]]` wikilink (incl. `[[target|display]]`) possibly wrapped
# in `( … )` plus any surrounding whitespace/dashes/commas/colons so we don't
# leave dangling connectives or empty parens where the citation used to be.
# Examples this must strip cleanly:
#   "foo [[n-1]] bar"        → "foo bar"
#   "foo ([[n-1]]) bar"      → "foo bar"
#   "foo ([[n-1]])."         → "foo."
#   "implemented in [[n-1]]" → "implemented in"   (trailing cleanup below)
#   "foo — [[n-1]] — bar"    → "foo bar"
_INLINE_WIKILINK_RE = re.compile(
    r"\s*[—\-–,:;]?\s*\(?\s*\[\[[^\]]+\]\]\s*\)?\s*[—\-–,:;]?\s*"
)
# Empty-paren fragments the LLM occasionally leaves when the wikilink was
# the only thing inside the parens.
_EMPTY_PARENS_RE = re.compile(r"\(\s*\)")


def _strip_inline_wikilinks(text: str) -> str:
    """Remove any `[[...]]` wikilinks the LLM embedded in artifact text.

    The render path always appends the citation as a trailing wikilink,
    so leaving inline copies in the text produces duplicated citations in
    the final markdown line. This keeps the text content clean, including
    the `( ... )` and `— ... —` wrappers the LLM sometimes puts around a
    citation, and the empty parens left when the wikilink was the sole
    content of the parens.
    """
    cleaned = _INLINE_WIKILINK_RE.sub(" ", text)
    cleaned = _EMPTY_PARENS_RE.sub(" ", cleaned)
    # Collapse double spaces and tidy trailing punctuation dangling on its own.
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    # Fix " ." / " ," / " ;" artifacts where punctuation got separated.
    cleaned = re.sub(r"\s+([.,;:])", r"\1", cleaned)
    # "implemented in ." → "implemented in" (strip trailing " in .")
    cleaned = re.sub(r"\s+(in|at|via|as|by|on|to)\s*\.\s*$", ".", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" —-–,:;")


def plan_to_dict(plans: list[ConceptPlan]) -> dict:
    """Serialize a plan list for JSON dump.

    Token estimates use the very rough heuristic of 4 chars/token, plus a
    small fixed overhead per request for the task prompt. These are
    estimates for user review — actual usage is reported in the run log.
    """
    CHARS_PER_TOKEN = 4
    OVERHEAD_TOKENS_PER_REQUEST = 400  # instruction + current hub state

    total_notes = sum(len(p.unprocessed_notes) for p in plans)
    total_input_tokens = sum(
        (p.est_input_chars // CHARS_PER_TOKEN) + OVERHEAD_TOKENS_PER_REQUEST * len(p.unprocessed_notes)
        for p in plans
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_concepts": len(plans),
        "total_notes": total_notes,
        "est_input_tokens": total_input_tokens,
        "concepts": [
            {
                "concept": p.concept,
                "domains": p.domains,
                "total_notes_in_vault": p.total_notes,
                "unprocessed_count": len(p.unprocessed_notes),
                "est_input_chars": p.est_input_chars,
                "unprocessed_notes": [
                    {
                        "id": n.id,
                        "type": n.type,
                        "title": n.title,
                        "project": n.project,
                        "date": n.date,
                        "path": n.path,
                        "body_chars": n.body_chars,
                    }
                    for n in p.unprocessed_notes
                ],
            }
            for p in plans
        ],
    }
