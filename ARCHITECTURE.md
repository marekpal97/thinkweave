# personal_mem — Architecture

## Document roles

personal_mem ships three top-level docs with sharp non-overlapping roles:

| Doc | Audience | Purpose |
|---|---|---|
| `README.md` | New users | Pitch, quickstart, install. |
| `CLAUDE.md` | Agents in-session | Retrieval contract, lifecycles, operational rules. ≤150 LOC. |
| `ARCHITECTURE.md` (this) | Contributors | Layer boundaries, source primitive, capability model, coherence mechanics. |

If you're an agent answering a user question, read `CLAUDE.md` first. If you're reading code or adding a new source type, you're in the right place.

This document describes the shape of the codebase for contributors: the two layers the framework is split into, what a **source** is, how the three source capabilities (import / acquire / discover) fit together, and how everything ties through the ontology. If you're reading this before adding a new source type or writing a new skill, start here.

## Two layers

personal_mem splits cleanly into two layers with a one-way dependency.

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Claude Code layer                                                       │
│                                                                         │
│   commands/*.md                  src/personal_mem/surfaces/hooks/       │
│   (procedural skills)            (SessionStart, Pre, Post, Stop)        │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │  (imports only)
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Knowledge layer (src/personal_mem/)                                     │
│                                                                         │
│   core/         schemas, config, vault, indexer, embeddings             │
│   retrieval/    search, context, temporal                               │
│   synthesis/    concept_hub, theme_hub, concepts, landing, judge        │
│   sources/      registry, frontmatter, intake, config (sources.yaml)    │
│   surfaces/cli/ `mem` CLI                                               │
│   surfaces/mcp/ MCP tool surface (the `mem_*` tools)                    │
│   surfaces/hooks/ Claude Code hooks (handler + install)                 │
│   importers/    one-shot CLI importers (chatgpt, claude_mem, …)         │
│   operations/   cross-cutting jobs (the seam between surfaces and core) │
│   flows.py, extract.py, enrich.py, prune.py                             │
└─────────────────────────────────────────────────────────────────────────┘
```

The dependency rule: `core` imports nothing from the rest; `retrieval` and `synthesis` import only from `core` (and their own neighbors); `surfaces/` is a thin shell that orchestrates the others. None of these import from each other's surfaces (no `core` reaching into `surfaces/cli`). If you find yourself wanting to import `surfaces.*` from `core/`, stop — you're mixing concerns.

The Claude Code layer sits on top: hooks feed session events into the knowledge layer via the CLI; skills drive the knowledge layer via MCP tools. Both are clients of the knowledge API; neither is a peer.

## The source primitive

A **source** is a note of `type: source` representing external content — a paper, a repo, an article, a newsletter post, a conversation export. It's one of the four note types (`note`, `session`, `decision`, `source`) and enjoys the same first-class treatment in the index, graph, and retrieval paths.

What makes a source distinct is **routing**: sources live under `vault/sources/` (global) or `vault/projects/{project}/sources/` (project-scoped), bucketed by their `source_type`. Every source type is declared in a single place — `src/personal_mem/sources/registry.py` — as a `SourceTypeSpec`:

```python
SourceTypeSpec(
    slug="paper",               # canonical source_type value
    bucket="papers",            # subfolder under sources/
    layout="folder",            # flat | folder | author_folder
    aliases=("arxiv",),         # legacy names folded into slug on write
    skills=("research", "discover"),
    description="Research papers (arXiv, PDFs).",
)
```

`VaultManager.create_note` reads the registry, normalises the incoming `source_type`, and dispatches on `spec.layout`. Three layouts exist:

- **`flat`** — single file at `bucket/<slug>.md`. Used by `conversation` (ChatGPT exports). No companion content.
- **`folder`** — `bucket/<slug>/source.md` with companion raw content (`raw.md`, `snapshot.md`, `paper.pdf`, …) alongside. The default for most types.
- **`author_folder`** — `bucket/<author>/<slug>/source.md`. Used by substack so each publication's corpus clusters under one folder. Falls back to `folder` layout when `author` is missing.

**The registry is open-world.** A source written with an unregistered `source_type` isn't an error — it falls through to the `folder` layout with an empty bucket (e.g. `sources/<slug>/source.md`). This keeps experimentation cheap: you can ingest a one-off before you've added a registry entry, then promote it later.

### Canonical source frontmatter

Every source note carries a canonical set of fields. The `build_source_frontmatter` helper in `src/personal_mem/sources/frontmatter.py` builds the dict with consistent ordering and names:

```python
from personal_mem.sources import build_source_frontmatter

fm = build_source_frontmatter(
    source_type="paper",
    title="Attention Is All You Need",
    url="https://arxiv.org/abs/1706.03762",
    authors=["Vaswani", "Shazeer", "Parmar"],
    arxiv_id="1706.03762",
    publication="NeurIPS 2017",
)
```

| Field               | Purpose                                                       |
|---------------------|---------------------------------------------------------------|
| `source_type`       | Canonical slug. Drives routing.                               |
| `title`             | Human-readable title. Also the filename slug source.          |
| `url`               | Canonical URL/URI. Empty string is legal for local content.   |
| `authors`           | List of strings. Use `[]` when unknown.                       |
| `concepts`          | Ontology terms (≥2). Feeds the knowledge graph.               |
| `proposed_concepts` | New vocabulary not yet in `ontology.yaml`. Reviewed later.    |
| `raw_path`          | Relative path to the raw companion file (`raw.md`, …).        |
| *…source-specific*  | Whatever your importer needs (`arxiv_id`, `publication`, …).  |

The helper doesn't enforce a schema — it's a convention, not a validator. Source-specific fields are merged via `**extra`.

## The three capabilities

A source type can expose up to three capabilities. Each is optional; most source types implement one or two.

```
┌────────────────┐     ┌─────────────────┐     ┌──────────────────┐
│   IMPORT       │     │   ACQUIRE       │     │   DISCOVER       │
│                │     │                 │     │                  │
│ one-shot:      │     │ batch drain:    │     │ gap analysis:    │
│ user gives you │     │ queue or disk   │     │ find what's      │
│ a URL/file,    │     │ inbox → many    │     │ missing → queue  │
│ you produce    │     │ source notes    │     │ new import work  │
│ one source     │     │                 │     │                  │
└────────────────┘     └─────────────────┘     └──────────────────┘
```

Each capability maps to a **skill file** under `commands/`. Skills are procedural markdown prompts — they're read by Claude Code (or the headless runner) and executed as plain natural-language instructions with tool access. There is no shared "skill framework" in code because the fetch/parse/interpret logic is genuinely different per source type; abstracting it would be premature.

Current mapping:

| Source type    | Import                   | Acquire                       | Discover    |
|----------------|--------------------------|-------------------------------|-------------|
| `paper`        | `/research → /research-paper`   | `/drain --source-type paper`   | `/discover` |
| `repo`         | `/research → /research-repo`    | `/drain --source-type repo`    | `/discover` |
| `article`      | `/research → /research-article` | `/drain --source-type article` | `/discover` |
| `substack`     | —                              | `/substack`                    | —           |
| `news`         | `/news <url>`                  | `/drain --source-type news` (RSS+cron → JSONL → Haiku triage → Sonnet writers) | — |
| `newsletter-events` | —                         | `/newsletter` (Gmail label → JSONL → Sonnet writers; event-grain) | — |
| `newsletter-concepts` | —                       | `/newsletter` (Gmail label → JSONL → Sonnet writers; concept-grain) | — |
| `conversation` | `mem import chatgpt` (CLI)     | —                              | —           |
| `claude-history` | —                            | `/onboard` (one-shot retroactive import; CLI: `mem drain --source claude-history`) | — |

Three acquisition patterns coexist — on purpose, because the inputs differ:

- **JSONL queue** — `/drain --source-type <slug>` drains items from `vault/.mem/queues/<slug>.jsonl`. The Queue primitive (see §"Queue primitive") supports claim/dedup/archive. Items live outside the note graph until they become source notes.
- **Disk inbox** — `/substack` drains files from `~/substack_inbox/` (outside the vault) and moves them to `_processed/<date>/` on success. Nothing mutates vault state until `mem_create` lands a source note.
- **Mail connector** — `/newsletter` queries the user's mailbox via a swappable connector (`gmail` today, `outlook`/`imap` slot in behind the same `senders` / `lookback_days` / `processed_label` contract). The canonical inbound filter is the per-type `senders:` allowlist in `sources.yaml` — addresses or bare domains, composed into `from:(...)` for the connector. Empty allowlist + empty `mail_query` is a deliberate halt (no whole-inbox fan-out). The skill enqueues each new message to JSONL with `embedded_body`, fans out `research-newsletter-worker` subagents, then applies `processed_label` on the mail server to every successfully-written message. Three re-read guards stack: the label (primary, server-side), queue `dedup_keys: [message_id, url]` (secondary, at enqueue), and a worker `mem_search(message_id)` (tertiary, at write).

Legacy `todo+research` notes are migrated into the matching JSONL queue by `mem doctor --migrate` (see `operations/migrations.py`).

Both patterns are correct; they're serving different input flows (knowledge-layer artifacts vs browser-clipped files). A new source type should pick whichever matches its input.

Skills declare their capabilities in their YAML frontmatter:

```yaml
---
name: research
source_type: [paper, repo, article]
capabilities: [import, acquire]
tools:
  - Read
  - WebFetch
  - Bash
  - mem_search
  - mem_create
  # ...
description: Ingest arxiv papers, GitHub repos, and web articles as source notes.
---
```

`mem skill list` reads these headers and shows every skill's type, capabilities, and description at a glance.

## Ontology as the joint vocabulary

The ontology is what glues the knowledge layer together. Every note — regardless of type or project — can carry a `concepts` frontmatter list. Notes that share ≥2 concepts auto-link in the graph. Concept hubs (`vault/concepts/topics/{concept}.md`) aggregate learning artifacts across the whole vault. And every source-ingestion skill starts with the same two lines:

```
Read src/personal_mem/ontology.yaml
mem_concepts(min_count=2)
```

Loading is centralised in `src/personal_mem/concepts.py:load_ontology` — a minimal YAML parser with no external dependencies. Skills read the file directly (no MCP round-trip) because it's small and changes rarely. `mem_concepts` then returns the live distribution from the index, so skills know both the canonical vocabulary (from `ontology.yaml`) and the in-use vocabulary (from the vault).

New terms a skill encounters go into `proposed_concepts`, not `concepts`, so `/mem-resolve-concepts` can canonicalise them in a later pass. This is the one place where drift is resolved: the ontology is the final authority on canonical terms, and promotion from `proposed` to canonical requires explicit review.

The ontology ties everything together because it's the only vocabulary shared across:

- Sources (what a paper is about)
- Decisions (what architectural area a decision touches)
- Sessions (what the session worked on)
- Notes (what a user note covers)
- Concept hubs (the synthesis layer per concept)

A concept named in any of these places is the same concept. That's how a paper's finding can inform a decision on an unrelated project — they share a concept, so the graph connects them. Nothing else in the system has this property.

## Adding a new source type

The five-step pattern (registry entry → config defaults → skill file → optional research subskill → smoke test) is documented end-to-end in §"Adding a new source type" further down, with a worked `podcast` example. Skim that section before editing anything; nothing else in the framework should need to change.

## Running skills

Skills live in `commands/*.md` as plain markdown and run inside Claude Code via the Skill tool. Claude Code provides the full tool surface (`Read`, `Bash`, `WebFetch`, `WebSearch`, every `mem_*` tool via the personal_mem MCP server) and executes the procedure interactively.

For bulk, non-interactive work there are two targeted paths — neither requires a generic skill runner:

- **Concept hub backfill** — `mem drain --target hubs --via batch` (alias `mem hubs run`) ships its own OpenAI Batches API path. It doesn't route through a skill file; it reads the plan JSON and calls the Batches API directly. `mem hubs link` is the analogous one-shot pass that rewrites flat `new` flags into `agrees`/`contradicts`/`extends` relationships across existing hub log entries (also via OpenAI Batches).
- **Autopilot** — `claude -p --model sonnet --dangerously-skip-permissions` invoked from cron gives headless skill execution with the full Claude Code tool surface.

If you need a new headless path that isn't either of these, add a CLI subcommand next to `mem hubs run` rather than reintroducing a generic runner.

## Prompt primitive

Every user prompt submitted in Claude Code is captured as a structured event in the active session's JSONL buffer. The `UserPromptSubmit` hook (registered by `mem hooks install`, handled in `surfaces/hooks/handler._handle_user_prompt_submit`) appends one line per submission:

```jsonl
{"ts": "2026-05-02T15:47:00+00:00", "type": "prompt", "text": "What does the indexer skip?", "session_id": "cc-uuid", "cwd": "/path"}
```

The schema is intentionally flat — same buffer file the Edit/Write/Bash post-tool events land in, just discriminated by `"type": "prompt"`. `extract.extract_prompts(events_jsonl)` lifts these rows into `Prompt` dataclasses (`ts`, `text`, `session_id`, `project`, `cwd`).

**Probe classification.** `extract.classify_probe(prompt, events)` is a conservative heuristic — it returns `True` only when the text reads like a question (ends with `?` or opens with a lead phrase like *what is*, *explain*, *how does*) **and** no `Edit`/`Write` event lands within the next 3 events of the buffer. False negatives over false positives — STATE.md's "Open Probes" section is more useful when sparse and accurate than when noisy.

**Where it's consumed:**

- `synthesis/landing._gather_prompt_probes` walks both archived `vault/projects/<project>/sessions/*/events.jsonl` and active `.mem/buffer/<session_uuid>.jsonl` files, applies `classify_probe`, and merges the result with `probe`-tagged notes for STATE.md's "Open Probes" section.
- `mem_prompts` MCP tool (`surfaces/mcp/tools/prompts.py` → `operations.search.query_prompts`) gives skills read-only access to prompts, project-scoped, with optional `since` / `limit` filters. `/discover` uses it to bias gap analysis toward what the user has actually been asking.

The legacy `probe` *tag* becomes a manual override only. The canonical signal is the prompt event; the tag stays load-bearing for back-compat (you can still hand-tag a note `probe` to surface it on STATE.md), but new code should reach for the prompt primitive.

**Auto-todo extraction.** A side-channel in the same module: `extract.extract_todos(text)` scans free-form text for `TODO: …` / `FIXME: …` / `we should …` / `next step: …` / `follow-up: …` patterns and returns `Todo` dataclasses. Wired into `mem_extract` (gated by `auto_todo_extraction` in `sources.yaml`, default `True`); each match becomes a note tagged `[todo, auto]`. `mem backlog` shows an `[auto]` marker so they're distinguishable from hand-curated todos; `mem backlog --hide-auto` filters them out. Promotion is by deleting the `auto` tag; dismissal is by deleting the note.

## Themes — global narrative aggregators

A theme is a NoteType (`type: theme`, prefix `thm-`) that captures a temporal narrative — the kind of story external sources, news, and decisions cite in concert. Themes live at `vault/themes/`, **globally**, regardless of project. The `project:` frontmatter field is informational (primary stake), never a filing rule.

The shape mirrors a concept hub: an `## Essence` (slow-moving thesis), a `## Catalyst log` (append-only dated events using the same grammar as concept-hub catalyst logs), and `## Open questions`. Decisions express themes via `implements: [thm-XXXX]` plus optional `implements_catalyst: YYYY-MM-DD`. The global `vault/THEMES.md` landing doc renders an Active table plus per-theme Mermaid temporal DAG (catalysts + decisions hung off the catalyst they implement). `/themes-resolve` is the periodic dedup/hygiene skill — same posture as `/mem-resolve-concepts`.

