# personal_mem

Obsidian-native universal memory layer. Markdown is the source of truth; SQLite is a derived, rebuildable index.

## Commands

- `uv run pytest` — run tests (394 tests)
- `uv run mem init` — initialize a new vault
- `uv run mem add --type note --project X --tags "a,b" "Title"` — create a note
- `uv run mem index [--full] [--embed]` — rebuild SQLite index
- `uv run mem search "query"` — FTS search
- `uv run mem graph <id>` — show local graph
- `uv run mem stats` — vault health
- `uv run mem backlog [--project X]` — list notes tagged `todo`, grouped by project
- `uv run mem concepts list [--prefix X] [--min-count N]` — list concepts with counts
- `uv run mem concepts tighten` — find near-duplicate concepts
- `uv run mem concepts merge <from> <to>` — rename concept across all notes + update aliases
- `uv run mem hubs status [--concept X]` — per-concept processed state (cited vs unprocessed)
- `uv run mem hubs plan [--concept X] [--project Y] [--limit-notes N] [--limit-concepts N]` — walk vault, write JSON backfill plan
- `uv run mem hubs run --plan <path> [--dry-run]` — execute backfill via OpenAI SDK + Batches API (gpt-5-mini)
- `uv run mem hooks install` — install Claude Code hooks (Pre/Post/Stop)
- `uv run mem landing [--project X] [--doc decisions|backlog|state|all]` — generate project landing documents
- `uv run mem restructure [--dry-run]` — consolidate notes/decisions into session folders

## Session Context & Extraction

**Session start (auto)**: The SessionStart hook injects ~7–10k tokens of structured project context at the start of every Claude Code session — recent wrapped sessions, STATE.md, BACKLOG, recent decisions, concept histogram, and the MCP tool manifest. No action needed — if the hook is installed (`mem hooks install`), Claude wakes up already oriented. To re-fetch mid-session, call `mem_project_snapshot(project=<name>)`.

**Retrieval protocol** — when asked about prior sessions, recent work, vault contents, or "what happened last time":

1. **SessionStart context first** (cost: zero). The hook output already in your context contains recent sessions, decisions, backlog, STATE.md, and concepts. READ IT before doing anything else.
2. **One MCP call** if you need a specific note or search: `mem_read(id)`, `mem_search(query)`, `mem_timeline()`, `mem_context()`. These query the SQLite index — fast and precise.
3. **One file Read** for codebase files you know the path to (e.g. `commands/research.md`).
4. **NEVER** spawn Explore agents to crawl the vault filesystem with `find`/`ls`/`grep` on `/mnt/c/Users/marek/vault/`. The entire memory system exists to make this unnecessary. If you catch yourself reaching for filesystem exploration of the vault, stop — the answer is in steps 1-3.

**Before `/clear` or `/exit`, always run `/mem-wrap`** to extract session knowledge. Three extraction paths:

1. **Auto (Stop hook)**: Fires at exit/Ctrl+C. Performs thin extraction — builds summary from metadata (files, commits, tests), strips event logs, archives buffer as `events.jsonl`, marks `processed: true` + `auto_extracted: true`. No LLM insights.
2. **Manual (`/mem-wrap`)**: Full LLM extraction — curated insights, decisions with rationale, rich summaries. Can enrich auto-extracted sessions (`force=true`).
3. **Pre-clear (CLAUDE.md rule)**: Before `/clear`, always run `/mem-wrap` first. There is no clear hook — this is the only way to preserve context before clearing.

After upgrading personal_mem, re-run `mem hooks install` to pick up newly-added hooks (e.g. SessionStart).

## Architecture

4 note types: `note` (default), `session` (auto from hooks), `decision` (lifecycle), `source` (external).

**Vault directory structure**:
```
vault/projects/{project}/
  DECISIONS.md                     # landing: decision ledger + DAG (auto-generated)
  BACKLOG.md                       # landing: open items + stalled proposals (auto-generated)
  STATE.md                         # landing: state of play for humans (LLM-assisted)
  sessions/
    {session-id}-{date}/           # each session gets its own folder
      session.md                   # clean summary (events stripped post-extraction)
      events.jsonl                 # archived raw event log
      derived-note.md              # notes extracted from this session
      derived-decision.md          # decisions extracted from this session
    misc/                          # catch-all for standalone notes/decisions
      standalone-note.md
      standalone-decision.md
  sources/                         # external sources
```

