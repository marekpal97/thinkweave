---
name: podcast
owns_mechanic: podcast_inbox
source_type: podcast-events, podcast-concepts
capabilities: [acquire]
consumes: [weave_sources_config, weave_queue]
produces: [vault/.weave/queues/podcast-events.jsonl, vault/.weave/queues/podcast-concepts.jsonl, vault/sources/podcast-events/**, vault/sources/podcast-concepts/**]
tools:
  - Read
  - Bash
  - weave_queue
  - weave_sources_config
description: Thin orchestrator over the podcast intake rails. Calls the `rss_poll` discover strategy to enqueue from per-show RSS feeds, then `/drain --source-type podcast-*` to fan out writer subagents. Headless-safe.
---

# /podcast — Podcast intake (orchestrator)

`/podcast` is a thin orchestrator that wires two existing rails:

1. **Discover** — `weave discover --strategy rss_poll --source-type <slug>` polls every show's RSS feed for each `podcast-*` source type and enqueues new episodes (dedup against queue + indexer happens inside the strategy).
2. **Drain** — `/drain --source-type <slug>` peeks the queue and fans out `research-podcast-worker` Sonnet subagents. Each worker downloads the MP3 enclosure and hands it to Gemini Flash via the Files API for a structured brief.

No RSS-parsing code or writer-spawn logic lives in this skill — both rails are reusable for any source family. This file just sequences them and aggregates the reports.

**Arguments (all optional):**
- `<source-type>` — limit to one type, e.g. `/podcast podcast-events`. Default: all `podcast-*` types from config.
- `--limit N` — forwarded to `/drain`.

---

## Step 0 — Discover the source-type set

```
weave_sources_config()
```

Pick every key under `sources.` whose slug starts with `podcast-`. If `<source-type>` was passed, filter to just that one. If no `podcast-*` types are configured, stop with `"No podcast source types in sources.yaml — nothing to do."`.

For each type, check that the file pointed at by `feed_config:` exists and contains at least one outlet with a non-placeholder `feeds:` entry. Types whose feed file is empty (or only commented-out examples) are skipped here with a hint:

> `podcast-events`: no outlets configured in `vault/config/PRIORITIES.yaml (intake.podcast_events.outlets)`. Add a show by uncommenting an example or adding a new outlet block.

---

## Step 1 — Poll RSS feeds via the rss_poll discover strategy

For each `podcast-*` type with at least one configured outlet:

```bash
uv run weave discover --strategy rss_poll --source-type <slug>
```

The strategy:
- Reads `feed_config:` from `sources.yaml` to get the outlets file path,
- Parses every outlet's `feeds:` URLs via feedparser,
- Extracts the `<enclosure>` audio URL, `<itunes:duration>`, `<itunes:episode>`, and `<guid>` per item,
- Applies `lookback_days` cutoff and per-outlet `daily_cap`,
- Dedups against active queue + recent archive + the SQLite indexer (URLs already noted),
- Enqueues new items into `vault/.weave/queues/<slug>.jsonl`.

Stdout is a JSON list of descriptors. Two kinds matter here:

| `kind` | Meaning |
|---|---|
| `enqueued` | One row per newly enqueued episode — surface in the report |
| `summary` | One row per source type with stats (`feeds`, `entries_seen`, `enqueued`, `dup_queue`, `dup_indexer`, `stale_lookback`, `cap_hit`, `feed_errors`) |

A `kind: external` with `status: error` indicates `feedparser_missing` — surface the install hint and stop.

If `summary.enqueued == 0` for a type, skip step 2 for it.

---

## Step 2 — Drain via /drain

For each type that got fresh items:

```
Skill(skill="drain", args="--source-type <slug> [--limit N]")
```

Under the plugin install, skills resolve namespaced — if `Skill(skill="drain")` fails with an unknown skill, retry as `thinkweave:drain`.

`/drain` handles Path B (writer-only, no triage) for `podcast-*` — it peeks the queue, fans out `research-podcast-worker` subagents at `drain_parallelism` (default 2 — Gemini Files API uploads are bandwidth-bound), validates allowed-failure prefixes, and archives outcomes.

**Why parallelism: 2 and not 4 like news/newsletter?** Each podcast worker downloads a 20-100MB MP3 and uploads to Gemini before generation. Higher parallelism saturates upstream bandwidth and rarely improves wall-clock. Override per-type in `sources.yaml` if you have a fat pipe.

The orchestrator returns the drain report verbatim per type.

---

## Step 3 — Report

```
Podcast intake summary:
  podcast-events:
    rss_poll: <listed> entries seen, <enqueued> enqueued
             (dup_queue: D, dup_indexer: I, stale: S, cap_hit: C, feed_errors: E)
    drain:   <accepted> ⇒ <src-IDs, max 6 then …>
             idempotent_skip: K, fetch_failed: F
  podcast-concepts:
    [same shape]

  Themes:
    (signals surface on next `/dream` scan; no per-drain count)
```

Event-grain podcast sources are indexed at write time; clusters surface via `detect_signals` on the next `/dream` cycle — no per-drain mint step here.

---

## Re-read guard recap

For your own debugging — if you ever wonder why a re-run skipped or wrote something, the guards live in the two rails:

1. **`rss_poll` strategy dedups against active queue + recent archive + indexer URLs.** An episode whose URL is already a `type: source` note in the vault won't re-enqueue, even months later. `entry_id` (the RSS `<guid>`) is the most stable key — `audio_url` can change when a show migrates CDNs.
2. **`research-podcast-worker` idempotent_skip.** A `weave_search(entry_id)` hit short-circuits to `status="done"` no-op — covers the case where the queue was wiped but the vault note exists.

Podcasts have no equivalent to newsletter's `processed_label` (RSS feeds carry no server-side state). Guards 1 and 2 together cover what mail's three-layer guard does.

---

## URL paste path

The `/research` router classifies pasted URLs by `url_patterns` and dispatches the matching one-off skill. For podcast URLs (`feeds.megaphone.fm/...`, `feeds.libsyn.com/...`, etc.), `/research <url>` enqueues to the matching `podcast-*` queue and then immediately fans out a single worker via `/drain` — same two-rail composition as `/podcast`, just on one URL.

**One caveat:** the URL pasted into `/research` should be the RSS feed URL (the strategy needs to parse the feed to find the episode's `<enclosure>`). Pasting just a Spotify or Apple Podcasts episode URL won't work — those are player links, not RSS items. Find the show's RSS feed first.

The `url_patterns` for both `podcast-events` and `podcast-concepts` overlap intentionally; the router picks the first matching type in registry order (`podcast-events` wins by default — change the order in `vault/config/sources.yaml` to flip the default).

---

## When to use related skills

| Skill | Best for |
|---|---|
| `/podcast` | Discover (rss_poll) + drain all `podcast-*` queues in one shot |
| `/podcast podcast-events` | Same, limited to one source type |
| `weave discover --strategy rss_poll --source-type podcast-events` | Discover only (no drain) — useful in cron flows that drain separately |
| `/drain --source-type podcast-events` | Drain only (queue was already filled) |
| `/research <rss-url>` | One-off URL paste — enqueue + drain one episode || `/source-fit` | Diagnose whether a new show shape fits the existing two types |

---

## What this skill does NOT do

- Parse RSS itself — that lives in the `rss_poll` discover strategy, reusable by any source family.
- Spawn writer subagents itself — that lives in `/drain` Path B, reusable too.
- Download MP3s. That's the worker's job (and the worker hands them straight to Gemini via the Files API; nothing persists locally).
- Resolve Spotify / Apple Podcasts player URLs to their underlying RSS feeds. Those platforms intentionally obscure the canonical RSS; use Castro / Overcast / Podchaser, or the iTunes lookup API: `curl -s 'https://itunes.apple.com/lookup?id=<APPLE_ID>' | jq -r '.results[0].feedUrl'`.
- Apply server-side processed labels. Podcast RSS has no equivalent — the rss_poll + worker idempotent_skip guards cover the cases newsletter's mail-label primary covers.
- Run a Haiku admission triage. The show allowlist *is* the admission gate.
- Require interactive auth. Headless cron use via `claude -p "/podcast"` works as-is.

---

## Prerequisites

The audio-extraction step (inside `research-podcast-worker`) requires:

1. `pip install thinkweave[gemini]` — installs `google-genai>=1.0`.
2. `GOOGLE_API_KEY` env var (or `.env` in `$THINKWEAVE_VAULT` / CWD).

If the SDK is missing or the key is unset, drain workers return `api_error: missing_sdk` / `api_error: missing_api_key` — surface in the report and leave the queue items pending until fixed.

`feedparser` is required by the rss_poll strategy. Install via `uv add --optional news feedparser` (already a transitive dep of the `[news]` extra).

### Why Gemini Flash over Whisper?

Gemini 2.5 Flash transcribes + summarises audio in a single API call (~$0.05-0.15 per 1-hour episode), where Whisper requires two hops (transcription, then a separate LLM summary call) at ~$0.36/hr plus the summary cost. The project already standardised on Gemini Flash for the YouTube workers — adding podcast on the same path keeps one model, one API key, one prompting pattern.

The youtube-worker had a refusal-rate problem with Gemini Flash on captioned conference content (3/3 on an AI Engineer sample) and migrated to `youtube-transcript-api`. That problem doesn't transfer here: spoken-word podcasts don't have the visual / multimodal ambiguity that triggered the refusals, and there's no `podcast-transcript-api` equivalent to migrate to (no platform exposes a free transcripts endpoint for podcasts the way YouTube exposes captions).
