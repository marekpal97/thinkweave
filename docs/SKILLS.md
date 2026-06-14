# Thinkweave — Skills & subagent workers

The full catalog of Claude Code skills (`commands/*.md`), the subagent-worker roster (`agents/*.md`), and the dual-route (`--via inline|batch`) convention. The thin operating guide is [CLAUDE.md](../CLAUDE.md); structural narrative is [ARCHITECTURE.md](../ARCHITECTURE.md).

## Table of contents

- [Skills catalog](#skills-catalog)
- [Dual-route convention](#dual-route-convention)
- [Subagent workers](#subagent-workers)

## Skills catalog

Generated from `commands/*.md` frontmatter. Re-run `weave skill list` to regenerate.

| Skill | owns_mechanic | source_type | capabilities | Purpose |
|---|---|---|---|---|
| `/weave-wrap` | session_extraction | — | — | Inline-compose insights/decisions → `weave_extract` → `weave wrap-finalize` deterministic tail (prune → index → judge → landing → drift). Live mode interactively; catch-up mode is invoked nightly by `dream-wrap-worker` (no separate cron). |
| `/dream` | vault_synthesis | — | — | Two-phase subagent orchestrator. **Phase 1** (synthesis) fans out 5 workers in parallel — `dream-{promotion,merge,theme,essence,priority}-worker` — merges plan fragments, runs `weave dream apply` (one index rebuild, one maintenance.jsonl line). **Phase 2** (composition + consumption) fans out 5 workers in dependency waves — `dream-wrap-worker` (catch-up unwrapped sessions) + `dream-judge-worker` (drain rejudge queue) + `dream-seam-link-worker` (stitch cross-parent linkage on freshly-folded hubs via `weave hubs apply-linkage`) + `dream-seam-worker` (reconcile the CC-auto-memory↔vault **memory seam** — judge each dirty CC fact's vault twin, write the durable map via `weave seam commit`) in parallel, then `dream-digest-worker` (compose vault-global `type: digest` notes at `vault/digests/YYYY-MM-DD-<grain>.md`, one per non-empty grain). One cron entry replaces three (`/dream`, `/weave-wrap` catch-up, `/judge-prediction --drain`). Headless-safe; never prompts. |
| `/tighten` | ontology_hygiene | — | — | On-demand ontology-tightening front door for BOTH hub families (concepts + themes): review drift-v2 dedup pairs AND N-ary grain-coarsening clusters in one approval table, then run the per-family structural tails (promotion, dead-vocab prune, essence refresh, catalyst-text/title backfill). The nightly `/dream` runs the same mechanism unattended via `dream-{promotion,merge,essence}-worker`. Replaced the split `/mem-resolve-concepts` + `/themes-resolve` skills (2026-06-13). |
| `/ingest` | input_routing | * | import | Universal input router — URL / file / text / structured-id → dispatch to specialist skill. |
| `/capture` | text_capture | — | import | Inline-text ingestion (snippet, quote, fragment) → weave_create. |
| `/ingest-paper-file` | paper_file_ingest | paper | import | Local PDF paper → text extraction → weave_create as paper. |
| `/research` | url_routing | paper, repo, article, news | import | Atomic-unit front door for one URL — classifies via `url_patterns` and dispatches to `research-paper/-repo/-article` (or `/news` for news outlets). Not a rail; the same atomic units run inside `/drain`. |
| `/drain` | queue_drain | — | acquire | Consumer rail. Drains a per-source-type acquisition queue; Path A (sequential) for paper/repo/article, Path B (subagent fan-out) for news/youtube-*/newsletter-*/podcast-*. |
| `/discover` | research_discovery | * | discover | Producer rail. Runs registered strategies — internal-state (`decision_review`, `prompt_gap`) and external-trigger (`rss_poll`, `mail_poll`, `external_tool_runner`). |
| `/substack` | substack_inbox | substack | acquire | Drain Substack disk inbox (no queue — user clipped post is itself the discovery step). |
| `/newsletter` | newsletter_inbox | newsletter-events, newsletter-concepts | acquire | Orchestrator: Gmail auth → `mail_poll` plan from discover → fetch threads → enqueue → `/drain` → apply `processed_label`. |
| `/youtube` | youtube_inbox | youtube-events, youtube-concepts | acquire | Orchestrator: `rss_poll` from discover → `/drain`. Headless-safe (no OAuth). |
| `/podcast` | podcast_inbox | podcast-events, podcast-concepts | acquire | Orchestrator: `rss_poll` from discover (per-show RSS, picks `<enclosure>` audio URL) → `/drain` (workers hand MP3 to Gemini Flash via Files API). Headless-safe. |
| `/news` | news_url_ingest | news | import | One-off news URL ingest. No triage gate — atomic-unit dispatch (same posture as `/research <url>` for paper/repo/article). |
| `/update-hubs` | concept_hubs | — | — | Concept-hub sync — incremental (default) or bulk (`--bulk [inline\|batch]`). |
| `/onboard` | project_bootstrap | — | bootstrap | First-run flow: mandatory historical Claude Code import (always step 1), ontology bootstrap from imported `proposed_concepts:`, focus + source-type configuration, per-project hooks, first landing docs. Idempotent — re-running only does what's still missing. **Not** for vault init (`weave init`) or machine setup (`weave install`). |
| `/source-fit` | source_diagnosis | — | — | Read-only: classify a free-form input description against existing source types. Returns covered / adapt / scaffold. Vault-scope. |
| `/source-scaffold` | source_scaffold | — | — | Generative: create a new source type via vault overlay + machine-global skill file (`~/.claude/commands/<slug>.md`). Vault-scope. |
| `/enrich-notes` | concept_enrichment_inline | — | — | Inline LLM concept enrichment via the running model. The `weave enrich --via inline` route; pairs with `--via batch` (wrapper async fan-out). Picked automatically by `choose_route()` when no key OR ≤200 candidates. |
| `/import-chatgpt` | chatgpt_import_inline | — | import | Inline ChatGPT-export import. The `weave import chatgpt --via inline` route; same role as `/enrich-notes` for ChatGPT data exports. |
| `/hubs-link` | hubs_linkage_inline | — | — | Inline temporal-DAG linkage for concept hubs. The `weave hubs link --via inline` route; pairs with `--via batch`. |
| `/judge-prediction` | prediction_judging | — | — | Predicted-outcome judge — the running session IS the judge (no API call). Invoked live by `/weave-wrap` on supersession, headlessly by `dream-judge-worker`, or manually via `weave judge --rejudge`. See [Lifecycles §Decision](LIFECYCLES.md#predicted-outcome-rlvr-substrate). |

## Dual-route convention

Four CLI subcommands take `--via {inline,batch}`: `weave import claude-code --enrich`, `weave import chatgpt`, `weave enrich`, `weave hubs link`. `inline` = the corresponding CC skill above runs the work via the running model (no provider key required); `batch` = the wrapper (`core/agent_client.batch_completions_sync`) fans out N async completions to the configured provider (provider+model from `vault/config/api.yaml::overrides.<op>`). When `--via` is omitted, `operations/_backfill_route.choose_route` picks: explicit flag > size threshold + key presence > inline.

| CLI subcommand | inline route (CC skill) | batch route (wrapper fan-out) |
|---|---|---|
| `weave enrich` | `/enrich-notes` | `core/agent_client.batch_completions_sync` |
| `weave import chatgpt` | `/import-chatgpt` | wrapper async fan-out |
| `weave hubs link` | `/hubs-link` | wrapper async fan-out |
| `weave import claude-code --enrich` | `/enrich-notes` | wrapper async fan-out |

## Subagent workers

The subagent workers live in `agents/*.md` (one `.claude/agents/<worker>.md` file each). They are not user-facing `/` skills — orchestrators (`/dream`, `/drain`) fan them out via the `Task` tool, each emitting a strict JSON outcome line. New workers plug in via one `DreamTaskSpec` registry entry plus one agent file — see [ARCHITECTURE.md §"Dream orchestrator"](../ARCHITECTURE.md#dream-orchestrator-two-phase-mirrors-drain).

### Dream phase-1 workers (synthesis — emit plan fragments)

| Worker | What it judges |
|---|---|
| `dream-promotion-worker` | Proposed-concept promotions (count ≥ `dream.promotion_threshold`). |
| `dream-merge-worker` | Concept drift pairs, theme dup candidates, AND N-ary grain-coarsening clusters (drift v2, cosine-evidenced). |
| `dream-theme-worker` | Theme mint/extend from cluster signals + distills catalyst entries for theme log gaps. |
| `dream-essence-worker` | Whether hub essences (themes AND concept hubs) need composing or rewriting. |
| `dream-priority-worker` | Priority signals from recent probe pressure. |

### Dream phase-2 workers (composition + consumption — write directly, emit side-effects)

| Worker | What it does |
|---|---|
| `dream-wrap-worker` | Catch up unwrapped sessions (subsumes the standalone `/weave-wrap` cron). |
| `dream-judge-worker` | Drain the prediction rejudge queue (writes `prediction_match` only; never touches decision status). |
| `dream-seam-link-worker` | Drain the seam-link queue — judge cross-parent catalyst pairs on freshly-folded hubs, write ref-dates via `weave hubs apply-linkage`. |
| `dream-seam-worker` | Reconcile Claude Code auto-memory against the vault — resolve each dirty CC fact's twin, judge confirmed-fresh/stale/diverged/durable-unique, write the durable map via `weave seam commit`. |
| `dream-digest-worker` | Compose grain-split daily knowledge-first digest notes (one per non-empty grain). |

### Research workers (acquisition — `/drain` Path B fan-out)

| Worker | Source pipeline |
|---|---|
| `research-news-worker` | News brief from a single queue item (admission decided upstream by the Haiku triage helper). |
| `research-newsletter-worker` | Brief from a single email-newsletter queue item (admission settled upstream by curated mail labels). |
| `research-youtube-worker` | Brief from a single YouTube queue item — pulls captions via `youtube-transcript-api` (no LLM on the live path). |
| `research-podcast-worker` | Brief from a single podcast queue item — downloads the audio enclosure, hands it to Gemini Flash via the Files API for transcription + summary. |

> Path A research subskills (`research-paper`, `research-repo`, `research-article`) are sequential skills, not `Task` subagents — they live under `commands/research/` and are invoked by `/research` or `/drain` directly.

### Triage

| Worker | What it does |
|---|---|
| `news-triage-worker` | Stage-1 triage for the news pipeline — classifies a batch of news items against the active-themes catalog (`vault/THEMES.md`), returns per-index verdicts (`keep`/`keep_unfiled`/`drop`) + `theme_id` + reason. Cheap Haiku call invoked by `/drain --source-type news` before the writer fan-out. |
