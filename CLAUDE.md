# personal_mem — Agent guide

## 1. What this is for an agent

personal_mem is an Obsidian-native memory layer: markdown is the source of truth, SQLite is a derived index. As an agent you do not crawl the vault filesystem — you query through the `mem_*` MCP tools (or `mem` CLI). Sessions, decisions, sources, themes, and concept hubs are all first-class notes connected by a shared concept ontology. Retrieve through the retrieval contract (§2); preserve session knowledge via `/mem-wrap` before clearing context. Architecture lives in `ARCHITECTURE.md` — this file is for you.

## 2. Retrieval contract

Three modalities, plus compositions on top.

- **FTS** — `mem_search(query, mode='fts')`. Keyword/phrase. Cheap. Empty `query` returns recent matches honouring filters (list mode).
- **Similarity** — `mem_search(query, mode='similar')`. Concept-shaped query, no keyword. Soft-fails to FTS when embeddings unavailable.
- **Hybrid** — `mem_search(query, mode='hybrid')`. Unsure → RRF fusion (k=60).
- **Graph** — `mem_graph(id, depth, filter=…)`. Structural walk over typed edges. Filter dispatches the variant: `''` (default — walk from `id`), `'source_lens'` (was `mem_source_lens`), `'decisions_for_file'` (was `mem_decisions_for_file`), `'concept_walk'` (was `mem_concept_search`). The legacy alias tools were deleted 2026-05-21 — call the canonical name.

Compositions:

- `mem_context(query, type=[…])` — FTS → similarity-via-concept → recency, deduped budget blob.
- `mem_project_snapshot(project)` — re-fetch the SessionStart context payload.
- `mem_timeline(project, days)` — chronological window of sessions + decisions.

All filters take `since` / `until` ISO dates; `mem_search` accepts `concepts=[…]` to combine text + concept; `mem_graph` accepts `note_type` / `project` projection.

| If you want to… | Use |
|---|---|
| Find X (keyword/phrase) | `mem_search` (`mode=fts`, fall back to `hybrid`) |
| Tell me about Y (budgeted blob) | `mem_context` |
| What touches Z (note id walk) | `mem_graph` |
| State of project P right now | `mem_project_snapshot` |
| What happened in window W | `mem_timeline` |

## 3. Lifecycles

**Session.** Hooks accumulate events + insights + commits + tests into a session note. Stop hook auto-extracts (thin: archive events as `events.jsonl`, mark `processed: true` + `auto_extracted: true`). `/mem-wrap` runs as a single inline pass: compose insights/decisions, call `mem_extract` once, then `mem wrap-finalize` (the deterministic tail — prune → index → judge → landing → drift, zero model turns). Two minor variants — *live* (in-session, conversation is the source) and *catch-up* (headless, e.g. cron `claude -p "/mem-wrap"`, working off `events.jsonl` + git). Self-decides what to record; never prompts. For non-code conversations, `mem_extract` auto-creates a session note.

**Concept.** Notes carry `concepts: [...]` (≥2 required). Notes sharing ≥1 concept auto-link (configurable via `concept_edge_threshold`, default 1). `vault/concepts/topics/{concept}.md` is the synthesis hub: `## Essence` (≤500w mental model) + `## Learning log` (append-only, every entry cites `[[note-id]]` with a flag — `new`/`agrees`/`contradicts`/`extends`). Backfill via `mem hubs run` (OpenAI Batches); incremental via `/update-hubs`. `/mem-resolve-concepts` is the periodic hygiene pass (merge near-dupes, prune dead vocabulary, update ontology). The shipped `ontology.yaml` is a minimal seed — concept namespaces and the domain hierarchy are user-chosen; the framework imposes nothing. Concepts populate as the vault grows.