The temporal DAG is shared infrastructure (`src/personal_mem/retrieval/temporal.py`): both concept hubs and themes parse the same `flag[ ref]` log grammar, build a `TemporalGraph`, and render Mermaid via the same primitive. Concept hubs gain an auto `## Evolution` section when the log has any non-`new` ref; themes inline a per-theme DAG inside `THEMES.md`.

### Concept hub vs theme hub

Both hubs are the synthesis layer over the vault. They share a spine — `## Essence` plus an append-only `## Catalyst log` with the same flag grammar (`new` / `agrees` / `contradicts` / `extends`). That spine is implemented exactly once, in `synthesis/hub.py`, as the `Hub` + `HubLogEntry` dataclasses. Concept-hub and theme-hub modules are thin specialisations.

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

**No auto-theme-detection.** Themes are explicit acts of synthesis.

Historically concept hubs used `## Learning log`; the canonical heading on both surfaces is now `## Catalyst log`. `synthesis/hub.migrate_hub_log_heading` is the idempotent rename, wired into `mem index --full`.

## Queue primitive

Per-source-type acquisition state lives in plain JSONL on disk — never inside the vault's note graph. The `Queue` class (`src/personal_mem/sources/queue.py`) is the single API:

```python
from personal_mem.sources import Queue

q = Queue.for_source_type("paper", vault_root)
q.enqueue({"url": "https://arxiv.org/abs/...", "title": "..."})
item = q.dequeue()                                   # FIFO; skips claimed
items = q.peek(5)
q.claim(item_id)                                     # idempotent
q.archive(item_id, status="done")                    # move to dated archive
conflict = q.dedup_check(new_item, keys=[...])       # active + 30 days archive
```

