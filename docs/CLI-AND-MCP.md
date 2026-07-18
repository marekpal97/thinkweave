# Thinkweave — CLI & MCP reference

The full CLI subcommand reference, the MCP tool surface, the CLI↔MCP surface contract, and the environment variables. Thin operating guide: [CLAUDE.md](../CLAUDE.md). Structural narrative: [ARCHITECTURE.md](../ARCHITECTURE.md).

## Table of contents

- [CLI reference](#cli-reference)
- [MCP tool surface](#mcp-tool-surface)
- [Surface contract — CLI ↔ MCP](#surface-contract--cli--mcp)
- [Environment](#environment)

## CLI reference

The CLI exposes **48 subcommands** total via `_DISPATCH` in `surfaces/cli/__init__.py`. Agents work primarily through MCP tools (see below); the CLI is for setup, admin, and the small set of operations without MCP parity. The console command is `mem` (the Python package is `thinkweave`; the MCP server id is `thinkweave`).

Consolidations to keep in mind: wikilink materialisation lives under `weave index --materialize-links` (was `weave connect`, deleted 2026-05-21); the `weave_concepts*` MCP tools are folded into `weave_concepts(action=...)`; `weave_source_lens` + `weave_decisions_for_file` are folded into `weave_graph(filter=...)`. The Phase-4-C deprecation aliases for both CLI and MCP names were removed 2026-05-21 — call the canonical names.

```
weave init                                    # initialize vault + config/sources.yaml
weave config {show|set-vault PATH}            # inspect/persist user config (vault path);
                                            #   platform-resolved (XDG / %APPDATA%)
weave add --type {note|theme|...} "Title"     # create a note
weave index [--full] [--embed] [--only-new|--since DATE] [--materialize-links]
                                            # rebuild SQLite index (+ wikilinks).
                                            # --embed --only-new is the keep-warm
                                            # cron path: re-embed only notes whose
                                            # updated_at > last cached embedding.
weave search "q" [--type X] [--concept Y]     # FTS / similarity / hybrid
weave graph <id>                              # local graph
weave context "q" [--type X]                  # 3-layer retrieval (FTS → concept → recency)
weave stats                                   # vault health (deprecated → weave doctor)
weave doctor [--migrate]                      # coherence linter (+ optional data migrations)
weave backlog [--project X]                   # todo notes + active queue items
weave decisions [--file <path>] [--project X] # decision ledger lookup
weave project {list|show|set-active}          # project registry on the vault
weave concepts {list|merge|hubs|drift|notes|prune}
weave hubs {status|plan|link|repair}          # concept-hub backfill (use `weave drain --target hubs` to execute)
weave themes rebuild-registry                 # rebuild themes.yaml from canonical theme files
weave drain --target hubs --via {inline|batch}  # batch path replaces `weave hubs run`
weave queue {list|inspect|peek}               # per-source-type acquisition queues
weave hooks {install|uninstall|status}
weave landing [--project X] [--doc all]       # regenerate DECISIONS/BACKLOG/STATE/THEMES
weave flow {list|show|run}                    # named workflow pipelines
weave schedule {list|install|uninstall}       # render scheduling.yaml onto the host
                                            # scheduler — crontab on Linux/macOS,
                                            # Windows Task Scheduler (schtasks).
                                            # [--dry-run] [--only j1,j2]
weave skill {list|show <name>}                # inspect commands/*.md frontmatter
weave sources {list|show <slug>}              # inspect source-type registry
weave prune-orphans [--yes]                   # delete abandoned session folders (used by /wrap)
weave wrap-finalize <ses-id> [--project X]    # deterministic tail of /wrap: prune→index→judge→landing→drift (--json for headless)
weave seam {surface|commit}                   # memory-seam (CC auto-memory ↔ vault): dirty-diff + write durable map (dream-seam-worker's hands)
weave rlvr export [--project] [--since] [--until] [--committed-only]  # JSONL stream of decision-context RLVR rows (decisions + loop trajectories)
weave trajectory judge [--phase both|1|2] [--limit N] [--json]  # deterministic issue-loop trajectory outcome judge (phase-2 dream-outcome-worker rail)
weave steering {evidence [--module PATH] | gate --proposals-json FILE} [--json]  # evidence-gated steering: per-module signals + drop-no-evidence/budget-cap gate the slow loop #61 calls
weave update <note_id> [-f key=val ...]       # frontmatter / body-append for headless flows
weave import {claude-code|claude-history|file|chatgpt|messenger} [path] [--via {inline|batch}]
weave intake {enumerate|archive}              # drop-folder helpers for /substack and friends
weave discover [--project X]                  # cross-project research gap analysis
weave show <id>                               # render a single note
weave link <src_id> <tgt_id> [--type X]       # add typed edge
weave install [--vault PATH] [--yes]          # register MCP server in ~/.claude.json
weave dev-link                                # clone-dev flagless plugin loading via a ~/.claude/skills/ symlink (@skills-dir)
weave dev-unlink                              # remove the dev-link symlink
weave mcp                                     # invoke the MCP server (used by ~/.claude.json)
```

**Agents shouldn't run** `weave doctor`, `weave stats`, `weave flow`, `weave intake`, `weave import`, `weave prune-orphans`, `weave install`, `weave mcp`, `weave init`, `weave config`, `weave hooks`, `weave schedule` directly — they belong in cron flows or interactive admin. There is no MCP parity for these subcommands.

## MCP tool surface

The MCP server (id `thinkweave`, so tools are addressed `mcp__thinkweave__weave_*` and the short names stay `weave_*`) exposes 17 tools:

`weave_search`, `weave_create`, `weave_read`, `weave_update`, `weave_link`, `weave_unlink`, `weave_context`, `weave_graph` (filter-dispatched), `weave_concepts` (action-dispatched), `weave_extract`, `weave_judge`, `weave_landing`, `weave_timeline`, `weave_project_snapshot`, `weave_queue`, `weave_sources_config`, `weave_prompts`.

## Surface contract — CLI ↔ MCP

The boundary principle: **MCP tools are the agent operation surface; the CLI is for admin, cron, and headless skill orchestration** — plus exactly four narrow *agent-Bash* entries that in-session agents and dream workers invoke from a Bash tool mid-flow: `weave wrap-finalize`, `weave hubs apply-linkage`, `weave landing --doc`, and `weave judge --rejudge/--drain`. Everything else an agent needs goes through `weave_*` MCP tools; everything a human or crontab needs goes through `mem`. Where both surfaces exist for one operation, they are thin wrappers over the same `operations/` function (see [ARCHITECTURE.md §"Operations layer"](../ARCHITECTURE.md#operations-layer)). The contract is pinned mechanically by `tests/test_surface_contract.py` (schema↔dispatch wiring, doc-referenced subcommands, worker tool allowlists, inventory counts); `_DISPATCH` in `surfaces/cli/__init__.py` is grouped by the same audience labels.

Full inventory — 48 CLI subcommands × 17 MCP tools (audience: *agent* = MCP-only, *admin-cron* = CLI-only, *both* = paired surfaces; *agent-Bash* marks the four CLI carve-outs):

| Operation | CLI subcommand | MCP tool | Audience |
|---|---|---|---|
| Search (FTS / similar / hybrid) | `weave search` | `weave_search` | both (CLI = retrieval debug) |
| Budgeted context blob | `weave context` | `weave_context` | both (CLI = retrieval debug) |
| Graph walk (filter-dispatched) | `weave graph` | `weave_graph` | both (CLI = retrieval debug) |
| Read one note | `weave show` | `weave_read` | both (CLI = retrieval debug) |
| Timeline window | `weave timeline` | `weave_timeline` | both (CLI = retrieval debug) |
| Project snapshot | `weave project-snapshot` | `weave_project_snapshot` | both (CLI = retrieval debug) |
| Prompt / probe surfacing | `weave prompts` | `weave_prompts` | both (CLI = retrieval debug) |
| Create note | `weave add` | `weave_create` | both (CLI = headless flows) |
| Update note | `weave update` | `weave_update` | both (CLI = headless flows) |
| Add typed edge | `weave link` | `weave_link` | both (CLI = headless flows) |
| Remove typed edge | `weave unlink` | `weave_unlink` | both (CLI = headless flows) |
| Concept ops (action-dispatched) | `weave concepts` | `weave_concepts` | both (CLI = hygiene orchestration) |
| Session extraction | — | `weave_extract` | agent |
| Decision / prediction judging | `weave judge` | `weave_judge` | both — `--rejudge/--drain` is **agent-Bash** |
| Landing docs regeneration | `weave landing` | `weave_landing` | both — `--doc` is **agent-Bash** |
| Acquisition-queue inspection | `weave queue` | `weave_queue` | both |
| Source-type registry | `weave sources` | `weave_sources_config` | both |
| /wrap deterministic tail | `weave wrap-finalize` | — | **agent-Bash** |
| Hub backfill / linkage | `weave hubs` | — | admin-cron — `apply-linkage` is **agent-Bash** |
| Decision ledger lookup | `weave decisions` | — | admin-cron |
| Todo backlog | `weave backlog` | — | admin-cron |
| SQLite index rebuild | `weave index` | — | admin-cron |
| Importers (claude-code / chatgpt / …) | `weave import` | — | admin-cron |
| Vault health | `weave stats` | — | admin-cron |
| Coherence linter | `weave doctor` | — | admin-cron |
| Named workflow pipelines | `weave flow` | — | admin-cron |
| Host scheduler render | `weave schedule` | — | admin-cron |
| Hook install / status | `weave hooks` | — | admin-cron |
| Vault init | `weave init` | — | admin-cron |
| User config (vault path) | `weave config` | — | admin-cron |
| MCP server registration | `weave install` / `weave uninstall` | — | admin-cron |
| Hook pause toggle | `weave pause` / `weave resume` | — | admin-cron |
| MCP server entry point | `weave mcp` | — | admin-cron (infrastructure) |
| Drop-folder intake helpers | `weave intake` | — | admin-cron |
| Skill registry inspection | `weave skill` | — | admin-cron |
| Queue drain (consumer rail) | `weave drain` | — | admin-cron (orchestration) |
| Discovery strategies (producer rail) | `weave discover` | — | admin-cron (orchestration) |
| Dream scan / apply | `weave dream` | — | admin-cron (orchestration) |
| Themes registry rebuild | `weave themes` | — | admin-cron |
| Project registry | `weave project` | — | admin-cron |
| Orphan session pruning | `weave prune-orphans` | — | admin-cron |
| RLVR substrate export | `weave rlvr` | — | admin-cron |
| Loop trajectory outcome judge | `weave trajectory` | — | admin-cron (dream-outcome-worker rail) |
| Evidence-gated steering gate | `weave steering` | — | admin-cron (slow-loop #61 proposal gate) |

## Environment

- `THINKWEAVE_VAULT` — vault root (default `~/vault`). The legacy `PERSONAL_MEM_VAULT` is honored as a migration fallback.
- `THINKWEAVE_PROJECT` — default project name. The legacy `PERSONAL_MEM_PROJECT` is honored as a migration fallback.
- `THINKWEAVE_WEAVE_DIR` — relocate the derived-state directory (`index.db`, `embeddings.db`, `buffer/`, logs — everything normally under `vault_root/.weave`) to a different path, independent of `vault_root`. The vault markdown must stay wherever Obsidian points at it, but this directory is derived/rebuildable, so pointing it at fast local disk helps when the vault lives on slow, remote, or virtualized storage (a Windows drive crossed from WSL2, a NAS, a Dropbox mount). `~` is expanded; a relative path resolves against `vault_root`. Same knob is settable as a top-level `weave_dir` key in `vault/config/config.toml` (see the "User configuration layout" section of [ARCHITECTURE.md](../ARCHITECTURE.md)); this env var wins when both are set.
- `OPENAI_API_KEY` — required by embeddings (`weave index --embed`), the ChatGPT importer, and the hub batch backfill (`weave hubs link --via batch`).

After upgrading Thinkweave, re-run `weave hooks install` to pick up newly-added hooks (e.g. SessionStart).
