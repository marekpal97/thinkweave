"""Prune noisy singleton concepts (count=1, not in ontology, not domain-relevant).

Uses parse_frontmatter / render_frontmatter so both inline (`concepts: [a, b]`)
and multi-line YAML list formats are handled.

Usage:
    uv run python scripts/prune_singletons_2026_05_04.py [--dry-run]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/home/marekpal97/python_projects/personal_mem/src")

from personal_mem.core.config import load_config
from personal_mem.core.indexer import Indexer
from personal_mem.core.vault import parse_frontmatter, render_frontmatter
from personal_mem.synthesis.concepts import build_keep_set, get_all_concepts, load_ontology

DOMAIN_MARKERS = {
    # Math
    "theorem", "lemma", "proof", "equation", "polynomial", "matrix", "vector",
    "integral", "derivative", "convergence", "distribution", "variance",
    "eigenvalue", "factorization", "decomposition", "approximation",
    "coefficient", "exponent", "logarithm", "algebra",
    "calculus", "topology", "manifold", "subspace", "orthogonal",
    "gaussian", "binomial", "poisson", "bayesian", "stochastic",
    "combinatorics", "permutation", "combinations", "probability",
    "fourier", "laplace", "markov", "monte-carlo", "mcmc",
    # ML / DL
    "neural", "gradient", "backprop", "activation", "embedding",
    "attention", "transformer", "encoder", "decoder", "convolution",
    "pooling", "dropout", "normalization", "regularization",
    "classifier", "regression", "clustering", "reinforcement",
    "supervised", "unsupervised", "self-supervised", "contrastive",
    "loss-function", "epoch", "learning-rate", "optimizer",
    "overfitting", "underfitting", "precision", "recall",
    "cnn", "rnn", "lstm", "gru", "gan", "vae",
    "bert", "gpt", "tokeniz", "embed",
    "tf-idf", "nlp", "sparsity", "negative-sampling",
    "hinge-loss", "class-weight", "resampling", "bootstrap",
    "layernorm", "batchnorm",
    # Finance
    "option", "volatility", "delta", "gamma", "theta", "vega",
    "portfolio", "sharpe", "hedge", "futures",
    "bond", "equity", "valuation", "dcf", "arbitrage",
    "leverage", "margin", "spread", "condor",
    "straddle", "strangle", "collar", "covered",
    "black-scholes", "fama-french", "capm",
    "brokerage", "trading", "market-making",
    "retail-investor", "sell-side",
    # Fitness / health
    "hypertrophy", "strength", "muscle", "protein", "calori",
    "exercise", "bench", "squat", "deadlift",
    "pull-up", "push-up", "cardio", "hiit", "recovery",
    "tendon", "biomechanic", "collagen", "creatine",
    "vitamin", "supplement", "macronutrient", "nutrition",
    "cancer", "mole", "dermatolog", "vaccination", "allerg",
    "infection", "medication", "dosage", "symptom", "shoulder",
    # Physics
    "quantum", "qubit", "photon", "particle", "antimatter", "cern",
    # Specific tools / proper nouns
    "graphql", "keras", "networkx", "spark", "itertools",
    "geojson", "vitest", "typescript",
}


def is_domain_concept(concept: str) -> bool:
    cl = concept.lower()
    return any(marker in cl for marker in DOMAIN_MARKERS)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    cfg = load_config()
    vault = cfg.vault_root

    idx = Indexer(config=cfg)
    all_concepts = get_all_concepts(idx.db)
    idx.close()

    ontology_keep = build_keep_set(load_ontology())
    singletons = {c for c, n in all_concepts.items() if n == 1}

    # Decide what to remove
    remove: set[str] = set()
    keep_count = 0
    for c in singletons:
        if c in ontology_keep:
            keep_count += 1
        elif is_domain_concept(c):
            keep_count += 1
        else:
            remove.add(c)

    print(f"Singletons total: {len(singletons)}")
    print(f"  Kept (in ontology or domain-marker match): {keep_count}")
    print(f"  Removing: {len(remove)}")
    print()

    files_modified = 0
    instances_removed = 0

    for path in vault.rglob("*.md"):
        rel = str(path.relative_to(vault))
        if rel.startswith("concepts/") or rel.startswith(".archive/"):
            continue
        if path.name in {"DECISIONS.md", "BACKLOG.md", "STATE.md", "RESEARCH_FOCUS.md", "THEMES.md"}:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if "concepts:" not in text:
            continue

        fm, body = parse_frontmatter(text)
        existing = fm.get("concepts")
        if not existing:
            continue
        if isinstance(existing, str):
            existing = [c.strip() for c in existing.split(",") if c.strip()]
        if not isinstance(existing, list):
            continue

        filtered = [c for c in existing if c.lower() not in remove]
        if len(filtered) == len(existing):
            continue

        instances_removed += len(existing) - len(filtered)
        files_modified += 1

        if dry_run:
            continue

        fm["concepts"] = filtered
        new_text = render_frontmatter(fm) + "\n" + body
        path.write_text(new_text, encoding="utf-8")

    verb = "would modify" if dry_run else "modified"
    print(f"{verb.capitalize()}: {files_modified} files")
    print(f"Concept instances removed: {instances_removed}")

    if dry_run:
        print("\nSample of concepts that would be removed (first 30):")
        for c in sorted(remove)[:30]:
            print(f"  {c}")
        return 0

    print("\nRebuilding index...")
    idx = Indexer(config=cfg)
    idx.rebuild(full=True)
    idx.close()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
