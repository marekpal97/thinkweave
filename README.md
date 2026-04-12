# personal_mem

An Obsidian-native universal memory layer for agentic systems. Markdown is the source of truth; SQLite is a derived, rebuildable index.

personal_mem gives Claude Code (or any MCP-aware agent) durable, searchable memory across sessions: a vault of markdown notes typed as `note`, `session`, `decision`, or `source`; a knowledge graph built from wikilinks and shared concepts; and a set of MCP tools for search, retrieval, and creation. Sessions are captured automatically via Claude Code hooks, summarized at exit, and can be enriched by a `/mem-wrap` skill at session end.

It is designed to be **lightweight**, **file-first** (your notes live in plain `.md` files browseable in Obsidian), and **extensible** — adding a new source type is a dict entry plus a skill file, not a framework rewrite.

## Install

```bash
git clone https://github.com/marekpal97/personal_mem.git
cd personal_mem
uv pip install -e .[all]       # core + MCP server + embeddings
```

Or as a library dependency:

```bash
uv add personal-mem[mcp,embeddings]
```

Requires Python ≥ 3.11. The core has **zero runtime dependencies**; MCP and embeddings are opt-in extras.

## Quickstart

```bash
# 1. Point at a vault location (anywhere — Obsidian can open it later).
export PERSONAL_MEM_VAULT=~/vault
export PERSONAL_MEM_PROJECT=myproject   # optional — auto-detected from git

# 2. Initialize the vault directory structure.
uv run mem init

# 3. Install Claude Code hooks into the current project's .claude/ folder.
#    This wires SessionStart context injection + auto-capture of sessions.
uv run mem hooks install

# 4. (Optional) Register the MCP server so Claude Code can call mem_search etc.
#    See your Claude Code docs for mcp-server registration — the script is
#    `uv run python -m personal_mem.mcp.server`.
```

Open a fresh Claude Code session in that directory — the SessionStart hook injects ~7–10k tokens of project context (recent sessions, decisions, backlog, concepts, MCP tool manifest) before your first turn.

## Vault layout

```
vault/
├── projects/
│   └── {project}/
│       ├── DECISIONS.md          # auto-generated decision ledger + DAG
│       ├── BACKLOG.md            # auto-generated open items
│       ├── STATE.md              # human-oriented overview (LLM-assisted)
│       └── sessions/
│           └── {session-id}-{date}/
│               ├── session.md    # clean summary
│               ├── events.jsonl  # archived raw event log
│               ├── derived-note.md
│               └── derived-decision.md
├── sources/                      # external reference material
│   ├── papers/
│   ├── repos/
│   ├── articles/
│   ├── conversations/
│   └── substack/
├── daily/
├── templates/
└── .mem/
    ├── index.db                  # SQLite FTS + graph index
    ├── embeddings.db             # optional semantic search cache
    └── concept_aliases.yaml      # vocabulary canonicalization
```

The SQLite index is derived — delete it and run `uv run mem index --full` to rebuild from markdown.

## MCP tools

When the MCP server is registered, Claude gets a set of memory primitives. The most important ones:

| Tool | Purpose |
|---|---|
| `mem_search(query, mode, project, type, tags, limit)` | FTS / semantic / hybrid (RRF) search |
| `mem_concept_search(concepts, match_mode, project, min_matches)` | Find notes by concept intersection/union |
| `mem_context(query, project, type, concepts)` | 3-layer retrieval (FTS → concept expansion → recency) |
| `mem_timeline(project, days)` | Chronological sessions + decisions (omit project for cross-project ranking) |
| `mem_read(id)` / `mem_graph(id, depth)` | Fetch a note or walk its typed-edge graph |
| `mem_source_lens(source_id)` | Walk out from a source to its downstream decisions/sessions |
| `mem_decisions_for_file(file_path, project)` | Every decision touching a file |
| `mem_project_snapshot(project)` | On-demand project overview (same payload the SessionStart hook injects) |
| `mem_create(note_type, title, body, tags, concepts, project, session_id)` | Create a note |
| `mem_update(note_id, frontmatter_updates, body_append)` | Update a note |
| `mem_link(source_id, target_id, edge_type)` | Add a typed edge |
| `mem_extract(session_id, ...)` | Enrich a session with LLM insights + decisions |
| `mem_judge(session_id)` | Evaluate decisions against git evidence |
| `mem_concept_source_counts(concepts)` | Bulk under-source check for gap analysis |
| `mem_landing(project, doc, sections)` | Regenerate landing documents (DECISIONS/BACKLOG/STATE) |

