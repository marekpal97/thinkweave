---
name: research-article
source_type: article
capabilities: [import]
tools:
  - Read
  - Write
  - WebFetch
  - WebSearch
  - mem_search
  - mem_concepts
  - mem_graph
  - mem_create
  - mem_update
  - mem_link
  - mem_queue
description: Fetch a web article, extract argument + claims + evidence, write it as a `source_type: article` note. Called from `/research` (router) or `/drain --source-type article`.
---

# /research-article — Ingest an article

Single-URL pipeline. The router classified the URL as an article (the
default fallback when no paper/repo pattern matched).

## Steps

### 1. Fetch

```
WebFetch("<url>")
```

If the response is too short or garbled (JS-heavy SPA), try a backup:
```
WebSearch("<article title or first sentence>")
```
Look for a cached/alternative version (Wayback Machine, content
republished elsewhere). If still nothing useful, skip — log the URL.

### 2. Load ontology + check vault

```
Read src/personal_mem/ontology.yaml
mem_concepts(min_count=2)
mem_search(query="<key terms>", mode="hybrid", limit=5)
mem_graph(filter="concept_walk", concepts=["<best-fit>"], match_mode="any", limit=5)
```

### 3. Write the source note

```
mem_create(
  type="source",
  title="<article title>",
  body="<argument + evidence brief — see template below>",
  tags=["article"],
  concepts=["<≥3 ontology concepts>"],
  frontmatter={
    "source_type": "article",
    "url": "<canonical URL>",
    "authors": ["<author>"],
    "publication": "<site name if identifiable>",
    "proposed_concepts": ["<new concepts>"]
  }
)
```

Save the fetched text to the source directory:
```
Write <source_dir>/raw.md
mem_update(note_id="<src-id>", frontmatter_updates={"raw_path": "raw.md"})
```

### 4. Link + archive queue

Link related vault notes via `relates_to`. If invoked from `/drain`,
archive the queue item with status `done`.

### 5. Report

`src-id`, title, concepts, proposed concepts, related vault notes.

---

## Body template (article)

```markdown
## Core Argument
[the thesis, not just the topic]

## Key Claims & Evidence
- [specific claim + evidence]
- [distinguish data-backed claims from opinion / anecdote]
- [quantitative findings or benchmarks cited]

## Technical Detail
[methods, frameworks, approaches discussed]
[concrete examples or case studies]

## Vault Connections
- Relates to [[existing-note-title]] — [why]

## Raw Content
[[<slug>/raw.md]]
```

## Concept rules

Same as `/research-paper`: ≥3 concepts, ontology-first, propose new ones
under `proposed_concepts`.
