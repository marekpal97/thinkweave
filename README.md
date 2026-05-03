# personal_mem

Obsidian-native universal memory layer for Claude Code. Markdown is the
source of truth; SQLite is a derived, rebuildable index.

personal_mem gives Claude Code (or any MCP-aware agent) durable memory
across sessions: a typed vault (note / session / decision / source /
theme), a knowledge graph from wikilinks and shared concepts, and MCP
tools for search, retrieval, and creation. Sessions are captured via
hooks and enriched by `/mem-wrap` before you `/clear`.

## Install

```bash
# Clone from wherever you got it (your fork, the upstream repo, etc.)
git clone <your-fork-or-org>/personal_mem.git
cd personal_mem
pip install -e ".[all]"   # mcp + httpx + openai extras
```

`[all]` is recommended — bare `pip install -e .` skips the MCP server,
embedding similarity, and OpenAI Batches hub-backfill. Drop down to
`[mcp]` if you only want the MCP surface.

In Claude Code:

```
/plugin add ./.claude/plugins/personal-mem
/onboard
```

`/onboard` walks the rest: vault location, source types, retroactive
Claude session import, hooks install, first index, landing docs.

## Daily loop

- `/ingest <thing>` — universal front door for any input shape (URL, file path, inline text, structured ID)
- `/research <url>` — URL-explicit shortcut (paper / repo / article)
- `/drain` — process queued items in batch
- `/discover` — find research gaps via configured strategies
- `/mem-wrap` — extract session knowledge before `/clear`

## Architecture

- **Three retrieval modalities**: FTS, similarity, graph.
- **Hub abstraction**: concepts (vocabulary) and themes (narrative
  arcs) share a spine.
- **Source-type registry**: paper / repo / article ship as defaults;
  add your own via `sources.yaml` + a single skill file.

[CLAUDE.md](CLAUDE.md) — LLM agent runtime · [ARCHITECTURE.md](ARCHITECTURE.md) — contributors · [LICENSE](LICENSE) — MIT.
