# Thinkweave — Lifecycles

The deep-dive reference for every first-class lifecycle in Thinkweave: session, concept, theme, decision, source, prompt, context-served. The thin operating rules live in [CLAUDE.md](../CLAUDE.md); the structural narrative lives in [ARCHITECTURE.md](../ARCHITECTURE.md). This file preserves the full mechanics — flag grammars, state-transition tables, the drift-v2 doctrine, the seam-link invariant, the RLVR substrate.

## Table of contents

- [Session](#session)
- [Concept](#concept)
- [Theme](#theme)
- [Prompt](#prompt)
- [Decision](#decision)
- [Source](#source)
- [Context-served (RLVR substrate)](#context-served-rlvr-substrate)

## Session

Hooks accumulate events + insights + commits + tests into a session note. The Stop hook auto-extracts (thin: archive events as `events.jsonl`, mark `processed: true` + `auto_extracted: true`). `/wrap` runs as a single inline pass: compose insights/decisions, call `weave_extract` once, then `weave wrap-finalize` (the deterministic tail — prune → index → judge → landing → drift, zero model turns). Two minor variants — *live* (in-session, conversation is the source) and *catch-up* (headless, working off `events.jsonl` + git). The *catch-up* variant is invoked nightly by `dream-wrap-worker` (phase 2 of `/dream`) for any session that lacks `processed: true` and has a non-empty `events.jsonl` — there is no separate `/wrap` catch-up cron entry. Self-decides what to record; never prompts. For non-code conversations, `weave_extract` auto-creates a session note.

## Concept

Notes carry `concepts: [...]` (≥2 required). Notes sharing ≥1 concept auto-link (configurable via `concept_edge_threshold`, default 1). `vault/concepts/topics/{concept}.md` is the synthesis hub: `## Essence` (≤500w mental model) + `## Learning log` (append-only, every entry cites `[[note-id]]` with a flag — `new`/`agrees`/`contradicts`/`extends`). Backfill via `weave drain --target hubs` (batch path; the old `weave hubs run`); incremental via `/update-hubs`. Routine dedup runs nightly inside `/dream` (drift v2 — see below); `/tighten` is the on-demand front door over the same helpers (plus the ontology restructuring dream doesn't touch). The shipped `ontology.yaml` is a minimal seed — concept namespaces and the domain hierarchy are user-chosen; the framework imposes nothing. Concepts populate as the vault grows.

### Drift v2 (2026-06-11) — geometry-guarded dedup for BOTH hub families

The dream scan unions string near-dupes with **centroid-cosine pairs** (`synthesis/geometry.py`: a concept's vector is the mean of its notes' cached embeddings; themes use their own note embedding) at `dream.cosine_threshold` (default 0.8), ships each surviving pair with an evidence packet (cosine, ontology domains, `same_domain`, counts, co-occurrence, sample titles), ranks by cosine, and caps at `dream.drift_cap`.

**Verdict memory lives in the maintenance log** — apply records applied `merges`/`theme_merges` and the worker's `distinct_pairs` rulings in the cycle line's `verdicts` block; `geometry.judged_pairs` reads them back so the pool drains (`weave dream scan --rejudge-pairs` re-opens).

**Merges fold, never delete**: the losing hub's catalyst log is interleaved into the winner (`fold_hub_logs` — citation dedup, `fold_pending_*` provenance stamps, essence stash + `essence_updated` clear routes reconciliation to the essence worker), and the loser is archived (`topics/_archive/` with `merged-into:`) or status-tombstoned (themes).

**Seam-link invariant: entries never change hubs without a seam-link pass** — every fold enqueues the winner on `.mem/seam_link_queue.jsonl`; the phase-2 `dream-seam-link-worker` judges cross-parent entry pairs only (fold dates × the rest, `/hubs-link` rubric) and writes through `weave hubs apply-linkage` (validated by `validate_linkage_revision`; `--clear-fold` clears the stamps and retires the queue item atomically).

## Theme

`type: theme`, prefix `thm-`, statuses `active → dormant → resolved` / `merged-into:thm-X` — **dormant/resolved change by hand only** (the 2026-05-30 teardown dropped all automatic lifecycle). The automatic mutations are mint, extend, and — since the 2026-06-11 drift-v2 doctrine — **dedup-merge**: `/dream` surfaces `theme_dup_candidates` (embedding cosine over the themes' cached vectors), the merge worker rules merge/distinct, and apply runs `merge_theme_into` (catalyst-log fold + `relates_to` repoint + `merged-into:` tombstone with the file kept on disk + registry upsert + seam-link enqueue). Distinct rulings are recorded in the maintenance-log `verdicts` block so pairs are never re-litigated. Canonical themes live at `vault/themes/{thm-XXXX}-{slug}.md` regardless of project. Three sections: `## Essence`, `## Catalyst log` (same grammar as concept-hub log), `## Open questions`. Decisions implementing a theme carry `implements: [thm-XXXX]`. `/tighten` is the periodic hygiene pass — merge near-duplicate themes and rewrite stale essences (no candidate promotion, no dormancy detection).

### Source-coupled theme floating

Whether a source type produces theme signals is controlled by the `temporal_grain` field on `SourceTypeSpec`. Event-shaped types (`substack`, `news`, `newsletter-events`, `youtube-events`, `podcast-events`) get `temporal_grain='event'` — `detect_signals` clusters their recent sources, **primarily by the `proposed_theme:` stamp** (name-family clusters, ≥2 sources, with near-variant slugs folded by token overlap), and only falls back to concept clustering for *unstamped* sources (≥3 sharing ≥2 concepts). Sources already filed to a theme (`relates_to: thm-…`) are excluded. Concept-shaped types (`paper`, `repo`, `article`, `newsletter-concepts`, `youtube-concepts`, `podcast-concepts`) get `temporal_grain='concept'` — concept hubs handle them, no theme floating. Conversation-style intake gets `temporal_grain='none'`.

### Naming is agent-driven (not SDK)

The post-create hook in `VaultManager.create_note` keeps the SQLite index warm but writes no theme stubs. `/dream`'s scan surfaces `theme_cluster_signals` — each carries `cluster_kind` (`name`/`concept`), a `label`, the folded `proposed_names`/`related_names`, and `covering_themes` ranked by label↔slug token match + IDF-weighted concept overlap. The apply phase mints a canonical theme via the `theme_mints` plan key (worker-composed `title` + `essence` — empty essences are rejected) or links new sources to an existing one via `theme_extensions`; both carry worker-distilled per-source `catalysts` entries that become the log lines (no more generic "extend"/"cluster seed" text). The scan also emits `theme_log_gaps` — sources filed directly to a theme (`relates_to:` stamped at create time, e.g. news triage `keep`) that never got a catalyst entry; the theme worker distills them into ordinary extensions. This mirrors how concepts work — LLM judgment lives in the agent turn, the MCP/Python layer just records.

### Per-source candidate (`proposed_theme:`)

The structural analog of `proposed_concepts:`. When no active theme fits, event-grain workers **default to** stamping `proposed_theme: <slug>` on the source frontmatter — same register as `concepts: [foo]` vs `proposed_concepts: [foo]` for concepts. `detect_signals` groups these stamps into arc families (a token-Jaccard merge folds near-variant slugs, e.g. the `iran-*` family) and surfaces each as a signal carrying a `label` (the most-supported variant) and `related_names` (the rest, with distinct-source counts). `/dream`'s apply phase uses `label` as the slug directly, only composing fresh for an unstamped concept cluster. Slug shape: 1–3 kebab words, label-like, no dates.

### Registry (`vault/config/themes.yaml`)

The structural analog of `ontology.yaml`. Single source of truth for the canonical thm-id set, kept in sync by `mint_theme_from_signal` (each mint upserts a row; failures don't cascade). Per entry: `{id, slug, status, concepts, parent, project}`. Enables an O(1) `is_canonical` lookup at create time — `operations/notes.create_note` runs a soft validation gate on `relates_to: [thm-X]` refs, dropping unknown thm-ids with a warning (the gentle counterpart of the strict concept gate). Rebuild from markdown via `weave themes rebuild-registry` when the registry drifts.

### Slug-encodes-grain convention (newsletter pair)

When a source family needs both grains, name the variants by grain rather than by topic — `newsletter-events` / `newsletter-concepts`, not `newsletter-finance` / `newsletter-tech`. The grain is the only per-type behaviour that differs; topic is per-item via `concepts:` and `relates_to:`. The user pre-classifies their subscriptions by which mail label they file each newsletter under. One skill (`/newsletter`) and one writer (`research-newsletter-worker`) cover both — adding a third grain-variant later is a registry spec + a config block, zero skill code, because the skill discovers every `newsletter-*` source type from config.

### Concept vs theme: the disambiguation test

- **Concept** = invariant vocabulary term identifying a *category*, *capability*, or *mechanism* (e.g. `finance/regime`, `mcp/server-config`, `retrieval/hybrid`). Ontology-grade. Doesn't have a story arc. Lives forever.
- **Theme** = narrative arc identifying an *unfolding event* (e.g. `thm-aaaa1111: AI capex unwind 2026`). Has beginning/middle/end. Always cites ≥1 concept.

The disambiguation test for an LLM agent:

- "X capability" / "X technique" / "X area of work" → concept
- "X event" / "X period" / "X transition" / "X campaign" → theme
- If the candidate name has a year, a quarter, or "rollout/unwind/launch/pivot" — it's a theme.
- If you cannot picture an `## Essence` paragraph that wouldn't change in 5 years — it's a theme.

**Auto-floated arcs, judgment-gated themes.** Event-grain sources stamp `proposed_theme:` slugs; `/dream` clusters these into arc signals but mints a canonical `thm-` theme only when its apply turn judges the arc real. The disambiguation test gates the mint: a cluster named like a capability/technique (no time horizon, no narrative arc) is skipped, not minted. There are no `cand-*` stubs — the LLM turn is the gate.

See [ARCHITECTURE.md §"Themes vs concept hubs"](../ARCHITECTURE.md#themes-vs-concept-hubs) for the hub-spine structural view (shared `synthesis/hub.py`, the temporal DAG renderer).

## Prompt

Captured by the `UserPromptSubmit` hook as a JSONL event (`{"type": "prompt", "text", "session_id", "ts", "cwd"}`) inside the active session's events buffer. `extract.extract_prompts` lifts them into `Prompt` dataclasses; `extract.classify_probe` applies a conservative heuristic (text ends with `?` / opens with a probe lead phrase, no follow-up Edit/Write within 3 events) to flag exploratory questions. Surfaced in STATE.md "Open Probes" and to `/discover` via the `weave_prompts` MCP tool. The legacy `probe` *tag* becomes a manual override only — the canonical signal is now the prompt event itself.

Probe **texts** are first-class on the acquisition rails (not just count-reduced concept pressure): `/dream`'s scan carries `recent_probes` as `{concept: {count, probes}}`, the priority worker copies the texts into `queue_item.probes`, `focus_research` descriptors carry `probe_texts` — and `/drain` resolves URL-less priority items by a two-step search (concept picks the lane, probe texts aim the query). The Indexer also projects archived prompt events into SQL — `prompts(session_id, seq, ts, text, classification, project)` + `prompt_concepts(session_id, seq, concept)` (probe rows only, attributed via the shared `core.events.match_probe_concepts` substring rule) — the join substrate tying probes to `note_concepts` / themes / `hub_log_entries` timelines. Live consumers (`recent_probe_details`, `weave_prompts`) intentionally stay on the JSONL walk (covers active buffers + not-yet-indexed sessions); the table is for joins/analytics and rebuilds via `weave index --full`.

## Decision

Four states forming the lifecycle `proposed → accepted → deprecated|superseded`.

| State | Trigger | Auto / manual | Git tie-in |
|---|---|---|---|
| `proposed` | `weave_create`, `weave_extract` with `outcome: abandoned\|partial`, OR `weave_extract` with `outcome: committed` where no session commit matches `file_paths` (B8 tighten 2026-05-29) | Auto (default) | None — the `committed: bool` field still records the user-asserted classification |
| `accepted` | `weave_extract` over a session whose hooks captured commits (`outcome: committed`) AND at least one commit's files intersect the decision's `file_paths` | Auto | **Required** — `commit_refs:` always populated (load-bearing post-B8) |
| `superseded` | `weave_judge_and_writeback` maps a `superseded` verdict (blame survival: the predecessor's committed lines were replaced). A new decision's `supersedes: [dec-X]` declaration is only a re-judge *trigger* — it enqueues dec-X but never flips status on its own (evidence-gated 2026-06-13) | Auto (judge writeback) | **Required** — the flip rests on git-blame evidence, run in `wrap-finalize` + `dream apply` |
| `deprecated` | `weave_update(status="deprecated")` | Manual | None — deprecation is structural, not code-driven |

`weave_judge` is read-only — emits a verdict (`kept`/`superseded`/`reverted`/`unknown`) from structural evidence (commit/tests/re-edits). Never writes. The verdict-to-status writeback lives in `operations/decisions.py` (`weave_judge_and_writeback`): `kept→accepted`, `superseded→superseded`, `reverted→deprecated`, `unknown→no change`.

### Evidence-gated supersession (2026-06-13)

A `supersedes: [dec-X]` declaration is a re-judge **trigger, not proof** — none of the three write paths (`weave_extract`, `weave_create`, `weave_update`) flip dec-X's status; they only enqueue it on `.mem/rejudge_queue.jsonl`. The structural flip is owned by `decisions.rejudge_supersession_predecessors` (a batch wrapper over `weave_judge_and_writeback`), run in two git-bearing contexts: **`wrap-finalize`** re-judges the predecessors this session's decisions supersede (the wrap worker holds the session's commits), and **`dream apply`**'s rejudge hand-off step drains the headless/deferred backlog. Either way `superseded` lands only when blame survival shows the predecessor's committed lines were actually replaced — a predecessor whose lines still co-contribute stays `kept` (per `dec-41247de0`), and one whose successor isn't committed yet waits in the queue. This makes the `superseded` badge as load-bearing as the B8-gated `accepted` badge and closes the old eager-flip false-positive (`n-b895bd07`). The `dream-judge-worker` (phase-2 prediction judge) never touches status — it writes `prediction_match` only.

### Decisions without git tie-in, by design

`/capture` or direct `weave_create` outside any session, non-code conversations (hooks never fired), `outcome: abandoned` (no code change expected), and decisions added retroactively to a session note's body. All stay `proposed` with no `commit_refs:` and no judge verdict; promotion to `deprecated` remains manual.

### Decisions with `outcome: committed` but no matching commits, post-B8

The user-asserted intent is preserved on `committed: true` in frontmatter, but `status` stays `proposed` until either (a) a future `/wrap` catches up commits the hook missed, (b) `weave_judge_and_writeback` finds re-edit evidence and emits `kept`, or (c) the user manually accepts via `weave_update`. The 228 historical accepted-without-commit_refs decisions in the live vault are pre-B8 frozen state and are not auto-demoted.

### Predicted-outcome (RLVR substrate)

Decisions can optionally carry a `predicted_outcome:` — a single prose sentence carrying BOTH a claim AND a manifestation pointer ("check X" / "look for Y" / "after Z"). Frontmatter shape: `predicted_outcome:` (the prose string), `prediction_history:` (append-only list of `{match, judged_at, reason}` entries), `prediction_match:` (denormalized tail entry's match, for cheap reads), `judged_at:` (denormalized tail entry's timestamp).

Verdicts are five: `confirmed | contradicted | pending | unevaluable | stale` — `stale` means "was true at the time but no longer applies because the substrate moved on" (only emitted when the decision has been superseded or its pointer references something that no longer exists).

The judge is the `/judge-prediction` Claude Code skill — the running session IS the judge, no API call. Three invocation paths:

1. **live**, piggybacked on `/wrap` when a decision supersedes — the composer writes the verdict via `weave_update` inline;
2. **headless**, invoked nightly by `dream-judge-worker` (phase 2 of `/dream`) which drains `.mem/rejudge_queue.jsonl` + any stale `pending` verdicts found via the index (cap `dream.rejudge_cap`, default 20/run, no separate `/judge-prediction` cron entry);
3. **manual**: `weave judge --rejudge <dec-id>`.

When a new decision declares `supersedes: [dec-X]`, the **immediate** predecessor is enqueued for re-judging — no cascade up the chain (the LLM can flag deeper concerns in its `reason`, but those require manual rejudge). New predictions land as `pending` (initialized by `wrap-finalize`); the skill takes over from there. Feeds `weave rlvr export`. Composed inline by `/wrap` from session conversation; never prompted for.

See [ARCHITECTURE.md §"Decision lifecycle"](../ARCHITECTURE.md#decision-lifecycle) for the state diagram and the `synthesis/judge.py` read-only-verdict structural view.

## Source

External content: `paper`, `repo`, `article`, `conversation`, `substack`, `news`, `newsletter-events`, `newsletter-concepts`, `youtube-events`, `youtube-concepts`, `podcast-events`, `podcast-concepts`, … Routed by `src/thinkweave/acquisition/sources/registry.py` (`SourceTypeSpec`). Three layouts: `flat`, `folder`, `author_folder`. Per-source-type behaviour (queue path, drain strategy, dedup keys) is overridable in `vault/config/sources.yaml`. See [ARCHITECTURE.md §"The source primitive"](../ARCHITECTURE.md#the-source-primitive) for the `SourceTypeSpec` shape and the open/closed registry asymmetry.

### Acquisition spine — discover → drain over a per-type atomic unit

Every source type lands on the same two-rail producer/consumer spine:

- **Discover** (`/discover` skill → `discover/strategies/*`) is the **producer rail**. It runs registered strategies that emit queue items. Two flavors: *internal-state* (`decision_review`, `prompt_gap`) observe the vault and propose what to ingest; *external-trigger* (`rss_poll`, `mail_poll`, `external_tool_runner`) observe the outside world and either enqueue directly (rss_poll) or emit a plan a skill executes against MCP (mail_poll → `/newsletter` runs the Gmail dance). The two-flavor split is **intentional** — gap-emitters describe a need (concept/decision metadata), enqueue-emitters write the queue; forcing gap-emitters to enqueue would conflate "scan and report" with "decide what to do" (the latter legitimately lives in `/discover`).
- **Drain** (`/drain --source-type X`) is the **consumer rail**. It peeks the queue, dispatches Path A (sequential `Skill` for paper/repo/article) or Path B (`Task` subagent fan-out for news/youtube-*/newsletter-*), validates worker outcomes, archives, runs post-batch hooks.
- **Atomic unit** — the per-source-type subskill / worker (`research-paper`, `research-repo`, `research-article`, `research-news-worker`, `research-youtube-worker`, `research-newsletter-worker`). One source item → one source note. Called by `/drain` over a queue, or invoked directly by `/research <url>` for a one-shot URL.
- **`/research <url>`** is the URL-classifier front door to the atomic unit — not a third rail, just a convenience for ad-hoc URLs.

`/substack` and `weave import {chatgpt|claude-mem}` legitimately skip discover: the user (or an external export) has already done the discovery step. `/news <url>` is a one-shot bypass too — no triage gate, same posture as `/research`.

### RSS-poll intake (news, youtube-*)

The `rss_poll` discover strategy is generic over source_type. All feed registries live under `PRIORITIES.yaml::intake.<slug>` (the standalone `*_feeds.yaml` files were retired 2026-06-13): news/podcast use `intake.<slug>.outlets` (per-outlet daily caps, `prefer_embedded` capture); youtube uses `intake.<slug>.channels: [UCxxx, ...]` (one feed per channel, `lookback_days` cutoff). Flavor is derived from the slug shape, not a config field. Both flavors dedup against the active queue + recent archive (`Queue.dedup_check`) **plus** the SQLite indexer (URL already a `type: source` note) — the indexer guard covers re-emits months later that the 30-day archive lookback misses. Headless: `claude -p "/discover --strategy rss_poll --source-type news"` is the canonical cron invocation; the legacy `scripts/pull_news_feeds.py` is now a deprecation shim that delegates to the strategy.

### Mail-connector intake (newsletters)

Email newsletters land via a provider-agnostic mail connector — `gmail` today, `outlook` and `imap` are slots. The `mail_poll` discover strategy *composes* the per-type Gmail query (sender allowlist → `from:(s1 OR s2 OR ...)`, optional `mail_query` extras, `-label:<processed_label>` exclusion, `newer_than:Nd` lookback) and emits a `mail_fetch_needed` plan. The `/newsletter` skill *executes* the plan against Gmail MCP — fetch threads, enqueue, drain, then `label_thread` on every queue item archived `done`. Empty allowlist + empty `mail_query` is a deliberate halt in the strategy, not a whole-inbox fan-out. Three re-read guards stack: (1) **mail label** (primary — survives queue wipes); (2) **queue dedup** on `[message_id, url]`; (3) **`weave_search(message_id)` backstop** in the worker. The Gmail connector's first run is interactive OAuth; subsequent runs use the cached token (operationally headless-equivalent). Strict-headless cron use waits for the `imap` connector.

## Context-served (RLVR substrate)

Each session captures the notes served to it: a single `type: startup` event at SessionStart (notes in the SessionStart payload + `token_est`) plus one `type: retrieval` event per MCP retrieval call during the session (`weave_search`, `weave_context`, `weave_graph`, `weave_read`, `weave_timeline`, `weave_project_snapshot`). Buffered to the same per-session JSONL as action events; `archive_buffer` (Stop time / `weave_extract`) splits them into sibling `events.jsonl` and `retrieval_log.jsonl`. The Indexer projects every session's `retrieval_log.jsonl` into `context_served(session_id, note_id, source ∈ {startup, onthefly}, ts)` — rebuildable from markdown. The RLVR row's `context.cited_onthefly_ids` / `cited_startup_only_ids` come from intersecting decision body wikilinks against this table; a note served both via startup and on-the-fly counts as onthefly (the stronger signal).
