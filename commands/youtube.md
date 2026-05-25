---
name: youtube
owns_mechanic: youtube_inbox
source_type: youtube-events, youtube-concepts
capabilities: [acquire]
consumes: [mem_sources_config, mem_queue]
produces: [vault/.mem/queues/youtube-events.jsonl, vault/.mem/queues/youtube-concepts.jsonl, vault/sources/youtube-events/**, vault/sources/youtube-concepts/**]
tools:
  - Read
  - Bash
  - mem_queue
  - mem_sources_config
description: Thin orchestrator over the YouTube intake rails. Calls the `rss_poll` discover strategy to enqueue from channel RSS feeds, then `/drain --source-type youtube-*` to fan out writer subagents. Headless-safe.
---

# /youtube — YouTube channel intake (orchestrator)

`/youtube` is a thin orchestrator that wires two existing rails:

1. **Discover** — `mem discover --strategy rss_poll --source-type <slug>` polls every channel's RSS feed for each `youtube-*` source type and enqueues new videos (dedup against queue + indexer happens inside the strategy).
2. **Drain** — `/drain --source-type <slug>` peeks the queue and fans out `research-youtube-worker` Sonnet subagents.

No RSS-parsing code or writer-spawn logic lives in this skill — both rails are reusable for any source family. This file just sequences them and aggregates the reports.

**Arguments (all optional):**
- `<source-type>` — limit to one type, e.g. `/youtube youtube-events`. Default: all `youtube-*` types from config.
- `--limit N` — forwarded to `/drain`.

---

## Step 0 — Discover the source-type set

```
mem_sources_config()
```

Pick every key under `sources.` whose slug starts with `youtube-`. If `<source-type>` was passed, filter to just that one. If no `youtube-*` types are configured, stop with `"No youtube source types in sources.yaml — nothing to do."`.

For each type, check that `channels:` is non-empty. Types with empty channel allowlists are skipped here (with a hint to add channel IDs).

---

## Step 1 — Poll RSS feeds via the rss_poll discover strategy

For each `youtube-*` type with a non-empty `channels:` list:

```bash
uv run mem discover --strategy rss_poll --source-type <slug>
```

The strategy:
- Builds `https://www.youtube.com/feeds/videos.xml?channel_id=<id>` for each channel,
- Parses via feedparser, applies `lookback_days` cutoff,
- Dedups against active queue + recent archive + the SQLite indexer (URLs already noted),
- Enqueues new items into `vault/.mem/queues/<slug>.jsonl`.

Stdout is a JSON list of descriptors. Two kinds matter here:

| `kind` | Meaning |
|---|---|
| `enqueued` | One row per newly enqueued video — surface in the report |
| `summary` | One row per source type with stats (`feeds`, `entries_seen`, `enqueued`, `dup_queue`, `dup_indexer`, `stale_lookback`, `feed_errors`) |

A `kind: external` with `status: error` indicates `feedparser_missing` — surface the install hint and stop.

If `summary.enqueued == 0` for a type, skip step 2 for it.

---

## Step 2 — Drain via /drain

For each type that got fresh items:

```
Skill(skill="drain", args="--source-type <slug> [--limit N]")
```

`/drain` handles Path B (writer-only, no triage) for `youtube-*` — it peeks the queue, fans out `research-youtube-worker` subagents at `drain_parallelism`, validates allowed-failure prefixes, and archives outcomes. The orchestrator returns the drain report verbatim per type.

---

## Step 3 — Report

```
YouTube intake summary:
  youtube-events:
    rss_poll: <listed> entries seen, <enqueued> enqueued
             (dup_queue: D, dup_indexer: I, stale: S, feed_errors: E)
    drain:   <accepted> ⇒ <src-IDs, max 6 then …>
             idempotent_skip: K, fetch_failed: F
  youtube-concepts:
    [same shape]

  Themes:
    (signals surface on next `/dream` scan; no per-drain count)
```

Event-grain YouTube sources are indexed at write time; clusters surface via `detect_signals` on the next `/dream` cycle — no per-drain mint step here.

---

## Re-read guard recap

For your own debugging — if you ever wonder why a re-run skipped or wrote something, the guards live in the two rails:

1. **`rss_poll` strategy dedups against active queue + recent archive + indexer URLs.** A video whose URL is already a `type: source` note in the vault won't re-enqueue, even months later.
2. **`research-youtube-worker` idempotent_skip.** A `mem_search(video_id)` hit short-circuits to `status="done"` no-op — covers the case where the queue was wiped but the vault note exists.

YouTube has no equivalent to newsletter's `processed_label` (RSS feeds carry no server-side state). Guards 1 and 2 together cover everything mail's three-layer guard does.

---

## URL paste path

The `/research` router classifies pasted URLs by `url_patterns` and dispatches the matching one-off skill. For YouTube URLs (`youtube.com/watch`, `youtu.be/`, `youtube.com/shorts`), `/research <url>` enqueues to the matching `youtube-*` queue and then immediately fans out a single worker via `/drain` — same two-rail composition as `/youtube`, just on one URL.

The `url_patterns` for both `youtube-events` and `youtube-concepts` overlap intentionally; the router picks the first matching type in registry order (`youtube-events` wins by default — change the order in `vault/.mem/sources.yaml` to flip the default).

---

## When to use related skills

| Skill | Best for |
|---|---|
| `/youtube` | Discover (rss_poll) + drain all `youtube-*` queues in one shot |
| `/youtube youtube-events` | Same, limited to one source type |
| `mem discover --strategy rss_poll --source-type youtube-events` | Discover only (no drain) — useful in cron flows that drain separately |
| `/drain --source-type youtube-events` | Drain only (the queue was already filled) |
| `/research <yt-url>` | One-off URL paste — enqueue + drain one video |
| `/themes-resolve --promote <cand-id>` | Promote a floated candidate stub into a canonical `thm-` theme |
| `/source-fit` | Diagnose whether a new channel shape fits the existing two types |

---

## What this skill does NOT do

- Parse RSS itself — that lives in the `rss_poll` discover strategy, reusable by any source family.
- Spawn writer subagents itself — that lives in `/drain` Path B, reusable too.
- Download videos or audio files. Gemini consumes YT URLs natively; no local files.
- Apply server-side processed labels. YouTube RSS has no equivalent — the rss_poll + worker idempotent_skip guards cover the cases newsletter's mail-label primary covers.
- Run a Haiku admission triage. The channel allowlist *is* the admission gate.
- Require interactive auth. Headless cron use via `claude -p "/youtube"` works as-is.

---

## Prerequisites

The transcript-extraction step (inside `research-youtube-worker`) requires:

1. `pip install personal-mem[youtube]` — installs `youtube-transcript-api`. No API key, no auth.

If the SDK is missing, drain workers return `transcript_api_failed: missing_sdk` — surface in the report and leave the queue items pending until installed.

`feedparser` is required by the rss_poll strategy. Install via `uv add --optional news feedparser` (already a transitive dep of the `[news]` extra).

### Why transcripts over Gemini Flash?

The previous PR routed every YT video through Gemini Flash, which gave back pre-extracted `summary` / `key_developments` / `key_moments` in one call. The empirical refusal rate on captioned conference content turned out to be much higher than the spec's ~5% assumption (3/3 on an AI Engineer sample). YouTube's own auto-captions cover essentially all English uploads, so the transcripts route gets you (a) a lower-cost zero-API path and (b) a much higher success rate on the channels you actually subscribe to. The trade is that the worker now derives `key_developments` / `key_moments` from the raw transcript itself (Sonnet handles 15-30K-char talks fine) rather than dropping in Gemini's structured payload.

The `gemini_extract` module is still present and tested — re-engage it as a fallback for `transcripts_disabled` videos by editing `research-youtube-worker.md` step 3 to chain a Gemini call after a transcripts failure.
