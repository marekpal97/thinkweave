---
name: themes-resolve
owns_mechanic: theme_synthesis
description: Periodic theme hygiene — merge near-duplicate themes and rewrite stale essences. Mirrors /mem-resolve-concepts for the global theme set.
tools: [Read, Edit, Bash, mem_search, mem_read, mem_update, mem_link]
---

# /themes-resolve — Theme synthesis & hygiene

The periodic *manual* theme-maintenance pass. Two jobs:

1. **Dedup** — find near-duplicate themes (same arc, two IDs) and merge.
2. **Essence refresh** — rewrite stale `## Essence` sections so the thesis
   still matches the recent catalyst log.

Both are genuinely-semantic calls, so they stay in the LLM turn. Theme
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

For each suspected pair, read both (`mem_read`) and confirm they describe
the *same* unfolding event (not merely overlapping concepts — two distinct
arcs can share `geopolitics`). When they are the same arc:

- Pick the survivor (the better slug / richer essence / earlier id).
- Move any unique catalyst-log entries and `cites:` from the loser into the
  survivor (`Edit`).
- Set the loser's `status: merged-into:thm-<survivor>` via `mem_update`.
- Repoint sources that `relates_to:` the loser at the survivor
  (`mem_link` / `mem_unlink`).

## Step 2 — Essence refresh

For each active theme whose recent catalysts (read the last ~10 log entries)
have drifted from its `## Essence`, rewrite the essence in place with `Edit`
— keep it ≤500 words, slow-moving, citing concepts not named events. Touch
only essences that are genuinely stale; leave the rest.

## Notes

- **Manual skill.** Run it when theme hygiene is due; it is not on the
  `/dream` cron path. `/dream` keeps themes *current* (mint/extend); this
  skill keeps them *clean* (merge dups, tighten essences).
- **No prompts mid-flow.** Decide, apply, report — same posture as
  `/mem-resolve-concepts`.
- **Registry.** After merges, run `uv run mem themes rebuild-registry` so
  `vault/.mem/themes.yaml` reflects the surviving set.