Storage layout:

```
vault/.mem/queues/
  paper.jsonl                 # active queue (one JSON per line)
  repo.jsonl
  article.jsonl
  _processed/
    2026-05-03/
      paper.jsonl             # archived items, status stamped
```

Items get a UUID `id` and `enqueued_at` timestamp on enqueue if absent. Claims are written via tempfile + `os.replace` (atomic per-process); the design assumes a single user, not concurrent workers.

`dedup_check` consults the active queue plus the last 30 days of archive. `keys` are pulled from `sources.<type>.dedup_keys` in `sources.yaml`, so a paper queue dedups on `arxiv_id`, `doi`, `url`, `title` while an article queue dedups on `url`, `title`. String comparisons are case- and whitespace-insensitive.

The queues directory is excluded from the SQLite index — acquisition state is not knowledge.

## User configuration — `sources.yaml`

`vault/.mem/sources.yaml` overlays per-vault defaults. The shipped `DEFAULT_CONFIG` (in `src/personal_mem/sources/config.py`) is the source of truth; the user file overlays it key-by-key via `load_user_config(vault_root)`. Missing user file → defaults; malformed YAML → defaults (the loader is robust for `mem doctor` to surface errors later).

The schema has four top-level sections; everything is optional:

```yaml
# Per-source-type overrides. Keys mirror SourceTypeSpec slugs.
sources:
  paper:
    queue: vault/.mem/queues/papers.jsonl
    research_skill: research-paper        # commands/research/research-paper.md
    drain_strategy: anthropic_batch       # inline | anthropic_batch | openai_batch
    dedup_keys: [arxiv_id, doi, url, title]
    url_patterns: [arxiv.org, openreview.net]
    intake_folder: ~/papers_inbox
  repo:
    queue: vault/.mem/queues/repos.jsonl
    research_skill: research-repo
    dedup_keys: [github_url, slug]
    url_patterns: [github.com, gitlab.com]
  # ... article, substack, conversation, claude-history are also pre-shipped.

# Per-project knobs. Strategy lists drive `mem discover`; per-strategy
# settings (stale_days, min_mentions, external tools) live nested under
# the strategy name.
projects:
  default:
    discover_strategies: [concept_coverage]
  myresearch:
    discover_strategies: [concept_coverage, decision_review]
    decision_review:
      stale_days: 45
  external_signals:        # any project name; news triage, market signals, paper feeds, …
    discover_strategies: [external_tool_runner]
    external_tool_runner:
      tools:
        - command: ["./scripts/scrape_signals.py"]
        - command: "python -m mytools.gh_trending"
      timeout: 90

# Landing-doc filenames. Override these to use your own vocabulary
# (STATUS.md, ADR.md, …); the indexer, mem landing, and the SessionStart
# hook all read from this map.
landing_files:
  state: STATE.md
  backlog: BACKLOG.md
  decisions: DECISIONS.md
  themes: THEMES.md
  research_focus: RESEARCH_FOCUS.md

auto_todo_extraction: true
```

