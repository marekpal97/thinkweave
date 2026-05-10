---
name: mem-resolve-concepts
owns_mechanic: ontology_hygiene
consumes: [mem_concepts]
produces: [ontology.yaml, vault/concepts/topics/*.md]
tools:
  - Read
  - Edit
  - Bash
  - mem_concepts
description: Periodic concept and ontology hygiene. Merge near-duplicates, prune noise, update `ontology.yaml`, regenerate concept hubs. Designed to run under 2 min.
---

# /mem-resolve-concepts — Concept & Ontology Hygiene

Periodic concept maintenance, organised into three phases:

1. **Concepts** — merge near-dupes, surface ontology candidates, surface redundant hubs.
2. **Hubs** — prune orphan hub pages, regenerate skeletons for any new ontology concept, re-render Evolution sections.
3. **Ontology** — write back accepted changes to `ontology.yaml`, prune dead vocabulary, rebuild the index.

Designed to run in under 2 minutes. Steps below correspond to phases.

## Steps

### 1. Scan

Two discovery calls cover everything:

- `mem_concepts_drift(threshold=5, max_items=30)` — near-duplicate concepts and ontology candidates (string-similarity-based; filtered below).
- `uv run mem doctor` — coherence linter: tag/concept overlap, unknown tags, and **dead vocabulary** (ontology concepts assigned to <2 notes). The dead-vocab list feeds Phase 3's ontology pruning step.

Optionally surface redundant-hub candidates with `uv run mem concepts drift --hubs` — pre-filtered hub pairs whose Essence content overlaps (Jaccard ≥ 0.4). The pair list is structural; semantic judgment lives in Phase 3.

Do NOT call `mem_concepts_tighten` (too noisy) or dump the full concept list.

### 2. Filter drift output (deterministic)

Drift detection produces many string-similarity false positives. The
filtering rules are now Python helpers — pipe the raw drift output through
them before applying any LLM judgment:

```python
from personal_mem.synthesis.concepts import (
    filter_drift_candidates,        # near-dup pairs
    filter_promotion_candidates,    # ontology candidates
)

surviving_pairs = filter_drift_candidates(drift["near_duplicates"])
surviving_candidates = filter_promotion_candidates(drift["candidates"])
```

What the helpers drop:

- Near-dup pairs where both concepts are ≤3 chars (short concepts produce
  noisy edit-distance hits like `ai` ≈ `api`).
- Substring matches where the shorter concept is a generic English word
  that happens to appear inside a longer domain term (`activation-functions`
  ≈ `function`).
- Promotion candidates that are domain-path concepts (`swe-python`),
  generic process terms (`architecture`, `testing`), or contain underscores
  (project-name leakage like `personal_mem`).

Apply LLM judgment **only on the survivors** — decide whether each pair is
a real near-dup (typo, plural, alias) and whether each candidate is a
genuine domain term worth promoting.

### 3. Present compact action plan

One table, one pass. No prose explanations, no separate sections. Format:

```
## Concept Hygiene — Action Plan

Stats: X total concepts, Y singletons, Z ontology coverage

### Merges (N pairs)
| From → To | Notes | Reason |
|-----------|-------|--------|
| `old` → `new` | 13 | plural |

### Ontology additions (N)
| Concept (count) | Domain |
|-----------------|--------|
| `wandb` (18) | ml-training |

Approve all and go, or list exceptions.
```

Keep the tables tight — no row numbers, no separate count columns, no explanations beyond one word. The user should be able to scan this in 10 seconds.

### 4. Execute on approval

**Merges**: Use `scripts/batch_merge.py` pattern: write a temporary Python script that runs all merges with a single index rebuild at the end. Do NOT call `mem_concepts_merge` N times.

```python
# Pattern: all file edits first, one rebuild at end
for from_c, to_c in merges:
    merge_concept_in_notes(vault_root, from_c, to_c)
    # update aliases in memory
save_aliases(cfg, aliases)
```

**Ontology additions**: Edit `src/personal_mem/ontology.yaml` directly — concepts sorted alphabetically within their domain sections. This is a source file, not a vault artifact.

**Hub regeneration + index rebuild** (always, after merges and/or ontology edits):

```python
from personal_mem.synthesis.concepts import (
    add_hub_wikilinks,
    generate_domain_hubs,
    generate_concept_hub_skeletons,
    hubs_marker_path,
    load_ontology,
)
from personal_mem.core.indexer import Indexer

ontology = load_ontology()
generate_domain_hubs(cfg, ontology)              # thin navigation pages
generate_concept_hub_skeletons(cfg, ontology)    # no-op if concept hub exists
add_hub_wikilinks(cfg, ontology)

idx = Indexer(config=cfg)
idx.rebuild(full=True)
idx.close()

# Touch marker so drift_report knows hubs are fresh
hubs_marker_path(cfg).touch()
```

`generate_concept_hub_skeletons` NEVER overwrites existing concept hubs — it only creates empty stubs for concepts that don't have a hub yet. LLM-written essence and learning-log content is preserved across regenerations. This ensures `ontology.yaml` changes propagate to hub pages, wikilinks, and the index in one pass. No manual `mem concepts hubs` step needed.

### 4.4. Surface promotion candidates from proposed_concepts (default step)

Strict creation policy means new vocabulary lives in `proposed_concepts:`
until a term reaches critical mass. This step lifts those that have:

```bash
uv run mem concepts proposed-counts --min-count 5
```

Pipe the proposed-counts output through `filter_promotion_candidates()`
(same helper as step 2) — it strips domain-path concepts, generic process
terms, and underscore-bearing project-name leakage. Apply LLM judgment
only on the survivors to decide which genuinely deserve canonicalisation,
then present them as a compact promotion table:

```
### Promotions (N candidates)
| Term (count) | Domain | Reason |
|--------------|--------|--------|
| `streaming-ingestion` (8) | swe-data | recurrent across pipeline notes |
| `regime-shift` (6) | finance-markets | thematic in trade-ideas |
```

On approval, run one promotion per row:

```bash
uv run mem concepts promote streaming-ingestion --domain swe-data
uv run mem concepts promote regime-shift --domain finance-markets
```

Each call: writes the term into `vault/.mem/ontology.yaml` under the
chosen domain, walks every note carrying it in `proposed_concepts:` and
moves it to `concepts:`, ensures the hub skeleton at
`vault/concepts/topics/{term}.md`, and rebuilds the index.

Below-threshold proposed terms persist — they may grow into promotion
candidates next round, or get caught by `prune-singletons` if they
remain count=1 noise.

### 4.5. Canonical singleton noise prune (default step)

Run **after** merges and promotions land — both can shift a count=1
concept into count≥2, so this comes last. Strips the canonical
(`concepts:`) singleton noise floor:

```bash
uv run mem concepts prune-singletons --dry-run    # preview
uv run mem concepts prune-singletons              # apply (rebuilds index)
```

**Scope: `concepts:` only — `proposed_concepts:` is sanctuary by
design.** Emergent vocabulary enters proposed at count=1 (its natural
starting state). Pruning that field on count alone would undo the
demotion sweep's work and erase legitimate candidates that simply
haven't accumulated yet. Cleaning the proposed pool happens through
promotion (step 4.4) or via the `/mem-resolve-concepts` reviewer's
explicit kill list — never via automated count-based pruning.

A canonical singleton is kept when:

- the concept appears in the merged ontology (seed + vault override), or
- any domain-marker substring matches the concept name. The built-in
  marker set (math, ML, finance, fitness, physics, common tools) lives at
  `synthesis/concepts.py:DOMAIN_MARKERS`; add domains specific to your
  vault by listing substrings under
  `<vault>/.mem/ontology.yaml::domain_markers` — vault entries *extend*
  the built-ins (never replace them) so package upgrades stay safe.

Anything else is pruned. Under the strict creation policy this prune is
mostly a guardrail — once `demote-non-ontology` has run, the only
canonical singletons left are pre-policy leftovers and direct vault
edits. If a domain you care about isn't covered by the markers (e.g.
you want all `chem-*` singletons preserved), add `chem` (or any other
substring that matches your concept names) to your vault override's
`domain_markers:` list and rerun.

### 5. Hub coherence review

After merges, ontology edits, and regeneration, do one sweep over the concept hubs at `vault/concepts/topics/*.md` to check their health. This step is LLM judgment, not rule-based — no numeric thresholds.

For each concept hub (or a sample of 10–15 per run if the vault is large), read it and check three things:

**Split candidate**: Has the learning log drifted into multiple distinct sub-concepts? If the entries tagged `agentic-harness` are really about three separate things (memory, planning, tool use), propose splitting. Output: `split: <concept> → [<child-1>, <child-2>, ...]` with one-line reasoning.

**Merge candidate**: Does another hub cover materially the same ground? If `knowledge-graph` and `concept-graph` have log entries that are essentially about the same thing, propose merge. Output: `merge: <concept-a> + <concept-b>` with one-line reasoning. Cross-reference with step 2's near-duplicate list — many merge candidates will already be there.

**Stale essence**: Has the essence been overtaken by recent log entries? If the essence says "X works like Y" but half the recent log entries contradict that, flag for revision. Output: `rewrite essence: <concept>` with one-line reasoning. If `mem hubs run` flagged it during backfill, it'll be obvious here.

Present as a compact section in the action plan:

```
### Hub hygiene (N candidates)
| Action | Concept(s) | Reason |
|--------|------------|--------|
| split | agentic-harness → memory, planning, tool-use | log drifted across distinct areas |
| merge | knowledge-graph + concept-graph | same idea |
| rewrite essence | transformer | 5 contradicts entries since last revision |
```

**Do not autofix these.** Present suggestions, the user approves selectively. Splits and merges happen via `mem concepts merge` (for merges) or manual editing of the ontology + hub page splits (for splits). Essence rewrites happen inline via Edit on the hub page — read the last ~15 log entries, rewrite the essence to reflect current understanding, keep it ≤500 words.

Skip this step entirely if there are no concept hubs yet (fresh vault).

### 6. Phase 2 — Hubs: archive orphans

After merges and/or ontology edits, run:

```bash
uv run mem concepts hubs --prune          # dry-run: list orphan hubs
uv run mem concepts hubs --prune --apply  # archive them
```

An orphan hub is a `vault/concepts/topics/<concept>.md` whose underlying concept has zero vault assignments AND isn't in `ontology.yaml`. **`--apply` archives, not deletes** — files move to `vault/concepts/topics/_archive/` so the synthesis work is preserved if the concept gets re-promoted later. The directory lives *inside* `topics/` so non-recursive scans (`mem hubs status`, `mem hubs link`, `mem hubs repair`) skip archived files automatically.

Hub archival also runs *automatically* at the end of `uv run mem concepts demote-non-ontology` — every term that exits the canonical pool has its hub relocated in the same operation. This step catches leftovers from ad-hoc ontology edits or older merges that pre-date the doctrine.

### 7. Phase 3 — Ontology: prune dead vocabulary

Re-run `uv run mem doctor` to see the **Dead vocabulary** section. For any concept with 0 notes that's still in `ontology.yaml`:

- If it's clearly a typo or merged-away concept → remove from `ontology.yaml`.
- If it's a real but unused concept (legitimately zero notes today) → leave it; the ontology can be aspirational for slow-growing domains.

Don't auto-prune. The user approves selectively.

### 8. Report (3 lines)

```
Done. Merged N pairs (X notes). Added M to ontology, removed K dead.
Pruned P orphan hubs. H hub hygiene suggestions (approved S). Concepts: before → after.
```

No verification drift check needed — if the merges ran without error, they worked.
