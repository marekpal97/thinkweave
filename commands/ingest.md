---
name: ingest
owns_mechanic: input_routing
source_type: [paper, repo, article, "*"]
capabilities: [import]
consumes: [weave_sources_config, weave_concepts, weave_create]
produces: [vault/sources/**]
tools:
  - Read
  - Bash
  - WebFetch
  - weave_sources_config
  - weave_concepts
  - weave_create
description: Universal input router — classifies input shape (URL / file / text / structured-id) and dispatches to the appropriate ingestion skill. The single user-facing front door for getting external content into the vault.
---

# /ingest — Universal Input Router

The single front door for getting external content into the vault. You
hand `/ingest` a URL, a file path, an inline text snippet, or a
structured ID — it figures out the input *shape*, decides what kind of
source it likely is, and dispatches to the right specialist skill. New
users start here.

This file is the **dispatch layer only**. No fetching, no
summarization, no `weave_create` directly. All of that lives in the
dispatched subskills (see `/research`, `/research-paper`, `/capture`,
`/ingest-paper-file`, …). Keep this file thin.

---

## Classification table

Classify on input shape first, then dispatch. First match wins. Under the plugin install, skills resolve namespaced — if a bare `Skill(skill="X")` dispatch fails with an unknown skill, retry as `thinkweave:X` (applies to every dispatch row below).

| Input shape         | Detection                                                                                                            | Dispatch                                                                                                  |
|---------------------|----------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------|
| **URL**             | starts with `http://` or `https://`, or matches a `url_patterns` entry from `weave_sources_config()`                    | `Skill(skill="research", args="<url>")` (delegates source-type classification to `/research`)              |
| **Existing file**   | resolves to an actual file on disk; check via `Bash("test -f <path> && echo OK")`                                     | file-type detection (see below) → dispatch to file-shape subskill                                          |
| **Structured ID**   | starts with a known scheme prefix: `arxiv:`, `doi:`, `gh:` / `github:`, `isbn:`, `hf:` / `huggingface:`, `pmid:`, …    | resolve to canonical URL or fetch path → fall through to the URL row                                       |
| **Inline text**     | none of the above; user supplied raw text via `--text "..."` flag or piped on stdin                                   | `Skill(skill="capture", args="<text>")`                                                                    |

### Structured-ID resolution (cheat sheet)

- `arxiv:2401.12345` → `https://arxiv.org/abs/2401.12345`
- `doi:10.1000/xyz123` → `https://doi.org/10.1000/xyz123`
- `gh:owner/repo` or `github:owner/repo` → `https://github.com/owner/repo`
- `isbn:9780262033848` → search-fallback (no canonical URL); create a `note` source with the ID and a TODO marker for manual lookup.
- `hf:org/model` → `https://huggingface.co/org/model`

After resolution, re-classify the canonical URL via the URL row.

---

## File-type detection

When the input resolves to a file on disk, branch on extension:

- **`.pdf`** → if the document is a paper (arxiv ID in filename or first-page text, academic title structure), dispatch to `Skill(skill="ingest-paper-file", args="<path>")`. If it's a generic PDF (book chapter, slide deck, report), dispatch to the generic file-ingest fallback below.
- **`.epub` / `.mobi`** → book-shaped. For now, create a `note` source with title + author extracted from the file's metadata (or filename) and the file copied as a raw companion. **Flag as a follow-up** to add a proper `book` source-type spec.
- **`.md` / `.txt`** → plain text. `Read` the file and treat its contents as inline text → dispatch to `/capture` with the file body as the text argument.
- **`.png` / `.jpg` / `.jpeg` / `.webp` / `.gif`** → image. For now, create a `note` source with the image copied as a companion plus a brief description (filename + Read-derived caption if you want to peek). **Flag as a follow-up** to add OCR / multimodal interpretation.
- **`.html` / `.htm`** → treat as a saved web page; extract the visible text (basic strip), then dispatch to `/capture` with the extracted text and the original URL preserved in frontmatter if discoverable from the file.
- **Other** → fall back to a generic file-ingest flow: create a `note` source via `weave_create` with the file copied as a raw companion and a stub one-line summary derived from the filename. Surface clearly in the report.

---

## Modes

### A. Direct input (default)

User passes a single argument. Classify it via the table above; dispatch.

```
/ingest https://arxiv.org/abs/2401.12345
/ingest ~/Downloads/transformer-paper.pdf
/ingest arxiv:2401.12345
/ingest --text "Quote: 'X is the new Y' — overheard at a meetup"
```

### B. Multiple inputs

User passes `--inputs file1 url1 file2 …` — classify each in turn, dispatch sequentially, then report a summary table.

### C. Stdin / `--text "…"` (inline text)

User pipes content or supplies `--text "…"`. Treat as inline text → dispatch to `/capture` with the text as the argument.

```
echo "interesting quote about regimes" | /ingest
/ingest --text "Conversation note: prefer reasoning models for X."
```

---

## Dispatch protocol

For each input:

1. **Classify** the shape via the table above.
2. **Resolve** if it's a structured ID (rewrite to a canonical URL, then re-enter at the URL row).
3. **Dispatch** via the `Skill` tool with `skill="<name>"` and `args="<input>"`.
4. **Capture** the returned `src-` ID (or skip / failure reason) for the report.

Do not fan out concurrently; process inputs sequentially so each
subskill's ontology load is amortised across its own batch instead of
being interleaved.

---

## Concept assignment rules

Same rules as `/research` and `/discover`. Subskills enforce these — `/ingest` itself does not assign concepts. For reference:

1. Use ontology terms only — load the vault's merged ontology via `weave_concepts(action="list")` (not the source-tree `ontology.yaml`).
2. Genuinely-new terms → `proposed_concepts`, never `concepts`.
3. Minimum 2 concepts per created source.

---

## Reporting

Per input:

- classified shape (URL / file / text / structured-id)
- dispatched skill (e.g. `research`, `capture`, `ingest-paper-file`)
- returned `src-` ID (or skip reason / failure)

End-of-batch summary:

```
## /ingest report
- Processed: N
- Skipped:   M  (reasons listed above)
- Failed:    K  (reasons listed above)

Next: <suggested follow-up — e.g. "Run /wrap to capture session
context" or "Run /discover for downstream gap analysis">
```

---

## What this skill does NOT do

- **No fetching.** All HTTP / file reads happen in the dispatched subskill. `/ingest` never touches `WebFetch` or `Read` for content (it may use `Read` to spy at a file's first bytes for type detection, but the body content is the subskill's job).
- **No summarization.** Subskills produce the brief.
- **No `weave_create` directly.** Subskills own the write.
- **No source-type classification.** `/research` classifies URLs; the file-/text-shape subskills classify their own content. `/ingest` only dispatches by *shape*.
- **No queue management.** For batch drains use `/drain --source-type <slug>`; `/ingest` is the single-input front door.

---

## Why a shape-first router

Today the framework's IMPORT capability splits across multiple
input-shape doors:

- `/research` — URL only
- `/substack` — disk inbox only
- `weave import` — CLI file/dir only
- inline-pasted text — no front door at all

A new user with a *thing* in hand has to know which command matches
their input shape *before* they know what source type it is. That's
the wrong polarity. `/ingest` inverts it: hand it any shape, it
figures out where to send it.

Source-type classification still happens — but later, inside the
subskill that knows the shape's quirks (URL pattern matching for
`/research`, file-type extension for `/ingest-paper-file`, content
introspection for `/capture`).