**Theme.** `type: theme`, prefix `thm-`, lifecycle `candidate → active → dormant → resolved` / `merged-into:thm-X`. Canonical themes live at `vault/themes/{thm-XXXX}-{slug}.md` regardless of project; pre-canonical candidates live at `vault/themes/_candidates/{cand-XXXX}-{slug}.md` and never carry a `thm-` ID. Three sections: `## Essence`, `## Catalyst log` (same grammar as concept-hub log), `## Open questions`. Decisions implementing a theme carry `implements: [thm-XXXX]`. `/themes-resolve` is the periodic hygiene pass — also handles candidate promotion (`--promote`) and stale-candidate archival.

*Source-coupled theme floating:* whether a source type produces theme signals is controlled by the `temporal_grain` field on `SourceTypeSpec`. Event-shaped types (`substack`, `news`, `newsletter-events`, `youtube-events`, `podcast-events`) get `temporal_grain='event'` — `detect_signals` finds clusters in them (≥3 recent sources sharing ≥2 concepts, no covering theme). Concept-shaped types (`paper`, `repo`, `article`, `newsletter-concepts`, `youtube-concepts`, `podcast-concepts`) get `temporal_grain='concept'` — concept hubs handle them, no theme floating. Conversation-style intake gets `temporal_grain='none'` (no theme floating).

*Naming is agent-driven (not SDK).* As of 2026-05-25, the post-create hook in `VaultManager.create_note` keeps the SQLite index warm but no longer writes mechanical candidate stubs. `/dream`'s scan surfaces raw `theme_cluster_signals`; the apply phase composes a real kebab slug + 1-sentence essence per cluster (using the disambiguation test below) and mints canonical themes directly via the `theme_promotions_from_signal` plan key, skipping the `cand-*` intermediate. This mirrors how concepts work — LLM judgment lives in the agent turn, the MCP/Python layer just records. Pre-2026-05-25 mechanical-stub writes are still available via the explicit `mem themes scan-candidates` CLI for diagnostic sweeps, but no production path calls it.

*Per-source candidate (`proposed_theme:`), the structural analog of `proposed_concepts:`.* Event-grain workers' step 5a writes `proposed_theme: <slug>` on the source frontmatter when no active theme fits but the worker can name an arc — same register as `concepts: [foo]` vs `proposed_concepts: [foo]` for concepts. `aggregate_proposed_themes` tallies stamps per concept cluster; `detect_signals` attaches the top-voted slug as `voted_slug` on the surfaced signal. `/dream`'s apply phase prefers `voted_slug` (lex-first tie-break across distinct-vote slugs) over composing fresh, only inventing a name when no cluster source proposed one. Slug shape: 1–3 kebab words, label-like, no dates — same rule as the dream-composed case.

*Registry (`vault/.mem/themes.yaml`), the structural analog of `ontology.yaml`.* Single source of truth for the canonical thm-id set, kept in sync by `mint_theme_from_signal`, `promote_candidate`, and `/dream`'s `theme_status_changes` apply step (every mutation upserts; failures don't cascade). Per entry: `{id, slug, status, concepts, parent, project}`. Enables an O(1) `is_canonical` lookup at create time — `operations/notes.create_note` runs a soft validation gate on `relates_to: [thm-X]` refs, dropping unknown thm-ids with a warning (the gentle counterpart of the strict concept gate). Rebuild from markdown via `mem themes rebuild-registry` when the registry drifts.

*Slug-encodes-grain convention (newsletter pair):* when a source family needs both grains, name the variants by grain rather than by topic — `newsletter-events` / `newsletter-concepts`, not `newsletter-finance` / `newsletter-tech`. The grain is the only per-type behaviour that differs; topic is per-item via `concepts:` and `relates_to:`. The user pre-classifies their subscriptions by which mail label they file each newsletter under. One skill (`/newsletter`) and one writer (`research-newsletter-worker`) cover both — adding a third grain-variant later is a registry spec + a config block, zero skill code, because the skill discovers every `newsletter-*` source type from config.

