# /mem-resolve-concepts — Concept & Ontology Hygiene

Periodic concept maintenance. Merge duplicates, prune noise, update ontology, regenerate hubs. Designed to run in under 2 minutes.

## Steps

### 1. Scan

Run `mem_concepts_drift(threshold=5, max_items=30)` to get near-duplicates and ontology candidates. That's the only discovery call you need — do NOT call `mem_concepts_tighten` (too noisy) or dump the full concept list.

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
from personal_mem.concepts import generate_hub_pages, add_hub_wikilinks, hubs_marker_path, load_ontology
from personal_mem.indexer import Indexer

ontology = load_ontology()
generate_hub_pages(cfg, ontology)
add_hub_wikilinks(cfg, ontology)

idx = Indexer(config=cfg)
idx.rebuild(full=True)
idx.close()

# Touch marker so drift_report knows hubs are fresh
hubs_marker_path(cfg).touch()
```

This ensures `ontology.yaml` changes propagate to hub pages, wikilinks, and the index in one pass. No manual `mem concepts hubs` step needed.

### 5. Report (3 lines)

```
Done. Merged N pairs (X notes). Added M to ontology. Hubs regenerated. Concepts: before → after.
```

No verification drift check needed — if the merges ran without error, they worked.
