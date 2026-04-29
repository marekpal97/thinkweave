# personal_mem — Architecture

This document describes the shape of the codebase for contributors: the two layers the framework is split into, what a **source** is, how the three source capabilities (import / acquire / discover) fit together, and how everything ties through the ontology. If you're reading this before adding a new source type or writing a new skill, start here.

## Two layers

personal_mem splits cleanly into two layers with a one-way dependency.

```
┌────────────────────────────────────────────────────────────────────────┐
│ Claude Code layer                                                      │
│                                                                        │
│   commands/*.md          src/personal_mem/hooks/                       │
│   (procedural skills)    (SessionStart, Pre, Post, Stop)               │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │  (imports only)
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Knowledge layer                                                        │
│                                                                        │
│   vault.py       — note CRUD, frontmatter, wikilinks, layout routing   │
│   indexer.py     — SQLite FTS5 index, concept edges, hash dedup        │
│   search.py      — FTS, graph traversal, hybrid search                 │
│   concepts.py    — ontology loader, concept merging/tightening         │
│   hubs.py        — concept hub parse/diff/write                        │
│   landing.py     — DECISIONS / BACKLOG / STATE generators              │
│   sources/       — source-type registry + canonical frontmatter        │
│   mcp/server.py  — MCP tool surface (the `mem_*` tools)                │
│   cli.py         — `mem` CLI bridging skills and knowledge layer       │
└────────────────────────────────────────────────────────────────────────┘
```

The knowledge layer modules (`vault`, `indexer`, `search`, `concepts`, `hubs`, `landing`, `sources`) import **only** from each other and from `config` / `schemas`. None of them import from `hooks/` or `cli.py`. This is checked by reading; no linter enforces it. If you're adding to the knowledge layer and find yourself wanting to `import ... from personal_mem.hooks`, that's a signal you're mixing concerns — stop and rethink.

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

| Source type    | Import      | Acquire       | Discover      |
|----------------|-------------|---------------|---------------|
| `paper`        | `/research` | `/research --queue` | `/discover` |
| `repo`         | `/research` | `/research --queue` | `/discover` |
| `article`      | `/research` | `/research --queue` | `/discover` |
| `substack`     | —           | `/substack`   | —             |
| `conversation` | `mem import chatgpt` (CLI) | — | — |

Two distinct acquisition patterns coexist — on purpose, because the inputs differ:

- **Semantic queue** — `/research --queue` drains notes tagged `todo+research` from the vault itself. Claim-before-fetch via a `processing` tag; items that fail mid-run stay stuck in `processing` and are recoverable.
- **Disk inbox** — `/substack` drains files from `~/substack_inbox/` (outside the vault) and moves them to `_processed/<date>/` on success. Nothing mutates vault state until `mem_create` lands a source note.

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

Five steps. Nothing else should need to change.

### 1. Add a `SourceTypeSpec` entry

Edit `src/personal_mem/sources/registry.py`:

```python
"podcast": SourceTypeSpec(
    slug="podcast",
    bucket="podcasts",
    layout="folder",
    skills=("podcast",),
    description="Podcast episode transcripts. Ingested via /podcast.",
),
```

Pick the layout: `flat` (single-file summary, no raw companion), `folder` (slug subdir with raw alongside — the usual choice), or `author_folder` (show-level nesting for serial content).

### 2. Copy `_source_template.md` to `commands/{your-skill-name}.md`

The template is the universal skill scaffold with YAML frontmatter (`source_type`, `capabilities`, `tools`, `description`) plus three clearly marked capability sections (import / acquire / discover) and three always-on sections (ontology tie-in, frontmatter shape, reporting).

### 3. Fill in frontmatter + delete unused capability sections

Declare what your skill actually does. A skill can ship with just one capability — `/substack` is acquire-only. Delete the Import or Discover sections if your skill doesn't implement them.

### 4. Write the bespoke fetch/parse/interpret logic

Per capability section. This is where per-source variation lives and where the template explicitly warns against abstraction. Pattern-match from:

