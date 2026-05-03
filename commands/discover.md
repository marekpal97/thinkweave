---
name: discover
owns_mechanic: research_discovery
source_type: [paper, repo, article]
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
description: Cross-project research discovery. Runs the configured strategy list (`projects.<name>.discover_strategies` in `sources.yaml`); strategies emit gap descriptors that this skill resolves into queue items.
---

# /discover — Strategy-driven gap analysis

`/discover` is now a **thin shell** over the discovery-strategy
registry. The framework ships four built-ins:

| Strategy | What it surfaces |
|---|---|
| `concept_coverage` | Load-bearing concepts with thin source coverage. |
| `decision_review` | `proposed`/`accepted` decisions stalled past N days. |
| `theme_drift` | `active` themes whose Catalyst log has gone silent. |
| `external_tool_runner` | User-provided scripts emitting JSONL queue items. |

Per-project strategy lists live in `vault/.mem/sources.yaml`:

```yaml
projects:
  default:
    discover_strategies: [concept_coverage]
  myresearch:
    discover_strategies: [concept_coverage, decision_review]
  external_signals:      # any project name; e.g. news triage, market signals, paper feeds, …
    discover_strategies: [external_tool_runner]
    external_tool_runner:
      tools:
        - command: ["./scripts/scrape_signals.py"]
```

## Steps

### 1. Fetch the gap list

```
mem discover --project <name>            # configured strategies
mem discover --strategy concept_coverage # one strategy
mem discover --strategy theme_drift      # explicit, no project
```

`mem discover` returns a JSON list — one entry per gap descriptor.
Each entry has a `strategy` field, a `kind` (`gap` / `review` /
`drift` / `external`), and strategy-specific keys (e.g. `concept`,
`source_count`, `decision_id`, `theme_id`).

If the list is empty, report "no gaps found this run" and exit.

### 2. Resolve gaps into queue items

Per gap descriptor:

- **`kind: gap` (concept_coverage)** — run a WebSearch for the
  concept, dedup against existing sources / queue items via
  `mem_concept_source_counts(concepts=[<name>])`, and create up to
  3 queue items via `mem_queue` (or `mem_create` with `tags=[todo,
  research]` for legacy backlog routing). Each new item carries
  `Gap: [[<concept>]]` in its body and the gap concept(s) in its
  `concepts` frontmatter (using ontology terms — load
  `Read src/personal_mem/ontology.yaml` plus
  `mem_concepts(min_count=2)` for the live distribution).

- **`kind: review` (decision_review)** — surface the decision in the
  run report; do not auto-queue. The user inspects via `mem show
  <decision_id>` and decides whether to flip status or schedule a
  re-discussion.

- **`kind: drift` (theme_drift)** — list the silent themes; suggest
  flipping status to `dormant` via `/themes-resolve`. Do not write.

- **`kind: external` (external_tool_runner)** — payloads are
  user-shaped. If they look like queue items (have `url` / `title`
  fields), enqueue via `mem_queue`. Otherwise pass through to the
  run report.

### 3. Concept assignment rules

When creating queue items in step 2, follow the same three rules
`/research` follows:

1. **Use ontology terms only.** Pull from
   `src/personal_mem/ontology.yaml`. Minimum 2, ideally 3-4.
2. **Genuinely-new terms** go in `proposed_concepts`, not `concepts`.
   `/mem-resolve-concepts` canonicalises them later.
3. **The `Gap:` line in the body must be a wikilink** to an ontology
   concept — that's the graph edge.

### 4. Run-audit note

Create one `discover-run` audit note per execution. Record:

- The strategies that ran and how many gaps each emitted.
- The WebSearch queries you tried (concept_coverage only).
- Skip reasons for filtered hits (already-ingested, already-queued).
- Queue items created, grouped by gap.

The note's `concepts` frontmatter is the union of gap concepts; tags
are `discover-run` + `audit`. No project arg — discover runs are
cross-project.

### 5. Report

```
## Discovery — <date>

### Strategies run
- concept_coverage: 4 gaps found
- decision_review: 2 stalled decisions

### Queue items created
| # | Title | URL | Strategy | Gap |
|---|-------|-----|----------|-----|

### Stalled decisions to revisit
- [[dec-XXXXXXXX]] — <title>

### Skipped
- N already in vault
- N already queued
- N excluded by filter
```

## Adding a new strategy

Drop a file under `src/personal_mem/discover/strategies/` exposing a
module-level `STRATEGY` instance, then add one `register()` call in
`src/personal_mem/discover/strategies/__init__.py`. The strategy
implements:

```python
class MyStrategy:
    name = "my_strategy"

    def run(self, vault, project, config):
        # Returns list[dict] — gap descriptors.
        return [...]
```

No edits to the CLI, the MCP surface, or this skill are needed.
Mention the new strategy in the project's
`discover_strategies` list in `sources.yaml` and `mem discover` will
pick it up automatically.

## Notes

- Per-project queues are first-class. `RESEARCH_FOCUS.md` is no
  longer assumed singular — projects with their own queue cadence
  can route directly to a per-project file.
- Concept-coverage caps queue items at 3-5 per run; volume is by
  design, not a bug. The matching `/research` cadence is
  `/loop 30m /research --queue --batch 3`.
