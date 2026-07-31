---
name: discover
owns_mechanic: research_discovery
source_type: [paper, repo, article, news, youtube-events, youtube-concepts, newsletter-events, newsletter-concepts]
capabilities: [discover]
consumes: [weave_concepts, weave_search, weave_timeline, weave_queue]
produces: [vault/.weave/queues/*.jsonl, vault/reports/discover/*]
tools:
  - Read
  - Bash
  - WebSearch
  - weave_concepts
  - weave_search
  - weave_read
  - weave_timeline
  - weave_queue
  - weave_sources_config
description: Strategy-driven discovery. Runs the configured strategy list; strategies emit gap descriptors and (for external-trigger ones) directly enqueue. The single producer rail in the discover→drain spine.
---

# /discover — Strategy-driven discovery

`/discover` is the single **producer rail** in the discover → drain spine. It runs a registered strategy (or the configured list) and surfaces what was found. Strategies split into two flavors:

| Flavor | Trigger | Examples | Side effect |
|---|---|---|---|
| **Internal-state** | Observe vault | `decision_review`, `prompt_gap` | Emit gap descriptors; the skill resolves them (WebSearch → enqueue) |
| **External-trigger** | Observe outside world | `rss_poll`, `mail_poll`, `external_tool_runner` | Strategy enqueues directly (rss_poll) or emits a plan (mail_poll) |

Both flavors share the same `run(vault, project, config)` contract.

### Gap-emitters vs enqueue-emitters: an intentional dual (C20)

The two flavors are **not** a TODO to merge. Gap-emitters scan vault state and report what's missing; enqueue-emitters observe outside signals and write the queue directly. Each emits a fundamentally different shape:

- Gap-emitter output (`kind: "gap"`) carries concept/decision metadata and *describes* a need. The skill decides what to do about it (WebSearch + `weave_queue`, or surfacing in the report). Forcing gap-emitters to enqueue would conflate "scan and report" with "decide what to do" — the strategies would need synthesis logic that legitimately lives in this skill.
- Enqueue-emitter output (`kind: "enqueued"` / `kind: "mail_fetch_needed"`) is already a queue item or a plan for one. The skill just records what happened.

Future strategies that emit gap descriptors stay gap-emitters; future strategies that observe external signals enqueue directly. The split is healthy separation of concerns, not duplication.

## Built-in strategies

| Strategy | Flavor | What it surfaces |
|---|---|---|
| `decision_review` | internal-state | `proposed`/`accepted` decisions stalled past N days |
| `prompt_gap` | internal-state | Hyphenated-compound terms probed about that aren't in the ontology — routed to proposal, not research |
| `external_tool_runner` | external-trigger | User-provided scripts emit JSONL queue items |
| `rss_poll` | external-trigger | RSS/Atom polling for any source type with `feed_config:` or `channels:` configured. Enqueues directly into the matching queue. |
| `mail_poll` | external-trigger | Composes the effective Gmail query (sender allowlist + `processed_label` exclusion + lookback). Emits a `mail_fetch_needed` plan that `/newsletter` executes via Gmail MCP. |

## Per-project strategy lists live in `vault/config/sources.yaml`:

```yaml
projects:
  default:
    discover_strategies: []
  myresearch:
    discover_strategies: [decision_review]
  external_signals:
    discover_strategies: [external_tool_runner]
    external_tool_runner:
      tools:
        - command: ["./scripts/scrape_signals.py"]
```

`rss_poll` and `mail_poll` are typically invoked explicitly per source-type from the consumer skill (`/youtube`, `/newsletter`) or from cron, not added to a project's default `discover_strategies` — they're keyed by `source_type`, not by project.

## Steps

### 1. Fetch the descriptor list

```
weave discover --project <name>                              # configured strategies
weave discover --strategy decision_review                    # one strategy, all projects
weave discover --strategy rss_poll --source-type news        # one strategy, one source type
weave discover --strategy mail_poll --source-type newsletter-events
```

`--source-type X` is read by external-trigger strategies (rss_poll, mail_poll) to limit work to one type; internal-state strategies ignore it.

`weave discover` returns a JSON list — one entry per descriptor. Each entry has a `strategy` field, a `kind`, and strategy-specific keys.

If the list is empty, report "no descriptors this run" and exit.

### 2. Resolve descriptors

Dispatch on `kind`:

- **`kind: review` (decision_review)** — surface the decision in the run report; do not auto-queue. The user inspects via `weave show <decision_id>` and decides whether to flip status or schedule a re-discussion. If `focus.watch_themes` in `PRIORITIES.yaml` is non-empty and the decision's `implements:` intersects it, mark the row "(watched theme)" in the report.

- **`kind: ontology_proposal` (prompt_gap)** — the user has probed about a hyphenated-compound term not in the ontology. Surface as a candidate for `/tighten` to canonicalise; do not WebSearch or enqueue research items from it.

- **`kind: research_focus` (focus_research)** — a declared focus concept with its substrate exemplars, probe evidence, and `source_coverage` partition. If coverage shows a real gap (e.g. zero sources of a type the concept plausibly needs), resolve it: compose a WebSearch from the concept, **tightened by `probe_texts`** (the user's verbatim open questions — search what they asked, not just the slug), and enqueue the best hit via `weave_queue`. **Classify the hit's URL and enqueue into the matching per-type queue** — same rule `/research`'s classification table uses (`weave_sources_config()`'s `url_patterns`: `arxiv.org`/`openreview.net` → `paper`, `github.com`/`gitlab.com` → `repo`, unmatched → `article` fallback). **Never enqueue with `source_type: "research"` or omit `source_type`** — there is no generic "research" queue in the paper/repo/article split, and items enqueued without a real per-type `source_type` are silently orphaned (nothing drains them; this produced a 54-item stuck backlog in `research.jsonl` before the split-classification step was added here). Carry `concept`, `url`, `title`, and the `probes` field over so the drain-side writer keeps the angle. If coverage looks adequate, surface the row in the report only. This is the discover-rail twin of `/dream`'s `queue_item.probes`.

- **`kind: external` (external_tool_runner)** — payloads are user-shaped. If they look like queue items (have `url` / `title` fields), enqueue via `weave_queue`. Otherwise pass through to the run report.

- **`kind: enqueued` (rss_poll)** — the strategy already enqueued. Surface the row in the report (title, url, outlet/channel, queue_item_id); no further action.

- **`kind: summary` (rss_poll)** — per-source-type stats counters. Surface in the report.

- **`kind: mail_fetch_needed` (mail_poll)** — the strategy composed the Gmail query but cannot execute it from Python. Surface it in the report; the caller (`/newsletter`) is expected to execute. When `/discover` is invoked standalone (without `/newsletter` orchestrating), the skill reports the plan and stops — it does not call Gmail MCP itself, by design.

- **`kind: external` with `status: error`** (rss_poll / mail_poll) — surface the reason + hint verbatim. Common causes: `feedparser_missing`, `empty_allowlist`, `connector_not_implemented`.

### 3. Persist the run report

Write the report (section 4's exact content) to `vault/reports/discover/discover-<YYYYMMDD-HHMMSS>.md` via Bash (`mkdir -p` the directory first). This mirrors `/dream`'s `vault/reports/dream/*` — the `reports/` tree is the user-visible home for autonomous-run reports, deliberately excluded from the SQLite index (materialized narrative, not source material), and landing's "Recent Maintenance" section links the newest ones. No project subfolder — discover runs are cross-project.

Headless cron runs make this the **only** durable record of internal-state findings (stalled decisions, ontology proposals) — the chat report below evaporates with the transcript. Do not skip it even when every strategy came back empty; an "all quiet" report is itself signal. (This step replaces the former `discover-run` audit note — run audits are operational narrative and don't belong in the knowledge index.)

### 4. Report

```
## Discovery — <date>

### Strategies run
- decision_review: 3 stalled decisions surfaced (1 watched-theme)
- prompt_gap: 2 ontology-proposal candidates
- rss_poll (news): 8 enqueued (12 dup_queue / 3 dup_indexer / 0 stale)
- mail_poll (newsletter-events): plan emitted — `<effective_query>`

### Queue items created
| # | Title | URL | Strategy | Note |
|---|-------|-----|----------|------|

### Stalled decisions to revisit
- [[dec-XXXXXXXX]] — <title>

### Mail fetch plans (require /newsletter to execute)
- newsletter-events: <effective_query>

### Skipped
- N already in vault
- N already queued
- N excluded by filter
```

## Adding a new strategy

Drop a file under `src/thinkweave/acquisition/discover/strategies/` exposing a module-level `STRATEGY` instance, then add one `register()` call in `src/thinkweave/acquisition/discover/strategies/__init__.py`. The strategy implements:

```python
class MyStrategy:
    name = "my_strategy"

    def run(self, vault, project, config):
        # Returns list[dict] — descriptors.
        return [...]
```

For external-trigger strategies, read `config.get("_runtime", {}).get("source_type")` to honor the CLI `--source-type` filter.

No edits to the CLI, the MCP surface, or this skill are needed. Mention the new strategy in the project's `discover_strategies` list in `sources.yaml` (if it should run by default) and `weave discover` will pick it up.

## Notes

- Per-project queues are first-class. `RESEARCH_FOCUS.md` is no longer assumed singular — projects with their own queue cadence can route directly to a per-project file.
- `RESEARCH_FOCUS.md` is the user-authored research-priority surface — `/discover` reads it as ambient input but never writes it. The `## Concept Gaps` section in that doc is where on-focus, load-bearing, under-sourced concepts are tracked; gap-emitter strategies upstream of `/discover` were retired in favour of that hand-curated surface.
- For `rss_poll` cron: `claude -p "/discover --strategy rss_poll --source-type news"` is the canonical headless invocation. Replaces the legacy `scripts/pull_news_feeds.py`.
