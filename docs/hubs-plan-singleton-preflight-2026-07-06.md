# `weave hubs plan`/`status` don't cross-check against singleton concept noise before sizing a backfill

**Where:** `surfaces/cli/hubs.py` (`_hubs_plan`, `_hubs_status`), which call
`synthesis/concept_hub.build_plan` / `all_concepts_in_vault` with no
filtering against the same noise criteria `weave concepts
prune-singletons` uses; `commands/update-hubs.md`'s `--bulk` section,
which doesn't mention singleton pruning as a prerequisite.

## Context

`/tighten`'s docs (`commands/tighten.md`, step 3, "Concept structural
tail") already know about this exact cleanup and document it correctly:

> **Singleton prune** — `weave concepts prune-singletons --dry-run` then
> apply (`concepts:` only; `proposed_concepts:` is sanctuary).

So this isn't a missing feature — `prune-singletons` exists and does the
right thing. The gap is that **nothing adjacent to the hub-backfill
workflow points at it**, and the failure mode if you don't happen to know
to run it first is expensive and easy to trigger by accident.

## What happens

`prune-singletons`'s own dry-run criteria (count=1, not in
`ontology.yaml`, not matching a domain marker — "the noise floor of LLM
enrichment") is exactly the class of concept that inflates a first-time
`weave hubs plan`. On a real, actively-tagged ~3,500-note vault that had
never run `/tighten`'s structural tail, this wasn't a marginal effect:

```
Singletons: 424 total — kept 4 (ontology) + 28 (domain markers), removing 392.
Would prune 392 concept instances from 59 files.
```

**392 of 543 concepts (72%) were pure noise**, concentrated on just 59
notes. `weave hubs plan` and `weave hubs status`
(`_hubs_plan`/`_hubs_status` in `surfaces/cli/hubs.py`) have no awareness
of this distinction at all — they iterate every concept in
`all_concepts_in_vault(cfg)` and report pair counts / token estimates
that mix real, valuable concepts with hundreds of one-off tags about to
be deleted, with nothing in the output flagging the difference.

Practically, this means: run `weave hubs plan` on a vault for the first
time, see a plan with hundreds of concepts, and (per `commands/
update-hubs.md`'s own guidance to switch to `--bulk` for a large plan)
submit it to `weave drain --target hubs --via batch` — and you'll spend
real API calls synthesizing catalyst-log entries and minting hub pages
for concepts that `prune-singletons` would delete a moment later. That
work (and cost) is thrown away entirely. We only avoided this because "541
concepts feels like a lot" prompted a manual detour into `weave concepts
drift` / `prune-singletons --dry-run` before submitting the real batch —
nothing in the backfill path itself prompted that check.

## Suggested fix

- `weave hubs plan` (and/or `weave hubs status`) could cross-check its
  concept list against `prune-singletons`'s exact criteria and print a
  warning line when the noise fraction is non-trivial, e.g.:
  ```
  Note: 392 of 543 concepts in this plan are singleton, non-ontology
  terms. Consider `weave concepts prune-singletons --dry-run` before
  backfilling — see /tighten step 3.
  ```
- `commands/update-hubs.md`'s `--bulk` section could explicitly recommend
  a `prune-singletons` pass as a prerequisite for a first-time/backfill-
  scale run, the same way it already recommends switching to `--bulk
  batch` above a pair-count threshold — this is the same "warn before the
  expensive path" instinct, just missing one adjacent check.

## Related

Companion finding to `docs/migration-findings-2026-07-06.md` (item 3 in
that doc describes the concept-dedup mechanism itself working correctly
via `drift_pairs`/`coarsen_clusters` — this is a narrower, separate gap
about workflow integration between the existing hygiene tooling and the
backfill planner, not a correctness bug in either tool individually.
