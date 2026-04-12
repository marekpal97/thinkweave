# /research — Ingest Sources into the Knowledge Vault

You are processing one or more URLs (arxiv papers, GitHub repos, web articles) into structured source notes in the personal_mem vault. Sources are bucketed by `source_type` under `vault/sources/`:

- `paper` → `vault/sources/papers/<slug>/source.md` + `paper.pdf` (or `raw.txt`)
- `repo` → `vault/sources/repos/<slug>/source.md` + `snapshot.md`
- `article` → `vault/sources/articles/<slug>/source.md` + `raw.md`

Routing is handled automatically by `VaultManager.create_note` — set `source_type` in frontmatter and the file lands in the right bucket. Only use `repo` (never `github`) as the source type for GitHub repositories; the legacy alias is normalised but shouldn't be written fresh.

Each source type is expected to pair with a dedicated ingestion/search scaffold. `/research` and `/discover` handle papers, repos, and articles. New source types (YouTube, podcasts, Messenger imports) should get their own bucket *and* their own skill — do not stretch `/research` to cover them.

**Arguments**: One or more URLs, or flags:
- `--queue` — process pending queue items instead of explicit URLs
- `--batch N` — process up to N items then stop (default: 1). Use with `--queue` or `/loop`.
- `--resolve` — URL resolution mode: find URLs for `needs-url` items (no ingestion)

---

## Mode A: URL Resolution (`--resolve`)

Lightweight pass that finds URLs for `needs-url` queue items. Run this first to prepare items for ingestion.

```
/research --resolve --batch 20
```

### R1. Fetch needs-url items

```
mem_search(query="", tags=["todo", "research", "needs-url"], type="note", limit=<batch size>)
```
Process in FIFO order (oldest first).

### R2. Resolve each item

For each item:
1. Extract the title and context blurb from the note body
2. Construct a targeted search query:
   - Papers: `"<exact paper title> arxiv"` or `"<title> <first author> arxiv"`
   - Repos: `"<repo name or description> github"`
   - Articles: `"<title> <publication if known>"`
3. Run `WebSearch(query)` — check the top 2-3 results
4. **Match found**: Update the note with the resolved URL and remove `needs-url`:
   ```
   mem_update(note_id="<id>", frontmatter_updates={"tags": ["todo", "research"]}, body_append="\n\nResolved URL: <url>")
   ```
   The item is now a normal queue item, ready for `/research --queue`.
5. **No match after 2 queries**: Skip — leave tagged `needs-url`, move to next item

### R3. Report

- Items resolved / skipped
- Remaining `needs-url` count
- Suggested next: "Run `/research --queue --batch N` to ingest resolved items"

---

## Mode B: Ingestion (default)

Full ingestion pipeline — fetches content, extracts technical brief, creates source notes.

## Steps

### 1. Determine What to Process

**If URLs are provided**: Use those directly (batch flag ignored).

**If `--queue` or `--batch N` is passed**: Search for pending research queue items:
```
mem_search(query="", tags=["todo", "research"], type="note", limit=<batch size>)
```
Exclude items tagged `processing` or `needs-url`. Process in FIFO order (oldest first by date).

**If nothing is provided**: Check for pending queue items (same search). If none, report "No URLs provided and queue is empty."

### 1b. Claim the Item

Before processing each item, claim it to prevent double-processing by parallel runs:
```
mem_update(note_id="<id>", frontmatter_updates={"tags": ["processing", "research"]})
```
This replaces `todo` with `processing`. If the item fails mid-processing, it stays tagged `processing` — recoverable via `mem_search(tags=["processing", "research"])`.

Process items **one at a time** through steps 2-9, then loop back for the next item until the batch is exhausted or the queue is empty.

### 2. Classify the URL

Extract the URL from the queue note body. If no URL found, skip the item (mark `done`, note "No URL").

Classify by pattern — no LLM needed:
- `arxiv.org` → **paper**
- `github.com` → **repo**
- Everything else → **article**

### 3. Size Check — Skip Oversized Content

Before fetching full content, check for size signals:

**For repos (GitHub)**:
1. Run `Bash("gh repo view <owner/repo> --json diskUsage,name,description,primaryLanguage,stargazerCount,languages,defaultBranchRef")` to get metadata
2. If `diskUsage > 50000` (50MB, unit is KB): **skip** — log as "Repo too large, skipped" and move to next item
3. If `diskUsage > 10000` (10MB): **README-only mode** — don't clone, just fetch README

**For articles/papers**:
- If WebFetch returns content that looks like a full book (>100k chars, or URL patterns like `/book/`, Google Books, Project Gutenberg): **skip entirely** — log as "Book detected, skipped"
- Normal papers and articles proceed as usual

**Hard rule**: Never attempt to download or summarize books. They are too large to process meaningfully and will produce shallow summaries.

