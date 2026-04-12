# /{SKILL_NAME} — Ingest {SOURCE_TYPE} into the Knowledge Vault

<!--
  SKELETON for a new source-type ingestion skill.

  To use this template:
    1. Copy this file to commands/{skill-name}.md
    2. Replace every {PLACEHOLDER} below with your source-specific values.
    3. Add `"{source_type}": "{bucket_name}"` to VaultManager._SOURCE_BUCKETS
       in src/personal_mem/vault.py.
    4. Delete this comment block before committing.

  For worked examples showing what finished skills look like, read:
    - commands/research.md  (papers / repos / articles from URLs)
    - commands/substack.md  (disk-inbox drain with figure-aware multimodal ingestion)

  Keep your skill procedural and source-specific. Do NOT extract a shared
  "skill framework" — each ingestion path is deliberately bespoke because
  source types have genuinely different fetch/parse/interpret logic.
-->

You are ingesting {SOURCE_TYPE} entries into the personal_mem vault. Each ingested entry becomes a `source.md` under `vault/sources/{BUCKET_NAME}/<slug>/`, with any raw companion content (raw.md, snapshot.md, assets/) alongside.

**Arguments**: {ARGUMENT_DESCRIPTION — e.g. "One or more URLs", "--queue to drain the inbox", etc.}

## Steps

### 1. Load the ontology and existing concepts

Every ingestion skill starts here. Concept consistency is what makes the knowledge graph work — new sources must reuse existing vocabulary when possible.

```
Read src/personal_mem/ontology.yaml
mem_concepts(min_count=2)
```

### 2. Fetch the content

{DESCRIBE YOUR FETCH STRATEGY. Concrete examples to pattern-match from:
  - commands/research.md paper path: WebFetch the arxiv abstract page, parse the PDF link, download via curl, extract text.
  - commands/research.md repo path: Shallow git clone, walk the top-level files, concatenate the README + key source files into a snapshot.
  - commands/substack.md: Read the disk-inbox bundle, copy image assets, rewrite image paths, interpret each figure via multimodal Read.
  Be specific about: what to download, where to stage it temporarily, how to verify integrity.}

### 3. Extract concepts and metadata

For each entry:

- **Concepts**: map to existing ontology terms where possible. If you need a new term, put it in `proposed_concepts` (frontmatter field) instead of `concepts` so `/mem-resolve-concepts` can review it later. Minimum 2 concepts per source.
- **Metadata**: collect source-specific fields. {LIST THE FIELDS YOUR SOURCE TYPE NEEDS. E.g. for an email importer: `from`, `to`, `thread_id`, `received_date`, `message_id`. For a podcast importer: `show`, `episode_number`, `duration`, `transcript_source`.}

### 4. Check for duplicates

Before creating, check whether this source is already in the vault:

```
mem_search(query="<title or unique identifier>", type="source", limit=3)
```

If a hit comes back, either skip (default) or update the existing entry via `mem_update` — don't create a duplicate.

### 5. Create the source note

```
mem_create(
    note_type="source",
    title="<descriptive title>",
    body="<body template — see below>",
    tags=[{TAG_LIST — e.g. "research", "til", source-type-specific tags}],
    concepts=["<ontology-term-1>", "<ontology-term-2>", ...],
    frontmatter={
        "source_type": "{SOURCE_TYPE}",
        "url": "<canonical URL or URI>",
        {SOURCE_SPECIFIC_FIELDS},
    },
)
```

The `source_type` frontmatter field is what routes the file into its bucket under `vault/sources/{BUCKET_NAME}/`. That routing is handled automatically by `VaultManager.create_note` via the `_SOURCE_BUCKETS` map.

#### Body template

```markdown
## Summary
<2-4 sentences: what this source is, why it matters to the user's focus areas>

## Key claims
- <claim 1>
- <claim 2>
- <claim 3>

## Connections
<Wikilinks to related notes, sessions, or decisions already in the vault.
 Format: [[note-id]] optional annotation>

## Raw content
See [[<slug>/raw.md]] (or paper.pdf / snapshot.md / whichever companion file you staged).
```

### 6. Save raw companion content

Write any raw content (PDFs, cloned snapshots, clipped markdown, transcripts) alongside `source.md` in the same folder. The indexer automatically skips files named `raw.md`, `raw.txt`, and `snapshot.md` so they don't pollute the FTS or graph — they are archival artifacts, not standalone notes.

```python
raw_path = source_path.parent / "raw.md"     # or raw.txt, snapshot.md, paper.pdf
raw_path.write_text(raw_content, encoding="utf-8")
```

### 7. Link to related notes

If the source is relevant to an existing session or decision, add a typed edge:

```
mem_link(source_id="<new-source-id>", target_id="<related-id>", edge_type="derived_from")
```

### 8. Report

Print a short summary:

```
## {SOURCE_TYPE} Ingestion Report

### Ingested
- [<id>] <title> → <concepts>

### Skipped
- <N> duplicates (already in vault)
- <N> filtered (<reason>)

### Concepts
- Existing: <count reused>
- Proposed: <count of new terms needing /mem-resolve-concepts review>
```

## Source-specific notes

{USE THIS SECTION FOR ANYTHING PECULIAR TO YOUR SOURCE TYPE:
  - Authentication requirements and how to handle them
  - Rate limits or quota considerations
  - Known parsing edge cases
  - When to invoke this skill vs a related one
  - How this skill relates to /discover or other gap-analysis skills
}
