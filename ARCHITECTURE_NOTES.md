# Architecture — Deep-dive notes

Contributor reference for material spilled out of `ARCHITECTURE.md` for readability. Read this when you're touching the subsystem in question; skip it otherwise.

## Canonical source frontmatter

Every source note carries a canonical set of fields. The `build_source_frontmatter` helper in `src/personal_mem/sources/frontmatter.py` builds the dict with consistent ordering:

```python
from personal_mem.sources import build_source_frontmatter

fm = build_source_frontmatter(
    source_type="paper",
    title="Attention Is All You Need",
    url="https://arxiv.org/abs/1706.03762",
    authors=["Vaswani", "Shazeer", "Parmar"],
    arxiv_id="1706.03762",
    publication="NeurIPS 2017",
)
```

| Field               | Purpose                                                       |
|---------------------|---------------------------------------------------------------|
| `source_type`       | Canonical slug. Drives routing.                               |
| `title`             | Human-readable title. Also the filename slug source.          |
| `url`               | Canonical URL/URI. Empty string is legal for local content.   |
| `authors`           | List of strings. Use `[]` when unknown.                       |
| `concepts`          | Ontology terms (≥2). Feeds the knowledge graph.               |
| `proposed_concepts` | New vocabulary not yet in `ontology.yaml`. Reviewed later.    |
| `proposed_theme`    | Per-source theme candidate slug. Event-grain sources only.    |
| `relates_to`        | Theme IDs (`thm-XXXX`). Wikilink-equivalent edges.            |
| `raw_path`          | Relative path to the raw companion file (`raw.md`, …).        |
| *…source-specific*  | Whatever your importer needs (`arxiv_id`, `publication`, …).  |

The helper doesn't enforce a schema — it's a convention, not a validator. Source-specific fields are merged via `**extra`.

## Adding a new source type

Worked example: `podcast`. Five steps end-to-end.

### 1. Register the source type

Edit `src/personal_mem/sources/registry.py`:

```python
"podcast": SourceTypeSpec(
    slug="podcast",
    bucket="podcasts",
    layout="folder",        # episode-slug/source.md + companion files
    skills=("podcast",),
    temporal_grain="event", # → theme floating in /dream
    description="Podcast episode transcripts. Ingested via /podcast.",
),
```

Pick a layout: `flat` (single-file summary, no raw companion), `folder` (slug subdir with raw alongside — the usual choice), or `author_folder` (show-level nesting for serial content). Pick a `temporal_grain`: `event` if items are timeline-shaped (a date matters), `concept` if they're reference-shaped (the topic matters), `none` for conversation-style intake.

### 2. Add per-type config to `sources.yaml`

In `src/personal_mem/sources/config.py:DEFAULT_CONFIG['sources']`:

```python
"podcast": {
    "queue": "vault/.mem/queues/podcasts.jsonl",
    "drain_strategy": "inline",
    "dedup_keys": ["url", "episode_id", "title"],
    "url_patterns": ["overcast.fm", "pca.st", "spotify.com/episode"],
},
```

Add a matching block to `vault_templates/config/sources.yaml` so new vaults ship with the override stub.

### 3. Drop a skill at `commands/podcast.md`

Copy `commands/_source_template.md` and fill in:

```yaml
---
name: podcast
source_type: podcast
capabilities: [import, acquire]
tools: [Read, Bash, WebFetch, mem_create, mem_queue, ...]
description: Ingest podcast transcripts (Overcast, Pocket Casts, Spotify).
---
```

Implement the bespoke fetch + transcribe + summarise logic in the body — that's where per-source variation lives, and where the template warns against premature abstraction.

If your source type needs a binary extraction step (transcripts, audio summaries), drop the helper under `src/personal_mem/sources/extractors/` and shell out from the worker via `python -m personal_mem.sources.extractors.<name>` (see the YouTube / podcast workers for the pattern).

### 4. Add a research subskill (optional)

If `/research` should classify podcast URLs, add `commands/research/research-podcast.md` with the import logic. The router (`commands/research.md`) reads `url_patterns` from step 2 and dispatches automatically — no router edits.

### 5. Verify

```bash
mem sources show podcast        # registry entry visible
mem skill show podcast          # frontmatter parses
echo '{"url": "https://overcast.fm/+xyz", "title": "Test"}' \
  | mem queue add podcast --stdin
mem drain --source-type podcast --limit 1
```

If the smoke test creates a source note at `vault/sources/podcasts/<slug>/source.md`, the integration is live. No edits to `vault.py`, `queue.py`, the CLI, or the MCP server were needed at any step.