### 4. Fetch Content

Fetching strategy depends on source type. **Run fetches sequentially, not in parallel** — one failed WebFetch/Bash in a parallel batch cancels all siblings.

**For papers (arxiv)**:
1. `WebFetch` the abstract page (e.g. `https://arxiv.org/abs/2301.12345`) — extract title, authors, date, abstract
2. Then `WebFetch` the HTML version (e.g. `https://arxiv.org/html/2301.12345`) for fuller text — this has methods, results, and conclusions in readable form
3. If the HTML version isn't available, the abstract page + PDF text extraction is the fallback
4. Save whatever text you get — this becomes `raw.txt`

**For repos (GitHub)**:
1. Get structured metadata via `gh` CLI:
   ```
   Bash("gh repo view <owner/repo> --json name,description,primaryLanguage,stargazerCount,languages,defaultBranchRef,diskUsage")
   ```
2. Then fetch README content: `WebFetch("https://raw.githubusercontent.com/<owner>/<repo>/<branch>/README.md")`
3. For repos under 10MB, clone for deeper analysis:
   ```
   Bash("git clone --depth 1 <url> /tmp/research_clone_<slug>")
   ```
   Then `Read` key files: entry point, main module, architecture docs, pyproject.toml/setup.py
4. Concatenate key files into `snapshot.md`
5. Clean up temp clones: `Bash("rm -rf /tmp/research_clone_<slug>")`

**For articles (web)**:
1. `WebFetch` the URL — this returns the page content
2. If the content is too short or garbled (JS-heavy SPA), try `WebSearch` for the article title to find a cached/alternative version
3. The raw fetched content becomes the archival copy

### 5. Load the Concept Ontology (once per batch)

**Do this once at the start of the batch, not per item.** The ontology and concept list don't change within a single batch run.

```
Read /home/marekpal97/python_projects/personal_mem/src/personal_mem/ontology.yaml
```

```
mem_concepts(prefix="", min_count=1)
```

You need both because:
- `ontology.yaml` defines the domain structure (what domains exist, canonical concept names)
- `mem_concepts` shows what's actually in use (including concepts not yet in the ontology)

### 6. Search for Vault Connections

Before writing the source note, check what already exists:
```
mem_search(query="<key terms from the source>", mode="hybrid", limit=5)
```

Also search by the most relevant concepts you plan to assign:
```
mem_concept_search(concepts=["concept-a", "concept-b"], match_mode="any", limit=5)
```

Note which existing notes/sources/decisions connect to this material. This feeds the **Vault Connections** section.

### 7. Extract Knowledge and Write the Source Note

Now you have: raw content, ontology, and vault context. Create the source note.

