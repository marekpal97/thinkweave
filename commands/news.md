---
name: news
owns_mechanic: news_url_ingest
source_type: news
capabilities: [import]
consumes: [mem_concepts, mem_create, mem_link]
produces: [vault/sources/news/**]
tools:
  - Read
  - Bash
  - Task
  - mem_concepts
  - mem_create
  - mem_link
description: One-off URL ingest for news articles, mid-conversation. Dispatches a Sonnet writer directly; no triage gate (you've already decided this is worth briefing).
---

# /news — One-off news URL ingest

Use this when you've just read a news article and want to fold it into the vault now, without waiting for the next cron drain.

**No admission gate.** By typing `/news <url>` you're already saying "this is worth briefing." The Haiku title-triage that filters the cron firehose isn't relevant to a single user-decided URL — it would just add a model turn for no decision value. Triage stays in `/drain --source-type news` Path B Stage 1, where it's earning its keep filtering hundreds of items per drain.

This is the same admission posture as `/research <paper-url>` / `/research <repo-url>` / `/research <article-url>` — the atomic ingest unit, no per-item filter. News, paper, repo, and article all behave the same way from a one-off URL.

## Argument

`/news <url>` — single URL. Writer fires immediately.

(`--force` is gone — there is no triage gate to override.)

## Steps

### 1. Look up outlet metadata

Read `vault/config/PRIORITIES.yaml (intake.news.outlets)` and find the outlet whose feed `url_patterns` (or `name` substring) matches the host of the input URL. From that outlet entry pull:

- `name` (display name → `outlet_name`)
- `slug` → `outlet`
- `tier`
- `region`
- `language`
- `prefer_embedded` (set false here — we don't have a feed entry, the worker fetches via curl)

If no outlet matches the host: build a minimal stub — `outlet_name = <hostname>`, `outlet = <hostname-slug>`, `tier = 2` (untrusted by default), `region = "global"`, `language = "en"`. Surface this fact in the report so the user can decide whether to add the outlet to `news_feeds.yaml` for future cron pickup.

### 2. Build a synthetic queue item

```json
{
  "id": "manual-<short-hash>",
  "url": "<input url>",
  "title": "",
  "summary": "",
  "outlet": "<slug>",
  "outlet_name": "<display name>",
  "tier": <int>,
  "region": "<region>",
  "language": "<language>",
  "prefer_embedded": false,
  "embedded_body": null,
  "published": ""
}
```

The `title` / `summary` / `published` are blank — the writer fetches the article and extracts them.

### 3. Dispatch to the writer subagent

```
Task({
  subagent_type: "research-news-worker",
  model: "sonnet",
  description: "Write news brief: <hostname>",
  prompt: "<the JSON item above>\n\ntriage_verdict: keep_unfiled\ntheme_id: null\ntriage_reason: \"one-off user ingest\"\n\nProcess this single news item end-to-end. The vault root is <PERSONAL_MEM_VAULT — pass the absolute path>. Return your standard one-line JSON outcome as the final non-empty line of your response."
})
```

Under the plugin install the worker is registered as `personal-mem:research-news-worker` — if the bare type doesn't resolve, retry once with the prefix.

The synthesized `triage_verdict: keep_unfiled` mirrors what the cron drain emits for items that didn't theme-match — the writer files the note with `theme_unfiled: true` so the periodic theme-review pass can pick it up. If the user wants to file the new note under an explicit theme later, that's a manual `mem_link source_id target_id --type relates_to` call after the writer returns.

The writer fetches the article, extracts ontology-gated concepts, writes the brief, and `mem_create`s the source note.

### 4. Report

Parse the writer's JSON outcome:

- **`accepted`** → "Created `<src-id>` from `<outlet>`. Filed as **theme-unfiled** for periodic review. Concepts: `<list>`."
- **`fetch_failed`** → "Couldn't fetch the article — paywall, network failure, or bot wall. Source URL: `<url>`. Try saving the page text manually and using `/capture`."

If outlet was a stub (step 1), append: *"Outlet `<host>` isn't in `news_feeds.yaml`. Add it if you want this source pulled by cron."*

---

## When to use which path

| Path | Trigger |
|---|---|
| `/news <url>` | Just-read-it moment; one URL; want to process now. No triage, no filter — you've decided. |
| `/drain --source-type news` | Periodic (cron); processes the queue from RSS pull. Triage gate fires here against the title firehose. |
| `/research <url>` | URL is from a known platform and you're not sure which type. Router classifies; for news the router delegates here. |

---

## Notes

- For Polish-language URLs (`bankier.pl`, etc.): the writer translates inline and stashes the original in `<source_dir>/raw.md`. The user-facing brief is in English.
- No queue archive — the synthetic item never enters the queue.
- The writer subagent's `mem_create` call goes through `VaultManager.create_note`, which incrementally indexes the new event-grain source so `detect_signals` sees it on the next `/dream` scan. Theme naming is `/dream`'s job: it reads the enriched cluster signal (raw `proposed_theme:` tally + overlapping active themes) and either mints a new theme (`theme_mints`) or extends an existing one (`theme_extensions`).
