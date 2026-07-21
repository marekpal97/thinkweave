---
name: research
owns_mechanic: url_routing
source_type: [paper, repo, article, news, youtube-events, youtube-concepts, podcast-events, podcast-concepts]
capabilities: [import, acquire]
consumes: [weave_queue, weave_sources_config, weave_search]
produces: [vault/sources/papers/**, vault/sources/repos/**, vault/sources/articles/**, vault/sources/news/**, vault/sources/youtube-events/**, vault/sources/youtube-concepts/**, vault/sources/podcast-events/**, vault/sources/podcast-concepts/**]
tools:
  - Read
  - Bash
  - WebSearch
  - weave_queue
  - weave_sources_config
  - weave_search
description: Router skill — classifies URLs and dispatches to research-paper / research-repo / research-article (or /news for news outlets). For queue drain use `/drain --source-type <slug>`.
---

# /research — URL Router

Thin URL classifier. For each URL the user passes:

1. Read `weave_sources_config()` once — gives you the `url_patterns` per
   source type.
2. Match the URL against patterns. First match wins.
3. Dispatch to the matching subskill via `Skill(skill="research-<type>")`.
   Under the plugin install, skills resolve namespaced — if the bare name
   fails with an unknown skill, retry as `thinkweave:research-<type>`
   (same rule for every `Skill` dispatch in this file).

The actual fetch + summarize + `weave_create` lives in the subskills, where
each source type's quirks (PDF extraction for papers, `gh repo view`
metadata for repos, JS-heavy SPA fallbacks for articles) live alongside
their fetcher.

## Classification table

| Source type | Default `url_patterns`                                       | Subskill           |
|-------------|--------------------------------------------------------------|--------------------|
| `paper`     | `arxiv.org`, `openreview.net`                                | `research-paper`   |
| `repo`      | `github.com`, `gitlab.com`                                   | `research-repo`    |
| `news`      | `reuters.com`, `ft.com`, `bloomberg.com`, `wsj.com`, `bankier.pl` | `news`         |
| `youtube-*` | `youtube.com/watch`, `youtu.be/`, `youtube.com/shorts`       | `research-youtube` |
| `podcast-*` | `feeds.megaphone.fm`, `feeds.libsyn.com`, `anchor.fm`, … (RSS) | `research-podcast` |
| `article`   | (fallback for unmatched URLs)                                | `research-article` |

The patterns are read from `sources.<type>.url_patterns` in
`sources.yaml` — users override defaults per vault. New source types add
a registry entry + a `commands/research/research-<type>.md` subskill;
this router needs no edits. Match `youtube-*` / `podcast-*` **before** the
`article` fallback (specific host patterns first, fallback last).

**News is the exception.** Instead of a dedicated `research-news`
subskill, the router dispatches to the existing `/news` skill, which
runs the same Haiku title-triage as the `/drain --source-type news`
cron path before invoking a Sonnet writer subagent. The two-stage shape
is news-specific (triage gate + writer fan-out), so news doesn't fit
the per-URL Skill dispatch the other types use.

**YouTube and podcast each map two grains to one family subskill.** Both
`youtube-events` and `youtube-concepts` share url_patterns and dispatch to
`research-youtube`; both `podcast-events` and `podcast-concepts` dispatch to
`research-podcast`. The family subskill resolves the URL (oEmbed / RSS),
picks the event-vs-concept grain itself, enqueues, and drains one worker —
the one-shot analog of the `/youtube` and `/podcast` orchestrators. (Podcast
v1 takes an **RSS feed URL** and ingests its latest episode; Spotify/Apple
player URLs are rejected with a lookup hint.)

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
queue they meant or default to `paper` (the most common case). **In a
non-interactive session (headless `claude -p`, no way to ask) default to
`paper` immediately — do not emit the question.** An unanswerable question
is a silent, permanent no-op in that context: cron invocations of
`/research --queue --batch N` have historically hung on this exact
question every single run. Cron should prefer calling `/drain
--source-type <slug>` directly for each queue anyway (see
`scripts/example-crontab` / `vault/config/scheduling.yaml`), but this
skill must not dead-end headlessly regardless of how it's invoked.

## What this skill does NOT do

- No fetching. No summarization. No `weave_create`. All of that lives in
  the subskills. This file is ~50 LOC by design — pure dispatch.
- No URL resolution (`needs-url`). That mode is folded into the new
  queue model — items in the queue already have URLs. Legacy
  `todo+research+needs-url` notes are migrated by `weave doctor --migrate`.

## Reporting

Per URL: classified type, subskill called, returned `src-` ID (or skip
reason). At end of batch: total processed / skipped / failed.