Where each section is read:

| Section | Consumers |
|---|---|
| `sources.<type>.queue` / `dedup_keys` / `drain_strategy` / `research_skill` | `mem queue` / `mem drain` / `mem_queue` MCP |
| `sources.<type>.url_patterns` | `commands/research.md` URL router |
| `projects.<name>.discover_strategies` | `mem discover` (CLI) and `/discover` skill |
| `projects.<name>.<strategy>.{stale_days, min_mentions, tools, …}` | individual discovery strategies |
| `landing_files.{state, backlog, decisions, themes, research_focus}` | `synthesis/landing.py`, the indexer, `retrieval/context.py` |

`mem_sources_config` MCP exposes the merged dict to skills that don't want to re-parse the YAML themselves. The CLI exposes `mem sources list` / `mem sources show <slug>` for the source-type registry view.

## Discovery strategies

`mem discover` is a thin CLI shell over a strategy registry: it loads `projects.<name>.discover_strategies` from `sources.yaml`, looks each name up in `personal_mem.discover.strategies`, calls `strategy.run(vault, project, config)`, and prints the merged JSON result. Strategies don't write to the vault directly — they emit gap descriptors that `/discover` (or a cron flow) translates into queue items, BACKLOG entries, or per-project review files.

Built-in strategies:

