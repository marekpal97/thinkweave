# Evidence-gated steering (issue #62)

The slow self-improvement loop (**#61** — the weekly Routine that runs
`improve-arch` / `ponytail-audit` and files issues) must not invent work. This
gate is the contract between #61's *candidate* proposals and the issues it is
allowed to file: every proposal must cite evidence from the self-improvement
substrate, and only the top-`weekly_budget` by evidence weight survive per run.

**#61 is not yet built.** This slice ships the gate #61 will call. When #61
lands it MUST route its candidates through `weave steering gate` and file **only
what the `filed` list returns** — never a raw improve-arch/ponytail suggestion.

## The three signals

All computed read-only from the derived index (`operations/steering.build_evidence_index`),
each a pure aggregation over queried rows. Raw counts only — never a composite
score baked in; the weights rank, the counts ride the evidence block.

| Signal | Source | Keyed by |
|---|---|---|
| **rework / churn** | loop-run trajectory notes' `outcome_label` ∈ {`reworked`, `reworked-post-merge`} (#60 phase-1/2 verdicts) + summed `fix_rounds` | `files_touched` |
| **superseded/contested decisions** | decisions with status `superseded`/`deprecated` OR a `supersedes`/`superseded_by` link | `file_paths` |
| **gate-failure hotspots** | trajectory `gates[]` entries with `passed=false` | `files_touched` |

(A fourth signal — "behavioral pressure", per-concept PageRank from
`graph_ranks` — was deleted by #96: knowledge-ontology centrality is a category
error as a code-module-importance measure, and it was zero-default on any
undreamed vault anyway. Steering runs on the three observable signals above.)

## The gate contract

`gate_proposals(candidates, evidence_index, cfg) -> {filed, dropped}`:

- A **candidate** is `{module | paths, rationale}`. `paths` (or a single
  `module`) are repo paths; matching is prefix-aware (`src/ops/` covers
  `src/ops/dream.py`; `a/b.py` does not swallow `a/bc.py`).
- A candidate with **no nonzero evidence signal** is **dropped**
  (`reason: "no cited evidence"`) — the anti-invention gate. Admission is about
  evidence *presence*, deliberately independent of the weights (a signal weighted
  0 but with a nonzero raw count is still real evidence).
- Survivors are ranked by evidence `weight` (weighted sum of the raw counts,
  desc, stable on ties) and capped at `weekly_budget`; the overflow is dropped
  (`reason: "exceeded weekly budget"`).
- Each **filed** entry gains a `body` embedding a machine-readable ` ```json `
  evidence block (`{module, rework_count, fix_rounds, superseded_decisions,
  gate_failures, weight}`) — real counts from the index, never invented — plus
  an `evidence` dict of the same.

## CLI

```
weave steering evidence [--module PATH] [--json]     # inspect the computed signals
weave steering gate --proposals-json <file> [--json] # gate a candidate batch → {filed, dropped}
```

`--proposals-json` is a list of candidates (or `{"candidates": [...]}`). `#61`
writes its improve-arch/ponytail candidates there, runs `gate --json`, and files
each `filed[i].body` as an issue.

## Config knobs (`[steering]` in `config.toml`)

```toml
[steering]
weekly_budget = 3          # max proposals filed per run (the anti-invention cap)
weight_rework = 1.0        # per-signal multipliers for the evidence weight;
weight_fix_rounds = 1.0    # raw counts are always preserved, weights only rank
weight_superseded = 1.0
weight_gate_failures = 1.0
```

All optional; unset keys keep the defaults (budget 3, every weight 1.0). Nothing
is hardcoded posture — the budget and weights are loop config knobs.