All notes and decisions live in session folders. Derived content goes in its parent session's folder; standalone content (created via `mem add` or `mem_create` without session context) goes to `sessions/misc/`. Pass `--session <id>` (CLI) or `session_id` (MCP) to target a specific session.

**Tags vs concepts** — two distinct fields with different roles:
- `tags`: broad categories for filtering and organization (e.g. `debugging`, `performance`, `todo`, `til`, `refactor`). Searchable via FTS and `--tags` filter.
- `concepts`: domain-specific technical vocabulary for knowledge graph edges (e.g. `write-ahead-log`, `fts5`, `recursive-cte`). Notes sharing 2+ concepts auto-link. Managed via `mem concepts` CLI and aliases file (`vault/.mem/concept_aliases.yaml`).

Do not duplicate between them — a term belongs in one or the other.

**Concept assignment is mandatory** — every note and decision created via `mem_extract` MUST include a `concepts` array with minimum 2 concepts. Notes with <2 concepts cannot auto-link and will cluster as isolated islands in Obsidian. Before assigning concepts, call `mem_concepts` to load existing labels. Prefer specific domain terms over generic ones; use domain-qualified paths when they exist (`ml/deep-learning` not `deep-learning`).

The shipped `src/personal_mem/ontology.yaml` is a minimal seed — it grows as you use the vault via `/mem-resolve-concepts`. For a fuller reference showing how the ontology looks after months of use across ML, AI tooling, finance, and SWE, see `src/personal_mem/ontology.example.yaml`.

**Session lifecycle**: hooks accumulate events (with diff context) + `★ Insight` blocks + git commits/test results into session notes → Stop hook auto-extracts (thin summary, archive events) → `/mem-wrap` enriches with LLM insights and decisions via `mem_extract` → session folder contains clean summary + derived artifacts. For non-code conversations (no hooks fired), `mem_extract` auto-creates a session note.

**Decision lifecycle**: `proposed` → `accepted` → `deprecated`/`superseded`. Decisions capture both successful and abandoned approaches. `mem_judge` evaluates decisions against evidence (committed? tested? re-edited?) and assigns verdicts (kept/superseded/reverted/unknown).

**Follow-ups**: Any note can be tagged `todo` to mark it for later. `mem backlog` lists all `todo`-tagged notes. Never auto-add `todo` — only when the user explicitly asks.

**Tag conventions**: `todo` (open work item), `parked` (deliberately deferred, body explains why), `probe` (user question + discovery — learning artifact). These are regular tags on type=note, not separate note types.

**Trading themes** (project `trade_ideas`): narratives like "middle-east tail hedge 2026" or "AI capex unwind" are temporal stories, not invariants. They live as `type: note` + `tag: theme` in the `trade_ideas` project — body carries the living essence plus a dated catalyst log. Actual positions are `type: decision` + `tag: trade` in the same project, linking back to the theme note(s) they express. Theme notes cite concepts from the `finance/*` domains (`finance/regime`, `finance/geopolitics`, `finance/structure`, etc.) — they never introduce named-event concepts into the ontology. The cross-cycle pattern library emerges in the concept hubs (`reflation`, `middle-east-conflict`, `tail-hedge`); the timed narratives stay in theme notes.

**Landing documents**: Each project has 3 auto-generated landing docs (excluded from vault index):
- `DECISIONS.md` — decision table + Mermaid DAG. Agent-oriented. Refreshed every wrap.
- `BACKLOG.md` — todo items + stalled proposals + parked items. Agent-oriented. Refreshed every wrap.
- `STATE.md` — human-oriented overview. LLM-assisted narrative about what matters, key architecture, decisions to inspect, recent explorations (probes). Slow-moving — only update when the session genuinely changed the project's big picture.

Generate via `mem landing` (CLI) or `mem_landing` (MCP). `/mem-wrap` refreshes DECISIONS + BACKLOG automatically; STATE at agent discretion.

**Sources** live under `vault/sources/` (global) or `vault/projects/{name}/sources/` (project-scoped), bucketed by `source_type`. Each source type owns a dedicated subfolder and is paired with its own ingestion/search scaffold (e.g. `papers` + `repos` + `articles` are served by `/research` and `/discover`). Adding a new source type — YouTube, podcasts, Messenger — means adding a bucket and its own skill, not extending the research skill.

