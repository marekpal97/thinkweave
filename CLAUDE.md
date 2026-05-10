# personal_mem — Agent guide

## 1. What this is for an agent

personal_mem is an Obsidian-native memory layer: markdown is the source of truth, SQLite is a derived index. As an agent you do not crawl the vault filesystem — you query through the `mem_*` MCP tools (or `mem` CLI). Sessions, decisions, sources, themes, and concept hubs are all first-class notes connected by a shared concept ontology. Retrieve through the retrieval contract (§2); preserve session knowledge via `/mem-wrap` before clearing context. Architecture lives in `ARCHITECTURE.md` — this file is for you.

## 2. Retrieval contract

Three modalities, plus compositions on top.

- **FTS** — `mem_search(query, mode='fts')`. Keyword/phrase. Cheap. Empty `query` returns recent matches honouring filters (list mode).
- **Similarity** — `mem_search(query, mode='similar')`. Concept-shaped query, no keyword. Soft-fails to FTS when embeddings unavailable.
- **Hybrid** — `mem_search(query, mode='hybrid')`. Unsure → RRF fusion (k=60).
- **Graph** — `mem_graph(id, depth, filter=…)`. Structural walk over typed edges. Filter dispatches the variant: `''` (default — walk from `id`), `'source_lens'` (was `mem_source_lens`), `'decisions_for_file'` (was `mem_decisions_for_file`), `'concept_walk'` (was `mem_concept_search`). The old standalone tools remain as deprecation aliases for one release.

Compositions:

- `mem_context(query, type=[…])` — FTS → similarity-via-concept → recency, deduped budget blob.
- `mem_project_snapshot(project)` — re-fetch the SessionStart context payload.
- `mem_timeline(project, days)` — chronological window of sessions + decisions.

All filters take `since` / `until` ISO dates; `mem_search` accepts `concepts=[…]` to combine text + concept; `mem_graph` accepts `note_type` / `project` projection.

| If you want to… | Use |
|---|---|
| Find X (keyword/phrase) | `mem_search` (`mode=fts`, fall back to `hybrid`) |
| Tell me about Y (budgeted blob) | `mem_context` |
| What touches Z (note id walk) | `mem_graph` |
| State of project P right now | `mem_project_snapshot` |
| What happened in window W | `mem_timeline` |

## 3. Lifecycles

**Session.** Hooks accumulate events + insights + commits + tests into a session note. Stop hook auto-extracts (thin: archive events as `events.jsonl`, mark `processed: true` + `auto_extracted: true`). `/mem-wrap` enriches with LLM insights and decisions via `mem_extract`. For non-code conversations (no hooks fired), `mem_extract` auto-creates a session note.

**Concept.** Notes carry `concepts: [...]` (≥2 required). Notes sharing ≥1 concept auto-link (configurable via `concept_edge_threshold`, default 1). `vault/concepts/topics/{concept}.md` is the synthesis hub: `## Essence` (≤500w mental model) + `## Learning log` (append-only, every entry cites `[[note-id]]` with a flag — `new`/`agrees`/`contradicts`/`extends`). Backfill via `mem hubs run` (OpenAI Batches); incremental via `/update-hubs`. `/mem-resolve-concepts` is the periodic hygiene pass (merge near-dupes, prune dead vocabulary, update ontology). The shipped `ontology.yaml` is a minimal seed — concept namespaces and the domain hierarchy are user-chosen; the framework imposes nothing. Concepts populate as the vault grows.

**Theme.** `type: theme`, prefix `thm-`, lifecycle `candidate → active → dormant → resolved` / `merged-into:thm-X`. Canonical themes live at `vault/themes/{thm-XXXX}-{slug}.md` regardless of project; pre-canonical candidates live at `vault/themes/_candidates/{cand-XXXX}-{slug}.md` and never carry a `thm-` ID. Three sections: `## Essence`, `## Catalyst log` (same grammar as concept-hub log), `## Open questions`. Decisions implementing a theme carry `implements: [thm-XXXX]`. `/themes-resolve` is the periodic hygiene pass — also handles candidate promotion (`--promote`) and stale-candidate archival.

