---
name: research
owns_mechanic: url_routing
source_type: [paper, repo, article]
capabilities: [import, acquire]
consumes: [mem_queue, mem_sources_config, mem_search]
produces: [vault/sources/papers/**, vault/sources/repos/**, vault/sources/articles/**]
tools:
  - Read
  - Bash
  - WebSearch
  - mem_queue
  - mem_sources_config
  - mem_search
description: Router skill — classifies URLs and dispatches to research-paper / research-repo / research-article. For queue drain use `/drain --source-type <slug>`.
---

# /research — URL Router

Thin URL classifier. For each URL the user passes:

1. Read `mem_sources_config()` once — gives you the `url_patterns` per
   source type.
2. Match the URL against patterns. First match wins.
3. Dispatch to the matching subskill via `Skill(skill="research-<type>")`.

The actual fetch + summarize + `mem_create` lives in the subskills, where
each source type's quirks (PDF extraction for papers, `gh repo view`
metadata for repos, JS-heavy SPA fallbacks for articles) live alongside
their fetcher.

## Classification table

| Source type | Default `url_patterns`              | Subskill           |
|-------------|-------------------------------------|--------------------|
| `paper`     | `arxiv.org`, `openreview.net`       | `research-paper`   |
| `repo`      | `github.com`, `gitlab.com`          | `research-repo`    |
| `article`   | (fallback for unmatched URLs)       | `research-article` |

The patterns are read from `sources.<type>.url_patterns` in
`sources.yaml` — users override defaults per vault. New source types add
a registry entry + a `commands/research/research-<type>.md` subskill;
this router needs no edits.

## Modes

### A. Direct URLs (default)

User passes one or more URLs. For each URL:

1. Classify via the table above.
2. `Skill(skill="research-<type>", args="<url>")`.
3. Subskill returns the new `src-` ID (or a skip reason).

### B. Queue drain (`--queue` / `--batch N`)

Forward to `/drain --source-type <slug> [--limit N]`. The new triad
splits acquisition by source type, so there's no single "research queue"
to drain — there's a paper queue, a repo queue, an article queue.

If the user invokes `/research --queue` without a slug, ask once which
queue they meant or default to `paper` (the most common case).

## What this skill does NOT do

- No fetching. No summarization. No `mem_create`. All of that lives in
  the subskills. This file is ~50 LOC by design — pure dispatch.
- No URL resolution (`needs-url`). That mode is folded into the new
  queue model — items in the queue already have URLs. Legacy
  `todo+research+needs-url` notes are migrated by `mem doctor --migrate`.

## Reporting

Per URL: classified type, subskill called, returned `src-` ID (or skip
reason). At end of batch: total processed / skipped / failed.