The framework ships with a deliberately small default set: `paper`, `repo`, `article`, `substack`, `conversation`, `claude-history`. Every other source type — podcasts, YouTube, Messenger exports, RSS, email, Slack archives — is the user's to add via this pattern.

## sources.yaml vs source_types.yaml

The shipped `DEFAULT_CONFIG` lives in `src/personal_mem/sources/config.py` and seeds **two** overlay files that can both live under `vault/config/`:

| File | Loads what | Loaded by |
|---|---|---|
| `sources.yaml` | per-type behaviour (`queue`, `dedup_keys`, `drain_strategy`, `url_patterns`), per-project knobs (`discover_strategies`), `landing_files`, `auto_todo_extraction` | `sources/config.load_user_config` |
| `source_types.yaml` | new `SourceTypeSpec` registry entries (vault-side source-type extensions) | `sources/registry._load_user_specs` |

The split exists because **the registry is open-world but behaviour is closed-world**:

- A source note with an unregistered `source_type` works — `VaultManager.create_note` falls through to the `folder` layout (`sources/<slug>/source.md`). This makes one-off experimentation cheap.
- But `mem drain --source-type <undeclared>` errors out — the queue and dispatch tables need the spec to know where to look. So production paths require a registry entry.

The two files reflect this asymmetry: `source_types.yaml` is for *registering* new types (mostly used by `/source-scaffold`); `sources.yaml` is for *configuring* declared types. Most users only ever edit `sources.yaml`.

A future cleanup could collapse the two into one file with a `declared: bool` flag per type. The trade-off lives in a separate memo (`docs/sources-overlay-decision.md`).

## Prompt primitive

Every user prompt submitted in Claude Code is captured as a structured event in the active session's JSONL buffer. The `UserPromptSubmit` hook (registered by `mem hooks install`, handled in `surfaces/hooks/handler._handle_user_prompt_submit`) appends one line per submission:

```jsonl
{"ts": "2026-05-02T15:47:00+00:00", "type": "prompt", "text": "What does the indexer skip?", "session_id": "cc-uuid", "cwd": "/path"}
```

The schema is intentionally flat — same buffer file the Edit/Write/Bash post-tool events land in, just discriminated by `"type": "prompt"`. `core/events.extract_prompts(events_jsonl)` lifts these rows into `Prompt` dataclasses.

**Probe classification.** `core/events.classify_probe(prompt, events)` is a conservative heuristic — it returns `True` only when the text reads like a question (ends with `?` or opens with a lead phrase like *what is*, *explain*, *how does*) **and** no `Edit`/`Write` event lands within the next 3 events of the buffer. False negatives over false positives — STATE.md's "Open Probes" section is more useful when sparse and accurate than when noisy.

**Where it's consumed:**

- `synthesis/landing._gather_prompt_probes` walks both archived `vault/projects/<project>/sessions/*/events.jsonl` and active `.mem/buffer/<session_uuid>.jsonl` files, applies `classify_probe`, and merges the result with `probe`-tagged notes for STATE.md's "Open Probes" section.
- `mem_prompts` MCP tool (`surfaces/mcp/tools/prompts.py` → `operations.search.query_prompts`) gives skills read-only access to prompts, project-scoped, with optional `since` / `limit` filters. `/discover` uses it to bias gap analysis toward what the user has actually been asking.

The legacy `probe` *tag* becomes a manual override only. The canonical signal is the prompt event; the tag stays load-bearing for back-compat.

**Auto-todo extraction.** A side-channel in the same module: `core/events.extract_todos(text)` scans free-form text for `TODO: …` / `FIXME: …` / `we should …` / `next step: …` / `follow-up: …` patterns and returns `Todo` dataclasses. Wired into `mem_extract` (gated by `auto_todo_extraction` in `sources.yaml`, default `True`); each match becomes a note tagged `[todo, auto]`. `mem backlog` shows an `[auto]` marker so they're distinguishable from hand-curated todos.

## RLVR substrate — decision-context capture

Each decision has, in principle, two reward signals attached: an *outcome* (did the code land and stay?) and a *prediction* (did the call play out as the author guessed?). The framework records the substrate for both passively — no model turns, no extra hooks beyond the ones already installed — so a downstream RL loop can join decision rows against the context they came out of. The MVP shipped 2026-05-14; the pipeline below is the steady-state shape.

