# personal_mem

Obsidian-native universal memory layer for Claude Code. Markdown is the
source of truth; SQLite is a derived, rebuildable index.

personal_mem gives Claude Code (or any MCP-aware agent) durable memory
across sessions: a typed vault (note / session / decision / source /
theme), a knowledge graph from wikilinks and shared concepts, and MCP
tools for search, retrieval, and creation. Sessions are captured via
hooks and enriched by `/mem-wrap` before you `/clear`.

## Install — three scopes, three steps

personal_mem distinguishes **machine** (one per laptop), **vault** (one
per knowledge home), and **project** (one per repo). Each scope has one
verb that owns its setup; nothing else writes there.

### Once per machine

Requires [`uv`](https://github.com/astral-sh/uv) (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

```bash
git clone <your-fork-or-org>/personal_mem.git
cd personal_mem
uv sync --extra mcp                  # installs mem, mem-hook, mem-mcp
mem install --yes                    # registers personal-mem in ~/.claude.json
```

`mem install` is idempotent. It writes the personal-mem MCP-server
block into `~/.claude.json` if absent; if a different block exists, it
shows the diff and waits for `--yes` before overwriting. It does not
touch any vault or any project's `.claude/`.

Optional environment:

- `OPENAI_API_KEY` — embeddings (`mem index --embed`) and concept-hub
  bulk backfill (`/update-hubs --bulk batch`)
- `ANTHROPIC_API_KEY` — Anthropic Batches strategy for the Claude Code
  conversation seed (`/onboard` step 4 with `--via batch`)

### Once per vault

```bash
PERSONAL_MEM_VAULT=~/vault mem init
```

Creates `<vault>/.mem/sources.yaml` (overlay-friendly defaults), the
SQLite index, the ontology seed, and the concept-hub directory. Add
`PERSONAL_MEM_VAULT=...` to your shell rc to make it permanent.

### Each repo where you want memory active

```bash
cd <your-repo>
claude
> /onboard
```

`/onboard` registers this project in the vault, installs Claude Code
hooks (SessionStart, Pre/PostToolUse, Stop, UserPromptSubmit) into this
repo's `.claude/settings.json`, optionally seeds the vault from your
prior Claude Code conversations, and emits first per-project landing
docs.

## Daily loop

- `/ingest <thing>` — universal front door for any input (URL, file, text, ID)
- `/research <url>` — URL-explicit shortcut (paper / repo / article)
- `/drain` — process queued items in batch
- `/discover` — find research gaps via configured strategies
- `/source-fit "<describe new input shape>"` — does an existing source type cover this?
- `/source-scaffold <slug>` — generate a new source type (vault overlay + machine-global skill)
- `/mem-wrap` — extract session knowledge before `/clear`

## Architecture

- **Three retrieval modalities**: FTS, similarity, graph.
- **Hub abstraction**: concepts (vocabulary) and themes (narrative
  arcs) share a spine.
- **Source-type registry**: paper / repo / article / substack /
  conversation ship as defaults; add your own without forking via
  `/source-scaffold` (or `mem sources scaffold`).
- **Three install scopes**: machine (`mem install`) / vault (`mem
  init`) / project (`/onboard`). Each is idempotent; each owns
  exactly one set of artifacts. See ARCHITECTURE.md §Invocation
  surface for the stable-name contract.

[CLAUDE.md](CLAUDE.md) — LLM agent runtime · [ARCHITECTURE.md](ARCHITECTURE.md) — contributors · [LICENSE](LICENSE) — MIT.
