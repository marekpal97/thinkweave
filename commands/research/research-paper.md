---
name: research-paper
source_type: paper
capabilities: [import]
tools:
  - Read
  - Write
  - WebFetch
  - Bash
  - mem_search
  - mem_concepts
  - mem_concept_search
  - mem_create
  - mem_update
  - mem_link
  - mem_queue
description: Fetch a paper (arxiv / openreview / PDF), extract a technical brief, write it as a `source_type: paper` note. Called from `/research` (router) or `/drain --source-type paper`.
---

# /research-paper — Ingest a paper

Single-URL pipeline. The `/research` router classified the URL as a
paper; you handle the rest.

## Steps

### 1. Fetch

For arxiv:
1. `WebFetch` the abstract page (`https://arxiv.org/abs/<id>`) — title, authors, date, abstract.
2. `WebFetch` the HTML version (`https://arxiv.org/html/<id>`) for fuller body — methods, results, conclusions.
3. If HTML isn't available, abstract + (optionally) the PDF text are the fallback.

For openreview / generic PDFs: `WebFetch` the URL; if it returns a PDF,
pull whatever extracted text the harness gives back.

**Size guard**: if extracted text > 100k chars or the URL pattern looks
like a book (`/book/`, Google Books, Project Gutenberg), skip — log
"Book detected, skipped" and exit.

### 2. Load ontology + check vault

```
Read src/personal_mem/ontology.yaml
mem_concepts(min_count=2)
```

```
mem_search(query="<key terms from abstract>", mode="hybrid", limit=5)
mem_concept_search(concepts=["<best-fit concept>", …], match_mode="any", limit=5)
```

Note related notes for the **Vault Connections** section.

### 3. Dedup against the queue

```
mem_queue(action="peek", source_type="paper", n=50)
```

If the URL or the arxiv id matches an unclaimed queue item, that's the
one being drained — nothing extra to do. If the URL appears as an
already-archived item, skip — we've ingested this.

### 4. Write the source note

Use `mem_create` with `type="source"` and `source_type="paper"`.
Required frontmatter:

```
mem_create(
  type="source",
  title="<descriptive title>",
  body="<technical brief — see template below>",
  tags=["paper"],
  concepts=["<≥3 ontology concepts>"],
  frontmatter={
    "source_type": "paper",
    "url": "<canonical URL>",
    "authors": ["<author list>"],
    "arxiv_id": "<id if applicable>",
    "doi": "<id if applicable>",
    "abstract": "<first 300 chars>",
    "proposed_concepts": ["<new concepts not in ontology>"]
  }
)
```

`mem_create` returns the source directory path. Save the raw extracted
text alongside as `raw.txt` and update the note's `raw_path`:

```
Write <source_dir>/raw.txt
mem_update(note_id="<src-id>", frontmatter_updates={"raw_path": "raw.txt"})
```

**Do NOT set `project`** — sources are global.

### 5. Link

For papers that cite vault sources you found in step 2:
```
mem_link(source_id="<new-src-id>", target_id="<cited-src-id>", edge_type="cites")
```
For weaker connections: `relates_to`.

### 6. Archive queue item

If you were called by `/drain --source-type paper`, archive the queue
item:
```
mem_queue(action="archive", source_type="paper", item_id="<queue-id>", status="done")
```

### 7. Report

```
src-id: <id>
title: <title>
concepts: [<assigned>]
proposed_concepts: [<new>]
related: [<src-/n-IDs from vault connections>]
```

---

## Body template (paper)

```markdown
## Abstract
[verbatim from paper]

## Methods & Technical Approach
[architecture, loss functions, training regime, optimization]
[prior work this builds on — the specific technical foundation]
[key assumptions and constraints]

## Key Claims & Results
- [specific claim + quantitative evidence: "94.2% on X, up from 91.1%"]
- [ablation findings]
- [scaling behavior if relevant]

## Technical Underpinnings
[core insight or mechanism that makes this work]
[theoretical grounding]

## Limitations & Open Questions
- [what authors acknowledge doesn't work]
- [assumptions that may not hold]

## Vault Connections
- Relates to [[existing-note-title]] — [why]

## Raw Content
[[<slug>/raw.txt]]
```

## Concept rules

- ≥3 concepts. Map to ontology when natural; propose new ones (lowercase,
  hyphenated, specific) when the source introduces real novelty.
- Established → `concepts`. New → `proposed_concepts`. Never both.
- Don't duplicate between tags and concepts.
