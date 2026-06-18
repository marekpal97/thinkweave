---
name: research-youtube
source_type: youtube-events, youtube-concepts
capabilities: [import, acquire]
tools:
  - Read
  - Bash
  - weave_search
  - weave_sources_config
  - weave_queue
description: Resolve a single pasted YouTube URL into a queue item and drain one `research-youtube-worker`. Called from `/research` (router). The one-shot analog of `/youtube`'s discover+drain — same two rails, one video.
---

# /research-youtube — Ingest one pasted YouTube URL

Single-URL pipeline. The `/research` router classified the URL as YouTube;
you resolve it into the shape `research-youtube-worker` expects, enqueue it,
and drain exactly one worker. **No transcript fetch or brief writing lives
here** — that's the worker's job (transcript via `youtube-transcript-api`,
concept extraction, brief, `weave_create`). This skill is the resolution +
enqueue + drain wrapper, mirroring `/youtube` on a single video.

## Steps

### 1. Parse the `video_id`

From the pasted URL, extract the 11-char id:

- `youtube.com/watch?v=<ID>` → the `v` query param.
- `youtu.be/<ID>` → the path segment.
- `youtube.com/shorts/<ID>` → the path segment.

Canonicalise to `https://www.youtube.com/watch?v=<ID>`. If you can't find an
11-char id, stop and report `not a resolvable YouTube video URL`.

### 2. Idempotency guard

```
weave_search(query="<video_id>", mode="fts", limit=1)
```

If a result's frontmatter already carries this `video_id`, short-circuit —
report `already ingested: <src-id>` and stop. Do not enqueue.

### 3. Resolve title + channel via oEmbed

```bash
curl -s "https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=<ID>&format=json"
```

Parse `title` and `author_name` (the channel). oEmbed gives no `channel_id`
or publish date — leave those empty; the worker tolerates missing fields and
fills `duration_sec` / `transcript_language` from the transcript payload. If
oEmbed fails (404/empty), proceed with an empty title — the worker derives
the brief from the transcript regardless.

### 4. Pick the grain (events vs concepts)

Decide `source_type` from what the video plainly is:

- **`youtube-concepts`** (default) — tutorials, lectures, paper explainers,
  engineering deep-dives, anything durable. Most ad-hoc pastes land here.
- **`youtube-events`** — only when the video is clearly time-sensitive
  market / macro / news commentary (the event-grain shows). These float a
  theme candidate on create; concept-grain ones don't.

`temporal_grain` is `concept` or `event` to match. When unsure, default to
`youtube-concepts` / `concept` — a concept-grain note still reaches every hub
its concepts touch; it just skips theme attachment.

### 5. Enqueue the resolved item

```
weave_queue(action="enqueue", source_type="<slug>", item={
  "video_id": "<ID>",
  "url": "https://www.youtube.com/watch?v=<ID>",
  "title": "<oEmbed title or ''>",
  "channel": "<oEmbed author_name or ''>",
  "channel_id": "",
  "published": "",
  "description": "",
  "source_type": "<slug>",
  "temporal_grain": "<event|concept>"
})
```

`dedup_keys: [video_id, url]` are checked server-side — a `duplicate of …;
not enqueued` reply means it's already queued, which is fine; continue to
step 6 to drain it.

### 6. Drain one worker

```
Skill(skill="drain", args="--source-type <slug> --limit 1")
```

Under the plugin install, skills resolve namespaced — if `Skill(skill="drain")`
fails as unknown, retry as `thinkweave:drain`. `/drain` Path B peeks the
queue, fans out one `research-youtube-worker`, validates allowed-failure
prefixes, and archives the outcome.

> One-shot caveat: `/drain --limit 1` processes the **oldest** queued item. If
> this `youtube-*` queue already has a backlog, your pasted video drains after
> them — run `/youtube` (full poll + drain) or `/drain --source-type <slug>`
> to clear it. On a normally-drained queue the pasted video is the only item.

### 7. Report

Surface the worker's JSON outcome: the new `src-` id (or `idempotent_skip` /
`fetch_failed` reason). Echo the chosen `<slug>` and `video_id`.

## What this skill does NOT do

- Fetch the transcript or write the brief — that's `research-youtube-worker`.
- Poll RSS or process the whole queue — that's `/youtube`. This is one video.
- Download the video. Captions come from YouTube's transcript endpoint.
