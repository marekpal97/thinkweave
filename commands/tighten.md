---
name: tighten
owns_mechanic: ontology_hygiene
description: On-demand ontology-tightening front door for BOTH hub families — review drift-v2 dedup pairs AND N-ary grain-coarsening clusters (concepts + themes) in one approval table, then the per-family structural tails (promotion, dead-vocab, catalyst/title backfill). One process; the nightly /dream runs the same mechanism unattended.
tools: [Read, Edit, Bash, weave_concepts, weave_search, weave_read, weave_update]
---

# /tighten — Ontology tightening (unified front door)

The single on-demand front door over the same mechanism the nightly `/dream`
runs unattended. It replaces the split between `/mem-resolve-concepts` and
`/themes-resolve` — tightening is **one process** (a structural audit feeding
a dedup/coarsen step), symmetric across concept hubs and themes.

> **Why one skill.** The nightly `dream-merge-worker` already judges both
> families and both grains (pairwise merges + N-ary coarsenings). This skill
> is the *interactive, approval-gated* front of the identical `weave dream
> apply` path — run it for an immediate pass, to apply coarsenings when
> `dream.coarsen_apply` is off (surface-only posture), to re-litigate a
> recorded `distinct` ruling, or for the harder structural work the nightly
> loop leaves alone (splits, domain moves, dead-vocab pruning, catalyst/title
> backfill).

Designed to run in a few minutes. Steps below; apply only on approval.

## 1. Scan (shared, both families)

```bash
uv run weave dream scan --json > /tmp/tighten-scan.json   # all surfaces
uv run weave doctor                                       # tag/concept overlap, DEAD vocab
```

The scan payload carries, for this skill:

- `drift_pairs` — pairwise concept dedup (string ∪ centroid-cosine, evidence packets).
- `coarsen_clusters` — N-ary concept near-cliques that may collapse onto a coarser term (each with `members`, `min_cosine`, `common_domain`, `canonical_target_hint`).
- `theme_dup_candidates` — pairwise theme dedup.
- `theme_coarsen_clusters` — N-ary theme near-cliques (over-split arcs).

Judged items (past merges / coarsenings / distinct rulings) are excluded via
the maintenance-log verdict history. To re-open them, re-run with
`weave dream scan --rejudge`.

## 2. Cluster review — one approval table (both families)

Apply LLM judgment to the survivors. Use the same disambiguation discipline
as `dream-merge-worker`: a **merge** is same-concept (typo/plural/synonym); a
**coarsening** is distinct-but-finer siblings under one umbrella
(`theta`/`vega`/`gamma` → `greeks`); **distinct** is genuinely-separate grains
(permanent memory — write the reason). Present compact, one word of reasoning:

```
## Tightening — Action Plan

### Concept merges (N)            | from → to | notes | reason |
### Concept coarsenings (N)       | members → target (new?) | domain | reason |
### Theme merges (N)              | from → to | reason |
### Theme coarsenings (N)         | members → survivor | reason |

Approve all and go, or list exceptions.
```

On approval, assemble ONE plan JSON with the relevant keys and apply through
the shared path **with `--force-coarsen`** (so coarsenings fold even when the
nightly posture `dream.coarsen_apply` is false):

```bash
echo '<plan json: merges/coarsenings/theme_merges/theme_coarsenings/distinct_pairs/distinct_clusters>' \
  | uv run weave dream apply --plan - --force-coarsen --no-strict
```

Apply folds each loser hub into the winner (log preserved, archived
`merged-into:` tombstone), writes any new coarse term to `ontology.yaml`,
and records the verdict (with the `member_note_ids` + `fold_dates` snapshot
that makes `weave dream revert-coarsen <target>` an exact re-split). Distinct
rulings are recorded permanently.

## 3. Concept structural tail (audit the nightly loop leaves alone)

These need source-file edits and human approval, so they live here, not in
`/dream`. Run as needed:

- **Promotions** — `uv run weave concepts proposed-counts --min-count 5`, pipe
  through `filter_promotion_candidates`, then `uv run weave concepts promote <term> --domain <d>` per approved row.
- **Singleton prune** — `uv run weave concepts prune-singletons --dry-run` then apply (`concepts:` only; `proposed_concepts:` is sanctuary).
- **Dead vocabulary** — from `weave doctor`, remove clearly-dead terms from `ontology.yaml` by hand (leave aspirational ones).
- **Hub splits** — read 10–15 hubs; if a learning log drifted across distinct sub-concepts, propose `split: <c> → [child…]` (manual ontology edit + hub split). Present, don't autofix.
- **Orphan hubs** — `uv run weave concepts hubs --prune --apply`.

(These mirror the old `/mem-resolve-concepts` steps verbatim — same helpers.)

## 4. Theme structural tail

- **Essence refresh** — for an active theme whose `## Essence` is overtaken by recent catalysts, rewrite it inline (≤500 words) and stamp `essence_updated: YYYY-MM-DD`. (Routine essence composition runs nightly in `dream-essence-worker`; this is for an immediate fix.)
- **Catalyst / title backfill** — the one-time repair for legacy themes with generic catalyst text (`"extend"`, `"cluster seed"`) or missing titles: distill real catalyst lines from the cited sources and write a human `title:`.

## 5. Rebuild + report

`weave dream apply` already rebuilt the index for the cluster step. After any
manual Step 3/4 edits, rebuild once:

```bash
uv run weave index --full
```

Report (3 lines): merges/coarsenings applied (per family), promotions/dead-vocab/splits, theme essence/catalyst fixes, concept count before → after.