```
SessionStart hook ─────────────────────▶ buffer/<sid>.jsonl  (one {"type": "startup", "ids": [...], "token_est": N})
MCP retrieval tool call                        │
  └─▶ PostToolUse hook ────────────────▶ buffer/<sid>.jsonl  (one {"type": "retrieval", "tool": "...", "returned_ids": [...]})
Stop hook  /  mem_extract                      │
  └─▶ archive_buffer ──────────────────▶ events.jsonl  +  retrieval_log.jsonl  (sibling files in the session folder)
mem index  /  Indexer.rebuild ──────────▶ context_served(session_id, note_id, source ∈ {startup, onthefly}, ts)
/mem-wrap  →  mem wrap-finalize  →  mem_judge_and_writeback
  └─▶ verdict written to decision frontmatter; new predictions initialized to prediction_match: pending
/judge-prediction (skill — live on supersedes, or cron drain)
  └─▶ prediction_history append + prediction_match denormalized to tail entry
mem rlvr export ────────────────────────▶ JSONL stream (one row per decision)
```

**Capture.** Two writers, one buffer. The `SessionStart` hook (`surfaces/hooks/handler._handle_session_start`) writes exactly one `type: startup` event per session containing the ids served in the SessionStart payload plus a `token_est`. The `PostToolUse` hook (registered with two matchers — `Write|Edit|Bash` for the action stream and `mcp__personal-mem__.*` for the MCP stream, see `surfaces/hooks/install.py`) writes one `type: retrieval` event per MCP retrieval tool call. The captured tool set is the closed frozenset `RETRIEVAL_TOOLS` in `operations/retrieval_log.py` — six tools: `mem_search`, `mem_context`, `mem_graph`, `mem_read`, `mem_timeline`, `mem_project_snapshot`. A future retrieval tool must opt in explicitly; mutation tools (`mem_create`, `mem_link`, …) are deliberately excluded.

**Buffer split.** `core/buffer.archive_buffer` is called at Stop and inside `mem_extract`. It partitions the per-session buffer JSONL by event `type`: action/prompt events → `events.jsonl`, `retrieval` + `startup` events → `retrieval_log.jsonl` (sibling files in the session folder). When a session has retrieval events but no action events, `events.jsonl` is touched empty — the orphan-detector in `prune.py` keys off file presence, so the empty file keeps the "events.jsonl missing → orphan session" rule intact.

**Projection.** `Indexer._rebuild_context_served` (called from `rebuild` and from `index_paths` when a session folder is touched) walks every session's `retrieval_log.jsonl` and projects rows into the `context_served(session_id, note_id, source, ts)` table. `source` is `startup` for ids that came in via the SessionStart event, `onthefly` for ids returned by an MCP retrieval call. A note served both ways resolves to `onthefly` — the on-the-fly retrieval is the stronger signal. The table is fully rebuildable from markdown alone (the SQLite DB stays a derived index).

**Citation extraction.** `operations/rlvr_export.assemble_row` walks decision body wikilinks via `extract_wikilinks` and intersects them against `context_served` for the decision's session. Output: `cited_onthefly_ids` (decision body cites a note that was retrieved on the fly) vs `cited_startup_only_ids` (decision body cites a note that arrived only via the SessionStart payload). Frontmatter relations (`derived_from`, `relates_to`) are *not* counted — only semantic body citations.

**`prediction_match`.** A decision may carry a `predicted_outcome:` — a single prose string with claim + manifestation pointer (e.g. *"After the transcript-first ladder ships, the next /drain on the 3 queued AI Engineer videos archives all 3 as accepted (0 gemini_refused). Check the youtube-events queue archive after the next drain run."*). Three companion frontmatter keys: `prediction_history:` (append-only list of `{match, judged_at, reason}` entries), `prediction_match:` (denormalized tail entry's match), `judged_at:` (denormalized tail entry's timestamp). Verdict enum is five values: `confirmed | contradicted | pending | unevaluable | stale`. `stale` means "was true at the time, no longer applies because the substrate moved on" — only emitted when the decision has been superseded or its pointer references something that no longer exists. The judge is the `/judge-prediction` Claude Code skill (`commands/judge-prediction.md`), not an API call — the running session IS the judge. Three invocation paths: (1) live, piggybacked on `/mem-wrap` when a successor decision supersedes — the composer writes the verdict via `mem_update` inline; (2) headless via cron: `claude -p "/judge-prediction --drain"` drains `.mem/rejudge_queue.jsonl` + finds stale `pending` rows (cap 20/run); (3) manual: `mem judge --rejudge <dec-id>` enqueues + shells to the skill. `mem_judge_and_writeback` (`operations/decisions.py`) handles only structural `verdict` (`kept`/`superseded`/`reverted`/`unknown`) and the pending initializer for new `predicted_outcome` rows — prediction semantics now live entirely in the skill's LLM turn, not Python.