| Strategy | What it surfaces |
|---|---|
| `concept_coverage` | Load-bearing concepts whose source coverage falls below `min_sources` (default 2). Mirrors the original `/discover` default. |
| `decision_review` | `proposed`/`accepted` decisions older than `stale_days` (default 30). |
| `theme_drift` | `active` themes whose `## Catalyst log` has gone silent for `stale_days` (default 60). |
| `external_tool_runner` | Shells out to `projects.<name>.external_tool_runner.tools`; reads JSONL stdout, merges into the gap list. |

Each strategy lives in its own file under `src/personal_mem/discover/strategies/` and exposes a module-level `STRATEGY` instance plus a class with `name: str` and `run(vault, project, config) -> list[dict]`. Adding a new strategy is **one file plus one `register()` line** in `strategies/__init__.py` — no CLI, MCP, or skill edits required. This directory is the framework's growth axis post-launch: community extensions land here.

A strategy's config knobs are namespaced under the strategy name (e.g. `projects.myresearch.decision_review.stale_days`) so multiple projects can pull different parameters without colliding.

## Adding a new source type

Worked example: `podcast`. Five steps end-to-end.

### 1. Register the source type

Edit `src/personal_mem/sources/registry.py`:

```python
"podcast": SourceTypeSpec(
    slug="podcast",
    bucket="podcasts",
    layout="folder",        # episode-slug/source.md + companion files
    skills=("podcast",),
    description="Podcast episode transcripts. Ingested via /podcast.",
),
```

Pick a layout: `flat` (single-file summary, no raw companion), `folder` (slug subdir with raw alongside — the usual choice), or `author_folder` (show-level nesting for serial content).

### 2. Add per-type config to `sources.yaml`

In `src/personal_mem/sources/config.py:DEFAULT_CONFIG['sources']`:

```python
"podcast": {
    "queue": "vault/.mem/queues/podcasts.jsonl",
    "drain_strategy": "inline",
    "dedup_keys": ["url", "episode_id", "title"],
    "url_patterns": ["overcast.fm", "pca.st", "spotify.com/episode"],
},
```

Add a matching block to `vault_templates/.mem/sources.yaml` so new vaults ship with the override stub the user can edit.

### 3. Drop a skill at `commands/podcast.md`

Copy `commands/_source_template.md` and fill in:

```yaml
---
name: podcast
source_type: podcast
capabilities: [import, acquire]      # whichever your skill ships
tools: [Read, Bash, WebFetch, mem_create, mem_queue, ...]
description: Ingest podcast transcripts (Overcast, Pocket Casts, Spotify).
---
```

Implement the bespoke fetch + transcribe + summarise logic in the body — that's where per-source variation lives, and where the template warns against premature abstraction.

### 4. Add a research subskill (optional)

If `/research` should classify podcast URLs, add `commands/research/research-podcast.md` with the import logic. The router (`commands/research.md`) reads `url_patterns` from step 2 and dispatches automatically — no router edits.

### 5. Verify

```bash
mem sources show podcast        # registry entry visible
mem skill show podcast          # frontmatter parses
echo '{"url": "https://overcast.fm/+xyz", "title": "Test"}' \
  | mem queue add podcast --stdin
mem drain --source-type podcast --limit 1
```

If the smoke test creates a source note at `vault/sources/podcasts/<slug>/source.md`, the integration is live. No edits to `vault.py`, `queue.py`, the CLI, or the MCP server were needed at any step.

## Default source-type set

The framework ships with a deliberately small set: `paper`, `repo`, `article`, `substack`, `conversation`, `claude-history`. These cover the most common knowledge-worker inputs without baking in domain-specific assumptions. Every other source type — podcasts, YouTube, Messenger exports, RSS, email, Slack archives — is the user's to add via the five-step pattern above. The framework imposes no universe of sources; it just gives you the seam.

## Ontology — user-chosen, not framework-imposed

The shipped `src/personal_mem/ontology.yaml` is a minimal seed. The example file `ontology.example.yaml` shows what the original author's vault looks like after months of use — a mix of ML, AI tooling, finance, and SWE concepts — but **no domain hierarchy is privileged by the framework**. A vault that only ever imports cooking recipes will grow a `cuisine/`, `technique/`, `ingredient/` tree; a security-research vault will grow `cve/`, `exploit-class/`, `mitigation/`. The framework's only opinion is that concepts belong to a top-level domain (so the domain hub at `vault/concepts/<domain>.md` stays meaningful) and that new terms enter via `proposed_concepts` for canonicalisation through `/mem-resolve-concepts`.

## Acquisition triad — research / drain / discover

```
┌─────────────┐     ┌────────────┐     ┌───────────┐
│  /research  │     │  /drain    │     │ /discover │
│             │     │            │     │           │
│ classify URL│     │ drain queue│     │ find gaps │
│ → subskill  │ ←── │ for one    │ ←── │ → enqueue │
│ → mem_create│     │ source type│     │   leads   │
└─────────────┘     └────────────┘     └───────────┘
       │                  │                   │
       ▼                  ▼                   ▼
  source notes      source notes        queue items
```