*Source-coupled theme floating:* whether a source type auto-floats theme candidates is controlled by the `temporal_grain` field on `SourceTypeSpec`. Event-shaped types (`substack`, future `news`) get `temporal_grain='event'` — their post-ingest path runs a deterministic cluster check (≥3 recent sources sharing ≥2 concepts, no covering theme) and writes a candidate stub. Concept-shaped types (`paper`, `repo`, `article`) get `temporal_grain='concept'` — concept hubs handle them, no theme floating. Conversation-style intake gets `temporal_grain='none'` (no auto-floating). The disambiguation test below is preserved unchanged: candidate stubs propose themes, they don't redefine what a theme is.

**Prompt.** Captured by the `UserPromptSubmit` hook as a JSONL event (`{"type": "prompt", "text", "session_id", "ts", "cwd"}`) inside the active session's events buffer. `extract.extract_prompts` lifts them into `Prompt` dataclasses; `extract.classify_probe` applies a conservative heuristic (text ends with `?` / opens with a probe lead phrase, no follow-up Edit/Write within 3 events) to flag exploratory questions. Surfaced in STATE.md "Open Probes" and to `/discover` via the `mem_prompts` MCP tool. The legacy `probe` *tag* becomes a manual override only — the canonical signal is now the prompt event itself.

**Decision.** Four states forming the lifecycle `proposed → accepted → deprecated|superseded`.

| State | Trigger | Auto / manual | Git tie-in |
|---|---|---|---|
| `proposed` | `mem_create` or `mem_extract` with `outcome: abandoned\|partial` | Auto (default) | None |
| `accepted` | `mem_extract` over a session whose hooks captured commits (`outcome: committed`) | Auto | Yes — `commit_refs:` populated |
| `superseded` | New decision declares `supersedes: [dec-X]` in frontmatter, OR `mem_judge_and_writeback` maps a `superseded` verdict | Auto (frontmatter or judge writeback) | Inherited from triggering decision/judge run |
| `deprecated` | `mem_update(status="deprecated")` | Manual | None — deprecation is structural, not code-driven |

`mem_judge` is read-only — emits a verdict (`kept`/`superseded`/`reverted`/`unknown`) from structural evidence (commit/tests/re-edits). Never writes. The verdict-to-status writeback lives in `operations/decisions.py` (`mem_judge_and_writeback`): `kept→accepted`, `superseded→superseded`, `reverted→deprecated`, `unknown→no change`.

*Decisions without git tie-in, by design:* `/capture` or direct `mem_create` outside any session, non-code conversations (hooks never fired), `outcome: abandoned` (no code change expected), and decisions added retroactively to a session note's body. All four stay `proposed` with no `commit_refs:` and no judge verdict; promotion to `deprecated` remains manual.

**Source.** External content: `paper`, `repo`, `article`, `conversation`, `substack`, … Routed by `src/personal_mem/sources/registry.py` (`SourceTypeSpec`). Three layouts: `flat`, `folder`, `author_folder`. Per-source-type behaviour (queue path, drain strategy, dedup keys) is overridable in `vault/.mem/sources.yaml`.

## 4. Concepts vs tags vs themes

| Field | Role | Examples | Authority |
|---|---|---|---|
| `concepts` | Domain-specific technical vocabulary, drives graph edges | `write-ahead-log`, `fts5`, `recursive-cte` | `ontology.yaml` (canonical) + `concept_aliases.yaml` (aliases) |
| `tags` | Broad filtering categories | `debugging`, `todo`, `til`, `parked`, `probe` | `tag_vocabulary:` in `ontology.yaml` |
| `themes` | Global temporal narratives (`thm-XXXX`) | `risk-on-regime-2026`, `swe-refactor-arc` | `vault/themes/` |

Do not duplicate between `concepts` and `tags`. Run `mem doctor` to surface tag/concept overlap, unknown tags, dead vocabulary.

*Connectivity:* concepts drive graph edges (notes sharing ≥`concept_edge_threshold` concepts auto-link, default 1). Tags also produce `relates_to` edges but intentionally lightly — threshold 2 shared tags, with `todo`/`probe`/`parked`/`til` excluded and any tag covering >10% of notes capped out. Tags are *filter facets*, not graph substrate; if "tag connectivity feels light" — that's the design.

### Concept hub vs theme hub

Both hubs share a spine — `## Essence` (≤500w) plus an append-only `## Catalyst log` with the same flag grammar (`new` / `agrees` / `contradicts` / `extends`). The shared parse/render lives in `synthesis/hub.py`. They differ on identity, lifecycle, and how notes cite them.

