---
name: ingest-paper-file
owns_mechanic: paper_file_ingest
source_type: paper
capabilities: [import]
consumes: [weave_sources_config, weave_concepts, weave_create]
produces: [vault/sources/papers/**]
tools:
  - Read
  - Bash
  - Write
  - weave_sources_config
  - weave_concepts
  - weave_create
  - weave_update
description: Local PDF paper ingestion — extract text, derive title/authors, detect arxiv ID, write a `source_type: paper` note with the PDF and extracted text staged as companions. Called from `/ingest` for the local-PDF file shape.
---

# /ingest-paper-file — Ingest a Local PDF Paper

Single-file pipeline. The `/ingest` router classified the input as a
local PDF and routed it here. Mirrors `/research-paper`'s output shape
so downstream skills (`/discover`, hubs, graph walks) treat the result
identically — the only difference is the source: a file on disk
instead of a URL.

---

## Steps

### 1. Verify the file

```
Bash("test -f '<path>' && file '<path>' | grep -i pdf")
```

If the file isn't a PDF, abort and let `/ingest` fall back to the
generic file-ingest flow.

### 2. Extract text

```
Bash("pdftotext '<path>' - 2>/dev/null | head -c 200000")
```

The `head -c` cap is a defensive bound — long papers are still useful
truncated, and the full body remains in the staged PDF. If `pdftotext`
isn't available on the system (`which pdftotext` returns nothing):

- Fall back to creating a `note` source with the file copied as a raw
  companion and a TODO marker:

  ```
  ## TODO
  Manual summary required — `pdftotext` not available at ingest time.
  See [[<slug>/source.pdf]] for the original.
  ```

- Skip the rest of the steps below. Report `manual-summary-needed`
  in the dispatch return.

### 3. Identify metadata

From the extracted text (typically the first ~2000 chars contain title,
authors, abstract):

- **Title** — first non-trivial heading-shape line; strip "arXiv:..." preprints prefix if present.
- **Authors** — line(s) following the title, before the abstract; split on `,` and `and`.
- **Abstract** — text between an "Abstract" heading and the first body section.
- **arxiv ID** — regex over the first page for `arXiv:<id>` or a URL pattern matching `arxiv.org/abs/<id>`. Capture `<id>`.
- **DOI** — regex for `doi:` or `https://doi.org/<id>`.

If title can't be inferred, fall back to the filename (sans extension).
If authors can't be inferred, leave the list empty — `weave doctor`
flags missing authors as a hygiene issue, not a blocker.

### 4. Load ontology + check vault

```
Read src/thinkweave/ontology.yaml
weave_concepts(min_count=2)
```

```
weave_search(query="<title + key abstract phrases>", mode="hybrid", limit=5)
```

If a hit comes back with a matching arxiv ID or near-identical title,
skip — we already ingested this paper. Surface the existing `src-` ID
in the report so the user knows where it landed.

### 5. Write the source note

```
weave_create(
    type="source",
    title="<extracted title>",
    body="<technical brief — see template below>",
    tags=["paper"],
    concepts=["<≥3 ontology concepts>"],
    frontmatter={
        "source_type": "paper",
        "url": "<arxiv URL if id found, else empty>",
        "authors": ["<extracted authors>"],
        "arxiv_id": "<id if found>",
        "doi": "<id if found>",
        "abstract": "<first 300 chars of extracted abstract>",
        "ingest_origin": "local-pdf",
        "proposed_concepts": ["<new concepts>"],
    },
)
```

`weave_create` returns the source directory path (folder layout per the
`paper` SourceTypeSpec). **Do NOT set `project`** — sources are global.

### 6. Stage the PDF + extracted text as companions

Mirror the `folder` layout convention: the source note lives at
`<source_dir>/source.md`; the original PDF and the extracted text
become siblings.

```
Bash("cp '<original-path>' '<source_dir>/source.pdf'")
Write('<source_dir>/raw.txt', '<full extracted text>')
weave_update(note_id="<src-id>",
           frontmatter_updates={"raw_path": "raw.txt",
                                "pdf_path": "source.pdf"})
```

The indexer skips `raw.txt` automatically — it's an archival artifact,
not a standalone note.

### 7. Report

```
src-id: <id>
title: <title>
arxiv_id: <id or none>
concepts: [<assigned>]
proposed_concepts: [<new>]
ingest_origin: local-pdf
```

---

## Body template (paper)

Same as `/research-paper` — the brief shape is identical so downstream
consumers don't need to special-case the origin:

```markdown
## Abstract
[verbatim from paper]

## Methods & Technical Approach
[architecture, loss functions, training regime, optimization]
[prior work this builds on]
[key assumptions and constraints]

## Key Claims & Results
- [specific claim + quantitative evidence]
- [ablation findings]
- [scaling behavior if relevant]

## Technical Underpinnings
[core insight or mechanism that makes this work]

## Limitations & Open Questions
- [what authors acknowledge doesn't work]

## Vault Connections
- Relates to [[existing-note-title]] — [why]

## Raw Content
[[<slug>/raw.txt]] · [[<slug>/source.pdf]]
```

---

## Concept rules

Same as `/research-paper`:

- ≥ 3 concepts.
- Map to ontology when natural; propose new terms (lowercase,
  hyphenated, specific) for genuine novelty.
- Established → `concepts`. New → `proposed_concepts`. Never both.
- Do not duplicate between `tags` and `concepts`.

---

## What this skill does NOT do

- **No URL resolution.** If the paper exists at a URL too, the user can pass that to `/ingest` separately; this skill only handles the local file shape.
- **No multi-file ingestion.** One PDF per call. For batches, use `/ingest --inputs file1 file2 …`.
- **No OCR.** Scanned-image PDFs (where `pdftotext` returns empty) fall through to the manual-summary path.
