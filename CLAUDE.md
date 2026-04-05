# personal_mem

Obsidian-native universal memory layer. Markdown is the source of truth; SQLite is a derived, rebuildable index.

## Commands

- `uv run pytest` — run tests (190 tests)
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
- `uv run mem hooks install` — install Claude Code hooks (Pre/Post/Stop)
- `uv run mem landing [--project X] [--doc decisions|backlog|state|all]` — generate project landing documents
- `uv run mem restructure [--dry-run]` — consolidate notes/decisions into session folders

## Session Extraction

**Before `/clear` or `/exit`, always run `/mem-wrap`** to extract session knowledge. Three extraction paths:

1. **Auto (Stop hook)**: Fires at exit/Ctrl+C. Performs thin extraction — builds summary from metadata (files, commits, tests), strips event logs, archives buffer as `events.jsonl`, marks `processed: true` + `auto_extracted: true`. No LLM insights.
2. **Manual (`/mem-wrap`)**: Full LLM extraction — curated insights, decisions with rationale, rich summaries. Can enrich auto-extracted sessions (`force=true`).
3. **Pre-clear (CLAUDE.md rule)**: Before `/clear`, always run `/mem-wrap` first. There is no clear hook — this is the only way to preserve context before clearing.

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

**Session lifecycle**: hooks accumulate events (with diff context) + `★ Insight` blocks + git commits/test results into session notes → Stop hook auto-extracts (thin summary, archive events) → `/mem-wrap` enriches with LLM insights and decisions via `mem_extract` → session folder contains clean summary + derived artifacts. For non-code conversations (no hooks fired), `mem_extract` auto-creates a session note.

**Decision lifecycle**: `proposed` → `accepted` → `deprecated`/`superseded`. Decisions capture both successful and abandoned approaches. `mem_judge` evaluates decisions against evidence (committed? tested? re-edited?) and assigns verdicts (kept/superseded/reverted/unknown).

**Follow-ups**: Any note can be tagged `todo` to mark it for later. `mem backlog` lists all `todo`-tagged notes. Never auto-add `todo` — only when the user explicitly asks.

**Tag conventions**: `todo` (open work item), `parked` (deliberately deferred, body explains why), `probe` (user question + discovery — learning artifact). These are regular tags on type=note, not separate note types.

**Landing documents**: Each project has 3 auto-generated landing docs (excluded from vault index):
- `DECISIONS.md` — decision table + Mermaid DAG. Agent-oriented. Refreshed every wrap.
- `BACKLOG.md` — todo items + stalled proposals + parked items. Agent-oriented. Refreshed every wrap.
- `STATE.md` — human-oriented overview. LLM-assisted narrative about what matters, key architecture, decisions to inspect, recent explorations (probes). Slow-moving — only update when the session genuinely changed the project's big picture.

Generate via `mem landing` (CLI) or `mem_landing` (MCP). `/mem-wrap` refreshes DECISIONS + BACKLOG automatically; STATE at agent discretion.

**Sources** can be project-scoped (`vault/projects/{name}/sources/`) or global (`vault/sources/`).

## Key Files

- `src/personal_mem/vault.py` — VaultManager (note CRUD, inline YAML parser, wikilinks, `strip_section`)
- `src/personal_mem/indexer.py` — SQLite index builder (FTS5, edges, concept edges, SHA-256 dedup)
- `src/personal_mem/search.py` — FTS search, graph traversal (recursive CTEs)
- `src/personal_mem/hooks/handler.py` — Claude Code Pre/Post/Stop hooks (enriched events, git/test detection, auto-extract at Stop)
- `src/personal_mem/judge.py` — Structural decision judgment (no LLM, evidence-based)
- `src/personal_mem/cli.py` — CLI entry point
- `src/personal_mem/concepts.py` — Concept tightening (aliases, near-duplicate detection, merge)
- `src/personal_mem/landing.py` — Landing document generators (DECISIONS.md, BACKLOG.md, STATE.md)
- `src/personal_mem/mcp/server.py` — MCP server (15 tools: search, create, read, update, extract, link, context, graph, judge, unlink, concepts, concepts_tighten, concepts_merge, landing, timeline)
- `src/personal_mem/embeddings.py` — API-based embeddings with SQLite cache
- `commands/mem-wrap.md` — `/mem-wrap` skill for full LLM extraction

## Environment

- `PERSONAL_MEM_VAULT` — vault root (default: ~/vault)
- `PERSONAL_MEM_PROJECT` — default project name
- `OPENAI_API_KEY` — for embeddings (optional)

## Dependencies

Zero required. Optional: `mcp` (MCP server), `httpx` (embeddings API).