**Prompt.** Captured by the `UserPromptSubmit` hook as a JSONL event (`{"type": "prompt", "text", "session_id", "ts", "cwd"}`) inside the active session's events buffer. `extract.extract_prompts` lifts them into `Prompt` dataclasses; `extract.classify_probe` applies a conservative heuristic (text ends with `?` / opens with a probe lead phrase, no follow-up Edit/Write within 3 events) to flag exploratory questions. Surfaced in STATE.md "Open Probes" and to `/discover` via the `mem_prompts` MCP tool. The legacy `probe` *tag* becomes a manual override only — the canonical signal is now the prompt event itself.

**Decision.** Four states forming the lifecycle `proposed → accepted → deprecated|superseded`.

| State | Trigger | Auto / manual | Git tie-in |
|---|---|---|---|
| `proposed` | `mem_create` or `mem_extract` with `outcome: abandoned\|partial` | Auto (default) | None |
| `accepted` | `mem_extract` over a session whose hooks captured commits (`outcome: committed`) | Auto | Yes — `commit_refs:` populated |
| `superseded` | New decision declares `supersedes: [dec-X]` in frontmatter, OR `mem_judge_and_writeback` maps a `superseded` verdict | Auto (frontmatter or judge writeback) | Inherited from triggering decision/judge run |
| `deprecated` | `mem_update(status="deprecated")` | Manual | None — deprecation is structural, not code-driven |

`mem_judge` is read-only — emits a verdict (`kept`/`superseded`/`reverted`/`unknown`) from structural evidence (commit/tests/re-edits). Never writes. The verdict-to-status writeback lives in `operations/decisions.py` (`mem_judge_and_writeback`): `kept→accepted`, `superseded→superseded`, `reverted→deprecated`, `unknown→no change`.

*Decisions without git tie-in, by design:* `/capture` or direct `mem_create` outside any session, non-code conversations (hooks never fired), `outcome: abandoned` (no code change expected), and decisions added retroactively to a session note's body. All four stay `proposed` with no `commit_refs:` and no judge verdict; promotion to `deprecated` remains manual.

*Predicted-outcome (RLVR substrate):* decisions can optionally carry a `predicted_outcome:` — a single prose sentence carrying BOTH a claim AND a manifestation pointer ("check X" / "look for Y" / "after Z"). Frontmatter shape: `predicted_outcome:` (the prose string), `prediction_history:` (append-only list of `{match, judged_at, reason}` entries), `prediction_match:` (denormalized tail entry's match, for cheap reads), `judged_at:` (denormalized tail entry's timestamp). Verdicts are five: `confirmed | contradicted | pending | unevaluable | stale` — `stale` means "was true at the time but no longer applies because the substrate moved on" (only emitted when the decision has been superseded or its pointer references something that no longer exists). The judge is the `/judge-prediction` Claude Code skill — the running session IS the judge, no API call. Three invocation paths: (1) **live**, piggybacked on `/mem-wrap` when a decision supersedes — the composer writes the verdict via `mem_update` inline; (2) **headless** via cron: `claude -p "/judge-prediction --drain"` drains `.mem/rejudge_queue.jsonl` + finds stale `pending` verdicts (cap 20/run); (3) **manual**: `mem judge --rejudge <dec-id>`. When a new decision declares `supersedes: [dec-X]`, the **immediate** predecessor is enqueued for re-judging — no cascade up the chain (the LLM can flag deeper concerns in its `reason`, but those require manual rejudge). New predictions land as `pending` (initialized by `wrap-finalize`); the skill takes over from there. Feeds `mem rlvr export`. Composed inline by `/mem-wrap` from session conversation; never prompted for.

**Source.** External content: `paper`, `repo`, `article`, `conversation`, `substack`, `news`, `newsletter-events`, `newsletter-concepts`, `youtube-events`, `youtube-concepts`, `podcast-events`, `podcast-concepts`, … Routed by `src/personal_mem/sources/registry.py` (`SourceTypeSpec`). Three layouts: `flat`, `folder`, `author_folder`. Per-source-type behaviour (queue path, drain strategy, dedup keys) is overridable in `vault/.mem/sources.yaml`.

