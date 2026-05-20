# personal_mem

Obsidian-native universal memory layer for Claude Code. Markdown is the
source of truth; SQLite is a derived, rebuildable index.

personal_mem gives Claude Code (or any MCP-aware agent) durable memory
across sessions: a typed vault (note / session / decision / source /
theme), a knowledge graph from wikilinks and shared concepts, and MCP
tools for search, retrieval, and creation. Sessions are captured via
hooks and enriched by `/mem-wrap` before you `/clear`.

## Install

Two paths — the Claude Code plugin (recommended, one command) or the
legacy `pip install` + `mem install` flow (kept for users on a clone or
without marketplace access).

**Invocation contract.** All three registration paths (project-scope
`.mcp.json`, machine-scope `~/.claude.json` written by `mem install`,
and the plugin manifest) launch the MCP server via the same canonical
shape: `uv run --project <path> --extra mcp mem-mcp`. The `<path>`
differs by scope (`.` for the project file, an absolute path for the
machine file, `${CLAUDE_PLUGIN_ROOT}` for the plugin manifest), but the
launcher is always `uv`. This avoids Claude Code's stripped-PATH
launcher missing the bare `mem-mcp` console script. Run `mem doctor
--mcp` if registration looks wrong.

**Prerequisites either way:**

- **`uv` is required**, not optional — install with
  `curl -LsSf https://astral.sh/uv/install.sh | sh`. All three MCP
  invocations route through `uv run`; without it nothing resolves.

### Plugin install (recommended)

Once per machine — collapses MCP registration, hook installation, and
slash-command discovery into one operation:

```bash
claude plugin install personal-mem   # registers MCP, hooks, commands
# → restart Claude Code so the plugin's MCP server is picked up
```

The plugin manifest declares the MCP server inline, so no separate
`mem install` step is required.

After restart, run `/onboard` from any repo. It seeds your vault from
prior Claude Code conversations *unconditionally* (step 1), bootstraps
the ontology from imported `proposed_concepts:` (step 2), walks you
through focus + source-type configuration (step 3), and emits first
landing docs (step 4). Idempotent — re-running only does what's still
missing.

### Legacy install (no plugin)

If you can't use the plugin path (private fork, marketplace not
available, etc.):

```bash
git clone <your-fork-or-org>/personal_mem.git
cd personal_mem
uv sync --extra mcp                  # installs mem, mem-hook, mem-mcp
mem install --yes                    # registers personal-mem in ~/.claude.json
# → restart Claude Code now, before continuing
```

`mem install` is idempotent. It writes the personal-mem MCP-server
block into `~/.claude.json` if absent; if a different block exists, it
shows the diff and waits for `--yes` before overwriting. It does not
touch any vault or any project's `.claude/`. Hooks still need a separate
`mem hooks install` per repo (the plugin path declares hooks globally).

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

### `/onboard` — the first-run flow

After plugin (or legacy) install, run `/onboard` from any repo:

```bash
cd <your-repo>
claude
> /onboard
```

`/onboard` is the spine of new-user UX:

1. **Always-first**: imports prior Claude Code conversations (multi-
   project, auto-discovers everything under `~/.claude/projects/`).
   No skip option — this is what makes mem useful from the first query.
2. **Ontology bootstrap**: surfaces high-frequency `proposed_concepts:`
   from the import for canonicalisation.
3. **Focus + source-types**: walks you through which projects are
   active and which source types you want enabled.
4. **Per-project hooks** (legacy install only — plugin install handles
   hooks globally) and **first landing docs** for each active project.

Idempotent: re-running picks up wherever it left off.

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