|  | **Concept hub** | **Theme hub** |
|---|---|---|
| Identity | vocabulary term (e.g. `finance/regime`) | UUID (e.g. `thm-aaaa1111`) |
| Auto-update | yes (`/update-hubs` extracts from sessions) | no (authored only) |
| Lifecycle | none — concepts don't die | `active → dormant → resolved` / `merged-into:thm-X` |
| Citation direction | notes cite concept by `concepts: [...]` frontmatter | notes cite theme via `relates_to: [thm-X]` |
| Resolution skill | `/mem-resolve-concepts` | `/themes-resolve` |
| Storage | `vault/concepts/topics/{name}.md` | `vault/themes/{thm-X}-{slug}.md` |

**Disambiguation rule:**

- **Concept** = invariant vocabulary term identifying a *category*, *capability*, or *mechanism* (e.g. `finance/regime`, `mcp/server-config`, `retrieval/hybrid`). Ontology-grade. Doesn't have a story arc. Lives forever.
- **Theme** = narrative arc identifying an *unfolding event* (e.g. `thm-aaaa1111: AI capex unwind 2026`). Has beginning/middle/end. Always cites ≥1 concept.

**The disambiguation test for an LLM agent:**

- "X capability" / "X technique" / "X area of work" → concept
- "X event" / "X period" / "X transition" / "X campaign" → theme
- If the candidate name has a year, a quarter, or "rollout/unwind/launch/pivot" — it's a theme.
- If you cannot picture an `## Essence` paragraph that wouldn't change in 5 years — it's a theme.

**Auto-floated candidates, never auto-canonical themes.** Event-grain source types may produce candidate stubs at `vault/themes/_candidates/`, but candidates carry no `thm-` ID and don't show up in THEMES.md until `/themes-resolve --promote <cand-id>` mints one explicitly. The disambiguation test still gates promotion: a candidate that fails it (named like a capability/technique, no time horizon, no narrative arc) gets archived instead.

## 5. Skills

Generated from `commands/*.md` frontmatter. Re-run `mem skill list` to regenerate.

| Skill | owns_mechanic | source_type | capabilities | Purpose |
|---|---|---|---|---|
| `/mem-wrap` | session_extraction | — | — | Full LLM session extraction (insights, decisions, refresh DECISIONS+BACKLOG) |
| `/mem-resolve-concepts` | ontology_hygiene | — | — | Concept and ontology hygiene |
| `/themes-resolve` | theme_synthesis | — | — | Theme dedup, status changes, essence rewrites |
| `/ingest` | input_routing | * | import | Universal input router — URL / file / text / structured-id → dispatch to specialist skill. |
| `/capture` | text_capture | — | import | Inline-text ingestion (snippet, quote, fragment) → mem_create. |
| `/ingest-paper-file` | paper_file_ingest | paper | import | Local PDF paper → text extraction → mem_create as paper. |
| `/research` | url_routing | paper, repo, article | import, acquire | URL classifier; dispatches to research-paper/-repo/-article |
| `/drain` | queue_drain | — | acquire | Drain a per-source-type acquisition queue. |
| `/discover` | research_discovery | paper, repo, article | discover | Cross-project research gap analysis → queue items |
| `/substack` | substack_inbox | substack | acquire | Drain Substack disk inbox |
| `/update-hubs` | concept_hubs | — | — | Concept-hub sync — incremental (default) or bulk (`--bulk [inline\|batch]`). |
| `/onboard` | project_bootstrap | — | bootstrap | Per-project: register project in vault, install hooks, optional Claude Code seed, first landing docs. **Not** for vault init (`mem init`) or machine setup (`mem install`). |
| `/source-fit` | source_diagnosis | — | — | Read-only: classify a free-form input description against existing source types. Returns covered / adapt / scaffold. Vault-scope. |
| `/source-scaffold` | source_scaffold | — | — | Generative: create a new source type via vault overlay + machine-global skill file (`~/.claude/commands/<slug>.md`). Vault-scope. |

## 6. Operational rules