*Acquisition spine — discover → drain over a per-type atomic unit.* Every source type lands on the same two-rail producer/consumer spine:

- **Discover** (`/discover` skill → `discover/strategies/*`) is the **producer rail**. It runs registered strategies that emit queue items. Two flavors: *internal-state* (`concept_coverage`, `decision_review`, `theme_drift`) observe the vault and propose what to ingest; *external-trigger* (`rss_poll`, `mail_poll`, `external_tool_runner`) observe the outside world and either enqueue directly (rss_poll) or emit a plan a skill executes against MCP (mail_poll → `/newsletter` runs the Gmail dance).
- **Drain** (`/drain --source-type X`) is the **consumer rail**. It peeks the queue, dispatches Path A (sequential `Skill` for paper/repo/article) or Path B (`Task` subagent fan-out for news/youtube-*/newsletter-*), validates worker outcomes, archives, runs post-batch hooks.
- **Atomic unit** — the per-source-type subskill / worker (`research-paper`, `research-repo`, `research-article`, `research-news-worker`, `research-youtube-worker`, `research-newsletter-worker`). One source item → one source note. Called by `/drain` over a queue, or invoked directly by `/research <url>` for a one-shot URL.
- **`/research <url>`** is the URL-classifier front door to the atomic unit — not a third rail, just a convenience for ad-hoc URLs.

`/substack` and `mem import {chatgpt|claude-mem}` legitimately skip discover: the user (or an external export) has already done the discovery step. `/news <url>` is a one-shot bypass too — no triage gate, same posture as `/research`.

*RSS-poll intake (news, youtube-*).* The `rss_poll` discover strategy is generic over source_type. News uses `feed_config: vault/.mem/news_feeds.yaml` (outlets-yaml-driven, per-outlet daily caps, `prefer_embedded` capture); youtube uses `channels: [UCxxx, ...]` (one feed per channel, `lookback_days` cutoff). Both flavors dedup against the active queue + recent archive (`Queue.dedup_check`) **plus** the SQLite indexer (URL already a `type: source` note) — the indexer guard covers re-emits months later that the 30-day archive lookback misses. Headless: `claude -p "/discover --strategy rss_poll --source-type news"` is the canonical cron invocation; the legacy `scripts/pull_news_feeds.py` is now a deprecation shim that delegates to the strategy.

*Mail-connector intake (newsletters).* Email newsletters land via a provider-agnostic mail connector — `gmail` today, `outlook` and `imap` are slots. The `mail_poll` discover strategy *composes* the per-type Gmail query (sender allowlist → `from:(s1 OR s2 OR ...)`, optional `mail_query` extras, `-label:<processed_label>` exclusion, `newer_than:Nd` lookback) and emits a `mail_fetch_needed` plan. The `/newsletter` skill *executes* the plan against Gmail MCP — fetch threads, enqueue, drain, then `label_thread` on every queue item archived `done`. Empty allowlist + empty `mail_query` is a deliberate halt in the strategy, not a whole-inbox fan-out. Three re-read guards stack: (1) **mail label** (primary — survives queue wipes); (2) **queue dedup** on `[message_id, url]`; (3) **`mem_search(message_id)` backstop** in the worker. The Gmail connector's first run is interactive OAuth; subsequent runs use the cached token (operationally headless-equivalent). Strict-headless cron use waits for the `imap` connector.