- `/research` is now a thin URL classifier (~50 LOC). It dispatches to `commands/research/research-{paper,repo,article}.md` based on the URL pattern. Adding a new source type is one registry entry + one subskill — no router edits.
- `/drain` is the per-source-type queue worker. Single mode: `--source-type <slug>` drains `vault/.mem/queues/<slug>.jsonl` FIFO and dispatches each item to the matching `research-<slug>` skill. No other modes — synthesis and migration moved out (see below).
- `/discover` is project-aware: it reads `RESEARCH_FOCUS.md`, finds under-covered concepts, and enqueues new leads via `mem_queue`.

**Synthesis is a separate axis from acquisition.** Concept-hub backfill and theme dedup don't acquire anything — they aggregate what's already in the vault. Those are owned by `/update-hubs` (default = incremental; `--bulk inline|batch` = backfill) and `/themes-resolve`. Migration (one-shot retroactive imports such as `claude-history`) is also not acquisition; it runs as a CLI step inside `/onboard`, not as a skill mode of `/drain`. Keeping these axes verb-distinct is what restored the triad's clarity after the W3 split.

The CLI mirrors the triad: `mem queue` / `mem drain` / `mem update` are the headless surface (cron flows). `mem drain --target hubs --via {inline|batch}` and `mem drain --source claude-history` remain as headless plumbing — the skill-level surface for hub backfill is `/update-hubs --bulk`, and the skill-level surface for retroactive Claude import is `/onboard`. `mem hubs run` is a deprecation alias that prints a hint then dispatches to the same plumbing.

## Workflow stager

`mem flow` is a thin declarative layer over the existing skill+cron pattern. Flows live in `vault/.mem/flows.yaml` as named sequences of `claude -p` invocations; cron entries become one-liners that invoke `mem flow run <name>` instead of re-encoding the order and flags.

The shipped templates (`scripts/example-flows.yaml`, `scripts/example-crontab`) document the dialect: `description`, optional `log` path, `on_error` (continue / abort), and a list of `stages` each with `run` (the literal `claude -p` argument) and optional `sleep` (seconds after the stage). No templating, no conditionals, no parallel branches — when those are needed, prefer a separate primitive over expanding `FlowSpec`.

## Decision lifecycle

A decision note has a `status` frontmatter field with four legal values:

```
proposed ──▶ accepted ──▶ deprecated
                      └─▶ superseded
```

- **`proposed`** — under consideration; no commit yet, or `mem_extract` saw `outcome: abandoned`/`partial`.
- **`accepted`** — chosen. Auto-set by `mem_extract` when `outcome: committed`.
- **`deprecated`** — no longer applicable but not replaced. Set manually.
- **`superseded`** — replaced by a newer decision. Auto-set: when a new decision's frontmatter declares `supersedes: [dec-X]`, the target `dec-X.status` is flipped to `superseded` inline during the same `mem_extract` call. Single-purpose, no flag, no separate apply step. Implemented in the decision-creation loop in `mcp/server.py` (the `mem_extract` handler).

`judge.py` is **read-only** — it evaluates a decision against the structural evidence in the vault (was the file committed? did tests pass? was it re-edited later?) and emits a verdict (`kept` / `superseded` / `reverted` / `unknown`) plus a confidence score. It never writes back. The verdict is advisory: a caller (human in `/mem-wrap`, agent in a skill) decides what to do with it. This is deliberate — the only auto-flip in the system is the `supersedes`-declared one above, where the writer made the relationship explicit.

Note frontmatter is open-set — the indexer preserves unrecognized keys without modification, so downstream consumers can extend the schema (e.g. with `pipeline`, `run_id`, or other integration-specific keys) without forking the framework.

## RLVR substrate — decision-context capture

Each decision has, in principle, two reward signals attached: an *outcome* (did the code land and stay?) and a *prediction* (did the call play out as the author guessed?). The framework records the substrate for both passively — no model turns, no extra hooks beyond the ones already installed — so a downstream RL loop can join decision rows against the context they came out of. The MVP shipped 2026-05-14; the pipeline below is the steady-state shape.

```
SessionStart hook ─────────────────────▶ buffer/<sid>.jsonl  (one {"type": "startup", "ids": [...], "token_est": N})
MCP retrieval tool call                        │
  └─▶ PostToolUse hook ────────────────▶ buffer/<sid>.jsonl  (one {"type": "retrieval", "tool": "...", "returned_ids": [...]})
Stop hook  /  mem_extract                      │
  └─▶ archive_buffer ──────────────────▶ events.jsonl  +  retrieval_log.jsonl  (sibling files in the session folder)
mem index  /  Indexer.rebuild ──────────▶ context_served(session_id, note_id, source ∈ {startup, onthefly}, ts)
/mem-wrap  →  mem wrap-finalize  →  mem_judge_and_writeback
  └─▶ verdict written to decision frontmatter; new predictions initialized to prediction_match: pending
/judge-prediction (skill — live on supersedes, or cron drain)
  └─▶ prediction_history append + prediction_match denormalized to tail entry
mem rlvr export ────────────────────────▶ JSONL stream (one row per decision)
```

