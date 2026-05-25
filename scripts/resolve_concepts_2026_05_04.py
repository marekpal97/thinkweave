"""Concept resolve run — 2026-05-04.

3 merges + 3 ontology additions, single index rebuild, hub regen.
"""
import sys
sys.path.insert(0, '/home/marekpal97/python_projects/personal_mem/src')

from personal_mem.synthesis.concepts import (
    add_hub_wikilinks,
    generate_concept_hub_skeletons,
    generate_domain_hubs,
    hubs_marker_path,
    load_aliases,
    load_ontology,
    merge_concept_in_notes,
    save_aliases,
)
from personal_mem.core.config import load_config
from personal_mem.core.indexer import Indexer

cfg = load_config()

MERGES = [
    ("attention-mechanism", "attention"),
    ("autoresearch-module", "autoresearch"),
    ("math/numerical-linear-algebra", "math/linear-algebra"),
]

aliases = load_aliases(cfg)
total_changed = 0
print(f"Running {len(MERGES)} merges against vault: {cfg.vault_root}\n")
for from_c, to_c in MERGES:
    changed = merge_concept_in_notes(cfg.vault_root, from_c, to_c)
    total_changed += changed
    existing = aliases.get(to_c, [])
    if from_c not in existing:
        aliases[to_c] = existing + [from_c]
    print(f"  {from_c} → {to_c}: {changed} note(s)")

save_aliases(cfg, aliases)
print(f"\nMerged: {total_changed} note edits.")

# Re-render hubs and rebuild index in one pass.
ontology = load_ontology()
print("\nRegenerating concept hubs and domain hubs...")
generate_domain_hubs(cfg, ontology)
generate_concept_hub_skeletons(cfg, ontology)
add_hub_wikilinks(cfg, ontology)

print("\nRebuilding index...")
idx = Indexer(config=cfg)
idx.rebuild(full=True)
idx.close()

hubs_marker_path(cfg).touch()
print("\nDone.")