**Context-served (RLVR substrate).** Each session captures the notes served to it: a single `type: startup` event at SessionStart (notes in the SessionStart payload + `token_est`) plus one `type: retrieval` event per MCP retrieval call during the session (`mem_search`, `mem_context`, `mem_graph`, `mem_read`, `mem_timeline`, `mem_project_snapshot`). Buffered to the same per-session JSONL as action events; `archive_buffer` (Stop time / `mem_extract`) splits them into sibling `events.jsonl` and `retrieval_log.jsonl`. The Indexer projects every session's `retrieval_log.jsonl` into `context_served(session_id, note_id, source ∈ {startup, onthefly}, ts)` — rebuildable from markdown. The RLVR row's `context.cited_onthefly_ids` / `cited_startup_only_ids` come from intersecting decision body wikilinks against this table; a note served both via startup and on-the-fly counts as onthefly (the stronger signal).

## 4. Concepts vs tags vs themes

| Field | Role | Examples | Authority |
|---|---|---|---|
| `concepts` | Domain-specific technical vocabulary, drives graph edges | `write-ahead-log`, `fts5`, `recursive-cte` | `ontology.yaml` (canonical) + `concept_aliases.yaml` (aliases) |
| `tags` | Broad filtering categories | `debugging`, `todo`, `til`, `parked`, `probe` | `tag_vocabulary:` in `ontology.yaml` |
| `themes` | Global temporal narratives (`thm-XXXX`) | `risk-on-regime-2026`, `swe-refactor-arc` | `vault/themes/` |

Do not duplicate between `concepts` and `tags`. Run `mem doctor` to surface tag/concept overlap, unknown tags, dead vocabulary.

*Connectivity:* concepts drive graph edges (notes sharing ≥`concept_edge_threshold` concepts auto-link, default 1). Tags also produce `relates_to` edges but intentionally lightly — threshold 2 shared tags, with `todo`/`probe`/`parked`/`til` excluded and any tag covering >10% of notes capped out. Tags are *filter facets*, not graph substrate; if "tag connectivity feels light" — that's the design.

### Concept hub vs theme hub

Both hubs share a spine — `## Essence` (≤500w) plus an append-only `## Catalyst log` with the same flag grammar (`new` / `agrees` / `contradicts` / `extends`). The shared parse/render lives in `synthesis/hub.py`. They differ on identity, lifecycle, and how notes cite them.

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

**Auto-floated candidates, never auto-canonical themes.** Event-grain source types may produce candidate stubs at `vault/themes/_candidates/`, but candidates carry no `thm-` ID and don't show up in THEMES.md until `/themes-resolve --promote <cand-id>` mints one explicitly. The disambiguation test still gates promotion: a candidate that fails it (named like a capability/technique, no time horizon, no narrative arc) gets archived instead.

## 5. Skills

Generated from `commands/*.md` frontmatter. Re-run `mem skill list` to regenerate.