- **No filesystem crawls.** Never `find`/`ls`/`grep` the vault from a Bash tool. Use the SessionStart context (already in your conversation), MCP tools, or a single `Read` of a known file path.
- **One MCP call per question.** Pick the modality from §2; don't fan out unless the first call is genuinely insufficient.
- **Pre-`/clear`: run `/mem-wrap`.** There is no clear hook; this is the only way to preserve mid-session knowledge.
- **Concepts mandatory.** Every note created via `mem_extract` must carry ≥2 concepts. Load existing labels via `mem_concepts` before assigning. Prefer specific terms (`ml/deep-learning` over `deep-learning`).
- **Strict ontology gating.** Only ontology-listed terms may go in `concepts:`. Any new term goes in `proposed_concepts:`. The strict gate is server-enforced — `mem_extract`, `mem_create`, and the importers all run incoming concept lists through the merged ontology and shunt non-matches to `proposed_concepts:` automatically. Promotion (proposed → canonical) is `/mem-resolve-concepts`'s job, triggered when a proposed term reaches critical mass (default `count ≥ 5`). You don't pre-canonicalise; you just attach concepts and let the gate sort them.
- **Auto-todo only on request.** Never tag `todo` unless the user explicitly asks.

## 7. CLI reference (Bash)

Consolidations to keep in mind: `mem connect` is folded into
`mem index --materialize-links`; the `mem_concepts*` MCP tools are folded into
`mem_concepts(action=...)`; `mem_source_lens` + `mem_decisions_for_file` are
folded into `mem_graph(filter=...)`. Old names linger as deprecation aliases.

```
mem init                                    # initialize vault + .mem/sources.yaml
mem add --type {note|theme|...} "Title"     # create a note
mem index [--full] [--embed] [--materialize-links]   # rebuild SQLite index (+ wikilinks)
mem search "q" [--type X] [--concept Y]     # FTS / similarity / hybrid
mem graph <id>                              # local graph
mem stats                                   # vault health
mem doctor [--migrate]                      # coherence linter (+ optional data migrations)
mem backlog [--project X]                   # todo notes + active queue items
mem concepts {list|merge|hubs|drift|notes|prune}
mem hubs {status|plan|run|link|repair}      # concept-hub backfill (run = deprecation alias for `drain`)
mem drain --target hubs --via {inline|batch}  # batch path replaces `mem hubs run`
mem queue {list|inspect|peek}               # per-source-type acquisition queues
mem hooks {install|uninstall|status}
mem landing [--project X] [--doc all]       # regenerate DECISIONS/BACKLOG/STATE/THEMES
mem flow {list|show|run}                    # named workflow pipelines
mem skill {list|show <name>}                # inspect commands/*.md frontmatter
mem sources {list|show <slug>}              # inspect source-type registry
mem prune-orphans [--yes]                   # delete abandoned session folders (used by /mem-wrap)
mem update <note_id> [-f key=val ...]       # frontmatter / body-append for headless flows
mem enrich [--project X]                    # LLM concept enrichment (gpt-5-mini)
mem import {claude-mem|chatgpt|file|messenger}
mem intake {enumerate|archive}              # drop-folder helpers for /substack and friends
```

**Agents shouldn't run** `mem doctor`, `mem stats`, `mem flow`, `mem intake`,
`mem enrich`, `mem import`, `mem prune-orphans` directly — they belong in cron
flows or interactive admin. There is no MCP parity for these subcommands.

### MCP tool surface

The MCP server exposes 18 tools:

`mem_search`, `mem_create`, `mem_read`, `mem_update`, `mem_link`, `mem_unlink`,
`mem_context`, `mem_graph` (filter-dispatched), `mem_concepts` (action-dispatched),
`mem_extract`, `mem_judge`, `mem_landing`, `mem_enrich`, `mem_timeline`,
`mem_project_snapshot`, `mem_queue`, `mem_sources_config`, `mem_prompts`.

7 deprecation aliases (one release): `mem_concepts_tighten`, `mem_concepts_merge`,
`mem_concept_search`, `mem_concept_source_counts`, `mem_concepts_drift`,
`mem_source_lens`, `mem_decisions_for_file`. Calls work but log a deprecation
warning to stderr.

## Environment

- `PERSONAL_MEM_VAULT` — vault root (default `~/vault`)
- `PERSONAL_MEM_PROJECT` — default project name
- `OPENAI_API_KEY` — required by `mem enrich`, ChatGPT importer, embeddings, `mem hubs run`

After upgrading personal_mem, re-run `mem hooks install` to pick up newly-added hooks (e.g. SessionStart).