**The goal is a technical brief, not a summary.** You are writing something a researcher would find useful 6 months from now when they've forgotten the details. Extract:
- The specific **methods and techniques** used (not just "they used deep learning" — what architecture, what loss function, what training regime)
- The **key claims with evidence** (quantitative results, ablation findings, scaling behavior)
- The **technical underpinnings** that make this work (what prior work does it build on, what assumptions does it make)
- **Limitations and open questions** (what doesn't work, what the authors acknowledge as gaps)

This is the "real meat" — a dense technical brief, not a book report.

**Concept mapping rules**:
- Map to existing ontology concepts wherever they fit naturally
- When the source introduces concepts with no natural fit, propose new ones following the naming convention: lowercase, hyphenated, specific (e.g. `chinchilla-optimal`, not `training-efficiency`)
- Put established concepts in `concepts` frontmatter field, new proposals in `proposed_concepts`
- Do NOT duplicate between tags and concepts — concepts are for technical vocabulary, tags for broad categories
- Minimum 3 concepts per source — these are the primary linkage mechanism

**Call `mem_create`** with:

```
mem_create(
  type="source",
  title="<descriptive title>",
  body="<structured body — see templates below>",
  tags=["<broad category tags>"],
  concepts=["<mapped concepts from ontology>"],
  frontmatter={
    "source_type": "<paper|repo|article>",
    "url": "<original URL>",
    "authors": ["<author list>"],
    "proposed_concepts": ["<new concepts not in ontology>"],
    "<extra fields per type — see below>"
  }
)
```

**Do NOT set `project`** — source notes are global knowledge artifacts. Concept linkages bridge them to project-specific work.

**Extra frontmatter by type**:
- **paper**: `arxiv_id`, `doi` (if available), `abstract` (first 300 chars)
- **repo**: `repo_url`, `languages` (list), `stars` (int)
- **article**: `publication` (site name, if identifiable), `author` (singular for articles)

### 8. Save Raw Content

Parse the source directory path from the `mem_create` response (it returns "Source directory: /path/to/dir").

Save the raw content alongside:
- **paper**: `Write` the extracted text to `<source_dir>/raw.txt`
- **repo**: `Write` a concatenation of key files to `<source_dir>/snapshot.md`
- **article**: `Write` the full fetched text to `<source_dir>/raw.md`

Then update the source note to record the raw path:
```
mem_update(note_id="<src-id>", frontmatter_updates={"raw_path": "<filename>"})
```

### 9. Link and Update Queue

**Link to related content**: If step 6 found related notes/sources, create edges:
```
mem_link(source_id="<new-src-id>", target_id="<related-id>", edge_type="relates_to")
```

For papers that cite other papers already in the vault:
```
mem_link(source_id="<new-src-id>", target_id="<cited-src-id>", edge_type="cites")
```

**Update queue item** (if processing from queue): Only mark done **after confirming a `src-` ID was returned by `mem_create` in step 7**. If no source note was created (fetch failed, size skip, etc.), leave the item tagged `processing` — do NOT mark it `done`.
```
mem_update(note_id="<queue-note-id>", frontmatter_updates={"tags": ["done", "research"]})
```
This transitions `processing` → `done`. The item disappears from `mem backlog`.

**Then loop back to step 1b** for the next item in the batch. Stop when:
- Batch limit reached (N items processed)
- Queue is empty
- An item fails — log the error, leave it tagged `processing`, continue to next item

**Recovery**: Items stuck in `processing` (from failed runs) can be found via `mem_search(tags=["processing", "research"])` and retried by resetting their tag to `todo`.

### 10. Report

After all items in the batch are processed (or queue is empty), report:

**Per item:**
- Source note ID and title
- Source type
- Concepts assigned (existing) and proposed (new)
- Vault connections found
- Any errors or skips

**Batch summary:**
- Items processed / skipped / failed
- Queue remaining (how many `todo+research` items are left)
- Suggested next action: "Run `/research --queue --batch N` again to continue" or "Queue empty."

---

## Source Note Body Templates

### Paper (arxiv)

```markdown
## Abstract
[verbatim from paper]

## Methods & Technical Approach
[specific techniques: architecture details, loss functions, training regime, optimization approach]
[what prior work this builds on — not just "related work" but the specific technical foundation]
[key assumptions and constraints]

## Key Claims & Results
- [specific claim + quantitative evidence: "achieves 94.2% on X, up from 91.1% baseline"]
- [ablation findings: "removing component Y drops performance by Z%"]
- [scaling behavior if relevant]
- [distinguish strong claims (well-evidenced) from weaker ones (preliminary/theoretical)]

## Technical Underpinnings
[what makes this work — the core insight or mechanism]
[theoretical grounding if applicable]
[connections to broader frameworks (e.g. "extends NTK theory to finite-width networks")]

## Limitations & Open Questions
- [what the authors acknowledge doesn't work]
- [assumptions that may not hold]
- [gaps for future work]

## Vault Connections
- Relates to [[existing-note-title]] — [why: shared method, contradicts finding, extends work]
- Expands on concept [[concept-name]] which appears in [context]
- [contradictions or confirmations of existing vault knowledge]

## Raw Content
[[<slug>/raw.txt]]
```

### Repo (GitHub)

```markdown
## What It Does
[one paragraph elevator pitch — the problem it solves and for whom]

## Architecture & Approach
[how it's structured: key modules, data flow, entry points]
[what's technically interesting — not just features but implementation choices]
[dependencies and design constraints]

## Notable Patterns & Techniques
- [specific techniques worth remembering: algorithms, data structures, patterns]
- [design decisions that are instructive — why they chose X over Y]
- [performance characteristics if documented]

## Limitations & Trade-offs
- [what it doesn't handle]
- [known issues from README/issues]
- [scalability constraints]

## Vault Connections
- Relates to [[existing-note-title]] — [why]

## Raw Content
[[<slug>/snapshot.md]]
```

### Article (web)

```markdown
## Core Argument
[what this argues — the thesis, not just the topic]

## Key Claims & Evidence
- [specific claim + the evidence offered for it]
- [distinguish data-backed claims from opinion/anecdote]
- [any quantitative findings or benchmarks cited]

## Technical Detail
[methods, frameworks, or approaches discussed]
[concrete examples or case studies referenced]

## Vault Connections
- Relates to [[existing-note-title]] — [why]

## Raw Content
[[<slug>/raw.md]]
```

---

## Adding to the Queue

To add URLs for later processing (without ingesting now), create queue notes:
```
mem_create(
  type="note",
  title="<descriptive title of what to read>",
  body="<url>\n\n<why this is interesting, where you found it>",
  tags=["todo", "research"],
  concepts=["<relevant concepts if known>"]
)
```

These show up in `mem backlog` and get picked up by `/research --queue`.