| Skill | owns_mechanic | source_type | capabilities | Purpose |
|---|---|---|---|---|
| `/mem-wrap` | session_extraction | — | — | Inline-compose insights/decisions → `mem_extract` → `mem wrap-finalize` deterministic tail (prune → index → judge → landing → drift). Live + catch-up (headless) modes. |
| `/mem-resolve-concepts` | ontology_hygiene | — | — | Concept and ontology hygiene |
| `/themes-resolve` | theme_synthesis | — | — | Theme dedup, status changes, essence rewrites |
| `/ingest` | input_routing | * | import | Universal input router — URL / file / text / structured-id → dispatch to specialist skill. |
| `/capture` | text_capture | — | import | Inline-text ingestion (snippet, quote, fragment) → mem_create. |
| `/ingest-paper-file` | paper_file_ingest | paper | import | Local PDF paper → text extraction → mem_create as paper. |
| `/research` | url_routing | paper, repo, article, news | import | Atomic-unit front door for one URL — classifies via `url_patterns` and dispatches to `research-paper/-repo/-article` (or `/news` for news outlets). Not a rail; the same atomic units run inside `/drain`. |
| `/drain` | queue_drain | — | acquire | Consumer rail. Drains a per-source-type acquisition queue; Path A (sequential) for paper/repo/article, Path B (subagent fan-out) for news/youtube-*/newsletter-*/podcast-*. |
| `/discover` | research_discovery | * | discover | Producer rail. Runs registered strategies — internal-state (`concept_coverage`, `decision_review`, `theme_drift`) and external-trigger (`rss_poll`, `mail_poll`, `external_tool_runner`). |
| `/substack` | substack_inbox | substack | acquire | Drain Substack disk inbox (no queue — user clipped post is itself the discovery step). |
| `/newsletter` | newsletter_inbox | newsletter-events, newsletter-concepts | acquire | Orchestrator: Gmail auth → `mail_poll` plan from discover → fetch threads → enqueue → `/drain` → apply `processed_label`. |
| `/youtube` | youtube_inbox | youtube-events, youtube-concepts | acquire | Orchestrator: `rss_poll` from discover → `/drain`. Headless-safe (no OAuth). |
| `/podcast` | podcast_inbox | podcast-events, podcast-concepts | acquire | Orchestrator: `rss_poll` from discover (per-show RSS, picks `<enclosure>` audio URL) → `/drain` (workers hand MP3 to Gemini Flash via Files API). Headless-safe. |
| `/news` | news_url_ingest | news | import | One-off news URL ingest. No triage gate — atomic-unit dispatch (same posture as `/research <url>` for paper/repo/article). |
| `/update-hubs` | concept_hubs | — | — | Concept-hub sync — incremental (default) or bulk (`--bulk [inline\|batch]`). |
| `/onboard` | project_bootstrap | — | bootstrap | First-run flow: mandatory historical Claude Code import (always step 1), ontology bootstrap from imported `proposed_concepts:`, focus + source-type configuration, per-project hooks, first landing docs. Idempotent — re-running only does what's still missing. **Not** for vault init (`mem init`) or machine setup (`mem install`). |
| `/source-fit` | source_diagnosis | — | — | Read-only: classify a free-form input description against existing source types. Returns covered / adapt / scaffold. Vault-scope. |
| `/source-scaffold` | source_scaffold | — | — | Generative: create a new source type via vault overlay + machine-global skill file (`~/.claude/commands/<slug>.md`). Vault-scope. |

## 6. Operational rules

