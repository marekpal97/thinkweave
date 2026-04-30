---
name: mem-resolve-concepts
tools:
  - Read
  - Edit
  - Bash
  - mem_concepts
  - mem_concepts_drift
  - mem_concepts_merge
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

### 2. Filter drift output with LLM judgment

The drift detector uses string similarity which produces many false positives. Apply these filters before presenting anything:

**Near-duplicate filtering rules:**
- DISCARD edit-distance matches where both concepts are <=3 characters (e.g. `ai` ≈ `api`, `1rm` ≈ `gru`) — short concepts are almost never true duplicates
- DISCARD substring matches where the shorter concept is a generic English word that happens to appear inside a longer domain term (e.g. `activation-functions` ≈ `function`, `ab-testing` ≈ `testing`)
- KEEP only pairs where the concepts genuinely refer to the same thing: typos (`autoresarch` → `autoresearch`), singular/plural (`embedding` → `embeddings`), naming variants (`options_engine` → `options-engine`), redundant prefixes (`agentic-langgraph` → `langgraph`)

**Ontology candidate filtering rules:**
- DISCARD domain-path concepts used as tags (e.g. `swe/python`, `ml/deep-learning`) — these are ontology structure, not missing entries
- DISCARD generic process terms (e.g. `architecture`, `testing`, `documentation`, `configuration`) — these are tags, not concepts
- DISCARD project names used as concepts (e.g. `hive_swarm`, `options_engine`)
- KEEP genuine domain terms that belong in the ontology but aren't there yet

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
| `wandb` (18) | ml/training |

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
from personal_mem.concepts import (
    add_hub_wikilinks,
    generate_domain_hubs,
    generate_concept_hub_skeletons,
    hubs_marker_path,
    load_ontology,
)
from personal_mem.indexer import Indexer

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

### 6. Phase 2 — Hubs: prune orphans

After merges and/or ontology edits, run:

```bash
uv run mem concepts hubs --prune          # dry-run: list orphan hubs
uv run mem concepts hubs --prune --apply  # delete them
```

An orphan hub is a `vault/concepts/topics/<concept>.md` whose underlying concept has zero vault assignments AND isn't in `ontology.yaml`. After a merge, the renamed concept's hub is auto-deleted by `mem concepts merge`, so this catches leftovers from older merges or ad-hoc deletions.

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