- `commands/research.md` — import + acquire via URL classification + WebFetch/git-clone/curl
- `commands/substack.md` — acquire via disk-inbox drain + multimodal figure interpretation
- `commands/discover.md` — discover via concept-coverage analysis + queue generation

### 5. Verify

```bash
mem sources show podcast        # registry entry visible
mem skill show podcast          # frontmatter parses
```

End-to-end smoke test: run the skill in Claude Code (via the Skill tool) on a real input and check `mem_search(type="source", query="...")` finds the new note.

## Running skills

Skills live in `commands/*.md` as plain markdown and run inside Claude Code via the Skill tool. Claude Code provides the full tool surface (`Read`, `Bash`, `WebFetch`, `WebSearch`, every `mem_*` tool via the personal_mem MCP server) and executes the procedure interactively.

For bulk, non-interactive work there are two targeted paths — neither requires a generic skill runner:

- **Concept hub backfill** — `mem hubs run --plan <path>` ships its own OpenAI Batches API path. It doesn't route through a skill file; it reads the plan JSON and calls the Batches API directly. `mem hubs link` is the analogous one-shot pass that rewrites flat `new` flags into `agrees`/`contradicts`/`extends` relationships across existing hub log entries (also via OpenAI Batches).
- **Autopilot** — `claude -p --model sonnet --dangerously-skip-permissions` invoked from cron gives headless skill execution with the full Claude Code tool surface.

If you need a new headless path that isn't either of these, add a CLI subcommand next to `mem hubs run` rather than reintroducing a generic runner.

## Themes — global narrative aggregators

A theme is a NoteType (`type: theme`, prefix `thm-`) that captures a temporal narrative — the kind of story external sources, news, and decisions cite in concert. Themes live at `vault/themes/`, **globally**, regardless of project. The `project:` frontmatter field is informational (primary stake), never a filing rule.

The shape mirrors a concept hub: an `## Essence` (slow-moving thesis), a `## Catalyst log` (append-only dated events using the same grammar as hub learning logs), and `## Open questions`. Decisions express themes via `implements: [thm-XXXX]` plus optional `implements_catalyst: YYYY-MM-DD`. The global `vault/THEMES.md` landing doc renders an Active table plus per-theme Mermaid temporal DAG (catalysts + decisions hung off the catalyst they implement). `/themes-resolve` is the periodic dedup/hygiene skill — same posture as `/mem-resolve-concepts`.

The temporal DAG is shared infrastructure (`src/personal_mem/temporal.py`): both concept hubs and themes parse the same `flag[ ref]` log grammar, build a `TemporalGraph`, and render Mermaid via the same primitive. Concept hubs gain an auto `## Evolution` section when the log has any non-`new` ref; themes inline a per-theme DAG inside `THEMES.md`.

## Workflow stager

`mem flow` is a thin declarative layer over the existing skill+cron pattern. Flows live in `vault/.mem/flows.yaml` as named sequences of `claude -p` invocations; cron entries become one-liners that invoke `mem flow run <name>` instead of re-encoding the order and flags.

The shipped templates (`scripts/example-flows.yaml`, `scripts/example-crontab`) document the dialect: `description`, optional `log` path, `on_error` (continue / abort), and a list of `stages` each with `run` (the literal `claude -p` argument) and optional `sleep` (seconds after the stage). No templating, no conditionals, no parallel branches — when those are needed, prefer a separate primitive over expanding `FlowSpec`.

## A note on the importers under `src/personal_mem/importers/`

These are **one-shot CLI importers**, not skills. They're called via `mem import <source> <path>` and handle bulk migration from external formats: ChatGPT conversation exports, claude-mem databases, Messenger self-exports, Hive Swarm session logs, plain text files. They live next to the knowledge layer because they speak directly to `VaultManager`, but they're not part of the capability model — a contributor adding a new source type should usually write a skill (procedural markdown) rather than a CLI importer (Python module). The importers exist because some source formats predate the skill model; new work should go through skills.
