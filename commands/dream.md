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
- `theme_candidates`: cluster stubs in `vault/themes/_candidates/` with
  `{candidate_id, cluster_concepts, cluster_sources, candidacy,
  source_type}`.
- `dormant_themes`: themes with no catalysts in ≥ 90 days (helpers are
  deterministic — confirm, don't re-decide).
- `resolved_themes`: themes whose linked decisions are all terminal
  (deterministic — confirm).
- `stats`: count summary for the report.

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

**Theme candidates.** Apply the disambiguation test from CLAUDE.md §4:

- Capability / technique / area-of-work → archive (not a theme).
- Event / period / transition / campaign with time horizon → promote.
- Year-bearing names (`AI capex unwind 2026`) → promote.

For promotions, compose a short `essence` (≤300w paragraph capturing the
working thesis from `cluster_concepts` + `cluster_sources`). Add to
`plan["theme_promotions"]` as
`{"candidate_id", "title", "essence", "parent" (optional), "project"}`.

For archivals, add to `plan["candidates_archived"]` as
`{"candidate_id", "reason"}`.

**Dormant / resolved themes.** Helpers are deterministic — confirm the
verdict matches the theme's state and add a status change. Add to
`plan["theme_status_changes"]` as `{"theme_id", "new_status", "reason"}`.

**Essence rewrites.** For any canonical theme whose recent catalysts
contradict its essence (read the last ~10 catalyst entries via
`mem_read`), rewrite the `## Essence` section via `Edit` directly on the
theme file — keep ≤500 words. Then log the rewrite by adding
`{"theme_id", "reason"}` to `plan["essence_rewrites"]` (log-only — the
apply phase does not re-edit; this entry just records what you did).

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
