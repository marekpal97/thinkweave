# personal_mem — Agent guide

## 1. What this is for an agent

personal_mem is an Obsidian-native memory layer: markdown is the source of truth, SQLite is a derived index. As an agent you do not crawl the vault filesystem — you query through the `mem_*` MCP tools (or `mem` CLI). Sessions, decisions, sources, themes, and concept hubs are all first-class notes connected by a shared concept ontology. Retrieve through the retrieval contract (§2); preserve session knowledge via `/mem-wrap` before clearing context. Architecture lives in `ARCHITECTURE.md` — this file is for you.

## 2. Retrieval contract

Three modalities, plus compositions on top.

- **FTS** — `mem_search(query, mode='fts')`. Keyword/phrase. Cheap. Empty `query` returns recent matches honouring filters (list mode).
- **Similarity** — `mem_search(query, mode='similar')`. Concept-shaped query, no keyword. Soft-fails to FTS when embeddings unavailable.
- **Hybrid** — `mem_search(query, mode='hybrid')`. Unsure → RRF fusion (k=60).
- **Graph** — `mem_graph(id, depth, filter=…)`. Structural walk over typed edges. Filter is forward-looking (`source_lens` | `decisions_for_file` | `concept_walk`); during the transition the dedicated tools `mem_source_lens`, `mem_decisions_for_file`, `mem_concept_search` still exist.

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

**Concept.** Notes carry `concepts: [...]` (≥2 required). Notes sharing 2+ concepts auto-link. `vault/concepts/topics/{concept}.md` is the synthesis hub: `## Essence` (≤500w mental model) + `## Learning log` (append-only, every entry cites `[[note-id]]` with a flag — `new`/`agrees`/`contradicts`/`extends`). Backfill via `mem hubs run` (OpenAI Batches); incremental via `/update-hubs`. `/mem-resolve-concepts` is the periodic hygiene pass (merge near-dupes, prune dead vocabulary, update ontology).

**Theme.** `type: theme`, prefix `thm-`, lifecycle `active`/`dormant`/`resolved`/`merged-into:thm-X`. Lives at `vault/themes/{thm-XXXX}-{slug}.md` regardless of project. Three sections: `## Essence`, `## Catalyst log` (same grammar as concept-hub log), `## Open questions`. Decisions implementing a theme carry `implements: [thm-XXXX]`. `/themes-resolve` is the periodic hygiene pass.

**Decision.** Four states: `proposed → accepted → deprecated|superseded`.

- `proposed` — under consideration, no commit / `outcome: abandoned|partial`.
- `accepted` — auto-set by `mem_extract` when `outcome: committed`.
- `deprecated` — no longer applicable but not replaced. Manual.
- `superseded` — auto-set inline when a new decision declares `supersedes: [dec-X]` (single-purpose, no flag, no apply step).

`mem_judge` is read-only — emits a verdict (kept/superseded/reverted/unknown) from structural evidence (commit/tests/re-edits). Never writes.

**Source.** External content: `paper`, `repo`, `article`, `conversation`, `substack`, … Routed by `src/personal_mem/sources/registry.py` (`SourceTypeSpec`). Three layouts: `flat`, `folder`, `author_folder`. Per-source-type behaviour (queue path, drain strategy, dedup keys) is overridable in `vault/.mem/sources.yaml`.

## 4. Concepts vs tags vs themes

| Field | Role | Examples | Authority |
|---|---|---|---|
| `concepts` | Domain-specific technical vocabulary, drives graph edges | `write-ahead-log`, `fts5`, `recursive-cte` | `ontology.yaml` (canonical) + `concept_aliases.yaml` (aliases) |
| `tags` | Broad filtering categories | `debugging`, `todo`, `til`, `parked`, `probe` | `tag_vocabulary:` in `ontology.yaml` |
| `themes` | Global temporal narratives (`thm-XXXX`) | `risk-on-regime-2026`, `swe-refactor-arc` | `vault/themes/` |

Do not duplicate between `concepts` and `tags`. Run `mem doctor` to surface tag/concept overlap, unknown tags, dead vocabulary. *(Phase 3 B will expand this table with the unified hub model.)*

## 5. Skills

*(Phase 4 C5 will populate this table from `mem skill list`.)*

| Skill | Purpose |
|---|---|
| `/mem-wrap` | Full LLM session extraction (insights, decisions, refresh DECISIONS+BACKLOG) |
| `/mem-resolve-concepts` | Concept and ontology hygiene |
| `/themes-resolve` | Theme dedup, status changes, essence rewrites |
| `/research` | Ingest papers/repos/articles (URL or queue drain) |
| `/discover` | Cross-project research gap analysis → queue items |
| `/substack` | Drain Substack disk inbox |
| `/update-hubs` | Daily incremental concept-hub sync |

## 6. Operational rules

- **No filesystem crawls.** Never `find`/`ls`/`grep` the vault from a Bash tool. Use the SessionStart context (already in your conversation), MCP tools, or a single `Read` of a known file path.
- **One MCP call per question.** Pick the modality from §2; don't fan out unless the first call is genuinely insufficient.
- **Pre-`/clear`: run `/mem-wrap`.** There is no clear hook; this is the only way to preserve mid-session knowledge.
- **Concepts mandatory.** Every note created via `mem_extract` must carry ≥2 concepts. Load existing labels via `mem_concepts` before assigning. Prefer specific terms (`ml/deep-learning` over `deep-learning`). New terms go to `proposed_concepts`, never `concepts`.
- **Auto-todo only on request.** Never tag `todo` unless the user explicitly asks.

## 7. CLI reference (Bash)

```
mem init                                    # initialize vault + .mem/sources.yaml
mem add --type {note|theme|...} "Title"     # create a note
mem index [--full] [--embed]                # rebuild SQLite index
mem search "q" [--type X] [--concept Y]     # FTS / similarity / hybrid
mem graph <id>                              # local graph
mem stats                                   # vault health
mem doctor                                  # coherence linter
mem backlog [--project X]                   # todo notes
mem concepts {list|merge|hubs|drift|tighten}
mem hubs {status|plan|run|link|repair}      # concept-hub backfill
mem hooks {install|uninstall}
mem landing [--project X] [--doc all]       # regenerate DECISIONS/BACKLOG/STATE/THEMES
mem flow {list|show|run}                    # named workflow pipelines
```

## Environment

- `PERSONAL_MEM_VAULT` — vault root (default `~/vault`)
- `PERSONAL_MEM_PROJECT` — default project name
- `OPENAI_API_KEY` — required by `mem enrich`, ChatGPT importer, embeddings, `mem hubs run`

After upgrading personal_mem, re-run `mem hooks install` to pick up newly-added hooks (e.g. SessionStart).
