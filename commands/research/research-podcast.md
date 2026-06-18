---
name: research-podcast
source_type: podcast-events, podcast-concepts
capabilities: [import, acquire]
tools:
  - Read
  - Bash
  - weave_search
  - weave_sources_config
  - weave_queue
description: Resolve a single pasted podcast RSS-feed URL into a queue item for its latest episode and drain one `research-podcast-worker`. Called from `/research` (router). The one-shot analog of `/podcast`'s discover+drain.
---

# /research-podcast — Ingest one pasted podcast URL

Single-URL pipeline. The `/research` router classified the URL as a podcast;
you resolve the show's **RSS feed** to its most recent episode, enqueue that
episode, and drain one `research-podcast-worker`. **No audio download or
Gemini transcription lives here** — that's the worker's job. This skill is the
resolution + enqueue + drain wrapper.

> **v1 scope (read first).** This handles an **RSS feed URL** (e.g.
> `feeds.megaphone.fm/…`, `feeds.libsyn.com/…`) and ingests its **latest**
> episode — podcast RSS has no per-episode pasteable URL, so "the feed" means
> "the newest episode." Two cases are out of scope for v1:
>   - **Spotify / Apple player URLs** (`open.spotify.com/episode/…`,
>     `podcasts.apple.com/…`) — these obscure the canonical RSS. **Reject**
>     them in step 1 with the lookup hint; resolving them is deferred.
>   - **A specific older episode** — use `/podcast` (full feed poll) instead.

## Steps

### 1. Validate the URL shape

If the URL host is a **player** (`open.spotify.com`, `podcasts.apple.com`,
`pca.st`, `overcast.fm`), stop and report:

```
Player URLs don't expose the RSS feed /research-podcast needs. Find the show's
feed first — e.g.:  curl -s 'https://itunes.apple.com/lookup?id=<APPLE_ID>' | jq -r '.results[0].feedUrl'
then paste that feed URL.
```

Otherwise treat the URL as an RSS feed (it matched a `podcast-*` `url_pattern`
in `sources.yaml`, e.g. `feeds.megaphone.fm`, `feeds.libsyn.com`,
`feeds.transistor.fm`, `feeds.simplecast.com`, `anchor.fm`, `omny.fm`,
`rss.art19.com`, `spreaker.com`).

### 2. Resolve the latest episode from the feed

```bash
curl -sL "<feed-url>" | head -c 200000
```

From the first `<item>` in the feed XML, pull:

- `audio_url` — the `<enclosure url="…">` attribute (the MP3/audio link).
- `entry_id` — the `<guid>` text (the most stable dedup key).
- `title` — the `<item><title>`.
- `published` — the `<pubDate>` (ISO-normalise if easy; else leave raw).
- `show` — the channel-level `<title>` (above the items).

If there's no `<enclosure>` on the latest item, report
`feed has no audio enclosure on its latest episode` and stop.

### 3. Idempotency guard

```
weave_search(query="<entry_id>", mode="fts", limit=1)
```

If a result's frontmatter already carries this `entry_id`, short-circuit —
report `already ingested: <src-id>` and stop. Do not enqueue.

### 4. Pick the grain (events vs concepts)

- **`podcast-concepts`** (default) — deep-dives, lecture-style and technical
  explainer shows. Most ad-hoc pastes land here.
- **`podcast-events`** — markets / macro / interview shows whose episodes are
  time-sensitive commentary (these float a theme candidate on create).

`temporal_grain` is `concept` or `event` to match; default
`podcast-concepts` / `concept` when unsure.

### 5. Enqueue the resolved episode

```
weave_queue(action="enqueue", source_type="<slug>", item={
  "entry_id": "<guid>",
  "audio_url": "<enclosure url>",
  "url": "<feed-url>",
  "title": "<episode title>",
  "show": "<show title>",
  "published": "<pubDate>",
  "source_type": "<slug>",
  "temporal_grain": "<event|concept>"
})
```

`dedup_keys: [entry_id, audio_url, url]` are checked server-side — a
`duplicate of …; not enqueued` reply means it's already queued; continue to
step 6.

### 6. Drain one worker

```
Skill(skill="drain", args="--source-type <slug> --limit 1")
```

Under the plugin install, skills resolve namespaced — retry as
`thinkweave:drain` if the bare name is unknown. `/drain` Path B fans out one
`research-podcast-worker` (downloads the audio enclosure, hands it to Gemini
Flash via the Files API, writes the brief).

> One-shot caveat: `/drain --limit 1` processes the **oldest** queued item. On
> a backlogged `podcast-*` queue your episode drains after the others — use
> `/podcast` or `/drain --source-type <slug>` to clear it.

### 7. Report

Surface the worker's JSON outcome: the new `src-` id (or `idempotent_skip` /
`fetch_failed` reason). Echo the chosen `<slug>` and episode title.

## What this skill does NOT do

- Download the MP3 or call Gemini — that's `research-podcast-worker`.
- Resolve Spotify / Apple **player** URLs to their RSS feed (deferred — step 1
  rejects them with the iTunes-lookup hint).
- Ingest a *specific older* episode — use `/podcast` (full feed poll).
