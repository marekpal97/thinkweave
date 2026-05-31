---
name: dream
owns_mechanic: vault_hygiene
consumes: [mem_concepts, mem_search, mem_read, mem_update, mem_link]
produces: [ontology.yaml, vault/themes/*, vault/concepts/topics/*, vault/.mem/maintenance.jsonl]
tools:
  - Read
  - Edit
  - Bash
  - mem_concepts
  - mem_search
  - mem_read
  - mem_update
description: Periodic dream cycle — autonomous vault hygiene (concept promotion, theme lifecycle, drift review). Single Bash scan → LLM judgment → single Bash apply. Self-deciding, headless-safe, logs every cycle to maintenance.jsonl.
---

# /dream — Periodic vault-hygiene cycle

The cron-friendly successor to `/mem-resolve-concepts` and `/themes-resolve`.
One skill, three phases, two Bash calls bracketing one LLM judgment pass.

Self-deciding. **Never prompts the user.** Designed for `claude -p
"/dream"` cron use; works the same interactively.

The whole point: every cycle leaves one JSON line in
`vault/.mem/maintenance.jsonl` saying what it did. That log is the trust
substrate for autonomy — grep it any time to verify the cycle hasn't gone
sideways.

## Posture

This skill applies **LLM judgment only to the survivors of the Python
filters**. The scan phase already strips drift noise, domain-path
candidates, generic stopwords. Your job is the genuinely-semantic part —
"is this term ontology-worthy?", "does this theme essence still hold?".
If the filtered surface is empty, ship a no-op cycle and log it.

## Steps

### 1. Scan (one Bash call)

```bash
uv run mem dream scan --promotion-cap 20 --json
```

Returns a `DreamCycleScan` JSON payload with:

- `cycle_id` (carry into apply)
- `promotion_candidates`: `[{"concept": "x", "count": 12}, ...]`,
  post-filter, capped at 20 by count.
- `drift_pairs`: `[{"from": "a", "to": "b", "reason": "..."}, ...]` —
  survivors of `filter_drift_candidates`. Conservative on this vault;
  most cycles see 0-5.
- `theme_cluster_signals`: the **only** theme surface. Each entry is a
  cluster of recent event-grain sources, enriched for the mint-vs-extend
  decision:
  ```
  {source_type, cluster_kind, label, shared_concepts, source_count,
   sources: [{id, title, proposed_theme, date}, ...],   # newest first
   proposed_names: {slug: n_sources, ...},              # distinct-source counts
   related_names: {slug: n_sources, ...},               # variant slugs folded into this arc
   covering_themes: [{theme_id, slug, concepts, overlap, name_match, score, status}, ...]}
  ```
  - `cluster_kind` is `"name"` (the **primary** path — sources grouped on
    their `proposed_theme:` stamp, with near-variant slugs already folded
    into one arc) or `"concept"` (the **fallback** — *unstamped* sources
    grouped on shared concepts; `label` and `proposed_names` are empty).
  - `label` (name clusters) is the arc's working name — the most-supported
    variant; `related_names` are the other variants folded in (e.g. label
    `iran-war` with `related_names: {iran-war-resolution: 1, ...}`). Use
    `label` as the slug directly unless it reads badly.
  - `proposed_names` counts **distinct sources** per slug — the honest
    support, not appearances-across-clusters.
  - `covering_themes` is ranked: a non-zero `name_match` (label↔slug token
    overlap) is a **strong** extend signal that outranks any concept-only
    overlap; concept-only candidates (`name_match: 0`) only appear when ≥2
    *non-generic* concepts agree. Empty `covering_themes` → mint territory.
- `stats`: count summary for the report.

There is **no** theme lifecycle surface — dream never marks themes
dormant/resolved and never archives candidates. Theme status changes are
the user's call, by hand.

### 2. Apply LLM judgment (inline, in this turn)

Walk each surface in order; emit decisions into a `plan` dict.

**Promotions (the dominant work on this vault).** For each
`promotion_candidate` decide:

- Skip if generic (`refactoring`, `monitoring`, `validation` — broad
  process terms even after filter).
- Skip if project-name leakage (e.g. `personal-finance-assistant`,
  `imported-session` — vault structure, not vocabulary).
- Otherwise pick the **best ontology domain** by reading `ontology.yaml`
  (typically `swe-{tools,data,arch}`, `ml-{training,deep-learning}`,
  `finance-{markets,macro}`, ...). When in doubt, pick the narrowest
  domain that still makes sense.

Add `{"concept": <slug>, "domain": <domain>, "reason": <one-line>}` to
`plan["promotions"]`.

**Drift pairs.** For each, decide merge vs leave. Most pairs on this
vault are substring noise (`api ≈ fastapi`, `attention ≈ self-attention`)
— these are **not** the same concept; leave them. Only merge when the
shorter term is genuinely a typo / plural / alias of the longer.

Add merges to `plan["merges"]` as `{"from": "x", "to": "y", "reason": "..."}`.

**Theme cluster signals (mint vs extend).** For each signal in
`theme_cluster_signals`, decide one of three actions:

1. **Extend** — `covering_themes` is non-empty and the top one is genuinely
   the same arc. A non-zero `name_match` on the top covering theme is
   near-decisive (the label and theme slug share tokens); for concept-only
   matches (`name_match: 0`), confirm with `sources` titles before
   trusting it. Link the new sources to it. Add to
   `plan["theme_extensions"]` as
   `{"theme_id", "source_ids", "reason"}`. This is the common steady-state
   case — new drops landing on an arc you already track. The apply phase
   backfills `relates_to:` on each source and appends catalyst lines.
2. **Mint** — empty (or only weak, off-topic) `covering_themes`, and the
   cluster passes the disambiguation test from CLAUDE.md §4 (event /
   period / transition / campaign with a time horizon — not a capability
   or area-of-work). Compose:
   - `slug` — for a **name** cluster, use `label` directly (variants are
     already folded; `related_names` shows them). For a **concept**
     cluster (`label` empty), compose a fresh name from the `sources`
     titles. Rules: 1–3 kebab words, label-shaped (`iran-war`,
     `bond-vigilantes`, `memory-chip-supercycle`). No dates, no
     parentheticals, not a concatenation of the cluster's concepts.
   - `essence` — a 1-sentence narrative description (always compose this).
   Add to `plan["theme_mints"]` as `{"slug", "essence", "source_ids",
   "concepts" (top-3 from the cluster), "project" (optional), "parent"
   (optional)}`. The apply phase mints `thm-XXXX-{slug}.md` and backfills
   `relates_to: [thm-XXXX]` on each cluster source.
3. **Skip** — capability/technique/area-of-work, or too thin to name (a
   2-source name cluster you're unsure about is fine to leave). Do
   nothing; once an arc is minted/extended its sources are filed and stop
   resurfacing, so skipping costs nothing.

**No theme lifecycle.** Dream never marks themes dormant or resolved and
never changes a theme's `status`. That is the user's call, by hand — do
not emit any status-change plan key.

**Essence rewrites.** For any canonical theme whose recent catalysts
contradict its essence (read the last ~10 catalyst entries via
`mem_read`), rewrite the `## Essence` section via `Edit` directly on the
theme file — keep ≤500 words. Then log the rewrite by adding
`{"theme_id", "reason"}` to `plan["essence_rewrites"]` (log-only — the
apply phase does not re-edit; this entry just records what you did).

**Priority signals (Slice 1.5).** Read `scan().recent_probes` — a
`{concept: probe_count}` dict over the last 14 days of probe-classified
prompts. For each concept the user has been asking about that warrants
attention, decide one action:

- `enqueue` — the user has been probing about a concept with little
  source coverage / no theme / a stale hub, AND a concrete piece of
  research / read / source ingest would help. Compose `queue_item`:
  `{"source_type": "<one of vault's source-type slugs>", "title": "<one
  line>", "concept": "<concept>", "source": "dream-priority-signal", ...}`
  The apply phase only writes the queue item when the config flag
  `dream_enqueue_priority_signals` is True; otherwise the entry is
  counted as logged. This keeps the first cycle observable before any
  external mutation.
- `log` — the user has been probing about something already
  well-sourced / structurally fine, but the pressure is high enough to
  note in the report. No queue write; just shows up under "What I
  noted" in the report.

Add to `plan["priority_signals"]` as `{"concept", "probe_count",
"action": "enqueue"|"log", "queue_item"?, "reason"}`. Each `reason`
should be the one-line *why* — the user reads these to understand
exactly why dream surfaced each signal.

Cap at 5 priority signals per cycle. Skip concepts the user has only
probed once (signal is too thin).

### 3. Apply (one Bash call)

Write the plan to `/tmp/dream-plan-<cycle_id>.json` and run:

```bash
uv run mem dream apply --plan /tmp/dream-plan-<cycle_id>.json --json
```

Alternatively pipe via stdin:

```bash
echo '<plan-json>' | uv run mem dream apply --plan - --json
```

The apply phase batches every structural change with **one** index
rebuild at the end, then appends a single line to
`vault/.mem/maintenance.jsonl` capturing both intent (the plan) and
outcome (counts + errors + per-step timings). Returns a
`DreamCycleResult` JSON.

If any errors appear in the result, surface them in the wrap-up — the
errors-don't-cascade contract guarantees the other steps still ran, so
the cycle is partially successful, not failed.

### 4. Report (3 lines)

```
Dream cycle <id>. Promoted N concepts. Promoted T themes (A archived).
Marked D dormant, R resolved. E essence rewrites.
Logged to vault/.mem/maintenance.jsonl. Cycle took <wall-time>s.
```

Mirror the `/mem-wrap` wrap-up format. Keep it tight — the maintenance
log is where the detail lives.

## Notes

- **First cycle on a vault with backlog will hit the 20-promotion cap.**
  This is fine — the cycle drains across multiple nightly runs. Steady
  state is ~0-5 surfaced items per cycle.
- **The scan never crawls the filesystem from this skill.** All discovery
  is in the `mem dream scan` Bash call, which uses the SQLite index.
- **No prompts.** If the disambiguation test is ambiguous on a theme
  candidate, default to archiving — capability-named clusters age out
  cheaply, false promotion costs a theme-merge later.
- **Hub fill is out of scope for v1.** If you notice an empty concept hub
  while applying promotions, leave it — `mem drain --target hubs` owns
  hub population, deliberately decoupled.