```
vault/sources/
  RESEARCH_FOCUS.md                  # user-maintained priority list (stays at root)
  papers/                            # source_type: paper — /research, /discover
    scaling-laws-neural-lms/
      source.md                      # indexed summary + concepts + metadata
      paper.pdf                      # raw PDF, opened on demand via [[slug/paper.pdf]]
  repos/                             # source_type: repo — /research, /discover
    some-github-repo/
      source.md
      snapshot.md                    # key repo files concatenated
  articles/                          # source_type: article — /research, /discover
    some-blog-post/
      source.md
      raw.md                         # full article text
  conversations/                     # source_type: conversation — chatgpt importer
    1rm-estimate-calculation.md      # flat file, single-summary (no raw companion)
  substack/                          # source_type: substack — /substack (inbox drain)
    citrini-research/                # author-level nesting
      curious-case-of-disappearing-liquidity/
        source.md
        raw.md                       # clipped markdown with image refs rewritten
        assets/                      # figures copied from inbox bundle
          chart-1.png
```

Routing is declared in `src/personal_mem/sources/registry.py` — one `SourceTypeSpec` per source type, specifying `slug`, `bucket`, `layout` (`flat` | `folder` | `author_folder`), `aliases`, and the skills that handle it. `VaultManager.create_note` reads the registry and dispatches on `spec.layout`. Legacy `source_type: github` is normalised to `repo` via the `aliases` field. Unregistered source types fall back to the `folder` layout with an empty bucket (e.g. `sources/<slug>/source.md`). Adding a new source type means adding one registry entry plus a skill under `commands/` — no `vault.py` edits required. See `ARCHITECTURE.md` for the full source-primitive model.

**Research ingestion** (`/research`): Processes URLs (arxiv, GitHub, web) into source notes. Fetches content via WebFetch/WebSearch, maps concepts to `ontology.yaml`, saves raw content alongside. Queue items are `todo`+`research` tagged notes, visible via `mem backlog`. Can process ad-hoc URLs or drain the queue with `--queue`.

**Research discovery** (`/discover`): Cross-project gap analysis. Reads `vault/sources/RESEARCH_FOCUS.md` for priorities, analyzes concept coverage, searches for new papers/repos/articles, creates queue items. Designed for periodic use (`/loop 6h /discover`).

**Substack ingestion** (`/substack`): Drains a disk inbox (`$SUBSTACK_INBOX`, default `~/substack_inbox/`) of browser-clipped posts. Capture happens in the user's authenticated browser via Obsidian Web Clipper or MarkDownload — that's how paid content gets through without auth plumbing. Accepts flat `.md` files or folder bundles with companion images. Images are copied into `assets/`, image paths rewritten, and each figure is interpreted via multimodal Read so chart content becomes FTS-searchable. Processed entries archive to `~/substack_inbox/_processed/<date>/` — never deleted.

**RESEARCH_FOCUS.md** (`vault/sources/RESEARCH_FOCUS.md`): User-maintained priority list for discovery. Contains active focus areas, authors to follow, concept gaps (auto-populated by `/discover`), and exclusion filters.

## Concept hubs — synthesis layer

Each concept in the ontology gets two pages in `vault/concepts/`:

- **Domain hub** at `vault/concepts/{domain}.md` (e.g. `swe--python.md`) — thin navigation page. Lists child concepts with wikilinks to their concept hubs. Fully regenerable; hand-edits to the body are not preserved.
- **Concept hub** at `vault/concepts/topics/{concept}.md` — synthesis layer. Two sections:
  - `## Essence` — ≤500w working mental model, slow-moving, revised rarely
  - `## Learning log` — append-only learning artifacts extracted from vault notes, each citing its source via `[[note-id]]`. Each entry carries an observational flag (`new` / `agrees` / `contradicts` / `extends`).

**The hub page IS the processed ledger.** No `hub_processed` frontmatter marker on source notes. To find what's unprocessed for a concept, query SQLite for `note_concepts` where `concept = X`, diff against the `[[note-id]]` citations already on that concept's hub. Dropping and rebuilding a hub page is a legal operation — the next run picks up everything again.

**Cross-vault, cross-type scope.** Learning artifacts can come from any note type (sources, sessions, decisions, notes) across any project. A technique picked up in a coding session cites that session's note; a claim from a paper cites the source note. Both feed the same concept hub.

