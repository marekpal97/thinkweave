# Thinkweave — CLI & MCP reference

The full CLI subcommand reference, the MCP tool surface, the CLI↔MCP surface contract, and the environment variables. Thin operating guide: [CLAUDE.md](../CLAUDE.md). Structural narrative: [ARCHITECTURE.md](../ARCHITECTURE.md).

## Table of contents

- [CLI reference](#cli-reference)
- [MCP tool surface](#mcp-tool-surface)
- [Surface contract — CLI ↔ MCP](#surface-contract--cli--mcp)
- [Environment](#environment)

## CLI reference

The CLI exposes **44 subcommands** total via `_DISPATCH` in `surfaces/cli/__init__.py`. Agents work primarily through MCP tools (see below); the CLI is for setup, admin, and the small set of operations without MCP parity. The console command is `mem` (the Python package is `thinkweave`; the MCP server id is `thinkweave`).

Consolidations to keep in mind: wikilink materialisation lives under `mem index --materialize-links` (was `mem connect`, deleted 2026-05-21); the `mem_concepts*` MCP tools are folded into `mem_concepts(action=...)`; `mem_source_lens` + `mem_decisions_for_file` are folded into `mem_graph(filter=...)`. The Phase-4-C deprecation aliases for both CLI and MCP names were removed 2026-05-21 — call the canonical names.

```
mem init                                    # initialize vault + config/sources.yaml
mem add --type {note|theme|...} "Title"     # create a note
mem index [--full] [--embed] [--only-new|--since DATE] [--materialize-links]
                                            # rebuild SQLite index (+ wikilinks).
                                            # --embed --only-new is the keep-warm
                                            # cron path: re-embed only notes whose
                                            # updated_at > last cached embedding.
mem search "q" [--type X] [--concept Y]     # FTS / similarity / hybrid
mem graph <id>                              # local graph
mem context "q" [--type X]                  # 3-layer retrieval (FTS → concept → recency)
mem stats                                   # vault health (deprecated → mem doctor)
mem doctor [--migrate]                      # coherence linter (+ optional data migrations)
mem backlog [--project X]                   # todo notes + active queue items
mem decisions [--file <path>] [--project X] # decision ledger lookup
mem project {list|show|set-active}          # project registry on the vault
mem concepts {list|merge|hubs|drift|notes|prune}
mem hubs {status|plan|link|repair}          # concept-hub backfill (use `mem drain --target hubs` to execute)
mem themes rebuild-registry                 # rebuild themes.yaml from canonical theme files
mem drain --target hubs --via {inline|batch}  # batch path replaces `mem hubs run`
mem queue {list|inspect|peek}               # per-source-type acquisition queues
mem hooks {install|uninstall|status}
mem landing [--project X] [--doc all]       # regenerate DECISIONS/BACKLOG/STATE/THEMES
mem flow {list|show|run}                    # named workflow pipelines
mem schedule {list|install|uninstall}       # render scheduling.yaml onto the host
                                            # scheduler — crontab on Linux/macOS,
                                            # Windows Task Scheduler (schtasks).
                                            # [--dry-run] [--only j1,j2]
mem skill {list|show <name>}                # inspect commands/*.md frontmatter
mem sources {list|show <slug>}              # inspect source-type registry
mem prune-orphans [--yes]                   # delete abandoned session folders (used by /mem-wrap)
mem wrap-finalize <ses-id> [--project X]    # deterministic tail of /mem-wrap: prune→index→judge→landing→drift (--json for headless)
mem seam {surface|commit}                   # memory-seam (CC auto-memory ↔ vault): dirty-diff + write durable map (dream-seam-worker's hands)
mem rlvr export [--project] [--since] [--until] [--committed-only]  # JSONL stream of decision-context RLVR rows (one per decision)
mem update <note_id> [-f key=val ...]       # frontmatter / body-append for headless flows
mem enrich [--project X]                    # LLM concept enrichment (gpt-5-mini)
mem import {claude-mem|chatgpt|file|messenger}
mem intake {enumerate|archive}              # drop-folder helpers for /substack and friends
mem discover [--project X]                  # cross-project research gap analysis
mem show <id>                               # render a single note
mem link <src_id> <tgt_id> [--type X]       # add typed edge
mem install [--vault PATH] [--yes]          # register MCP server in ~/.claude.json
mem mcp                                     # invoke the MCP server (used by ~/.claude.json)
```

**Agents shouldn't run** `mem doctor`, `mem stats`, `mem flow`, `mem intake`, `mem enrich`, `mem import`, `mem prune-orphans`, `mem install`, `mem mcp`, `mem init`, `mem hooks`, `mem schedule` directly — they belong in cron flows or interactive admin. There is no MCP parity for these subcommands.

## MCP tool surface

The MCP server (id `thinkweave`, so tools are addressed `mcp__thinkweave__mem_*` and the short names stay `mem_*`) exposes 18 tools:

`mem_search`, `mem_create`, `mem_read`, `mem_update`, `mem_link`, `mem_unlink`, `mem_context`, `mem_graph` (filter-dispatched), `mem_concepts` (action-dispatched), `mem_extract`, `mem_judge`, `mem_landing`, `mem_enrich`, `mem_timeline`, `mem_project_snapshot`, `mem_queue`, `mem_sources_config`, `mem_prompts`.

## Surface contract — CLI ↔ MCP

The boundary principle: **MCP tools are the agent operation surface; the CLI is for admin, cron, and headless skill orchestration** — plus exactly four narrow *agent-Bash* entries that in-session agents and dream workers invoke from a Bash tool mid-flow: `mem wrap-finalize`, `mem hubs apply-linkage`, `mem landing --doc`, and `mem judge --rejudge/--drain`. Everything else an agent needs goes through `mem_*` MCP tools; everything a human or crontab needs goes through `mem`. Where both surfaces exist for one operation, they are thin wrappers over the same `operations/` function (see [ARCHITECTURE.md §"Operations layer"](../ARCHITECTURE.md#operations-layer)). The contract is pinned mechanically by `tests/test_surface_contract.py` (schema↔dispatch wiring, doc-referenced subcommands, worker tool allowlists, inventory counts); `_DISPATCH` in `surfaces/cli/__init__.py` is grouped by the same audience labels.

Full inventory — 43 CLI subcommands × 18 MCP tools (audience: *agent* = MCP-only, *admin-cron* = CLI-only, *both* = paired surfaces; *agent-Bash* marks the four CLI carve-outs):

| Operation | CLI subcommand | MCP tool | Audience |
|---|---|---|---|
| Search (FTS / similar / hybrid) | `mem search` | `mem_search` | both (CLI = retrieval debug) |
| Budgeted context blob | `mem context` | `mem_context` | both (CLI = retrieval debug) |
| Graph walk (filter-dispatched) | `mem graph` | `mem_graph` | both (CLI = retrieval debug) |
| Read one note | `mem show` | `mem_read` | both (CLI = retrieval debug) |
| Timeline window | `mem timeline` | `mem_timeline` | both (CLI = retrieval debug) |
| Project snapshot | `mem project-snapshot` | `mem_project_snapshot` | both (CLI = retrieval debug) |
| Prompt / probe surfacing | `mem prompts` | `mem_prompts` | both (CLI = retrieval debug) |
| Create note | `mem add` | `mem_create` | both (CLI = headless flows) |
| Update note | `mem update` | `mem_update` | both (CLI = headless flows) |
| Add typed edge | `mem link` | `mem_link` | both (CLI = headless flows) |
| Remove typed edge | `mem unlink` | `mem_unlink` | both (CLI = headless flows) |
| Concept ops (action-dispatched) | `mem concepts` | `mem_concepts` | both (CLI = hygiene orchestration) |
| Session extraction | — | `mem_extract` | agent |
| Decision / prediction judging | `mem judge` | `mem_judge` | both — `--rejudge/--drain` is **agent-Bash** |
| Landing docs regeneration | `mem landing` | `mem_landing` | both — `--doc` is **agent-Bash** |
| Concept enrichment | `mem enrich` | `mem_enrich` | both (CLI = admin-cron backfill) |
| Acquisition-queue inspection | `mem queue` | `mem_queue` | both |
| Source-type registry | `mem sources` | `mem_sources_config` | both |
| /mem-wrap deterministic tail | `mem wrap-finalize` | — | **agent-Bash** |
| Hub backfill / linkage | `mem hubs` | — | admin-cron — `apply-linkage` is **agent-Bash** |
| Decision ledger lookup | `mem decisions` | — | admin-cron |
| Todo backlog | `mem backlog` | — | admin-cron |
| SQLite index rebuild | `mem index` | — | admin-cron |
| Importers (claude-mem / chatgpt / …) | `mem import` | — | admin-cron |
| Vault health | `mem stats` | — | admin-cron |
| Coherence linter | `mem doctor` | — | admin-cron |
| Named workflow pipelines | `mem flow` | — | admin-cron |
| Host scheduler render | `mem schedule` | — | admin-cron |
| Hook install / status | `mem hooks` | — | admin-cron |
| Vault init | `mem init` | — | admin-cron |
| MCP server registration | `mem install` / `mem uninstall` | — | admin-cron |
| Hook pause toggle | `mem pause` / `mem resume` | — | admin-cron |
| MCP server entry point | `mem mcp` | — | admin-cron (infrastructure) |
| Drop-folder intake helpers | `mem intake` | — | admin-cron |
| Skill registry inspection | `mem skill` | — | admin-cron |
| Queue drain (consumer rail) | `mem drain` | — | admin-cron (orchestration) |
| Discovery strategies (producer rail) | `mem discover` | — | admin-cron (orchestration) |
| Dream scan / apply | `mem dream` | — | admin-cron (orchestration) |
| Themes registry rebuild | `mem themes` | — | admin-cron |
| Project registry | `mem project` | — | admin-cron |
| Orphan session pruning | `mem prune-orphans` | — | admin-cron |
| RLVR substrate export | `mem rlvr` | — | admin-cron |

## Environment

- `THINKWEAVE_VAULT` — vault root (default `~/vault`). The legacy `PERSONAL_MEM_VAULT` is honored as a migration fallback.
- `THINKWEAVE_PROJECT` — default project name. The legacy `PERSONAL_MEM_PROJECT` is honored as a migration fallback.
- `OPENAI_API_KEY` — required by `mem enrich`, the ChatGPT importer, embeddings, and the hub batch backfill (`mem drain --target hubs --via batch`).

After upgrading Thinkweave, re-run `mem hooks install` to pick up newly-added hooks (e.g. SessionStart).