Run `uv run python -m personal_mem.mcp.server` to start the server stdio, or register it in your Claude Code MCP config.

## Adding a new source type

personal_mem is designed so new kinds of reference material plug in without touching the framework. The canonical pattern: **1 dict entry + 1 skill file**.

Say you want to ingest emails (e.g. from a local mbox drain or a forwarded-to-folder setup).

### 1. Register the bucket

Add one line to `src/personal_mem/vault.py` in the `_SOURCE_BUCKETS` class attribute on `VaultManager`:

```python
_SOURCE_BUCKETS = {
    "paper": "papers",
    "repo": "repos",
    "article": "articles",
    "conversation": "conversations",
    "substack": "substack",
    "email": "emails",          # ← new
}
```

That is the entire framework change. `VaultManager.create_note` will now route any note with `source_type: email` into `vault/sources/emails/{slug}/source.md`. The indexer, search, MCP server, concept graph, and frontmatter schema all treat it uniformly — no special-casing anywhere.

### 2. Write the ingestion skill

Copy `commands/_source_template.md` to `commands/email.md` and fill in the placeholders. The skill file is a procedural spec that Claude Code reads when the user invokes `/email`; it documents what to fetch, how to parse it, which concepts to map, and what frontmatter fields are specific to your source type.

For inspiration, look at the three real skill files already in `commands/`:
- `commands/research.md` — papers, repos, articles (URL → PDF/git clone/HTML fetch)
- `commands/substack.md` — disk-inbox drain with multimodal figure interpretation
- `commands/discover.md` — the gap-analysis companion that proposes new queue items

Each is deliberately bespoke because each source has genuinely different ingestion logic. There is no shared skill framework, and that is on purpose — collapsing them into a base class would destroy the per-source variation that makes each useful.

### 3. (Optional) Propose domain concepts

If your new source type introduces vocabulary that isn't in `ontology.yaml` yet, don't add it eagerly. Instead, have the ingestion skill put new terms in the `proposed_concepts` frontmatter field. They will be picked up by `/mem-resolve-concepts` for review and canonicalized into `ontology.yaml` once they earn their place (count ≥ 5 across the vault).

### 4. Verify

```bash
# Unit — the bucket routes correctly
uv run python -c "
from personal_mem.vault import VaultManager
from personal_mem.schemas import NoteType
vm = VaultManager()
path = vm.create_note(
    NoteType.SOURCE,
    'Test Email',
    extra_frontmatter={'source_type': 'email', 'url': 'mid:123'},
)
print(path)
"
# Expect: .../vault/sources/emails/test-email/source.md
```

Then use the skill end-to-end from Claude Code and check `mem_search(query='...', type='source')` returns your new entry.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `PERSONAL_MEM_VAULT` | `~/vault` | Vault root directory |
| `PERSONAL_MEM_PROJECT` | auto (git) | Default project name |
| `OPENAI_API_KEY` | — | Enables embeddings for semantic search |

Project detection walks up from the current directory looking for `.git` (file or directory — worktrees supported). Override with `PERSONAL_MEM_PROJECT` per worktree if you need explicit control.

## Philosophy

- **Markdown is the source of truth.** The SQLite index is derived and disposable; your notes live in plain files you can open in Obsidian, edit with any editor, version in git, and read without any tooling.
- **Capture is automatic, enrichment is explicit.** Claude Code hooks accumulate tool events, git commits, test results, and `★ Insight` blocks into a session buffer. The Stop hook archives and summarizes thinly; full LLM enrichment happens only when you run `/mem-wrap` before clearing.
- **The knowledge graph is typed.** Edges come from wikilinks, shared concepts (≥2 overlaps auto-link), and explicit `mem_link` calls with edge types (`supersedes`, `derived_from`, `touches_file`, etc.). Concepts are the load-bearing vocabulary — tags are for filtering.
- **Extension means addition, not surgery.** New source types, new landing doc types, new importers — all of them plug in at well-defined seams (`_SOURCE_BUCKETS`, `landing.py`, `importers/`) without touching the rest.

## Development

```bash
uv run pytest              # 450+ tests
uv run ruff check src tests
```

The codebase is ~10k LOC with zero runtime dependencies in the core. See `CLAUDE.md` for architectural notes, the session + decision lifecycle, and the retrieval protocol. See `src/personal_mem/ontology.example.yaml` for a reference ontology populated across ML, AI tooling, finance, and SWE domains.

## License

MIT — see [LICENSE](LICENSE).
