"""Classify count=1 concepts as 'keep' (legitimate domain terms) or 'noise' (session artifacts).

Prints two lists: concepts to keep (legitimate) and concepts to remove (noise).
Does NOT modify any files — review output before running prune.
"""
import sys
sys.path.insert(0, '/home/marekpal97/python_projects/personal_mem/src')

from personal_mem.synthesis.concepts import get_all_concepts, load_ontology, build_keep_set
from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer

cfg = Config()
idx = Indexer(config=cfg)
all_concepts = get_all_concepts(idx.db)
idx.close()

ontology = load_ontology()
ontology_set = build_keep_set(ontology)

singletons = {c for c, count in all_concepts.items() if count == 1}

# Domain-specific vocabulary that indicates a legitimate concept (even at count=1)
DOMAIN_MARKERS = {
    # Math
    'theorem', 'lemma', 'proof', 'equation', 'polynomial', 'matrix', 'vector',
    'integral', 'derivative', 'convergence', 'distribution', 'variance',
    'eigenvalue', 'factorization', 'decomposition', 'approximation',
    'coefficient', 'exponent', 'logarithm', 'trigonometric', 'algebra',
    'calculus', 'topology', 'manifold', 'subspace', 'orthogonal',
    'gaussian', 'binomial', 'poisson', 'bayesian', 'stochastic',
    'combinatorics', 'permutation', 'combinations', 'probability',
    'fourier', 'laplace', 'markov', 'monte-carlo', 'mcmc',
    'stirling', 'cauchy', 'riemann',
    # ML/DL
    'neural', 'gradient', 'backprop', 'activation', 'embedding',
    'attention', 'transformer', 'encoder', 'decoder', 'convolution',
    'pooling', 'dropout', 'normalization', 'regularization',
    'classifier', 'regression', 'clustering', 'reinforcement',
    'supervised', 'unsupervised', 'self-supervised', 'contrastive',
    'loss', 'epoch', 'batch', 'learning-rate', 'optimizer',
    'inference', 'overfitting', 'underfitting', 'bias',
    'precision', 'recall', 'f1', 'roc', 'auc',
    'cnn', 'rnn', 'lstm', 'gru', 'gan', 'vae',
    'bert', 'gpt', 'llm', 'tokeniz', 'embed',
    'tf-idf', 'nlp', 'sparsity', 'sampling',
    # Finance
    'option', 'volatility', 'delta', 'gamma', 'theta', 'vega',
    'portfolio', 'sharpe', 'hedge', 'derivative', 'futures',
    'bond', 'equity', 'valuation', 'dcf', 'arbitrage',
    'risk', 'leverage', 'margin', 'spread', 'condor',
    'straddle', 'strangle', 'collar', 'covered',
    'black-scholes', 'fama-french', 'capm',
    'brokerage', 'trading', 'market',
    # Fitness
    'hypertrophy', 'strength', 'muscle', 'protein', 'calori',
    'exercise', 'rep', 'set', 'bench', 'squat', 'deadlift',
    'pull-up', 'push-up', 'cardio', 'hiit', 'recovery',
    'tendon', 'biomechanic', 'collagen', 'creatine',
    'vitamin', 'supplement', 'macronutrient', 'nutrition',
    # Quantum/Physics
    'quantum', 'qubit', 'photon', 'particle', 'wave',
    'relativity', 'entropy', 'thermodynamic',
    # Health/Medical
    'cancer', 'mole', 'dermatolog', 'vaccination', 'allerg',
    'infection', 'medication', 'dosage', 'symptom',
}

# Noise patterns: implementation/process artifacts, meta-concepts, session debris
NOISE_PREFIXES = [
    'sprint-', 'captain-', 'cell-', 'worker-',
    'briefing-', 'display-', 'terminal-',
    'dag-', 'plan-', 'module-',
    'ranking-', 'score-', 'scores-',
    'checkpoint-', 'commit-', 'branch-',
    'config-', 'configuration-',
    'output-', 'input-',
    'plugin-', 'skill-',
    'hook-', 'session-',
    'codebase-', 'code-',
    'project-', 'sprint-',
    'architecture-', 'architectural-',
    'implementation-', 'infrastructure-',
    'experiment-', 'evaluation-',
    'model-',  # model-coding, model-debugging, etc.
    'training-',  # training-loop, training-state, etc.
    'data-',  # data-access, data-collection, etc.
    'file-', 'schema-',
    'decision-', 'task-',
    'storage-', 'state-',
    'context-', 'event-',
    'narrative-', 'observation-',
]

NOISE_SUFFIXES = [
    '-implementation', '-management', '-command',
    '-script', '-module', '-pipeline',
    '-tracking', '-detection', '-system',
    '-verification', '-assessment',
    '-configuration', '-documentation',
    '-restructuring', '-refactoring',
    '-cleanup', '-improvements',
]

# Exact noise matches (too generic or meta)
NOISE_EXACT = {
    'analysis', 'design', 'function', 'algorithm', 'framework',
    'interface', 'client', 'server', 'status', 'loading',
    'capture', 'content', 'context', 'display', 'graph',
    'hook', 'index', 'memory', 'notes', 'outline',
    'package', 'patch', 'progress', 'protocol', 'rendering',
    'research', 'review', 'routing-function', 'saving', 'scoring',
    'script', 'selection', 'storage', 'strategy', 'summary',
    'tracking', 'ui', 'verification', 'workflow', 'todo',
    'navigation', 'notification', 'planning', 'production',
    'coordination', 'stability', 'resilience', 'scalability',
    'efficiency', 'readability', 'reuse', 'naming', 'defaults',
    'success', 'failures', 'findings', 'guidance', 'methods',
    'outcomes', 'patterns', 'criteria', 'observations',
    '__init__()', '__new__()', 'evaluate.py', 'classify_descriptions',
    'art', 'norm', 'async', 'csv', 'linear', 'loss', 'hive',
    'baseline', 'baselines', 'data', 'dataset', 'inference',
}


def is_domain_concept(concept: str) -> bool:
    """Check if concept contains domain-specific vocabulary."""
    c_lower = concept.lower()
    for marker in DOMAIN_MARKERS:
        if marker in c_lower:
            return True
    return False


def is_noise(concept: str) -> bool:
    """Check if concept matches noise patterns."""
    c_lower = concept.lower()
    if c_lower in NOISE_EXACT:
        return True
    for prefix in NOISE_PREFIXES:
        if c_lower.startswith(prefix):
            return True
    for suffix in NOISE_SUFFIXES:
        if c_lower.endswith(suffix):
            return True
    return False


# Classify
keep = set()
remove = set()
ambiguous = set()

for concept in sorted(singletons):
    if concept in ontology_set:
        keep.add(concept)
    elif is_domain_concept(concept):
        keep.add(concept)
    elif is_noise(concept):
        remove.add(concept)
    else:
        ambiguous.add(concept)

print(f"=== Singleton Classification ===")
print(f"Total singletons: {len(singletons)}")
print(f"Keep (domain concepts): {len(keep)}")
print(f"Remove (noise): {len(remove)}")
print(f"Ambiguous (needs review): {len(ambiguous)}")

print(f"\n--- KEEP ({len(keep)}) ---")
for c in sorted(keep):
    print(f"  {c}")

print(f"\n--- REMOVE ({len(remove)}) ---")
for c in sorted(remove):
    print(f"  {c}")

print(f"\n--- AMBIGUOUS ({len(ambiguous)}) ---")
for c in sorted(ambiguous):
    print(f"  {c}")
