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
description: One-off URL ingest for news articles, mid-conversation. Runs the same Haiku triage as `/drain --source-type news`, then dispatches a Sonnet writer for accepts. Supports `--force` to bypass triage when the user is sure.
---

# /news — One-off news URL ingest

Use this when you've just read a news article and want to fold it into the vault now, without waiting for the next cron drain. Same admission gate as the cron path: Haiku title-triage against the active-themes catalog, then Sonnet writer if `keep` or `keep_unfiled`.

## Argument

`/news <url>` — single URL.

`/news <url> --force` — skip triage; treat as `keep_unfiled` and dispatch the writer directly. Use when the article is clearly substantive but the title doesn't telegraph it (e.g., paywalled outlet whose RSS title is uninformative).

## Steps

### 1. Look up outlet metadata

Read `vault/.mem/news_feeds.yaml` and find the outlet whose feed `url_patterns` (or `name` substring) matches the host of the input URL. From that outlet entry pull:

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

The `title` / `summary` / `published` are blank for now. Title-triage needs a real title — see step 3.

### 3. Resolve title (cheap)

Triage works on the article *title*, not the URL. To get one without spinning up the full Sonnet writer:

```bash
curl -sL --max-time 15 -A 'personal-mem/1.0' '<url>' | grep -oP '(?<=<title>).*?(?=</title>)' | head -1
```

If that fails (paywall, JS-only page, Cloudflare), fall back to using the path slug as a degraded title. If `--force` is set, skip this step entirely and proceed to step 5 with an empty title.

### 4. Triage

Run the triage helper on the single item:

```bash
echo '[{"id":"manual-<hash>","title":"<resolved title>","outlet":"<slug>","tier":<n>}]' | \
  uv run python -m personal_mem.operations.news_triage \
    --themes <vault_root>/THEMES.md
```

Parse the verdict:

- `drop` → report to the user: *"Triage rejected: `<reason>`. URL not ingested. Use `/news <url> --force` to override."* Done.
- `keep` → record `theme_id`, proceed to step 5.
- `keep_unfiled` → proceed to step 5 with `theme_id: null`.

If `--force`, synthesise a `keep_unfiled` verdict with `theme_id: null` and `triage_reason: "user --force override"`.

### 5. Dispatch to the writer subagent

```
Task({
  subagent_type: "research-news-worker",
  model: "sonnet",
  description: "Write news brief: <hostname>",
  prompt: "<the JSON item above>\n\ntriage_verdict: <keep|keep_unfiled>\ntheme_id: <thm-X|null>\ntriage_reason: <reason>\n\nProcess this single news item end-to-end. The vault root is <PERSONAL_MEM_VAULT — pass the absolute path>. Return your standard one-line JSON outcome as the final non-empty line of your response."
})
```

The writer fetches the article, extracts ontology-gated concepts, writes the brief, and `mem_create`s the source note (filed under `relates_to: [theme_id]` if `keep`, or with `theme_unfiled: true` if `keep_unfiled`).

### 6. Report

Parse the writer's JSON outcome:

- **`accepted`, `theme_id` set** → "Created `<src-id>` from `<outlet>`. Filed under `[[<theme_id>]]`. Concepts: `<list>`."
- **`accepted`, `theme_id: null` (unfiled)** → "Created `<src-id>` from `<outlet>`. Filed as **theme-unfiled** for periodic review. Concepts: `<list>`."
- **`fetch_failed`** → "Couldn't fetch the article — paywall, network failure, or bot wall. Try `/news <url> --force` if you have the body in clipboard / can manually add."

If outlet was a stub (step 1), append: *"Outlet `<host>` isn't in `news_feeds.yaml`. Add it if you want this source pulled by cron."*

---

## When to use which path

| Path | Trigger |
|---|---|
| `/news <url>` | Just-read-it moment; one URL; want to process now. Triage decides admission. |
| `/news <url> --force` | Same, but skip the triage gate — you've decided this is worth the brief regardless. |
| `/drain --source-type news` | Periodic (cron); processes the queue from RSS pull. |
| `/research <url>` | URL is from a known platform (paper/repo/article) and you're not sure which type. Router classifies; for news the router delegates here. |

---

## Notes

- The triage helper reads `vault/THEMES.md`'s `## Catalog (active)` section. If your active-theme catalog is empty (`mem themes scan-candidates` hasn't run, or you haven't promoted any candidates), every triage will return `keep_unfiled` — meaning everything substantive is admitted but nothing gets theme-attached. That's the intended behavior: the unfiled pile is where emerging arcs collect until you promote them.
- For Polish-language URLs (`bankier.pl`, etc.): the writer translates inline and stashes the original in `<source_dir>/raw.md`. The user-facing brief is in English.
- No queue archive — the synthetic item never enters the queue.
- The writer subagent's `mem_create` call goes through `VaultManager.create_note`, which auto-fires `scan_candidates(source_type='news')` for event-grain sources. So even single-item ingest contributes to candidate floating — no separate `theme_scan` step needed. To force a fresh scan ignoring the per-create dedup, run `mem themes scan-candidates --source-type news` directly.
