---
name: themes-resolve
owns_mechanic: theme_synthesis
description: On-demand theme hygiene front door — review/merge duplicate themes via the drift-v2 helpers and rewrite stale essences. Routine dedup runs nightly inside /dream; run this for an immediate pass or the one-time catalyst/title backfill.
tools: [Read, Edit, Bash, mem_search, mem_read, mem_update, mem_link]
---

# /themes-resolve — Theme synthesis & hygiene (manual front door)

The *on-demand* theme-maintenance pass. Two jobs:

1. **Dedup** — review near-duplicate themes (same arc, two IDs) and merge.
2. **Essence refresh** — rewrite stale `## Essence` sections so the thesis
   still matches the recent catalyst log.

> **Routine dedup is `/dream`'s job (2026-06-11 doctrine).** The nightly
> cycle surfaces `theme_dup_candidates` (embedding cosine ≥ threshold,
> verdict-history-excluded), judges them in `dream-merge-worker`, and
> applies merges via the same `merge_theme_into` helper this skill uses.
> Run this skill when you want an immediate interactive pass, want to
> re-litigate a recorded `distinct` ruling, or for the one-time
> catalyst-text/title backfill (Step 3). Same helpers, same outcome —
> just user-triggered.

Both jobs are genuinely-semantic calls, so they stay in the LLM turn. Theme
*minting* and *extending* (linking newly-arrived sources to an arc) are
**not** this skill's job — `/dream` owns those from `theme_cluster_signals`.

> **No lifecycle.** As of the 2026-05-30 teardown there is no dormancy or
> resolution detection, and no `cand-*` candidate-promotion step. A theme's
> `status` changes only when the user decides — never on a timer, never from
> linked-decision state. (`find_dormant_themes` / `find_resolved_themes` /
> the candidate-stub CLI were removed.) Merging a duplicate is the one status
> change this skill makes, and only on an explicit same-arc judgment.

## Step 1 — Dedup

List the canonical theme set and look for two themes that are really the
same arc:

```
mem_search(query="", type="theme", limit=100)
```

Prefer the drift-v2 candidate generator over eyeballing — it carries
cosine + essence excerpts and excludes already-judged pairs:

```bash
uv run mem dream scan --json | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['theme_dup_candidates'], indent=2))"
```

For each suspected pair, read both (`mem_read`) and confirm they describe
the *same* unfolding event (not merely overlapping concepts — two distinct
arcs can share `geopolitics`). When they are the same arc, run the SAME
deterministic helper the dream apply step uses — do not hand-roll the moves:

```bash
uv run python -c "
from personal_mem.core.config import load_config
from personal_mem.synthesis.theme_candidates import merge_theme_into
print(merge_theme_into(load_config(), from_id='thm-LOSER', to_id='thm-SURVIVOR'))
"
```

This folds the loser's catalyst log + `cites:` into the survivor (dedup,
`fold_pending_*` provenance), repoints every `relates_to:`, sets the loser's
`status: merged-into:thm-<survivor>` (file kept — reversible), updates
`themes.yaml`, and enqueues the survivor on the seam-link queue so the next
`/dream` phase-2 pass stitches cross-parent linkage. Pairs judged NOT
duplicates: note them — the next dream cycle's merge worker records distinct
rulings; or leave them for it to judge.

## Step 2 — Essence refresh

For each active theme whose recent catalysts (read the last ~10 log entries)
have drifted from its `## Essence`, rewrite the essence in place with `Edit`
— keep it ≤500 words, slow-moving, citing concepts not named events. Touch
only essences that are genuinely stale; leave the rest. Also rewrite
placeholder essences (`_Awaiting first synthesis pass._`) and mint-time
one-liners (<50 words) on themes that have since accumulated ≥8 catalysts —
compose from the full log. When you rewrite, also set
`essence_updated: <today YYYY-MM-DD>` in the theme's frontmatter (the dream
essence-worker's growth trigger counts catalysts since that stamp).

## Step 3 — Catalyst text repair (backfill — run until the log reads clean)

Pre-2026-06 theme extensions wrote generic catalyst lines — the literal
texts `extend` and `cluster seed` instead of a distillation. For each
active theme:

1. Scan its `## Catalyst log` for entries whose text is `extend`,
   `cluster seed`, or similarly content-free.
2. `mem_read` each entry's cited source and rewrite the entry's text in
   place (`Edit`) with a 1–2 sentence distillation (≤200 chars) of what
   that source added to the arc — same artifact bar as `/update-hubs`
   extraction. Preserve the entry's date, flag, and citation link exactly;
   change only the text between the flag and the citation. Upgrade the
   flag from `new` to `agrees`/`extends`/`contradicts` when the source's
   relationship to the arc is clear.
3. If the theme has no human `title:` (frontmatter title equals the
   kebab slug), compose one in headline register ("Iran–Hormuz supply
   shock") and update both the `title:` frontmatter and the H1.

Skip themes whose logs already carry real distillations — this step is a
one-time repair for the pre-distillation era, idempotent by construction.

## Notes

- **Manual skill.** Run it when theme hygiene is due; it is not on the
  `/dream` cron path. `/dream` keeps themes *current* (mint/extend); this
  skill keeps them *clean* (merge dups, tighten essences).
- **No prompts mid-flow.** Decide, apply, report — same posture as
  `/mem-resolve-concepts`.
- **Registry.** After merges, run `uv run mem themes rebuild-registry` so
  `vault/config/themes.yaml` reflects the surviving set.