- **No filesystem crawls.** Never `find`/`ls`/`grep` the vault from a Bash tool. Use the SessionStart context (already in your conversation), MCP tools, or a single `Read` of a known file path.
- **One MCP call per question.** Pick the modality from §2; don't fan out unless the first call is genuinely insufficient.
- **Pre-`/clear`: run `/mem-wrap`.** There is no clear hook; this is the only way to preserve mid-session knowledge.
- **`/mem-wrap` is zero-API; latency is two model turns + one Bash call.** `mem_extract` is pure Python; the whole deterministic tail (prune → index → judge → landing → drift) is one Bash call (`mem wrap-finalize`) with no model turns. The only LLM cost is composing the insights/decisions inline and the wrap-up report. (An older revision spawned a Sonnet extraction subagent — reversed after measurement: spawn + verification overhead exceeded the per-turn savings for ≤5 notes. The `wrap-finalize` half of that redesign stayed; the subagent half didn't.)
- **Concepts mandatory.** Every note created via `mem_extract` must carry ≥2 concepts. Load existing labels via `mem_concepts` before assigning. Prefer specific terms (`ml/deep-learning` over `deep-learning`).
- **Strict ontology gating.** Only ontology-listed terms may go in `concepts:`. Any new term goes in `proposed_concepts:`. The strict gate is server-enforced — `mem_extract`, `mem_create`, and the importers all run incoming concept lists through the merged ontology and shunt non-matches to `proposed_concepts:` automatically. Promotion (proposed → canonical) is `/mem-resolve-concepts`'s job, triggered when a proposed term reaches critical mass (default `count ≥ 5`). You don't pre-canonicalise; you just attach concepts and let the gate sort them.
- **Auto-todo only on request.** Never tag `todo` unless the user explicitly asks.

## 7. CLI reference (Bash)

The CLI exposes **35 subcommands** total via `_DISPATCH` in `surfaces/cli/__init__.py`. Agents work primarily through MCP tools (see below); the CLI is for setup, admin, and the small set of operations without MCP parity.

Consolidations to keep in mind: wikilink materialisation lives under
`mem index --materialize-links` (was `mem connect`, deleted 2026-05-21);
the `mem_concepts*` MCP tools are folded into `mem_concepts(action=...)`;
`mem_source_lens` + `mem_decisions_for_file` are folded into
`mem_graph(filter=...)`. The Phase-4-C deprecation aliases for both CLI
and MCP names were removed 2026-05-21 — call the canonical names.

```
mem init                                    # initialize vault + .mem/sources.yaml
mem add --type {note|theme|...} "Title"     # create a note
mem index [--full] [--embed] [--only-new|--since DATE] [--materialize-links]
                                            # rebuild SQLite index (+ wikilinks).
                                            # --embed --only-new is the keep-warm
                                            # cron path: re-embed only notes whose
                                            # updated_at > last cached embedding.
mem search "q" [--type X] [--concept Y]     # FTS / similarity / hybrid
mem graph <id>                              # local graph
mem context "q" [--type X]                  # 3-layer retrieval (FTS → concept → recency)
mem stats                                   # vault health
mem doctor [--migrate]                      # coherence linter (+ optional data migrations)
mem backlog [--project X]                   # todo notes + active queue items
mem decisions [--file <path>] [--project X] # decision ledger lookup
mem project {list|show|set-active}          # project registry on the vault
mem concepts {list|merge|hubs|drift|notes|prune}
mem hubs {status|plan|link|repair}          # concept-hub backfill (use `mem drain --target hubs` to execute)
mem themes {list|scan-candidates|archive-stale-candidates|promote-candidate}
mem drain --target hubs --via {inline|batch}  # batch path replaces `mem hubs run`
mem queue {list|inspect|peek}               # per-source-type acquisition queues
mem hooks {install|uninstall|status}
mem landing [--project X] [--doc all]       # regenerate DECISIONS/BACKLOG/STATE/THEMES
mem flow {list|show|run}                    # named workflow pipelines
mem skill {list|show <name>}                # inspect commands/*.md frontmatter
mem sources {list|show <slug>}              # inspect source-type registry
mem prune-orphans [--yes]                   # delete abandoned session folders (used by /mem-wrap)
mem wrap-finalize <ses-id> [--project X]    # deterministic tail of /mem-wrap: prune→index→judge→landing→drift (--json for headless)
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

**Agents shouldn't run** `mem doctor`, `mem stats`, `mem flow`, `mem intake`,
`mem enrich`, `mem import`, `mem prune-orphans`, `mem install`, `mem mcp`,
`mem init`, `mem hooks` directly — they belong in cron flows or interactive
admin. There is no MCP parity for these subcommands.

### MCP tool surface

The MCP server exposes 18 tools:

`mem_search`, `mem_create`, `mem_read`, `mem_update`, `mem_link`, `mem_unlink`,
`mem_context`, `mem_graph` (filter-dispatched), `mem_concepts` (action-dispatched),
`mem_extract`, `mem_judge`, `mem_landing`, `mem_enrich`, `mem_timeline`,
`mem_project_snapshot`, `mem_queue`, `mem_sources_config`, `mem_prompts`.

## Environment

- `PERSONAL_MEM_VAULT` — vault root (default `~/vault`)
- `PERSONAL_MEM_PROJECT` — default project name
- `OPENAI_API_KEY` — required by `mem enrich`, ChatGPT importer, embeddings, `mem hubs run`

After upgrading personal_mem, re-run `mem hooks install` to pick up newly-added hooks (e.g. SessionStart).
