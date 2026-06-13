# Thinkweave — coding-agent operating guide

This file loads into your context every session. It is the lean operating surface: **how to drive the system, and where to find depth.** For mechanism and narrative, follow the [Reference index](#reference-index).

## What Thinkweave is

Thinkweave is an Obsidian-native memory layer: markdown is the source of truth, SQLite is a derived index. As an agent you do **not** crawl the vault filesystem — you query through the `weave_*` MCP tools (server id `thinkweave`, so tools are `mcp__thinkweave__weave_*`; the short names stay `weave_*`) or the `weave` CLI. Sessions, decisions, sources, themes, and concept hubs are all first-class notes connected by a shared concept ontology. Retrieve through the retrieval cheatsheet below; preserve session knowledge via `/weave-wrap` before clearing context.

## Retrieval cheatsheet

Three modalities, plus compositions on top.

- **FTS** — `weave_search(query, mode='fts')`. Keyword/phrase. Cheap. Empty `query` returns recent matches honouring filters (list mode).
- **Similarity** — `weave_search(query, mode='similar')`. Concept-shaped query, no keyword. Soft-fails to FTS when embeddings unavailable.
- **Hybrid** — `weave_search(query, mode='hybrid')`. Unsure → RRF fusion (k from `retrieval.rrf_k`, default 60).
- **Graph** — `weave_graph(id, depth, filter=…)`. Structural walk over typed edges. Filter dispatches the variant: `''` (default — walk from `id`), `'source_lens'`, `'decisions_for_file'`, `'concept_walk'`. The legacy alias tools (`weave_source_lens`, `weave_decisions_for_file`, `weave_concept_search`) were deleted 2026-05-21 — call the canonical name.

Compositions:

- `weave_context(query, type=[…])` — FTS → similarity-via-concept → recency, deduped budget blob.
- `weave_project_snapshot(project)` — re-fetch the SessionStart context payload.
- `weave_timeline(project, days)` — chronological window of sessions + decisions.

All filters take `since` / `until` ISO dates; `weave_search` accepts `concepts=[…]` to combine text + concept; `weave_graph` accepts `note_type` / `project` projection.

| If you want to… | Use |
|---|---|
| Find X (keyword/phrase) | `weave_search` (`mode=fts`, fall back to `hybrid`) |
| Tell me about Y (budgeted blob) | `weave_context` |
| What touches Z (note id walk) | `weave_graph` |
| State of project P right now | `weave_project_snapshot` |
| What happened in window W | `weave_timeline` |

## Operational rules

- **No filesystem crawls.** Never `find`/`ls`/`grep` the vault from a Bash tool. Use the SessionStart context (already in your conversation), MCP tools, or a single `Read` of a known file path.
- **One MCP call per question.** Pick the modality from the cheatsheet; don't fan out unless the first call is genuinely insufficient.
- **Pre-`/clear`: run `/weave-wrap`.** There is no clear hook; this is the only way to preserve mid-session knowledge.
- **`/weave-wrap` is zero-API; latency is two model turns + one Bash call.** `weave_extract` is pure Python; the whole deterministic tail (prune → index → judge → landing → drift) is one Bash call (`weave wrap-finalize`) with no model turns. The only LLM cost is composing the insights/decisions inline and the wrap-up report.
- **Concepts mandatory.** Every note created via `weave_extract` must carry ≥2 concepts. Load existing labels via `weave_concepts` before assigning. Prefer specific terms (`ml/deep-learning` over `deep-learning`).
- **Strict ontology gating.** Only ontology-listed terms may go in `concepts:`. Any new term goes in `proposed_concepts:`. The strict gate is server-enforced — `weave_extract`, `weave_create`, and the importers all run incoming concept lists through the merged ontology and shunt non-matches to `proposed_concepts:` automatically. Promotion (proposed → canonical) is `/tighten`'s job (and the nightly `dream-promotion-worker`), triggered when a proposed term reaches critical mass (`dream.promotion_threshold`, default `count ≥ 5`). You don't pre-canonicalise; you just attach concepts and let the gate sort them.
- **Auto-todo only on request.** Never tag `todo` unless the user explicitly asks.
- **Concepts vs tags vs themes.** `concepts` = domain vocabulary (drives graph edges, authority `ontology.yaml` + `concept_aliases.yaml`); `tags` = broad filter facets (`debugging`, `todo`, `til`, authority `tag_vocabulary:` in `ontology.yaml`); `themes` = global temporal narratives (`thm-XXXX`, in `vault/themes/`). Do not duplicate between `concepts` and `tags`. See [Lifecycles](docs/LIFECYCLES.md#concept-vs-theme-the-disambiguation-test) for the concept-vs-theme disambiguation test.
- **LLM provider abstraction.** Backfill and triage paths go through `core/agent_client.py` (AsyncOpenAI + per-provider `base_url`) configured via `vault/config/api.yaml`. Embeddings live in their own layer (`core/embedding_provider.py`). **Carve-out:** `sources/extractors/gemini_extract.py` keeps direct `google.genai` for the podcast Files API audio modality. See [Architecture §"LLM provider abstraction"](ARCHITECTURE.md#llm-provider-abstraction--coreagent_clientpy--coreembedding_providerpy).

## Reference index

When you need depth, go here. CLAUDE.md tells you *how to operate*; these tell you *how it works*.

| Doc | What's in it |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | The narrative reference: two layers, source primitive, capability lanes, acquisition spine, dream orchestrator, memory seam, ontology as joint vocabulary, themes-vs-concept-hubs, queue primitive, config layout, discovery strategies, coherence, operations layer, provider abstraction, surface contract. |
| [docs/LIFECYCLES.md](docs/LIFECYCLES.md) | Full lifecycle deep-dives: session, concept (drift-v2 + seam-link invariant), theme (floating, registry, disambiguation), decision (4-state table + evidence-gated supersession + predicted-outcome/RLVR), source (acquisition spine, RSS/mail intake), prompt, context-served. |
| [docs/SKILLS.md](docs/SKILLS.md) | The skills catalog (all `/` commands), the subagent-worker roster (dream phase-1/2, research, triage), and the `--via inline\|batch` dual-route convention. |
| [docs/CLI-AND-MCP.md](docs/CLI-AND-MCP.md) | The `weave` CLI subcommand reference, the 18 MCP tools, the CLI↔MCP surface contract (which audience owns each operation), and the environment variables. |

After upgrading Thinkweave, re-run `weave hooks install` to pick up newly-added hooks (e.g. SessionStart).