**Export.** `mem rlvr export [--project] [--since] [--until] [--committed-only]` (CLI surface; implementation in `operations/rlvr_export.export_rows`) yields one JSONL row per decision joining decision frontmatter + body citations + the `context_served` projection. The locked row schema is documented in the module docstring — `{decision_id, project, session_id, created_at, prediction: {text, match}, outcome: {verdict, committed, blame_lines, days_alive}, context: {n_retrievals_onthefly, cited_onthefly_ids, cited_startup_only_ids, startup_token_est}}`. The exporter opens one `Indexer`, caches `retrieval_log.jsonl` reads by session, and never writes back — feeding RL pipelines is a read-only consumption point.

See CLAUDE.md §3 "Context-served (RLVR substrate)" and "Predicted-outcome" for the agent-facing summary; the symbols and storage layout above are the contributor view.

## Workflow stager

`mem flow` is a thin declarative layer over the existing skill+cron pattern. Flows live in `vault/config/flows.yaml` as named sequences of `claude -p` invocations; cron entries become one-liners that invoke `mem flow run <name>` instead of re-encoding the order and flags.

The shipped templates (`scripts/example-flows.yaml`, `scripts/example-crontab`) document the dialect: `description`, optional `log` path, `on_error` (continue / abort), and a list of `stages` each with `run` (the literal `claude -p` argument) and optional `sleep` (seconds after the stage). No templating, no conditionals, no parallel branches — when those are needed, prefer a separate primitive over expanding `FlowSpec`.

## Running skills headless

Skills live in `commands/*.md` as plain markdown and run inside Claude Code via the Skill tool. Claude Code provides the full tool surface (`Read`, `Bash`, `WebFetch`, `WebSearch`, every `mem_*` tool via the personal_mem MCP server) and executes the procedure interactively.

For bulk, non-interactive work there are two targeted paths — neither requires a generic skill runner:

- **Concept hub backfill** — `mem drain --target hubs --via batch` (alias `mem hubs run`) ships its own OpenAI Batches API path. It doesn't route through a skill file; it reads the plan JSON and calls the Batches API directly. `mem hubs link` is the analogous one-shot pass that rewrites flat `new` flags into `agrees`/`contradicts`/`extends` relationships across existing hub log entries.
- **Autopilot** — `claude -p --model sonnet --dangerously-skip-permissions` invoked from cron gives headless skill execution with the full Claude Code tool surface.

If you need a new headless path that isn't either of these, add a CLI subcommand next to `mem hubs run` rather than reintroducing a generic runner.

## Deprecated MCP tool names (one release)

These aliases dispatch to canonical tools and log a warning to stderr. They will be removed after one release window.

- `mem_concepts_tighten`, `mem_concepts_merge`, `mem_concept_search`, `mem_concept_source_counts`, `mem_concepts_drift` → fold into `mem_concepts(action=...)`.
- `mem_source_lens`, `mem_decisions_for_file` → fold into `mem_graph(filter=...)`.

The CLI counterparts (`mem connect`, deleted 2026-05-21) have already been removed — only the MCP-side aliases still log warnings.

## Historical notes

- **`mem extract` relocation (Phase 1.6, commit `5420096`).** The deterministic event-parsing module was at `src/personal_mem/extract.py`; it collided semantically with `src/personal_mem/operations/extract.py` (the `mem_extract` heavy lift). It moved to `src/personal_mem/core/events.py` with the canonical name `events`.
- **`drain` → `hubs_batch` rename (Phase 1.3, commit `13b0f57`).** The OpenAI Batches monolith for concept-hub backfill was at `operations/drain.py`; this collided with the `/drain` skill (per-source-type queue drain). It moved to `operations/hubs_batch.py`.
- **`claude_mem` → `claude_history` rename (Phase 1.5, commit `1a37805`).** The importer was named `claude_mem` (after the source format); it now aligns with the source type slug `claude-history` (more descriptive, user-facing).
- **`extractors/` move (Phase 2.5).** `gemini_extract.py` and `transcript_extract.py` lived under `synthesis/` because of the LLM-call shape; they moved to `sources/extractors/` because they're per-source worker utilities, not vault-wide synthesis primitives.
- **`mail_connector` → `mail_provider` rename (commit `6c563c9`).** The reserved-slot naming was misleading (gmail-only ships v1); the new name signals it's provider-agnostic by design. Both names accepted at read time.