**Capture.** Two writers, one buffer. The `SessionStart` hook (`surfaces/hooks/handler._handle_session_start`) writes exactly one `type: startup` event per session containing the ids served in the SessionStart payload plus a `token_est`. The `PostToolUse` hook (registered with two matchers — `Write|Edit|Bash` for the action stream and `mcp__personal-mem__.*` for the MCP stream, see `surfaces/hooks/install.py`) writes one `type: retrieval` event per MCP retrieval tool call. The captured tool set is the closed frozenset `RETRIEVAL_TOOLS` in `operations/retrieval_log.py` — six tools: `mem_search`, `mem_context`, `mem_graph`, `mem_read`, `mem_timeline`, `mem_project_snapshot`. A future retrieval tool must opt in explicitly; mutation tools (`mem_create`, `mem_link`, …) are deliberately excluded.

**Buffer split.** `core/buffer.archive_buffer` is called at Stop and inside `mem_extract`. It partitions the per-session buffer JSONL by event `type`: action/prompt events → `events.jsonl`, `retrieval` + `startup` events → `retrieval_log.jsonl` (sibling files in the session folder). When a session has retrieval events but no action events, `events.jsonl` is touched empty — the orphan-detector in `prune.py` keys off file presence, so the empty file keeps the "events.jsonl missing → orphan session" rule intact.

**Projection.** `Indexer._rebuild_context_served` (called from `rebuild` and from `index_paths` when a session folder is touched) walks every session's `retrieval_log.jsonl` and projects rows into the `context_served(session_id, note_id, source, ts)` table. `source` is `startup` for ids that came in via the SessionStart event, `onthefly` for ids returned by an MCP retrieval call. A note served both ways resolves to `onthefly` — the on-the-fly retrieval is the stronger signal. The table is fully rebuildable from markdown alone (the SQLite DB stays a derived index).

**Citation extraction.** `operations/rlvr_export.assemble_row` walks decision body wikilinks via `extract_wikilinks` and intersects them against `context_served` for the decision's session. Output: `cited_onthefly_ids` (decision body cites a note that was retrieved on the fly) vs `cited_startup_only_ids` (decision body cites a note that arrived only via the SessionStart payload). Frontmatter relations (`derived_from`, `relates_to`) are *not* counted — only semantic body citations.

**`prediction_match`.** A decision may carry a `predicted_outcome:` — a single prose string with claim + manifestation pointer (e.g. *"After the transcript-first ladder ships, the next /drain on the 3 queued AI Engineer videos archives all 3 as accepted (0 gemini_refused). Check the youtube-events queue archive after the next drain run."*). Three companion frontmatter keys: `prediction_history:` (append-only list of `{match, judged_at, reason}` entries), `prediction_match:` (denormalized tail entry's match), `judged_at:` (denormalized tail entry's timestamp). Verdict enum is five values: `confirmed | contradicted | pending | unevaluable | stale`. `stale` means "was true at the time, no longer applies because the substrate moved on" — only emitted when the decision has been superseded or its pointer references something that no longer exists. The judge is the `/judge-prediction` Claude Code skill (`commands/judge-prediction.md`), not an API call — the running session IS the judge. Three invocation paths: (1) live, piggybacked on `/mem-wrap` when a successor decision supersedes — the composer writes the verdict via `mem_update` inline; (2) headless via cron: `claude -p "/judge-prediction --drain"` drains `.mem/rejudge_queue.jsonl` + finds stale `pending` rows (cap 20/run); (3) manual: `mem judge --rejudge <dec-id>` enqueues + shells to the skill. `mem_judge_and_writeback` (`operations/decisions.py`) handles only structural `verdict` (`kept`/`superseded`/`reverted`/`unknown`) and the pending initializer for new `predicted_outcome` rows — prediction semantics now live entirely in the skill's LLM turn, not Python.

**Export.** `mem rlvr export [--project] [--since] [--until] [--committed-only]` (CLI surface; implementation in `operations/rlvr_export.export_rows`) yields one JSONL row per decision joining decision frontmatter + body citations + the `context_served` projection. The locked row schema is documented in the module docstring — `{decision_id, project, session_id, created_at, prediction: {text, match}, outcome: {verdict, committed, blame_lines, days_alive}, context: {n_retrievals_onthefly, cited_onthefly_ids, cited_startup_only_ids, startup_token_est}}`. The exporter opens one `Indexer`, caches `retrieval_log.jsonl` reads by session, and never writes back — feeding RL pipelines is a read-only consumption point.

See CLAUDE.md §3 "Context-served (RLVR substrate)" and "Predicted-outcome" for the agent-facing summary; the symbols and storage layout above are the contributor view.

## Coherence — how the vault avoids duplication

Six distinct dedup mechanisms, each scoped to a different kind of overlap:

| Scenario | Mechanism | Where |
|---|---|---|
| Concept overlap (near-dupes) | `concept_aliases.yaml` + Levenshtein | `mem doctor`, `mem concepts drift` |
| Concept merge | rename across notes + delete stale hub | `mem concepts merge` |
| Source slug collision | filesystem check, auto-increments (`<slug>-1`, `<slug>-2`, …) | `VaultManager.create_note` |
| Note content dup | SHA-256 over body | indexer (skips on insert) |
| Theme dedup | manual via skill | `/themes-resolve` |
| Queue item dedup | `dedup_keys` from `sources.yaml` | `Queue.dedup_check` |

Concept aliasing is the only mechanism that mutates content automatically — everything else either flags (`drift`, `doctor`), silently sidesteps (slug auto-increment, hash skip), or defers to a human-in-the-loop skill (`merge`, `/themes-resolve`).

