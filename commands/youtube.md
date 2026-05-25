---
name: youtube
owns_mechanic: youtube_inbox
source_type: youtube-events, youtube-concepts
capabilities: [acquire]
consumes: [mem_sources_config, mem_queue, mem_search, mem_concepts, mem_create, mem_link]
produces: [vault/.mem/queues/youtube-events.jsonl, vault/.mem/queues/youtube-concepts.jsonl, vault/sources/youtube-events/**, vault/sources/youtube-concepts/**]
tools:
  - Read
  - Bash
  - Task
  - mem_search
  - mem_concepts
  - mem_create
  - mem_read
  - mem_update
  - mem_link
  - mem_queue
  - mem_sources_config
description: Drain YouTube channel RSS feeds into the vault. RSS poll → enqueue → writer fan-out. Topic-agnostic over `youtube-*` source types. Headless-safe (no OAuth).
---

# /youtube — YouTube channel intake

End-to-end orchestrator for the `youtube-*` family of source types. One run does:

1. For each registered `youtube-*` source type, poll every channel in its allowlist via that channel's RSS feed.
2. Enqueue new videos (queue dedup on `video_id` + `url`).
3. Fan out `research-youtube-worker` subagents in parallel; each worker calls Gemini Flash for transcript + summary, then writes the brief.
4. Archive queue items per outcome.

This skill mirrors `/newsletter`'s shape (writer fan-out from a JSONL queue) but uses RSS polling instead of mail. No OAuth, so headless cron use is fine (`claude -p "/youtube"`).

**Arguments (all optional):**
- `<source-type>` — limit to one type, e.g. `/youtube youtube-events`. Default: all `youtube-*` types from config.
- `--limit N` — cap items per type to fewer than `drain_batch_max`.

---

## Step 0 — Load config

```
mem_sources_config()
```

Discover the set to process: every key under `sources.` whose slug starts with `youtube-`. If `<source-type>` was passed, filter to just that one. For each, pull:

| Key | Used for |
|---|---|
| `channels` | **Canonical allowlist.** List of YouTube channel IDs (UCxxx form). Empty list → skip this source type. |
| `lookback_days` | Discard feed entries older than N days |
| `queue` | JSONL path |
| `subagent_type` | Should be `research-youtube-worker` |
| `subagent_model` | `sonnet` |
| `drain_parallelism` | Max concurrent writers per type |
| `drain_batch_max` | Cap items per drain per type |
| `dedup_keys` | `[video_id, url]` — enforced by `mem_queue(action="enqueue")` |

If no `youtube-*` types are configured, stop with `"No youtube source types in sources.yaml — nothing to do."`

If every type's `channels:` list is empty, stop with `"No channels configured for any youtube-* type. Add channel IDs to vault/.mem/sources.yaml under sources.youtube-events.channels / sources.youtube-concepts.channels."`

---

## Step 1 — Poll RSS feeds (per source type)

For each `youtube-*` type with non-empty `channels:`:

For each channel ID `<channel_id>`, the YouTube RSS feed URL is:

```
https://www.youtube.com/feeds/videos.xml?channel_id=<channel_id>
```

Fetch and parse via `feedparser` (already an installed optional dep under `[news]`). Use Bash:

```bash
uv run python -c "
import feedparser, json, sys, os
from datetime import datetime, timezone, timedelta

channels = ${channels_json}      # e.g. ['UCBJycsmduvYEL83R_U4JriQ']
lookback_days = ${lookback_days} # e.g. 7
cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

out = []
for cid in channels:
    feed = feedparser.parse(f'https://www.youtube.com/feeds/videos.xml?channel_id={cid}')
    if feed.bozo and not feed.entries:
        print(f'! feed unusable for {cid}: {feed.bozo_exception}', file=sys.stderr)
        continue
    channel_name = feed.feed.get('title', cid)
    for e in feed.entries:
        # feedparser exposes yt:videoId via the yt_videoid attr on >=6.0
        vid = getattr(e, 'yt_videoid', None) or e.get('id', '').split(':')[-1]
        if not vid:
            continue
        pub = e.get('published_parsed')
        if pub:
            pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
            if pub_dt < cutoff:
                continue
            published_iso = pub_dt.isoformat()
        else:
            published_iso = e.get('published', '')
        out.append({
            'video_id': vid,
            'url': e.get('link', f'https://www.youtube.com/watch?v={vid}'),
            'title': e.get('title', '').strip(),
            'channel': channel_name,
            'channel_id': cid,
            'published': published_iso,
            'description': (e.get('summary', '') or '')[:2000].strip(),
        })
print(json.dumps(out))
"
```

The script outputs one JSON array of candidate items on stdout. Parse it.

**Enqueue each candidate:**

```
mem_queue(
  action="enqueue",
  source_type="<this youtube-* slug>",
  item={
    "video_id": "<11-char YouTube ID>",
    "url": "https://www.youtube.com/watch?v=...",
    "title": "<video title>",
    "channel": "<channel display name>",
    "channel_id": "<UCxxxx>",
    "published": "<ISO date>",
    "description": "<first 2KB of RSS summary>",
  }
)
```

`mem_queue(action="enqueue")` applies the configured `dedup_keys: [video_id, url]` against active + recently-archived items. Re-enqueues of the same `video_id` are rejected — the primary re-read guard for YouTube (mirrors the queue dedup gate for news).

Surface a per-type tally: `enqueued: K, dedup-rejected: D, listed: L`.

If `K == 0` for a type, skip step 2 for it.

---

## Step 2 — Fan out writer subagents (per source type)

For each type with new queue items:

```
mem_queue(action="peek", source_type="<slug>", n=<drain_batch_max>)
```

For each peeked item, spawn one Task subagent in batches of `drain_parallelism`:

```
Task({
  subagent_type: "research-youtube-worker",
  model: "sonnet",
  description: "Write YT brief: <short title>",
  prompt: "<queue item dict, plus the spec's source_type and temporal_grain>\n\nProcess this queue item end-to-end per your spec. Return a single-line JSON outcome as the final non-empty line of your response."
})
```

The prompt must embed:
- The full queue item dict (id, video_id, url, title, channel, channel_id, published, description).
- `source_type: <youtube-events or youtube-concepts>`.
- `temporal_grain: <event or concept>` — the worker branches on this for theme attachment.

Collect each worker's final JSON line. Recognised outcomes (see `.claude/agents/research-youtube-worker.md`):

| Status | Meaning | Archive |
|---|---|---|
| `accepted` | New note written | `mem_queue(action="archive", item_id=..., status="done")` |
| `idempotent_skip` | Existing note matched `video_id` (worker's mem_search guard fired) | `status="done"` — successful no-op |
| `fetch_failed` | `gemini_refused:` → archive `status="failed"`; `gemini_failed:` → leave in queue for retry; `empty_transcript:` → `status="failed"`; `mem_create:` → `status="failed"` |

The `idempotent_skip` arm makes this safe to re-run after a crash mid-batch — a worker that finds a note for its `video_id` from a previous run silently succeeds, queue item archived `done`, no duplicates.

**Retry policy.** `gemini_failed:` reasons (transient SDK errors, rate limits) are left in the queue for the next drain. All other failures are archived — Gemini refusals are not retryable on the same video, empty transcripts won't change, and `mem_create:` errors indicate a vault problem the user needs to investigate.

---

## Step 3 — Report

```
YouTube drain summary:
  youtube-events:
    listed: L,  enqueued: K  (dedup-rejected: D)
    workers: <accepted> ⇒ <src-IDs, max 6 then …>
    idempotent_skip: I
    gemini_refused: GR
    gemini_failed (retry): GF
    empty_transcript: ET
    mem_create_failed: MF
  youtube-concepts:
    [same shape]

  Themes:
    candidate stubs floated: <count from events-grain auto-fire>
    (run `/themes-resolve` to review)
```

The candidate-stub count comes from `VaultManager.create_note`'s auto-fire — the worker doesn't need to invoke it explicitly. Stubs land at `vault/themes/_candidates/cand-XXXX-*.md`.

---

## Re-read guard recap

For your own debugging — if you ever wonder why a re-run skipped or wrote something:

1. **Queue dedup (primary)** — `mem_queue(action="enqueue")` rejects any item whose `video_id` matches an active or recently-archived queue row. Active for 30 days post-archive.
2. **Worker mem_search (secondary)** — `research-youtube-worker` step 2 does `mem_search(video_id)` and short-circuits to `idempotent_skip` on a hit. Covers the case where the queue was wiped but the vault note exists.

YouTube has no equivalent to newsletter's `processed_label` (RSS feeds don't carry server-side state). Guards 1 and 2 cover all the cases newsletter's three-layer guard does — guard 1 stops every re-emit at the queue layer; 2 covers queue wipes.

---

## URL paste path

The `/research` router classifies pasted URLs by `url_patterns` and dispatches the matching one-off skill. For YouTube URLs (`youtube.com/watch`, `youtu.be/`, `youtube.com/shorts`), `/research <url>` enqueues to the matching `youtube-*` queue and then immediately fans out a single worker. The user picks the grain by which arg they pass to `/research`, or `/research` defaults to `youtube-concepts` for ambiguous cases.

> Implementation note: `/research` dispatch by `url_patterns` already covers this — no special-casing needed in this skill. The `url_patterns` for both `youtube-events` and `youtube-concepts` overlap intentionally; the router picks the first matching type in registry order (`youtube-events` wins by default — change the order in `vault/.mem/sources.yaml` to flip the default).

---

## When to use related skills

| Skill | Best for |
|---|---|
| `/youtube` | Drain all `youtube-*` queues from RSS feeds |
| `/youtube youtube-events` | Drain just the event-grain queue |
| `/research <yt-url>` | One-off URL paste — enqueue + drain a single video |
| `/themes-resolve --promote <cand-id>` | Promote a floated candidate stub into a canonical `thm-` theme |
| `/source-fit` | Diagnose whether a new channel shape fits the existing two types |

---

## What this skill does NOT do

- Download videos or audio files. Gemini consumes YT URLs natively; no local files.
- Apply server-side processed labels. YouTube RSS has no equivalent — queue dedup is the only guard.
- Fall back to `youtube-transcript-api` or any other transcript source. Gemini is the only extraction path; refusals (~5% expected) get archived as `failed`.
- Run a Haiku admission triage. The channel allowlist *is* the admission gate — by adding a channel ID to config, the user pre-decides that this channel is worth briefing.
- Require interactive auth. Headless cron use via `claude -p "/youtube"` works as-is — no OAuth, no provider connector.

---

## Prerequisites

The Gemini extraction step requires:

1. `pip install personal-mem[gemini]` — installs `google-generativeai`.
2. `export GOOGLE_API_KEY=<your-key>` — get a free key at <https://aistudio.google.com/apikey>. Gemini 2.5 Flash free tier (10 RPM, 250 RPD) covers personal volume.

If either is missing, the worker will return `gemini_failed: missing_sdk` or `gemini_failed: missing_api_key` for every video — the orchestrator will surface this in the report and the queue items will stay queued until the prerequisites are met.
