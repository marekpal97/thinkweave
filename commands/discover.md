---
name: discover
owns_mechanic: research_discovery
source_type: [paper, repo, article, news, youtube-events, youtube-concepts, newsletter-events, newsletter-concepts]
capabilities: [discover]
consumes: [mem_concepts, mem_search, mem_timeline, mem_queue]
produces: [vault/.mem/queues/*.jsonl]
tools:
  - Read
  - Bash
  - WebSearch
  - mem_concepts
  - mem_search
  - mem_read
  - mem_timeline
  - mem_create
  - mem_queue
  - mem_sources_config
description: Strategy-driven discovery. Runs the configured strategy list; strategies emit gap descriptors and (for external-trigger ones) directly enqueue. The single producer rail in the discover→drain spine.
---

# /discover — Strategy-driven discovery

`/discover` is the single **producer rail** in the discover → drain spine. It runs a registered strategy (or the configured list) and surfaces what was found. Strategies split into two flavors:

| Flavor | Trigger | Examples | Side effect |
|---|---|---|---|
| **Internal-state** | Observe vault | `concept_coverage`, `decision_review`, `theme_drift` | Emit gap descriptors; the skill resolves them (WebSearch → enqueue) |
| **External-trigger** | Observe outside world | `rss_poll`, `mail_poll`, `external_tool_runner` | Strategy enqueues directly (rss_poll) or emits a plan (mail_poll) |

Both flavors share the same `run(vault, project, config)` contract.

## Built-in strategies

| Strategy | Flavor | What it surfaces |
|---|---|---|
| `concept_coverage` | internal-state | Load-bearing concepts with thin source coverage |
| `decision_review` | internal-state | `proposed`/`accepted` decisions stalled past N days |
| `theme_drift` | internal-state | `active` themes whose Catalyst log has gone silent |
| `external_tool_runner` | external-trigger | User-provided scripts emit JSONL queue items |
| `rss_poll` | external-trigger | RSS/Atom polling for any source type with `feed_config:` or `channels:` configured. Enqueues directly into the matching queue. |
| `mail_poll` | external-trigger | Composes the effective Gmail query (sender allowlist + `processed_label` exclusion + lookback). Emits a `mail_fetch_needed` plan that `/newsletter` executes via Gmail MCP. |

## Per-project strategy lists live in `vault/.mem/sources.yaml`:

```yaml
projects:
  default:
    discover_strategies: [concept_coverage]
  myresearch:
    discover_strategies: [concept_coverage, decision_review]
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
mem discover --project <name>                              # configured strategies
mem discover --strategy concept_coverage                   # one strategy, all projects
mem discover --strategy rss_poll --source-type news        # one strategy, one source type
mem discover --strategy mail_poll --source-type newsletter-events
```

`--source-type X` is read by external-trigger strategies (rss_poll, mail_poll) to limit work to one type; internal-state strategies ignore it.

`mem discover` returns a JSON list — one entry per descriptor. Each entry has a `strategy` field, a `kind`, and strategy-specific keys.

If the list is empty, report "no descriptors this run" and exit.

### 2. Resolve descriptors

Dispatch on `kind`:

- **`kind: gap` (concept_coverage)** — run a WebSearch for the concept, dedup against existing sources / queue items via `mem_concepts(action="source_counts", concepts=[<name>])`, and create up to 3 queue items via `mem_queue`. Each new item carries `Gap: [[<concept>]]` in its body and the gap concept(s) in its `concepts` frontmatter (ontology terms — load `Read src/personal_mem/ontology.yaml` plus `mem_concepts(min_count=2)` for the live distribution).

- **`kind: review` (decision_review)** — surface the decision in the run report; do not auto-queue. The user inspects via `mem show <decision_id>` and decides whether to flip status or schedule a re-discussion.

- **`kind: drift` (theme_drift)** — list the silent themes; suggest flipping status to `dormant` via `/themes-resolve`. Do not write.

- **`kind: external` (external_tool_runner)** — payloads are user-shaped. If they look like queue items (have `url` / `title` fields), enqueue via `mem_queue`. Otherwise pass through to the run report.

- **`kind: enqueued` (rss_poll)** — the strategy already enqueued. Surface the row in the report (title, url, outlet/channel, queue_item_id); no further action.

- **`kind: summary` (rss_poll)** — per-source-type stats counters. Surface in the report.

- **`kind: mail_fetch_needed` (mail_poll)** — the strategy composed the Gmail query but cannot execute it from Python. Surface it in the report; the caller (`/newsletter`) is expected to execute. When `/discover` is invoked standalone (without `/newsletter` orchestrating), the skill reports the plan and stops — it does not call Gmail MCP itself, by design.

- **`kind: external` with `status: error`** (rss_poll / mail_poll) — surface the reason + hint verbatim. Common causes: `feedparser_missing`, `empty_allowlist`, `connector_not_implemented`.

### 3. Concept assignment rules (for new `kind: gap` queue items)

1. **Use ontology terms only.** Pull from `src/personal_mem/ontology.yaml`. Minimum 2, ideally 3-4.
2. **Genuinely-new terms** go in `proposed_concepts`, not `concepts`. `/mem-resolve-concepts` canonicalises them later.
3. **The `Gap:` line in the body must be a wikilink** to an ontology concept — that's the graph edge.

### 4. Run-audit note

Create one `discover-run` audit note per execution. Record:

- The strategies that ran and how many descriptors each emitted.
- For internal-state: the WebSearch queries you tried (concept_coverage only) and skip reasons for filtered hits.
- For external-trigger: per-source-type enqueue stats from `kind: summary` rows.
- Stalled decisions / drift themes surfaced for review.
- Queue items created (grouped by descriptor kind).

The note's `concepts` frontmatter is the union of any gap concepts; tags are `discover-run` + `audit`. No project arg — discover runs are cross-project.

### 5. Report

```
## Discovery — <date>

### Strategies run
- concept_coverage: 4 gaps found
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

Drop a file under `src/personal_mem/discover/strategies/` exposing a module-level `STRATEGY` instance, then add one `register()` call in `src/personal_mem/discover/strategies/__init__.py`. The strategy implements:

```python
class MyStrategy:
    name = "my_strategy"

    def run(self, vault, project, config):
        # Returns list[dict] — descriptors.
        return [...]
```

For external-trigger strategies, read `config.get("_runtime", {}).get("source_type")` to honor the CLI `--source-type` filter.

No edits to the CLI, the MCP surface, or this skill are needed. Mention the new strategy in the project's `discover_strategies` list in `sources.yaml` (if it should run by default) and `mem discover` will pick it up.

## Notes

- Per-project queues are first-class. `RESEARCH_FOCUS.md` is no longer assumed singular — projects with their own queue cadence can route directly to a per-project file.
- Concept-coverage caps queue items at 3-5 per run; volume is by design, not a bug. The matching `/research` cadence is `/loop 30m /research --queue --batch 3`.
- For `rss_poll` cron: `claude -p "/discover --strategy rss_poll --source-type news"` is the canonical headless invocation. Replaces the legacy `scripts/pull_news_feeds.py`.
