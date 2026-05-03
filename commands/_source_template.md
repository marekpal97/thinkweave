---
source_type: {SLUG}
capabilities: [import]  # any subset of [import, acquire, discover] — delete the ones you don't implement
tools:
  - Read
  - WebFetch
  - Bash
  - mem_search
  - mem_create
  - mem_concepts
description: One sentence describing what this skill does. Appears in `mem sources list` and `mem skill list`.
---

# /{SKILL_NAME} — Ingest {SOURCE_TYPE} into the Knowledge Vault

<!--
  UNIVERSAL SOURCE-SKILL TEMPLATE.

  To add a new source type to personal_mem:

    1. Add a SourceTypeSpec entry to src/personal_mem/sources/registry.py
       with your source_type slug, bucket name, and layout ("flat",
       "folder", or "author_folder").

    2. Copy this file to commands/{your-skill-name}.md.

    3. Fill in the YAML frontmatter above:
         - source_type: your registered slug (or a list of slugs if the
           skill handles multiple types, like /research does)
         - capabilities: any subset of [import, acquire, discover]
         - tools: every tool the skill calls — used by `mem skill run` to
           know what tool surface to provide
         - description: one sentence

    4. Delete the capability sections below that your skill doesn't
       implement. A skill can ship with just one capability (e.g. /substack
       is acquire-only; the chatgpt CLI importer is import-only).

    5. Write the bespoke fetch/parse/interpret logic per section. Skills
       stay procedural — there is no shared fetch framework.

    6. Verify: `mem sources show {your-slug}` and `mem skill show
       {your-skill-name}` should find your new type and skill.

    7. Delete this comment block before committing.

  Two skills to pattern-match from:
    - commands/research.md   — import + acquire for paper/repo/article
    - commands/substack.md   — acquire-only for substack disk inbox

  Keep your skill procedural and source-specific. Do NOT extract a shared
  "skill framework" — each ingestion path is deliberately bespoke because
  source types have genuinely different fetch/parse/interpret logic.
-->

You are ingesting {SOURCE_TYPE} entries into the personal_mem vault. Each ingested entry becomes a `source.md` in the layout declared by this source type's `SourceTypeSpec` in `src/personal_mem/sources/registry.py`, with any raw companion content (`raw.md`, `snapshot.md`, `assets/`) alongside.

**Arguments**: {ARGUMENT_DESCRIPTION — e.g. "One or more URLs", "`--queue` to drain the research queue", "`--drain` to process the disk inbox"}

---

## Ontology tie-in (every run, before any capability)

Every source-ingestion skill starts here. Concept consistency is what makes the knowledge graph work — new sources must reuse existing vocabulary when possible.

```
Read src/personal_mem/ontology.yaml
mem_concepts(min_count=2)
```

Load the ontology **once at the start of the batch**, not per item. Map concepts to existing ontology terms where they fit. When a source introduces vocabulary with no natural fit, propose it via the `proposed_concepts` frontmatter field (not `concepts`) — `/mem-resolve-concepts` will canonicalise proposals in a later pass. Minimum 2 concepts per source note.

---

## Import (OPTIONAL — delete if not implemented)

One-shot: user hands you a URL, file path, or identifier, and you produce one source note.

### 1. Fetch the content

{DESCRIBE YOUR FETCH STRATEGY. Be specific about:
  - What to download and from where (`WebFetch`, `curl`, `git clone`, local `Read`)
  - Where to stage it temporarily
  - How to verify integrity (file size, content type, expected markers)
  - How to handle failures (retry? skip? leave for manual?)

  Concrete examples to pattern-match:
    - commands/research.md paper path: WebFetch arxiv abstract → parse PDF link → curl → extract text
    - commands/research.md repo path: shallow git clone → walk key files → concatenate into snapshot.md
    - commands/substack.md: Read disk-inbox bundle → copy assets → rewrite image paths → multimodal Read each figure}

### 2. Check for duplicates

Before creating, check whether this source is already in the vault:

```
mem_search(query="<title or URL fragment>", type="source", limit=3)
```

If a hit comes back, either skip (default) or update the existing entry via `mem_update` — don't create a duplicate.

### 3. Create the source note

```
mem_create(
    note_type="source",
    title="<descriptive title>",
    body="<body template — see below>",
    tags=[{TAG_LIST}],
    concepts=["<ontology-term-1>", "<ontology-term-2>", ...],
    frontmatter={
        "source_type": "{SLUG}",
        "url": "<canonical URL or URI>",
        "authors": [...],
        "proposed_concepts": [<new terms>],
        {SOURCE_SPECIFIC_FIELDS},
    },
)
```

The `source_type` field is what routes the file into its bucket under `vault/sources/`. Routing is handled automatically by `VaultManager.create_note` via the spec in `src/personal_mem/sources/registry.py`.

### 4. Save raw companion content

```
raw_path = source_path.parent / "raw.md"   # or raw.txt, snapshot.md, paper.pdf
raw_path.write_text(raw_content, encoding="utf-8")
```

The indexer automatically skips files named `raw.md`, `raw.txt`, and `snapshot.md` so they don't pollute FTS or the graph — they are archival artifacts, not standalone notes.

---

## Acquire (OPTIONAL — delete if not implemented)

Batch: drain a queue or inbox into multiple source notes.

### 1. Enumerate pending items

Two common patterns:

**Semantic queue (notes tagged `todo`+`research`)** — used by `/research --queue`:
```
mem_search(query="", tags=["todo", "research"], type="note", limit=<batch>)
```
Exclude items already tagged `processing`. Process FIFO (oldest first).

**Disk inbox (files on disk)** — used by `/substack`:
```
Bash("ls $MY_INBOX/*.md 2>/dev/null")
```
Process each file, move to `$MY_INBOX/_processed/<date>/` on success.

{DESCRIBE WHICH PATTERN YOUR SKILL USES and the exact command/call.}

### 2. Claim the item (queue pattern) or move-on-success (inbox pattern)

**Queue**: re-tag `todo` → `processing` so parallel runs don't double-process.
```
mem_update(note_id="<id>", frontmatter_updates={"tags": ["processing", "research"]})
```
If processing fails, the item stays tagged `processing` — recoverable.

**Inbox**: process the file in place; move it to `_processed/` only after `mem_create` succeeds. If processing fails, the file stays in the inbox and will be picked up on the next run.

### 3. Per-item processing

For each claimed/enumerated item, run the **Import** pipeline above (fetch → dedupe → create → save raw).

### 4. Finalize

**Queue**: re-tag `processing` → `done` after `mem_create` returns a `src-` ID.
```
mem_update(note_id="<id>", frontmatter_updates={"tags": ["done", "research"]})
```

**Inbox**: `Bash("mv <file> $MY_INBOX/_processed/$(date +%Y-%m-%d)/")`

---

## Discover (OPTIONAL — delete if not implemented)

Gap identification: analyse what's already in the vault, find what's missing, create queue items.

### 1. Load signals

{DESCRIBE YOUR SIGNAL SOURCES. Common ones:
  - `Read vault/sources/RESEARCH_FOCUS.md` for user-declared priorities
  - `mem_concepts(action="source_counts", concepts=[...])` for per-concept coverage
  - `mem_timeline(days=14)` for recent project activity
  - `mem_search` against existing sources to avoid re-queueing duplicates}

### 2. Identify gaps

{DESCRIBE YOUR GAP LOGIC. Usually a mix of:
  - Concepts with <N sources (under-researched)
  - Domains in RESEARCH_FOCUS that don't appear in recent timeline (stale)
  - Authors the user follows whose recent work isn't in the vault}

### 3. Propose queue items

For each gap, create a `todo`+`research` tagged note that `/research` can later drain:
```
mem_create(
    note_type="note",
    title="<what to look for>",
    body="<url or search strategy>\n\n<why this matters — which concept or focus area>",
    tags=["todo", "research"],
    concepts=["<target concept>"],
)
```

If you don't have a URL yet (only a topic to search), also tag `needs-url` so `/research --resolve` can find it:
```
tags=["todo", "research", "needs-url"]
```

---

## Frontmatter shape (every source note your skill writes)

Use the canonical helper in `src/personal_mem/sources/frontmatter.py`:

```python
from personal_mem.sources import build_source_frontmatter
fm = build_source_frontmatter(
    source_type="{SLUG}",
    title="<title>",
    url="<canonical URL>",
    authors=["..."],
    # ...any source-specific fields
)
```

Or inline in `mem_create`:

```
frontmatter={
    "source_type": "{SLUG}",
    "url": "<canonical URL>",
    "authors": [...],
    "proposed_concepts": [...],
    {SOURCE_SPECIFIC_FIELDS},
}
```

**Canonical fields** (present on every source note):
- `source_type` — your registered slug
- `title` — set via the `title=` argument of `mem_create`
- `url` — canonical URL or URI; empty string for local-only content
- `authors` — list of strings
- `concepts` — at least 2, mapped to ontology terms
- `proposed_concepts` — new terms not yet in the ontology (optional)

**Source-specific fields** go after the canonical set. List yours here:
```
{LIST THE FIELDS YOUR SOURCE TYPE NEEDS — e.g. for email: `from`, `to`, `thread_id`, `received_date`, `message_id`. For a podcast: `show`, `episode_number`, `duration`, `transcript_source`.}
```

---

## Body template

```markdown
## Summary
<2-4 sentences: what this source is, why it matters to the user's focus areas>

## Key claims
- <claim 1 with evidence>
- <claim 2 with evidence>
- <claim 3 with evidence>

## Connections
<Wikilinks to related notes, sessions, or decisions already in the vault.
 Format: [[note-id]] optional annotation>

## Raw content
See [[<slug>/raw.md]] (or paper.pdf / snapshot.md / whichever companion file you staged).
```

---

## Report (standard output at end of run)

```
## {SOURCE_TYPE} Skill Report

### Ingested
- [<src-id>] <title> → <concept-1>, <concept-2>
- ...

### Skipped
- <N> duplicates (already in vault)
- <N> filtered (<reason>)

### Concepts
- Existing: <count reused from ontology>
- Proposed: <count of new terms needing /mem-resolve-concepts review>

### Next
- <suggested follow-up command — e.g. "Run `/research --queue --batch N` to continue">
```

---

## Source-specific notes

{USE THIS SECTION FOR ANYTHING PECULIAR TO YOUR SOURCE TYPE:
  - Authentication requirements and how to handle them
  - Rate limits or quota considerations
  - Known parsing edge cases
  - When to invoke this skill vs a related one
  - How this skill relates to /discover or other gap-analysis skills
  - Optional dependencies (e.g. `gh` CLI, `curl`, `ffmpeg`)
}