**Embeddings freshness.** Hybrid and similarity retrieval read from `<vault>/.mem/embeddings.db` (a derived index, rebuildable from markdown). Without an external trigger nothing repopulates it as new sessions / decisions / sources land, so similarity silently degrades to FTS-only on recent content. The "keep-warm" contract is a cron line (`mem index --embed --only-new`) that filters notes whose `updated_at` exceeds the most recent `embeddings.created_at` and re-embeds only that delta. `mem doctor` advisories on a stale DB (`embeddings.db` mtime > 7 days) when `OPENAI_API_KEY` is in the environment. See `scripts/example-crontab` for the canonical block.

## Invocation surface

The framework's *internal* contracts (layer dependencies, operations seam,
retrieval modalities) are codified above. This section codifies the
*external* contracts — every name an outside system (Claude Code, cron,
another agent) can bind to. **These are public API. Renaming any of them
breaks consumers we can't see.**

| Surface | Name | Stability | What breaks if it moves |
|---|---|---|---|
| Console script | `mem` | stable | every shell invocation, every cron job, every `claude -p` autopilot line |
| Console script | `mem-hook` | stable | every Claude Code session (registered in `.claude/settings.json` by `mem hooks install`) |
| Console script | `mem-mcp` | stable | every MCP-server config that addresses personal_mem |
| MCP tool name | `mem_search`, `mem_create`, `mem_read`, `mem_update`, `mem_link`, `mem_unlink`, `mem_context`, `mem_graph`, `mem_concepts`, `mem_extract`, `mem_judge`, `mem_landing`, `mem_enrich`, `mem_timeline`, `mem_project_snapshot`, `mem_queue`, `mem_sources_config`, `mem_prompts` | stable | every skill that calls the tool by name |
| MCP tool name | `mem_concepts_tighten`, `mem_concepts_merge`, `mem_concept_search`, `mem_concept_source_counts`, `mem_concepts_drift`, `mem_source_lens`, `mem_decisions_for_file` | deprecated (one release) | nothing yet — aliases dispatch to the canonical tools and log a warning to stderr |
| Module entry | `python -m personal_mem.surfaces.mcp.server` | stable | rare — prefer the `mem-mcp` console script |
| Module entry | `python -m personal_mem.mcp.server` | back-compat shim | external configs that haven't migrated to `mem-mcp` yet |
| Hook subcommands | `mem-hook {session_start,user_prompt_submit,pre_tool_use,post_tool_use,stop}` | stable | every entry in `.claude/settings.json` written by `mem hooks install` |
| Skill files | `commands/<name>.md` filenames | stable | `/<name>` invocations and the plugin in `.claude/plugins/personal-mem/` (which symlinks the same files) |
| YAML keys | `sources.<slug>.{queue,research_skill,drain_strategy,dedup_keys,url_patterns,intake_folder}`, `projects.<name>.{discover_strategies,…}`, `landing_files.{state,backlog,decisions,themes,research_focus}`, `auto_todo_extraction` | stable | every user's `vault/.mem/sources.yaml` |

The rule: when restructuring internal modules, treat anything in the table
above as an immovable identifier. Internal layout (`personal_mem/foo/bar.py`)
is private; the names in this table are the contract. If you must rename one,
add a back-compat alias for one release before removing.

The `python -m personal_mem.mcp.server` shim exists because the original
external configs predate the `mem-mcp` console script. After enough release
windows for users to migrate, the shim can be dropped — but the console-script
name itself never moves.

## Operations layer

`src/personal_mem/operations/` is the seam between surfaces (CLI, MCP) and
the knowledge layer (`core/`, `retrieval/`, `synthesis/`, `sources/`). Note
creation, concept queries, hub backfill, etc. are implemented exactly once
here, then consumed by both surfaces.

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
      hubs.py         plan / status / repair
      decisions.py    list_by_file / judge (read-only)
      queue.py        list_queues / peek / inspect / enqueue (auto-dedup)
      drain.py        run_hubs_batch — OpenAI Batches monolith for hub backfill
      migrations.py   registry of one-shot vault data migrations
              ▼
   core/, retrieval/, synthesis/, sources/   ← knowledge layer
```

The dependency rule: operations may import from `core/`, `retrieval/`,
`synthesis/`, `sources/`, but never from `surfaces/`. CLI and MCP handlers
import from operations, not from the knowledge layer directly. So `cmd_add`
(CLI) and `mem_create` (MCP) both delegate to
`operations.notes.create_note(cfg, …)` — the same call, the same code path.

Operations functions take a `Config` (or `VaultManager` / `Indexer`) plus
parameters and return data. They don't `print` and they don't call
`sys.exit`. Surfaces own input shape (argparse / JSON) and output shape
(text / JSON). The one exception is `drain.run_hubs_batch`, which inherits
process-exit semantics from the long-running OpenAI Batches loop the previous
in-CLI implementation used; lifting it cleanly is a follow-up.

## A note on the importers under `src/personal_mem/importers/`

These are **one-shot CLI importers**, not skills. They're called via `mem import <source> <path>` and handle bulk migration from external formats: ChatGPT conversation exports, claude-mem databases, Messenger self-exports, plain text files. They live next to the knowledge layer because they speak directly to `VaultManager`, but they're not part of the capability model — a contributor adding a new source type should usually write a skill (procedural markdown) rather than a CLI importer (Python module). The importers exist because some source formats predate the skill model; new work should go through skills.
