# personal_mem — Architecture

## Document roles

| Doc | Audience | Purpose |
|---|---|---|
| `README.md` | New users | Pitch, quickstart, install. |
| `CLAUDE.md` | Agents in-session | Retrieval contract, lifecycles, operational rules. |
| `ARCHITECTURE.md` (this) | Contributors | Layer boundaries, source primitive, capability lanes, coherence mechanics. |
| `ARCHITECTURE_NOTES.md` | Contributors (deep-dive) | Worked examples, mechanism deep-dives, historical decisions spilled out of this doc. |

If you're answering a user question in-session, read `CLAUDE.md` first. If you're reading code or adding a new source type, you're in the right place.

## Two layers

personal_mem splits cleanly into two layers with a one-way dependency.

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Claude Code layer                                                       │
│   commands/*.md (skills) + surfaces/hooks/ (SessionStart, Pre, Post, Stop)│
└────────────────────────────────────┬────────────────────────────────────┘
                                     │  (imports only)
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Knowledge layer (src/personal_mem/)                                     │
│   core/         schemas, config, vault, indexer, embeddings, events     │
│   retrieval/    search, context, temporal                               │
│   synthesis/    hubs, concepts, themes, landing, judge                  │
│   sources/      registry, frontmatter, queue, intake, extractors        │
│   discover/     strategy registry (decision_review, rss_poll, mail_poll, …)│
│   operations/   pure functions consumed by both surfaces                │
│   surfaces/     cli/, mcp/, hooks/                                      │
│   importers/    one-shot CLI importers (chatgpt, claude_history, …)     │
└─────────────────────────────────────────────────────────────────────────┘
```

Dependency rule: `core/` imports nothing from the rest; `retrieval/` and `synthesis/` import only from `core/` and their neighbors; `operations/` may import any of the above but never from `surfaces/`. Surfaces are thin shells that delegate to `operations/`. If you find yourself wanting to import `surfaces.*` from `core/`, you're mixing concerns.

The Claude Code layer sits on top: hooks feed session events into the knowledge layer via the CLI; skills drive the knowledge layer via MCP tools. Both are clients of the knowledge API; neither is a peer.

## The source primitive

A **source** is a note of `type: source` representing external content — a paper, repo, article, newsletter post, conversation export. Every source type is declared once in `src/personal_mem/sources/registry.py` as a `SourceTypeSpec`:

```python
SourceTypeSpec(
    slug="paper",
    bucket="papers",
    layout="folder",            # flat | folder | author_folder
    aliases=("arxiv",),
    skills=("research", "discover"),
    temporal_grain="concept",   # event | concept | none
)
```

`VaultManager.create_note` reads the registry, normalises the incoming `source_type`, and dispatches on `spec.layout`:

- **`flat`** — single file at `bucket/<slug>.md`. Used by `conversation` (ChatGPT exports).
- **`folder`** — `bucket/<slug>/source.md` with companion files (`raw.md`, `snapshot.md`, `paper.pdf`, …) alongside. The usual choice.
- **`author_folder`** — `bucket/<author>/<slug>/source.md`. Used by substack so each publication's corpus clusters under one folder. Falls back to `folder` when `author` is missing.

`temporal_grain` decides whether the source type produces theme signals: `event` (news, substack, newsletter-events, podcast-events, youtube-events) triggers theme floating in `/dream`; `concept` (paper, repo, article, newsletter-concepts, podcast-concepts, youtube-concepts) routes to concept hubs; `none` (conversation) does neither.

**The registry is open-world** — a source with an unregistered `source_type` falls through to the `folder` layout with an empty bucket. Behaviour (drain, dedup, queue path) is closed-world — `/drain --source-type undeclared` errors. This asymmetry is intentional: experimentation is cheap, but production paths require a registry entry.

See [ARCHITECTURE_NOTES.md §"Canonical source frontmatter"](ARCHITECTURE_NOTES.md#canonical-source-frontmatter) for the full frontmatter table.

## Capability lanes

A source type can sit on up to four capability lanes. Each is optional; most types implement one or two.

```mermaid
flowchart LR
    classDef lane fill:#1f2937,stroke:#9ca3af,color:#f9fafb,rx:6,ry:6
    IM[IMPORT<br/>one-shot URL/file<br/>→ one source note]:::lane
    AC[ACQUIRE<br/>batch drain over<br/>queue or inbox]:::lane
    DI[DISCOVER<br/>strategy registry<br/>→ queue items / gap reports]:::lane
    SY[SYNTHESIS<br/>vault-wide aggregation<br/>→ hubs / themes / landing]:::lane
    DI --> AC
    IM --> SY
    AC --> SY
```

Each lane maps to skill files under `commands/`:

| Lane | Skills | Owns |
|---|---|---|
| import | `/research`, `/research-paper`, `/research-repo`, `/research-article`, `/news`, `/capture`, `/ingest-paper-file` | URL/file → one source note |
| acquire | `/drain`, `/substack`, `/newsletter`, `/youtube`, `/podcast` | Queue/inbox → many source notes |
| discover | `/discover` | Strategy registry: internal-state gap emitters (`decision_review`, `prompt_gap`) + external-trigger enqueuers (`rss_poll`, `mail_poll`, `external_tool_runner`) |
| synthesis | `/update-hubs`, `/themes-resolve`, `/dream`, `/mem-wrap`, `/mem-resolve-concepts` | Concept hubs, theme hubs, landing docs, ontology hygiene, session wrap |

The four lanes are verb-distinct on purpose. **Import** is one-shot (a URL or file the user hands you). **Acquire** is batch (a queue or inbox the user has been accumulating). **Discover** finds what's missing. **Synthesis** aggregates what's already in the vault — concept-hub backfill, theme dedup, landing-doc regeneration. None of these is a sub-mode of another; mixing them is what produced the historical naming drift the Phase 1 rename sweep (1.1) cleaned up.

Skills declare their lane via YAML frontmatter `capabilities: [...]`. `mem skill list` reads these headers.

### Source-type acquisition spine

The discover → drain spine is the only producer/consumer rail. Every source type lands on this shape:

```mermaid
flowchart LR
    classDef st fill:#1f2937,stroke:#60a5fa,color:#f9fafb,rx:6,ry:6
    classDef store fill:#0f172a,stroke:#a78bfa,color:#f9fafb,rx:6,ry:6
    D[/discover/]:::st -->|gap or enqueue| Q[queue JSONL<br/>vault/.mem/queues/]:::store
    Q -->|peek + claim| DR[/drain/]:::st
    DR -->|Path A: sequential Skill<br/>Path B: Task subagent fan-out| W[Worker]:::st
    W -->|mem_create| N[source note]:::store
    W -->|archive| QA[_processed/YYYY-MM-DD/]:::store
```

Path A (sequential) is for `paper`, `repo`, `article`. Path B (subagent fan-out) is for `news`, `youtube-*`, `newsletter-*`, `podcast-*` — high item-count source types where parallel writers pay off.

Some flows legitimately skip discover: `/substack` and `mem import {chatgpt|claude-history}` because the user (or an external export) has already done the discovery step; `/news <url>` and `/research <url>` because they're one-shot URL bypasses.

### Dream orchestrator (two-phase, mirrors /drain)

`/dream` is the second orchestrator in the repo. Same idiom as `/drain` (config-driven dispatch from a typed registry, scoped per-domain workers with strict JSON outcome contract, parallel fan-out, deterministic apply tail), specialised for the synthesis + composition + consumption lane instead of acquisition:

```mermaid
flowchart LR
    classDef st fill:#1f2937,stroke:#60a5fa,color:#f9fafb,rx:6,ry:6
    classDef store fill:#0f172a,stroke:#a78bfa,color:#f9fafb,rx:6,ry:6
    SC[mem dream scan]:::st --> P1[Phase 1<br/>5 synthesis workers]:::st
    P1 -->|plan fragments| AP[mem dream apply]:::st
    AP --> P2A[Phase 2 wave A<br/>wrap + judge]:::st
    P2A --> P2B[Phase 2 wave B<br/>digest]:::st
    AP --> ML[(maintenance.jsonl)]:::store
    P2B --> DG[(digests/YYYY-MM-DD.md)]:::store
```

- **Phase 1 (synthesis)** — 5 workers in parallel: `dream-{promotion,merge,theme,essence,priority}-worker`. Each emits a `plan_fragment` JSON outcome. Orchestrator merges, calls `mem dream apply` (one index rebuild, one `maintenance.jsonl` line).
- **Phase 2 (composition + consumption)** — 3 workers in dependency waves. Wave A in parallel: `dream-wrap-worker` (catch-up unwrapped sessions, subsumes the standalone `/mem-wrap` cron) + `dream-judge-worker` (drain rejudge queue, subsumes `/judge-prediction --drain`). Wave B after wave A: `dream-digest-worker` (compose `type: digest` note at `vault/projects/<p>/digests/YYYY-MM-DD.md`). Phase-2 workers write directly; they emit a `side_effects` list, not plan fragments.

**Extensibility seam.** `src/personal_mem/operations/dream_tasks.py::DreamTaskSpec` is the typed registry, structurally analogous to `sources/registry.py::SourceTypeSpec`. A new judgment, composition, or consumption domain plugs in via one `REGISTRY` entry (`surface_key, worker_name, plan_keys, has_signal, phase, depends_on`) plus one `.claude/agents/<worker>.md` file — no skill-text or orchestrator-code edits. Dependency edges (`depends_on`) let the orchestrator topologically sort the fan-out without per-domain branching.

**Operational vs epistemic separation (per dec-719e47e0 + n-d31cc330).** The dream report at `vault/reports/dream/<cycle_id>.md` is *operational* (what apply did this cycle). The digest at `vault/projects/<p>/digests/YYYY-MM-DD.md` is *epistemic* (what your knowledge gained today). Same orchestrator, separate workers, separate output files, separate prompt framings — no data-level conflation despite shared dispatch.

## Ontology as the joint vocabulary

The ontology is what glues the knowledge layer together. Every note — regardless of type or project — can carry a `concepts` frontmatter list. Notes that share ≥`concept_edge_threshold` concepts (default 1) auto-link in the graph. Concept hubs (`vault/concepts/topics/{concept}.md`) aggregate learning artifacts across the whole vault.

Loading is centralised in `src/personal_mem/synthesis/concepts.py:load_ontology` — a minimal YAML parser with no external dependencies. Skills read the file directly (no MCP round-trip) because it's small and changes rarely.

New terms a skill encounters go into `proposed_concepts`, not `concepts`. The gate is **server-enforced** — `mem_extract`, `mem_create`, and the importers all run incoming concept lists through the merged ontology and shunt non-matches to `proposed_concepts` automatically. Promotion to canonical requires `/mem-resolve-concepts` review (default threshold: `count ≥ 5`).

The ontology ties everything together because it's the only vocabulary shared across sources, decisions, sessions, notes, and concept hubs. A concept named in any of these places is the same concept. That's how a paper's finding can inform a decision on an unrelated project — they share a concept, so the graph connects them.

The shipped `ontology.yaml` is a minimal seed. The framework is opinion-free about which domains a vault grows; `ontology.example.yaml` shows one mature vault's shape but no domain hierarchy is privileged.

## Adding a new source type

The five-step pattern — registry entry → config defaults → skill file → optional research subskill → smoke test — is documented end-to-end in [ARCHITECTURE_NOTES.md §"Adding a new source type"](ARCHITECTURE_NOTES.md#adding-a-new-source-type) with a worked `podcast` example. Nothing else in the framework should need to change.

## Themes vs concept hubs

Both hubs share the same spine (`## Essence` + append-only `## Catalyst log` using the flag grammar `new` / `agrees` / `contradicts` / `extends`), implemented exactly once in `synthesis/hub.py`. They differ on identity, lifecycle, and citation direction:

|  | **Concept hub** | **Theme hub** |
|---|---|---|
| Identity | vocabulary term (e.g. `finance/regime`) | UUID (e.g. `thm-aaaa1111`) |
| Auto-update | yes (`/update-hubs` extracts) | no (mint + extend via `/dream`) |
| Lifecycle | none — concepts don't die | `active → dormant → resolved` (manual only) |
| Citation | `concepts: [...]` frontmatter | `relates_to: [thm-X]` frontmatter |
| Resolution skill | `/mem-resolve-concepts` | `/themes-resolve` |
| Storage | `vault/concepts/topics/{name}.md` | `vault/themes/{thm-X}-{slug}.md` |

The disambiguation test, registry mechanics (`themes.yaml`), and per-source-type theme floating (event-grain vs concept-grain) live in CLAUDE.md §3 "Theme" + §4 — that's the agent-facing rules-of-the-road. ARCHITECTURE.md only points to the structural file.

The temporal DAG renderer (Mermaid catalysts + decisions) is shared infrastructure in `retrieval/temporal.py`: both concept hubs and themes parse the same `flag[ ref]` log grammar, build a `TemporalGraph`, and render via the same primitive.

## Queue primitive

Per-source-type acquisition state lives in plain JSONL on disk — never inside the vault's note graph:

```
vault/.mem/queues/
  paper.jsonl                 # active queue (one JSON per line)
  _processed/2026-05-03/paper.jsonl   # archived items, status stamped
```

The `Queue` class (`sources/queue.py`) is the single API:

```python
q = Queue.for_source_type("paper", vault_root)
q.enqueue({"url": "...", "title": "..."})
item = q.dequeue()                  # FIFO; skips claimed
q.archive(item_id, status="done")   # → dated archive
conflict = q.dedup_check(item)      # active + N-day archive + SQLite URL check
```

Items get a UUID `id` and `enqueued_at` timestamp on enqueue if absent. Claims are written via tempfile + `os.replace` (atomic per-process); the design assumes a single user, not concurrent workers.

`dedup_check` consults the active queue + the last `SourceTypeSpec.dedup_lookback_days` of archive (7 for news, 30 for slower types) **plus** the SQLite indexer (URL already a `type: source` note). The indexer guard covers re-emits months later that the archive lookback misses. Keys come from `sources.<type>.dedup_keys` in `sources.yaml`.

The queues directory is excluded from the SQLite index — acquisition state is not knowledge.

## User configuration layout — `vault/config/`

All human-edit configuration files live under `vault/config/`. The hidden `vault/.mem/` directory is for runtime/derived state only (SQLite DBs, JSONL queues, batch buffers, logs).

```
vault/
├── config/                        # human-edit, top-level, visible
│   ├── PRIORITIES.yaml            # focus signals + intake registries (Phase 3.1)
│   ├── sources.yaml               # per-source-type behaviour overlay
│   ├── source_types.yaml          # registry overlay (optional)
│   ├── ontology.yaml              # canonical concept vocabulary
│   ├── concept_aliases.yaml       # near-dup mappings
│   ├── themes.yaml                # theme registry (code appends mints)
│   └── flows.yaml                 # cron flow definitions
├── .mem/                          # runtime state only
│   ├── embeddings.db, index.db*, queues/, buffer/, hubs_*, …
│   └── (no human-edit files)
└── …                              # vault content (concepts/, sources/, themes/, …)
```

Loaders use a backwards-compatible fallback (`vault/config/<filename>` → `vault/.mem/<filename>`) so pre-Phase-3.1 vaults keep working. Writes always commit forward to `vault/config/`. Legacy-location reads emit a one-per-session stderr deprecation warning.

### `PRIORITIES.yaml` — discover bias + intake registries

Two-section file owning the user-steerable surface that governs `/discover` mechanics and what external-trigger strategies enqueue:

```yaml
focus:
  active_projects: [...]            # foreground in landing docs
  watch_themes: [thm-...]           # surface in STATE.md + bias /discover
intake:
  news:               {outlets, drain_window_days}
  podcast_events:     {outlets}
  podcast_concepts:   {outlets}
  youtube_events:     {channels, lookback_days, drain_batch_max}
  youtube_concepts:   {channels, lookback_days, drain_batch_max}
  newsletter_events:  {senders, mail_query, label_overrides}
  newsletter_concepts:{senders, mail_query, label_overrides}
```

Loaded by `sources/priorities.py`. Read by `rss_poll` and `mail_poll` strategies (PRIORITIES.yaml wins; legacy paths fall back).

**Per-strategy thresholds + per-project strategy lists stay in `sources.yaml`** (operational tuning, not priorities — they're set once during onboarding and rarely revisited).

## User configuration — `sources.yaml`

`vault/config/sources.yaml` overlays per-vault defaults onto the shipped `DEFAULT_CONFIG` in `src/personal_mem/sources/config.py`. Four top-level sections, all optional:

```yaml
sources:                       # per-source-type overrides
  paper:
    queue: vault/.mem/queues/papers.jsonl
    dedup_keys: [arxiv_id, doi, url, title]
    url_patterns: [arxiv.org, openreview.net]
projects:                      # per-project knobs
  default: {discover_strategies: []}
  myresearch:
    discover_strategies: [decision_review]
    decision_review: {stale_days: 45}
landing_files:                 # filename overrides
  state: STATE.md
  decisions: DECISIONS.md
auto_todo_extraction: true
```

The optional `vault/config/source_types.yaml` overlay (loaded by `sources/registry.py`) registers new `SourceTypeSpec` entries at runtime — vault-side source-type extensions without forking the framework. See [ARCHITECTURE_NOTES.md §"sources.yaml vs source_types.yaml"](ARCHITECTURE_NOTES.md#sourcesyaml-vs-source_typesyaml) for the open/closed asymmetry.

`mem_sources_config` MCP exposes the merged dict to skills that don't want to re-parse the YAML themselves. The CLI exposes `mem sources list` / `mem sources show <slug>` for the registry view.

## Discovery strategies

`/discover` is the producer rail. It loads `projects.<name>.discover_strategies` from `sources.yaml`, dispatches to a registered strategy, and returns gap descriptors or queue items.

| Strategy | Flavor | What it does |
|---|---|---|
| `decision_review` | internal-state | Surfaces `proposed`/`accepted` decisions older than `stale_days` |
| `prompt_gap` | internal-state | Surfaces hyphenated-compound terms probed about that aren't in the ontology |
| `rss_poll` | external-trigger | Polls RSS feeds (news outlets, YouTube channels); directly enqueues |
| `mail_poll` | external-trigger | Composes a Gmail query → emits a plan `/newsletter` executes against Gmail MCP |
| `external_tool_runner` | external-trigger | Shells out to user-defined commands; reads JSONL stdout, merges into gap list |

Internal-state strategies describe a need (concept/decision metadata); external-trigger strategies write the queue. Forcing gap-emitters to enqueue would conflate "scan and report" with "decide what to do" — the latter legitimately lives in `/discover`.

Each strategy lives in its own file under `src/personal_mem/discover/strategies/` and implements the `Strategy` protocol (`_protocol.py`) — adding a new one is **one file plus one `register()` line** in `strategies/__init__.py`. This directory is the framework's growth axis post-launch.

## Decision lifecycle

A decision note has a `status` frontmatter field with four legal values:

```mermaid
stateDiagram-v2
    [*] --> proposed: mem_create / mem_extract<br/>(outcome: abandoned/partial,<br/>or outcome: committed with<br/>no matching session commit)
    proposed --> accepted: mem_extract finds<br/>commit matching file_paths
    accepted --> superseded: new decision declares<br/>supersedes: [dec-X]
    proposed --> superseded: same as above
    accepted --> deprecated: mem_update<br/>status=deprecated
    proposed --> deprecated: same as above
```

- `synthesis/judge.py` is **read-only** — emits a verdict (`kept`/`superseded`/`reverted`/`unknown`) from structural evidence (was the file committed? did tests pass? was it re-edited later?). Never writes back. Verdict-to-status writeback lives in `operations/decisions.mem_judge_and_writeback` (`kept→accepted`, `superseded→superseded`, `reverted→deprecated`, `unknown→no change`).
- The only auto-flip in the system is the `supersedes`-declared one above, where the writer made the relationship explicit.
- Decisions can carry a `predicted_outcome:` prose string with claim + manifestation pointer. The `/judge-prediction` skill (not an API call — the running session IS the judge) writes verdicts to `prediction_history`. See [ARCHITECTURE_NOTES.md §"RLVR substrate"](ARCHITECTURE_NOTES.md#rlvr-substrate--decision-context-capture) for the projection and export pipeline.

Note frontmatter is open-set — the indexer preserves unrecognized keys without modification, so downstream consumers can extend the schema (e.g. with `pipeline`, `run_id`, or other integration-specific keys) without forking the framework.

## Coherence — how the vault avoids duplication

Six distinct dedup mechanisms, each scoped to a different kind of overlap:

| Scenario | Mechanism | Where |
|---|---|---|
| Concept overlap (near-dupes) | `concept_aliases.yaml` + Levenshtein | `mem doctor`, `mem concepts drift` |
| Concept merge | rename across notes + delete stale hub | `mem concepts merge` |
| Source slug collision | filesystem check, auto-increments (`<slug>-1`, `-2`, …) | `VaultManager.create_note` |
| Note content dup | SHA-256 over body | indexer (skips on insert) |
| Theme dedup | manual via skill | `/themes-resolve` |
| Queue item dedup | `dedup_keys` from `sources.yaml` + indexer URL check | `Queue.dedup_check` |

Concept aliasing is the only mechanism that mutates content automatically — everything else either flags (`drift`, `doctor`), silently sidesteps (slug auto-increment, hash skip), or defers to a human-in-the-loop skill.

**Embeddings freshness.** Hybrid and similarity retrieval read from `<vault>/.mem/embeddings.db` (rebuildable from markdown). Without an external trigger nothing repopulates it as new content lands, so similarity silently degrades to FTS-only on recent content. The keep-warm contract is a cron line (`mem index --embed --only-new`) that re-embeds only the delta. `mem doctor` flags a stale DB (`embeddings.db` mtime > 7 days) when `OPENAI_API_KEY` is set.

## Invocation surface

The framework's *internal* contracts (layer dependencies, operations seam, retrieval modalities) are codified above. This section codifies the *external* contracts — every name an outside system (Claude Code, cron, another agent) can bind to. **These are public API. Renaming any of them breaks consumers we can't see.**

| Surface | Name | Stability | What breaks if it moves |
|---|---|---|---|
| Console script | `mem` | stable | every shell invocation, every cron job, every `claude -p` autopilot line |
| Console script | `mem-hook` | stable | every Claude Code session (registered in `.claude/settings.json` by `mem hooks install`) |
| Console script | `mem-mcp` | stable | every MCP-server config that addresses personal_mem |
| MCP tool name | `mem_search`, `mem_create`, `mem_read`, `mem_update`, `mem_link`, `mem_unlink`, `mem_context`, `mem_graph`, `mem_concepts`, `mem_extract`, `mem_judge`, `mem_landing`, `mem_enrich`, `mem_timeline`, `mem_project_snapshot`, `mem_queue`, `mem_sources_config`, `mem_prompts` | stable | every skill that calls the tool by name |
| Module entry | `python -m personal_mem.surfaces.mcp.server` | stable | rare — prefer the `mem-mcp` console script |
| Module entry | `python -m personal_mem.mcp.server` | back-compat shim | external configs that haven't migrated to `mem-mcp` yet |
| Hook subcommands | `mem-hook {session_start,user_prompt_submit,pre_tool_use,post_tool_use,stop}` | stable | every entry in `.claude/settings.json` written by `mem hooks install` |
| Skill files | `commands/<name>.md` filenames | stable | `/<name>` invocations and the `.claude/plugins/personal-mem/` symlinks |
| YAML keys | `sources.<slug>.{queue,research_skill,drain_strategy,dedup_keys,url_patterns,intake_folder}`, `projects.<name>.{discover_strategies,…}`, `landing_files.{state,backlog,decisions,themes,research_focus}`, `auto_todo_extraction` | stable | every user's `vault/config/sources.yaml` |

The rule: when restructuring internal modules, treat anything in this table as an immovable identifier. Internal layout (`personal_mem/foo/bar.py`) is private; the names here are the contract. If you must rename one, add a back-compat alias for one release before removing.

## Operations layer

`src/personal_mem/operations/` is the seam between surfaces (CLI, MCP) and the knowledge layer (`core/`, `retrieval/`, `synthesis/`, `sources/`). Note creation, concept queries, hub backfill, etc. are implemented exactly once here, then consumed by both surfaces.

```
surfaces/cli/  surfaces/mcp/         ← thin wrappers (5-10 LOC per handler)
       │             │
       └──────┬──────┘
              ▼
    operations/                      ← pure functions
      notes.py        create / read / update / link / unlink
      search.py       query_fts / query_similar / query_hybrid / query_context / query_prompts
      graph.py        walk(filter='source_lens'|'decisions_for_file'|'concept_walk'|…)
      concepts.py     list / tighten / merge / drift / source_counts / search
      decisions.py    list_by_file / judge_and_writeback
      queue.py        list_queues / peek / inspect / enqueue (auto-dedup)
      hubs_batch.py   run_hubs_batch — orchestrator over agent_client.batch_completions_sync
      _backfill_route.py  choose_route — picks inline (CC skill) vs batch (wrapper fan-out)
      dream.py        scan + apply for /dream synthesis + hygiene cycle
      wrap.py         /mem-wrap deterministic tail (prune → index → judge → landing → drift)
      rlvr_export.py  decision-context RLVR substrate export
              ▼
   core/, retrieval/, synthesis/, sources/   ← knowledge layer
```

Dependency rule: operations may import from `core/`, `retrieval/`, `synthesis/`, `sources/`, but never from `surfaces/`. CLI and MCP handlers import from operations, not from the knowledge layer directly. So `cmd_add` (CLI) and `mem_create` (MCP) both delegate to `operations.notes.create_note(cfg, …)` — the same call, the same code path.

Operations functions take a `Config` (or `VaultManager` / `Indexer`) plus parameters and return data. They don't `print` and they don't call `sys.exit`. Surfaces own input shape (argparse / JSON) and output shape (text / JSON).

## LLM provider abstraction — `core/agent_client.py` + `core/embedding_provider.py`

Pre-2026-06-06 personal_mem talked to three providers (OpenAI, Anthropic, Gemini) through three SDKs and four httpx call sites, with provider-specific Batches dances in `operations/hubs_batch.py` and `onboarding/enrich_batch.py`. After the API consolidation refactor (plan: `.claude/plans/go-back-to-the-scalable-firefly.md`):

```
            vault/config/api.yaml
            completion.{provider, model, max_tokens, batch_concurrency}
            embeddings.{provider, model}
            overrides.<op>.{provider, model, ...}
                            │
                            ▼ resolve_for_op() / embeddings_config()
            ┌───────────────┴───────────────┐
            ▼                               ▼
core/agent_client.py            core/embedding_provider.py
(AsyncOpenAI + per-provider     EmbeddingProvider protocol
 base_url for Anthropic /         OpenAI (httpx, default)
 Gemini OpenAI-compat)            SentenceTransformer (local)
                                  LiteLLM passthrough
get_completion / batch_completions     embed(texts) -> vectors
            │                               │
            ▼                               ▼
   Consumers (backfill ops)        Consumers (embedding paths)
   • operations/hubs_batch.py      • core/embeddings.py
   • onboarding/enrich_batch.py    • retrieval/search.py (mode='similar')
   • importers/chatgpt.py          • mem index --embed
   • enrich.py
   • surfaces/cli/_hubs_link.py
   (news-triage subagent stays on
    CC Task path, not the wrapper)

CARVE-OUT:
   • sources/extractors/gemini_extract.py — podcast audio Files API
     (direct google.genai; no chat-completion shape covers it)
```

Every wrapper call records spend exactly once via `core/spend.record_spend`. Dual-route surfaces (`--via {inline,batch}`) pick between the wrapper (batch) and a CC skill (inline) via `operations/_backfill_route.choose_route`.

## A note on the importers under `src/personal_mem/importers/`

These are **one-shot CLI importers**, not skills. They're called via `mem import <source> <path>` and handle bulk migration from external formats: ChatGPT exports, claude-history databases, Messenger self-exports, plain text files. They live next to the knowledge layer because they speak directly to `VaultManager`, but they're not part of the capability model — a contributor adding a new source type should usually write a skill (procedural markdown) rather than a CLI importer (Python module). The importers exist because some source formats predate the skill model; new work should go through skills.
