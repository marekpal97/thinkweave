---
name: capture
owns_mechanic: text_capture
capabilities: [import]
consumes: [weave_concepts, weave_create]
produces: [vault/sources/**, vault/notes/**]
tools:
  - Read
  - weave_concepts
  - weave_create
description: Inline-text ingestion (snippet, quote, brief, fragment) — classify content shape, propose source_type, create a `note` (or `source` when applicable) via `weave_create` with ontology-aligned concepts. Called from `/ingest` for the inline-text shape.
---

# /capture — Inline-Text Ingestion

You receive a chunk of free-form text — a quote, a snippet from a
chat, a paragraph the user pasted, a thought-fragment piped from
stdin. Turn it into a vault entry: `note` by default, `source` if the
content clearly maps to a registered source type (a Substack excerpt
already-fetched, a transcript paragraph, a self-contained quote with
attribution).

Called from `/ingest` for the inline-text input shape; also usable
directly when the user wants to capture text without going through
the router.

---

## Arguments

- Single positional argument (the text), **or** piped stdin, **or** `--text "…"`.
- Optional `--source-type <slug>` to skip content classification.
- Optional `--title "…"` to override the LLM-derived title.

---

## Steps

### 1. Receive the text

If invoked from `/ingest`, the text arrives as the `args` value. If
invoked directly with stdin or `--text`, normalise to a single string
in memory. If the text is empty (or whitespace-only), abort with
"No content captured."

### 2. Classify the content shape

Read the text and classify it (you, Claude, are the classifier — no
heuristics here):

| Shape       | Signal                                                                                       | Default `note_type`                                                |
|-------------|----------------------------------------------------------------------------------------------|--------------------------------------------------------------------|
| `quote`     | starts/ends with quotation marks; cites an author or speaker; under 500 chars                 | `note` (tag `quote`)                                                |
| `snippet`   | code block, formula, command, or short technical fragment                                     | `note` (tag `snippet`)                                              |
| `brief`     | self-contained mini-summary the user wrote themselves                                         | `note`                                                              |
| `fragment`  | partial paragraph, half-finished thought                                                      | `note` (tag `fragment`)                                             |
| `excerpt`   | clearly an excerpt of a known source type (substack post body, paper abstract, article copy)  | `source` with the matching `source_type`                            |
| `transcript`| dialogue lines, speaker labels, timestamp markers                                             | `source` with `source_type: conversation`                           |

When in doubt, default to `note`. The user can promote it later via
`weave update` if they decide it warrants a `source`.

### 3. Derive a title

- If `--title` was supplied, use it.
- Else: take the first sentence (cap at ~80 chars) or the first heading-shape line.
- Fallback: `"Captured note (<YYYY-MM-DD>)"`.

### 4. Load ontology + concept registry

```
Read src/thinkweave/ontology.yaml
weave_concepts(min_count=2)
```

Map the captured text to existing ontology terms. Minimum 2 concepts.
New vocabulary goes to `proposed_concepts`, never `concepts` —
`/weave-resolve-concepts` canonicalises proposals later.

### 5. Create the note

For the default (`note`) path:

```
weave_create(
    type="note",
    title="<derived title>",
    body="<the captured text, lightly formatted — preserve original
            whitespace; if it's a quote, wrap in '> ' blockquote>",
    tags=["captured", "<shape-tag from step 2>"],
    concepts=["<ontology-term-1>", "<ontology-term-2>", ...],
    frontmatter={
        "captured_at": "<ISO datetime>",
        "captured_via": "/capture",
        "proposed_concepts": ["<new terms>"],
    },
)
```

For the `source` path (when content clearly maps to a registered
source type), follow the same shape but with `type="source"` and the
appropriate `source_type` in frontmatter — see `commands/_source_template.md`
for the canonical fields. **Do NOT set `project`** — sources are global.

### 6. Report

```
- src-id (or n-id): <id>
- title: <title>
- shape: <quote | snippet | brief | fragment | excerpt | transcript>
- type: <note | source>
- concepts: [<assigned>]
- proposed_concepts: [<new>]
```

---

## Concept rules

- **Ontology-first.** Map to existing terms before proposing new ones.
- **≥ 2 concepts.** Concepts are the primary linkage mechanism — fewer than 2 means the note is orphaned in the graph.
- **No duplication between `tags` and `concepts`.** Tags are broad filtering categories; concepts are domain vocabulary.
- **New terms → `proposed_concepts`.** Never write directly to `concepts`.

---

## What this skill does NOT do

- **No fetching.** The text is already in hand. If the user passes a URL, that's `/ingest`'s job (which dispatches to `/research`).
- **No summarization of long bodies.** A captured paragraph is preserved verbatim — `/capture` is for content the user wants kept as-is, not for triggering a brief.
- **No image / multimodal interpretation.** Pure text only.
- **No automatic linking.** The note's concepts will materialise edges via the standard concept-graph machinery; `/capture` doesn't call `weave_link` itself.

---

## When to use `/capture` directly vs via `/ingest`

- Via `/ingest`: when you have a thing in hand and aren't sure which subskill should own it. `/ingest` will classify shape and dispatch.
- Directly: when you know up-front you want a quick text capture (e.g. a meeting quote you want to preserve immediately) and want to skip the dispatch overhead.
