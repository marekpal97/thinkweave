"""Batch concept merge: all file edits first, single index rebuild at the end."""
import sys
sys.path.insert(0, '/home/marekpal97/python_projects/personal_mem/src')

from personal_mem.concepts import merge_concept_in_notes, load_aliases, save_aliases
from personal_mem.config import Config
from personal_mem.indexer import Indexer

cfg = Config()

MERGES = [
    # Tier 1: spelling/naming variants
    ("embedding", "embeddings"),
    ("options_engine", "options-engine"),
    ("personal_finance_assistant", "personal-finance-assistant"),
    ("hive_swarm", "hive-swarm"),
    ("autoresarch", "autoresearch"),
    ("ft5", "fts5"),
    ("chroma-db", "chromadb"),
    ("status-line", "statusline"),
    ("agentic-langgraph", "langgraph"),
    ("ai-agents", "agentic-ai"),
    ("architctural-decisions", "architectural-decisions"),
    # Tier 2: semantic duplicates
    ("notebook", "notebooks"),
    ("unit-testing", "unit-tests"),
    ("checkpoint", "checkpointing"),
    ("hook-system", "hooks"),
    ("convolutional-neural-networks", "cnn"),
    ("recurrent-neural-networks", "rnn"),
    ("bugfix", "bug-fixing"),
    ("bug-fix", "bug-fixing"),
    ("agentic-systems", "agentic-ai"),
    # Domain path merges (Option B: leaf → domain path)
    ("deep-learning", "ml/deep-learning"),
    ("linear-algebra", "math/linear-algebra"),
    ("numerical-linear-algebra", "math/numerical-linear-algebra"),
    ("ml/agents", "ai/agents"),
]

aliases = load_aliases(cfg)
total_changed = 0

print(f"Running {len(MERGES)} concept merges against vault: {cfg.vault_root}\n")

for from_c, to_c in MERGES:
    changed = merge_concept_in_notes(cfg.vault_root, from_c, to_c)
    total_changed += changed
    # Update aliases
    existing = aliases.get(to_c, [])
    if from_c not in existing:
        existing.append(from_c)
    if from_c in aliases:
        for old in aliases.pop(from_c):
            if old != to_c and old not in existing:
                existing.append(old)
    aliases[to_c] = existing
    status = f"{changed} notes" if changed else "no matches"
    print(f"  {from_c} → {to_c}: {status}")

save_aliases(cfg, aliases)
print(f"\nTotal: {total_changed} notes updated across {len(MERGES)} merges")
print("Aliases saved. Rebuilding index (once)...")

idx = Indexer(config=cfg)
idx.rebuild(full=True)
idx.close()
print("Index rebuilt. Done.")
