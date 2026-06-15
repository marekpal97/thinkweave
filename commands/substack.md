---
name: substack
owns_mechanic: substack_inbox
source_type: substack
capabilities: [acquire]
consumes: [weave_search, weave_concepts, weave_create, weave_update, weave_link]
produces: [vault/sources/substack/**]
tools:
  - Read
  - Bash
  - weave_search
  - weave_concepts
  - weave_create
  - weave_update
  - weave_link
description: Drain the Substack disk inbox (browser-clipped posts) into the vault. Figure-aware via multimodal Read; archives processed bundles.
---

# /substack — Drain the Substack Inbox into the Vault

You are processing one or more browser-clipped Substack posts sitting in a disk inbox, turning each into a structured source note in the vault. Substack posts are captured **outside** of Claude Code — the user clips them in an authenticated browser at read time, and `/substack` drains whatever has accumulated.

**Source type**: `substack` → `vault/sources/substack/<author-slug>/<post-slug>/source.md` (+ `raw.md`, optionally `assets/*.png`). Routing is handled automatically by `VaultManager.create_note` — set `source_type: substack` and `author` in frontmatter and the file lands correctly.

---

## Capture happens outside Claude

The user captures posts using a **browser extension** that runs inside their authenticated browser session — the only path that handles paid Substack content without auth plumbing on our side.

**Recommended tools** (user picks one; the skill accepts whatever lands in the inbox):

- **Obsidian Web Clipper** (https://obsidian.md/clipper): the official Obsidian extension. Register a vault that maps to `$SUBSTACK_INBOX` (e.g. open the inbox folder as an Obsidian vault), then configure a Substack template that emits `url`, `author`, `publication`, `published` frontmatter and saves to the vault root. **Web Clipper does not download images locally** — it embeds remote `substackcdn.com` URLs in the markdown. The skill backfills these via curl during ingestion (step 9c).
- **MarkDownload** (browser extension): writes flat `.md` files to your downloads folder. Same remote-image situation as Web Clipper — backfilled at ingestion time.

The skill accepts both flat `.md` files (most common — Web Clipper output) and folder bundles (rarer — only if some clipper variant happens to ship companion images). Either way, images end up in `<source_dir>/assets/` after step 9.

---

## Arguments

- No args (default) or `--drain` — process everything in `$SUBSTACK_INBOX` (default `~/substack_inbox/`).
- `--limit N` — cap the number of items processed in one run.

Before starting, confirm inbox path: `echo $SUBSTACK_INBOX || echo ~/substack_inbox/`. If the inbox has more than 5 items and `--limit` isn't set, ask the user whether to process all of them or set a lower cap — batch image interpretation can burn through budget.

---

## Steps

### 1. Enumerate the inbox

```
Bash("weave intake enumerate ~/substack_inbox/")
```

Uses `weave intake` so `/email` and other drop-folder importers share the same enumeration semantics (`_processed/` skipped, flat vs folder classified, `<stem>-images/`/`<stem>_assets/` companion resolved).

Output is JSON: `[{"path": "...", "kind": "flat"|"folder", "companion_dir": "..."|null}, ...]`. Already excludes `_processed/`, dotfiles, loose non-`.md` files, and folders without any markdown. If the array is empty, report "Nothing to drain." and stop.

For each entry, dispatch on `kind`:

- `flat` → `Read` the `path`. If `companion_dir` is non-null, `ls` it to discover sibling images.
- `folder` → `ls` the `path`, prefer `index.md` else first `*.md` alphabetically; image candidates are siblings inside `path`.

### 2. Parse each input

Process items **one at a time** through steps 2–10, then loop back for the next until the batch is exhausted.

**For a flat file** (`~/substack_inbox/<post>.md`):
1. `Read` the file.
2. Parse YAML frontmatter from the head of the file (same format `vault.py` uses).
3. Body is everything after the closing `---`.
4. Look for a sibling image folder: `ls ~/substack_inbox/<post>-images/ 2>/dev/null` (MarkDownload convention) or `ls ~/substack_inbox/<post>_assets/ 2>/dev/null`.

**For a folder bundle** (`~/substack_inbox/<post>/`):
1. `ls` the directory to find the markdown file. Prefer `index.md`, else the first `*.md` alphabetically.
2. `Read` that markdown file.
3. Parse frontmatter and body.
4. Every image file in the same directory (`*.png`, `*.jpg`, `*.jpeg`, `*.webp`, `*.gif`, `*.svg`) is a figure candidate.

**Extract required metadata** with fallbacks:
- `title` ← frontmatter `title`, else first `# H1` in body, else filename
- `url` ← frontmatter `url`, else first `https://*.substack.com/p/*` link in the first 20 lines of body
- `publication` ← frontmatter `publication`, else parse `<publication>.substack.com` from the URL's hostname
- `author` ← frontmatter `author`, else (last resort) prompt the user inline: "Couldn't infer author for `<filename>`. Please provide:". **Don't silently write `unknown-author`.** The folder shape depends on this field.
- `published` ← frontmatter `published` / `date` / `date_published`, else leave blank.

If `url` can't be recovered, skip the item with an error logged — we won't ingest an un-sourced post.

### 3. Load ontology + concept registry (once per batch)

```
Read src/thinkweave/ontology.yaml
weave_concepts(min_count=1)
```

Identify which ontology branches the publication you're draining
maps to — substack is a generic newsletter platform, so the relevant
vocabulary depends on what the user follows. (Examples: a
finance-focused publication maps onto `finance/*` branches; an
ML-focused publication onto `ml/*`; a politics newsletter onto
whichever domain you've added for that subject.) If you spot a gap
in the ontology, propose new concepts in `proposed_concepts` and let
`/tighten` canonicalise them later.

### 4. Search vault for connections

```
weave_search(query="<key terms + named entities + themes from this post>", mode="hybrid", limit=5)
weave_graph(filter="concept_walk", concepts=["<concepts you're about to assign>"], match_mode="any", limit=5)
```

Note which existing sources/notes/decisions relate. These feed the **Vault Connections** section.

### 5. Defer image work until after `weave_create`

Images need to land inside the source note's directory so `raw.md` can reference them relatively. But the source directory doesn't exist yet — `weave_create` creates it. So:

1. Do steps 6–8 first (write the source note via `weave_create`, which returns the source directory path).
2. Then handle images in step 9: create `assets/`, copy local refs from a folder bundle (if any), curl-backfill remote refs from the body, and rewrite paths.

Hold the list of local image paths (from step 2's bundle discovery) and the list of remote image URLs (extracted from the body during step 6) in memory until you reach step 9.

### 6. Write a dense brief

**Not a summary — a dense brief.** You're writing something the user's future self will find useful 6 months from now when they've forgotten the post but need to remember what it argued.

Use the body template at the bottom of this file. Key sections:
- **Thesis** — the core argument, not the topic
- **Claims & Evidence** — specific claims + what backs them, distinguishing data-backed from opinion
- **Implications** — what the post argues should follow from the thesis. (For investment-research publications this is positioning / tickers / sectors / asset classes / timeframe; for ML or policy publications this is whatever actionable take the author leaves behind. Pick the framing that matches the source.)
- **Risks & Counterarguments** — what could invalidate the thesis
- **Vault Connections** — prior notes/decisions this relates to

Write the brief from the text body. Images are archived in `assets/` for later inspection but not interpreted at ingest time.

### 7. Concept mapping

- Map to whatever ontology branches actually fit the publication.
- Propose new concepts aggressively — lowercase, hyphenated, specific. Examples by domain: finance → `rate-cycles`, `sector-rotation`; ML → `chain-of-thought`, `kv-cache`; policy → `industrial-policy`, `permitting-reform`. Put new proposals in `proposed_concepts`.
- **Do NOT edit `ontology.yaml` inline** — consolidation happens via `/tighten` later.
- Minimum 3 concepts per source (concepts are the primary linkage mechanism).

### 8. Call `weave_create`

```
weave_create(
  type="source",
  title="<post title>",
  body="<structured brief from step 6>",
  tags=["substack"],
  concepts=["<mapped ontology concepts>"],
  frontmatter={
    "source_type": "substack",
    "url": "<post url>",
    "publication": "<e.g. Citrini Research>",
    "author": "<author name — drives folder nesting>",
    "published_date": "<ISO if extractable, else empty>",
    "proposed_concepts": ["<new concepts not in ontology>"],
  }
)
```

**Do NOT set `project`** — source notes are global knowledge artifacts. Concept linkages bridge them to project-specific work.

The response contains the source directory path. Parse it out — it's the target for `raw.md` and `assets/`.

### 9. Stage images and rewrite paths

Two kinds of image refs need to land in `<source_dir>/assets/`:

- **Local refs** from a folder bundle (`![alt](img1.png)`) — copy from the inbox folder.
- **Remote refs** from the typical Web Clipper output (`![alt](https://substackcdn.com/...)`) — Web Clipper does **not** download images locally, so we backfill them via curl during ingestion. This is the common path; expect most clips to have remote refs.

Both paths converge on the same `assets/` directory and path-rewriting logic. Images are archived for later retrieval — no interpretation at ingest time.

**9a. Create the assets directory**:

```
Bash("mkdir -p <source_dir>/assets")
```

**9b. Copy local images** (folder-bundle path, if applicable):

For each image discovered as a sibling of the inbox `.md` file:
```
Bash("cp <inbox-img-path> <source_dir>/assets/<filename>")
```
Preserve the original filename — that's what the markdown body references.

**9c. Backfill remote images via curl** (the common path):

Scan the body for `![alt](https://...)` references whose URL is **not** already a local relative path. For each remote ref, in document order, assign a sequential filename `img-1.png`, `img-2.png`, ..., and download:

```
Bash("curl -sL --max-time 30 -o <source_dir>/assets/img-N.png '<url>'")
```

Notes on the curl call:
- `-sL` = silent + follow redirects (Substack CDN URLs often redirect to S3).
- `--max-time 30` = bound the request so a hung CDN doesn't stall the whole drain.
- Quote the URL — Substack CDN URLs contain `,` and `:` which some shells interpret.
- Check the exit code via `$?` or by inspecting whether the file exists and has nonzero size after the call. On failure, **leave the URL remote in raw.md** and log: "Failed to backfill image N: <url>". Don't abort the item — the text brief is still worth keeping.
- Filename uses `.png` regardless of the actual format — browsers and Obsidian handle common formats fine.
- Build a mapping from remote URL → local filename so step 9d can rewrite paths correctly.

**9d. Rewrite image paths in the body** for `raw.md`:
- Local refs from 9b: `![alt](img1.png)` → `![alt](assets/img1.png)`
- Successfully-downloaded remote refs from 9c: `![alt](https://substackcdn.com/...)` → `![alt](assets/img-N.png)` using the URL→filename mapping
- Failed-download remote refs: leave as-is (`![alt](https://substackcdn.com/...)`)

Write the rewritten body to `<source_dir>/raw.md` via `Write`.

**9e. Update the source note** with `raw_path` and figure list:

```
weave_update(
  note_id="<src-id>",
  frontmatter_updates={"raw_path": "raw.md", "figures": ["assets/img-1.png", "assets/img-2.png"]}
)
```

Images are archived but not interpreted — the user can `Read` them later if needed for deeper analysis.

### 10. Link to related vault content

For each related note/source discovered in step 4:
```
weave_link(source_id="<new-src-id>", target_id="<related-id>", edge_type="relates_to")
```

### 11. Archive the inbox entry

Move the source from the inbox to a dated archive folder — never delete:

```
Bash("weave intake archive '<entry-path>' --inbox ~/substack_inbox/")
```

Uses `weave intake archive` so the dated-folder + companion-dir + collision-suffix logic is shared with `/email` and other drop-folder importers (and is unit-tested), instead of being open-coded in every skill.

`<entry-path>` is the `path` field returned by `weave intake enumerate` for this item. The command:
- Creates `~/substack_inbox/_processed/<YYYY-MM-DD>/` on demand.
- Moves the entry plus any companion dir (`<stem>-images/` or `<stem>_assets/`) for flat entries.
- Appends `-1`, `-2`, … to the basename on same-day name collisions, so retries after a partial failure are safe.
- Prints the final archive path on stdout. Exit code is non-zero on missing entry or entry-outside-inbox.

### 12. Loop or finish

Return to step 2 for the next item. Stop when:
- `--limit N` reached
- Inbox is empty (only `_processed/` remains)
- An item fails — log the error, leave it in the inbox, continue to the next

### 13. Report

For each processed item:
- Source note ID + title
- Author / publication
- Concepts assigned (existing) + proposed (new)
- Images backfilled (`X downloaded, Y failed` — list failed URLs so the user can spot CDN issues)
- Vault connections created
- Any errors/skips

Batch summary: N processed, M skipped, K errors, inbox remaining count. Suggested next action if anything is left.

---

## Failure handling

- **Parse error / missing URL** → log, skip the item (leave in inbox), continue.
- **`weave_create` failure** → log, skip, continue. Don't move the inbox entry until you have a confirmed `src-` ID.
- **Curl image backfill failure** (timeout, 403, network error) → log the URL and image index, leave that ref remote in `raw.md`, continue with the rest of the item. The text brief is still worth keeping.
- **No queue state to manage** — failures just mean "still in the inbox," which is the correct fallback.

---

## Known limitations (v1)

- **No URL-based ingestion path.** Free Substack posts still need to be clipped to the inbox — the skill doesn't fetch URLs directly. This is deliberate; it keeps the design to a single path.
- **No discovery integration.** `/substack` is ingest-only; it doesn't suggest posts to read or find authors to follow.
- **Images archived, not interpreted.** Figures are downloaded to `assets/` and paths rewritten, but no multimodal interpretation happens at ingest. Use `Read <source_dir>/assets/<file>` later for on-demand analysis.
- **Curl backfill assumes public CDN URLs.** Substack image CDN URLs are typically public even for paid posts (only the post HTML is paywalled), so plain `curl` works without auth headers. If a particular publication serves images behind auth, the backfill will 403 and those refs stay remote — surfaces in the per-item report so you can spot it.

---

## Source note body template

```markdown
## Thesis
[what the author is arguing — the core claim, not the topic]

## Claims & Evidence
- [specific claim + evidence: "argues X because Y", "cites Z data from source"]
- [distinguish data-backed claims from opinion/framework/anecdote]
- [quantitative findings — whatever metrics the post leans on (e.g.
  benchmark scores for an ML post; prices/flows for a finance post;
  poll numbers for a politics post)]
- [note which claims reference figures — images are in `assets/` for later inspection]

## Actionable Implications
- [named entities, categories, themes the post calls out by name]
- [what the author thinks the reader should do — try/avoid/watch — and the timeframe]
- [practical guidance: prerequisites, caveats, signals to wait for]

## Risks & Counterarguments
- [what the author acknowledges could invalidate the thesis]
- [base rates, prior analogies, unknown unknowns called out]

## Vault Connections
- Relates to [[note-title]] — [why: shared thesis, contradicts, prior iteration of the same theme]

## Raw Content
[[raw.md]]
```