**Two execution paths**, sharing the same `hubs.py` diff and parse/write logic:

1. **Backfill** via `mem hubs plan` + `mem hubs run` — OpenAI SDK + Batches API + gpt-5-mini. OpenAI caches prompt prefixes automatically for prompts ≥1024 tokens, so sorting requests by concept within the batch keeps the shared system prompt + hub state off the metered re-compute path. Use this for fresh vaults with many unprocessed notes across many concepts. Requires `OPENAI_API_KEY` in the environment and `pip install personal-mem[hubs]` for the optional `openai` dependency. Plan file lives at `.mem/hubs_plan.json`.
2. **Daily incremental** via `/update-hubs` skill — small deltas (1–20 notes total), runs inline via Claude Code. Use `mem hubs plan` + manual processing to append entries.

**Coherence hygiene** lives in `/mem-resolve-concepts`: split/merge/stale-essence suggestions, LLM judgment not thresholds, human accepts. Essence rewrites happen there too — `/update-hubs` flags but never rewrites the essence.

**What hub pages are NOT**:
- Not indexed for the concept→notes query (they're `type: concept-hub` / `type: domain-hub`, excluded from `notes_for_concept`)
- Not sources — they're synthesis artifacts
- Not auto-updated on `/research` or `/substack` ingest — those skills stay fast and cheap; hub updates are a separate explicit pass

## Key Files

- `src/personal_mem/vault.py` — VaultManager (note CRUD, inline YAML parser, wikilinks, `strip_section`; source routing delegates to `sources/registry.py`)
- `src/personal_mem/sources/registry.py` — Declarative source-type registry (`SourceTypeSpec` entries drive vault routing)
- `src/personal_mem/sources/frontmatter.py` — Canonical source-note frontmatter builder used by importers and skills
- `src/personal_mem/indexer.py` — SQLite index builder (FTS5, edges, concept edges, SHA-256 dedup)
- `src/personal_mem/search.py` — FTS search, graph traversal (recursive CTEs)
- `src/personal_mem/context.py` — Structured project-context payload builder (used by SessionStart hook and `mem_project_snapshot`)
- `src/personal_mem/hooks/handler.py` — Claude Code SessionStart/Pre/Post/Stop hooks (context injection at startup, enriched events, git/test detection, auto-extract at Stop)
- `src/personal_mem/judge.py` — Structural decision judgment (no LLM, evidence-based)
- `src/personal_mem/cli.py` — CLI entry point
- `src/personal_mem/concepts.py` — Concept tightening (aliases, near-duplicate detection, merge, domain + concept hub skeleton generators)
- `src/personal_mem/hubs.py` — Concept hub synthesis layer (parse/diff/write/render/LLM prompt contract, shared by `mem hubs` CLI and `/update-hubs` skill)
- `src/personal_mem/landing.py` — Landing document generators (DECISIONS.md, BACKLOG.md, STATE.md)
- `src/personal_mem/mcp/server.py` — MCP server (15 tools: search, create, read, update, extract, link, context, graph, judge, unlink, concepts, concepts_tighten, concepts_merge, landing, timeline)
- `src/personal_mem/embeddings.py` — API-based embeddings with SQLite cache
- `commands/mem-wrap.md` — `/mem-wrap` skill for full LLM extraction
- `commands/research.md` — `/research` skill for source ingestion (arxiv, GitHub, web)
- `commands/discover.md` — `/discover` skill for research gap analysis and queue generation
- `commands/mem-resolve-concepts.md` — `/mem-resolve-concepts` skill for periodic concept hygiene (merge dupes, update ontology, hub coherence review)
- `commands/substack.md` — `/substack` skill for Substack newsletter ingestion (disk-inbox drain, figure-aware via multimodal Read)
- `commands/update-hubs.md` — `/update-hubs` skill for daily incremental concept hub sync (small deltas, inline LLM)

## Environment

- `PERSONAL_MEM_VAULT` — vault root (default: ~/vault)
- `PERSONAL_MEM_PROJECT` — default project name
- `OPENAI_API_KEY` — required by `mem enrich`, the ChatGPT importer, embeddings, and `mem hubs run`

## Dependencies

Zero required. Optional: `mcp` (MCP server), `httpx` (embeddings API), `openai` (hubs backfill + enrich — `pip install personal-mem[hubs]`).
