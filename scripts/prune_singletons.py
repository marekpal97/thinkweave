"""Remove noisy singleton concepts (count=1, not domain-relevant) from vault notes.

Keeps: ontology concepts, domain-specific terms (math, ML, finance, fitness, health).
Removes: everything else with count=1.
"""
import sys
sys.path.insert(0, '/home/marekpal97/python_projects/personal_mem/src')

from pathlib import Path
from personal_mem.concepts import get_all_concepts, load_ontology, build_keep_set
from personal_mem.config import Config
from personal_mem.indexer import Indexer
from personal_mem.vault import VaultManager

cfg = Config()
idx = Indexer(config=cfg)
all_concepts = get_all_concepts(idx.db)
idx.close()

ontology = load_ontology()
ontology_set = build_keep_set(ontology)

singletons = {c for c, count in all_concepts.items() if count == 1}
non_singletons = {c for c, count in all_concepts.items() if count >= 2}

# Domain vocabulary markers — concepts containing these are kept
DOMAIN_MARKERS = {
    # Math
    'theorem', 'lemma', 'proof', 'equation', 'polynomial', 'matrix', 'vector',
    'integral', 'derivative', 'convergence', 'distribution', 'variance',
    'eigenvalue', 'factorization', 'decomposition', 'approximation',
    'coefficient', 'exponent', 'logarithm', 'algebra',
    'calculus', 'topology', 'manifold', 'subspace', 'orthogonal',
    'gaussian', 'binomial', 'poisson', 'bayesian', 'stochastic',
    'combinatorics', 'permutation', 'combinations', 'probability',
    'fourier', 'laplace', 'markov', 'monte-carlo', 'mcmc',
    'stirling', 'cauchy', 'riemann', 'seminorm', 'semidefinite',
    # ML/DL
    'neural', 'gradient', 'backprop', 'activation', 'embedding',
    'attention', 'transformer', 'encoder', 'decoder', 'convolution',
    'pooling', 'dropout', 'normalization', 'regularization',
    'classifier', 'regression', 'clustering', 'reinforcement',
    'supervised', 'unsupervised', 'self-supervised', 'contrastive',
    'loss-function', 'epoch', 'learning-rate', 'optimizer',
    'overfitting', 'underfitting',
    'precision', 'recall',
    'cnn', 'rnn', 'lstm', 'gru', 'gan', 'vae',
    'bert', 'gpt', 'tokeniz', 'embed',
    'tf-idf', 'nlp', 'sparsity', 'negative-sampling',
    'hinge-loss', 'class-weight', 'resampling', 'bootstrap',
    'layernorm', 'batchnorm',
    # Finance
    'option', 'volatility', 'delta', 'gamma', 'theta', 'vega',
    'portfolio', 'sharpe', 'hedge', 'futures',
    'bond', 'equity', 'valuation', 'dcf', 'arbitrage',
    'leverage', 'margin', 'spread', 'condor',
    'straddle', 'strangle', 'collar', 'covered',
    'black-scholes', 'fama-french', 'capm',
    'brokerage', 'trading', 'market-making',
    'retail-investor', 'sell-side',
    # Fitness/Health
    'hypertrophy', 'strength', 'muscle', 'protein', 'calori',
    'exercise', 'bench', 'squat', 'deadlift',
    'pull-up', 'push-up', 'cardio', 'hiit', 'recovery',
    'tendon', 'biomechanic', 'collagen', 'creatine',
    'vitamin', 'supplement', 'macronutrient', 'nutrition',
    'cancer', 'mole', 'dermatolog', 'vaccination', 'allerg',
    'infection', 'medication', 'dosage', 'symptom', 'shoulder',
    'gastrointestinal', 'digestion', 'musculoskeletal',
    # Quantum/Physics
    'quantum', 'qubit', 'photon', 'particle',
    'antimatter', 'cern',
    # Specific proper nouns / tools worth keeping
    'beveridge', 'graphql', 'keras', 'networkx', 'spark',
    'itertools', 'geojson', 'vitest', 'typescript',
}


def is_domain_concept(concept: str) -> bool:
    c_lower = concept.lower()
    for marker in DOMAIN_MARKERS:
        if marker in c_lower:
            return True
    return False


# Build keep set: ontology + domain-matching singletons + all non-singletons
keep = set()
remove = set()

for concept in singletons:
    if concept in ontology_set:
        keep.add(concept)
    elif is_domain_concept(concept):
        keep.add(concept)
    else:
        remove.add(concept)

print(f"=== Singleton Prune Plan ===")
print(f"Total singletons: {len(singletons)}")
print(f"Keeping: {len(keep)} (domain concepts / in ontology)")
print(f"Removing: {len(remove)}")
print()

# Now actually remove concepts from notes
files_modified = 0
concepts_removed = 0

for path in cfg.vault_root.rglob("*.md"):
    # Skip landing docs and non-vault files
    rel = str(path.relative_to(cfg.vault_root))
    if rel.startswith("concepts/") or path.name in ("DECISIONS.md", "BACKLOG.md", "STATE.md"):
        continue

    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        continue

    # Quick check — does this file even have concepts?
    if "concepts:" not in text:
        continue

    # Parse frontmatter to get concepts
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        continue

    # Find end of frontmatter
    end_idx = None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        continue

    # Find concepts line
    concepts_line_idx = None
    for i in range(1, end_idx):
        if lines[i].startswith("concepts:"):
            concepts_line_idx = i
            break
    if concepts_line_idx is None:
        continue

    # Parse concepts (inline YAML list)
    raw = lines[concepts_line_idx].split(":", 1)[1].strip()
    if raw.startswith("["):
        raw = raw.strip("[]")
    concepts = [c.strip().strip('"').strip("'").lower() for c in raw.split(",") if c.strip()]

    # Filter out removed singletons
    original_count = len(concepts)
    filtered = [c for c in concepts if c not in remove]

    if len(filtered) < original_count:
        removed_here = original_count - len(filtered)
        concepts_removed += removed_here

        if filtered:
            new_line = f"concepts: [{', '.join(filtered)}]"
        else:
            new_line = "concepts: []"

        lines[concepts_line_idx] = new_line
        path.write_text("\n".join(lines), encoding="utf-8")
        files_modified += 1

print(f"Files modified: {files_modified}")
print(f"Concept instances removed: {concepts_removed}")
print(f"\nRebuilding index...")

idx2 = Indexer(config=cfg)
idx2.rebuild(full=True)
idx2.close()
print("Done.")
