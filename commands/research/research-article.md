---
name: research-article
source_type: article
capabilities: [import]
tools:
  - Read
  - Write
  - WebFetch
  - WebSearch
  - weave_search
  - weave_concepts
  - weave_graph
  - weave_create
  - weave_update
  - weave_link
  - weave_queue
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
Read src/thinkweave/ontology.yaml
weave_concepts(min_count=2)
weave_search(query="<key terms>", mode="hybrid", limit=5)
weave_graph(filter="concept_walk", concepts=["<best-fit>"], match_mode="any", limit=5)
```

### 3. Write the source note

```
weave_create(
  type="source",
  title="<article title>",
  body="<argument + evidence brief — structured per vault/config/note_formats/article.md>",
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
weave_update(note_id="<src-id>", frontmatter_updates={"raw_path": "raw.md"})
```

### 4. Link + archive queue

Link related vault notes via `relates_to`. If invoked from `/drain`,
archive the queue item with status `done`.

### 5. Report

`src-id`, title, concepts, proposed concepts, related vault notes.

---

## Body template (article)

`Read` `<vault_root>/config/note_formats/article.md` and compose the body to
the sections it lists. That file is seeded at init and **user-editable** —
the user reshapes every article brief by editing it directly, no skill
change. Keep `## Vault Connections` and `## Raw Content` so graph links and
the raw pointer land. If the file is missing, fall back to a clear,
well-structured argument brief ending with `## Vault Connections` and
`## Raw Content`.

## Concept rules

Same as `/research-paper`: ≥3 concepts, ontology-first, propose new ones
under `proposed_concepts`.
