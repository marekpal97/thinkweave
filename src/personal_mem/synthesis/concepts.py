"""Concept tightening utilities — aliases, near-duplicate detection, merge,
pruning, and Obsidian hub page generation.

No external dependencies. Uses simple string similarity heuristics
to find near-duplicate concepts and a YAML aliases file for canonical mappings.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from personal_mem.core.config import Config


def _aliases_path(config: Config) -> Path:
    return config.mem_dir / "concept_aliases.yaml"


def load_aliases(config: Config) -> dict[str, list[str]]:
    """Load canonical → [aliases] from the aliases YAML file.

    Returns empty dict if file doesn't exist.
    """
    path = _aliases_path(config)
    if not path.exists():
        return {}

    # Minimal YAML parser for simple key: [list] format
    aliases: dict[str, list[str]] = {}
    current_key = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith(" ") and ":" in stripped:
            key, _, rest = stripped.partition(":")
            current_key = key.strip().lower()
            rest = rest.strip()
            if rest.startswith("[") and rest.endswith("]"):
                # Inline list: key: [a, b, c]
                items = [v.strip().strip("\"'") for v in rest[1:-1].split(",") if v.strip()]
                aliases[current_key] = [i.lower() for i in items]
            else:
                aliases.setdefault(current_key, [])
        elif line.startswith(" ") and stripped.startswith("- "):
            # Block list item
            item = stripped[2:].strip().strip("\"'").lower()
            if current_key:
                aliases.setdefault(current_key, []).append(item)
    return aliases


def save_aliases(config: Config, aliases: dict[str, list[str]]) -> Path:
    """Save aliases to the YAML file. Returns the file path."""
    path = _aliases_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = ["# Concept aliases: canonical → [aliases]",
             "# Auto-maintained by `mem concepts tighten/merge`", ""]
    for canonical in sorted(aliases):
        alias_list = sorted(set(aliases[canonical]))
        if alias_list:
            items = ", ".join(alias_list)
            lines.append(f"{canonical}: [{items}]")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def build_reverse_map(aliases: dict[str, list[str]]) -> dict[str, str]:
    """Build alias → canonical reverse lookup."""
    reverse: dict[str, str] = {}
    for canonical, alias_list in aliases.items():
        for alias in alias_list:
            reverse[alias.lower()] = canonical.lower()
    return reverse


def resolve_concept(concept: str, reverse_map: dict[str, str]) -> str:
    """Resolve a concept to its canonical form via aliases."""
    return reverse_map.get(concept.lower(), concept.lower())


def _normalize_stem(concept: str) -> str:
    """Normalize concept for comparison: strip hyphens, underscores, lowercase."""
    return concept.lower().replace("-", "").replace("_", "")


def _levenshtein(s1: str, s2: str) -> int:
    """Simple Levenshtein distance."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(
                prev[j + 1] + 1,
                curr[j] + 1,
                prev[j] + (0 if c1 == c2 else 1),
            ))
        prev = curr
    return prev[-1]


def find_near_duplicates(concepts: list[str]) -> list[tuple[str, str, str]]:
    """Find near-duplicate concept pairs.

    Returns list of (concept_a, concept_b, reason) tuples.
    """
    duplicates: list[tuple[str, str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    lower_concepts = sorted(set(c.lower() for c in concepts))

    for i, a in enumerate(lower_concepts):
        for b in lower_concepts[i + 1:]:
            if a == b:
                continue
            pair = (min(a, b), max(a, b))
            if pair in seen_pairs:
                continue

            reason = ""
            stem_a, stem_b = _normalize_stem(a), _normalize_stem(b)

            # Identical after stripping separators
            if stem_a == stem_b:
                reason = "identical stems"
            # One is substring of other
            elif len(a) >= 3 and len(b) >= 3 and (a in b or b in a):
                reason = "substring"
            # Levenshtein distance ≤ 2 (only for concepts of similar length;
            # skip very short strings where 2 edits is most of the word, e.g.
            # `1rm` vs `art` = e.d. 2 but unrelated)
            elif (
                min(len(a), len(b)) >= 4
                and abs(len(a) - len(b)) <= 2
                and _levenshtein(a, b) <= 2
            ):
                reason = f"edit distance {_levenshtein(a, b)}"

            if reason:
                seen_pairs.add(pair)
                duplicates.append((a, b, reason))

    return duplicates


def get_all_concepts(db) -> dict[str, int]:
    """Get all concepts with counts from the index database."""
    concept_counts: dict[str, int] = {}
    for row in db.execute(
        "SELECT concept, COUNT(*) as cnt FROM note_concepts GROUP BY concept"
    ):
        concept_counts[row["concept"]] = row["cnt"]
    return concept_counts


# Generic English/process terms that appear as substrings of real concepts
# but are never themselves domain-meaningful. Used by the deterministic
# drift filter to drop near-duplicate pairs where the shorter concept is
# one of these (e.g. `activation-functions` ≈ `function` is a false positive
# — the shared substring is generic English, not a real near-dup signal).
# Keep this list conservative: only add terms that have *no* domain meaning
# in any field. When in doubt, leave it out — the LLM still applies semantic
# judgment on the surviving pairs.
_DRIFT_STOPWORDS: frozenset[str] = frozenset({
    # Process / engineering generic
    "architecture", "testing", "configuration", "documentation",
    "deployment", "integration", "validation", "monitoring",
    "automation", "orchestration", "abstraction", "implementation",
    "maintenance", "operation", "operations", "review",
    # Software-shape generic
    "function", "method", "module", "component", "feature", "service",
    "system", "tool", "library", "framework", "package", "plugin",
    "interface", "api", "endpoint", "handler", "wrapper",
    # State / time / phase generic
    "state", "mode", "phase", "stage", "version", "status",
    "session", "context", "instance", "object", "value",
    # Communication / shape generic
    "summary", "report", "result", "output", "input", "data",
    "format", "structure", "pattern", "design", "model",
    # Generic action verbs as nouns
    "search", "filter", "match", "check", "compare", "track",
})


def filter_drift_candidates(
    pairs: list[tuple[str, str, str]],
    *,
    stopwords: frozenset[str] | None = None,
) -> list[tuple[str, str, str]]:
    """Drop drift pairs that are deterministic false positives.

    Rules applied (in order):

    1. Both concepts ≤ 3 characters — short concepts produce noisy
       substring/edit-distance hits (e.g. `ai` ≈ `api`, `1rm` ≈ `gru`).
    2. Substring match where the shorter concept is in
       :data:`_DRIFT_STOPWORDS` — generic English words that happen to
       appear inside longer domain terms (e.g. `activation-functions`
       ≈ `function`, `ab-testing` ≈ `testing`) are not true near-dups.

    Pairs surviving these rules still need LLM judgment on whether they're
    *actually* the same concept (typo, plural, alias) — the filter just
    removes the work the LLM would otherwise spend on obvious false
    positives. Keep the rule set conservative; when in doubt, leave the
    pair in and let the model decide.
    """
    keep_words = stopwords if stopwords is not None else _DRIFT_STOPWORDS
    out: list[tuple[str, str, str]] = []
    for a, b, reason in pairs:
        if len(a) <= 3 and len(b) <= 3:
            continue
        # Substring match where the shorter is generic English
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        if "substring" in reason and shorter in keep_words:
            continue
        out.append((a, b, reason))
    return out


def filter_promotion_candidates(
    concepts: list[str],
    *,
    stopwords: frozenset[str] | None = None,
    domain_prefixes: frozenset[str] | None = None,
) -> list[str]:
    """Drop promotion candidates that are deterministic false positives.

    Applied to ``proposed_concepts:`` reaching the promotion threshold —
    drops three classes that should never enter the canonical ontology:

    1. Domain-path concepts (``swe-python``, ``ml-deep-learning``) —
       these are ontology *structure*, not entries. Filtered by leading
       ``<prefix>-`` segment matching :data:`_DOMAIN_PREFIXES`.
    2. Generic process terms in :data:`_DRIFT_STOPWORDS` — `architecture`,
       `testing`, `configuration`, etc. — these are tags, not concepts.
    3. Underscore-bearing terms — by convention concepts are kebab-case
       lowercase; underscores indicate project-name leakage
       (``personal_mem``, ``options_engine``).

    Returns surviving candidates in the original order. Use the LLM only
    on this filtered list to decide which terms genuinely deserve ontology
    membership.
    """
    keep_words = stopwords if stopwords is not None else _DRIFT_STOPWORDS
    prefixes = domain_prefixes if domain_prefixes is not None else _DOMAIN_PREFIXES
    out: list[str] = []
    for c in concepts:
        cl = c.lower()
        if "_" in cl:
            continue
        head, _, _ = cl.partition("-")
        if head and head in prefixes:
            continue
        if cl in keep_words:
            continue
        out.append(c)
    return out


def suggest_similar(new_concept: str, existing: list[str], max_suggestions: int = 3) -> list[str]:
    """Suggest existing concepts similar to a new one.

    Returns list of existing concepts that are near-duplicates.
    """
    new_lower = new_concept.lower()
    new_stem = _normalize_stem(new_lower)
    suggestions = []

    for existing_concept in existing:
        ex_lower = existing_concept.lower()
        if ex_lower == new_lower:
            continue
        ex_stem = _normalize_stem(ex_lower)

        if new_stem == ex_stem:
            suggestions.append(ex_lower)
        elif len(new_lower) >= 3 and len(ex_lower) >= 3 and (new_lower in ex_lower or ex_lower in new_lower):
            suggestions.append(ex_lower)
        elif (
            min(len(new_lower), len(ex_lower)) >= 4
            and abs(len(new_lower) - len(ex_lower)) <= 2
            and _levenshtein(new_lower, ex_lower) <= 2
        ):
            suggestions.append(ex_lower)

    return suggestions[:max_suggestions]


def _seed_ontology_path() -> Path:
    """Path to the read-only ontology seed shipped with the package.

    The seed lives at ``src/personal_mem/ontology.yaml`` — one directory up
    from this module. Earlier the path used ``Path(__file__).parent`` which
    resolved to ``synthesis/ontology.yaml`` and silently never existed,
    leaving the system running on the vault override only.
    """
    return Path(__file__).parent.parent / "ontology.yaml"


def _vault_ontology_path() -> Path | None:
    """Path to the vault-local override, or None if no vault is configured."""
    try:
        from personal_mem.core.config import load_config

        return load_config().mem_dir / "ontology.yaml"
    except Exception:
        return None


def _ontology_path() -> Path:
    """Resolve the user-editable ontology path.

    Returns the vault override when it exists, else the shipped seed. This
    is the path printed in CLI hints ("add to ontology.yaml at X"). For
    reading the *effective* ontology, use ``_parse_ontology_file`` — it
    layers the seed beneath the vault override so new top-level keys
    shipped with the package don't get silently shadowed by older vaults.
    """
    override = _vault_ontology_path()
    if override is not None and override.exists():
        return override
    return _seed_ontology_path()


# Top-level ontology keys reserved for non-concept data. Filtered out of
# load_ontology() so callers iterating "domains → concepts" don't trip
# over them, but accessible via load_tag_vocabulary() and similar.
_RESERVED_ONTOLOGY_KEYS = frozenset({"tag_vocabulary", "domain_markers"})


def _parse_yaml_file(path: Path) -> dict[str, list[str]]:
    """Parse a single ontology YAML file into top-level key → [list]."""
    if not path.exists():
        return {}

    parsed: dict[str, list[str]] = {}
    current_key = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith(" ") and ":" in stripped:
            key, _, rest = stripped.partition(":")
            current_key = key.strip().lower()
            rest = rest.strip()
            if rest.startswith("[") and rest.endswith("]"):
                items = [v.strip().strip("\"'") for v in rest[1:-1].split(",") if v.strip()]
                parsed[current_key] = [i.lower() for i in items]
            else:
                parsed.setdefault(current_key, [])
        elif line.startswith(" ") and stripped.startswith("- "):
            # Strip trailing "# comment" before further processing.
            item_raw = stripped[2:]
            hash_pos = item_raw.find("#")
            if hash_pos >= 0:
                item_raw = item_raw[:hash_pos]
            item = item_raw.strip().strip("\"'").lower()
            if current_key and item:
                parsed.setdefault(current_key, []).append(item)
    return parsed


def _parse_ontology_file(path: Path | None = None) -> dict[str, list[str]]:
    """Parse the effective ontology — seed deep-merged with the vault override.

    With ``path=None`` (the canonical call): read the shipped seed, then
    layer the vault override on top via per-domain leaf-list union. For a
    domain key present in both files, the resulting leaf list is the
    de-duplicated union of both — neither file silently drops the other's
    leaves. For a domain key in only one file, that file's leaves stand
    alone.

    For non-list values (e.g. ``tag_vocabulary`` is a list-of-strings, not
    a domain → leaves shape) the vault override still replaces the seed
    when both are lists; if shapes differ, vault wins. The deep-merge
    semantic only applies where both files present a list.

    With ``path`` given: read only that file, no layering. Useful for
    tests and tooling that want a single source of truth.
    """
    if path is not None:
        return _parse_yaml_file(path)

    seed = _parse_yaml_file(_seed_ontology_path())
    override_path = _vault_ontology_path()
    if override_path is None or not override_path.exists():
        return seed

    override = _parse_yaml_file(override_path)
    merged: dict[str, list[str]] = dict(seed)
    for key, vault_value in override.items():
        seed_value = merged.get(key)
        if isinstance(seed_value, list) and isinstance(vault_value, list):
            # Per-domain leaf-list union, preserving order: seed first,
            # then any vault-only leaves appended. Case-insensitive dedup.
            seen = {leaf.lower() for leaf in seed_value}
            unioned = list(seed_value)
            for leaf in vault_value:
                if leaf.lower() not in seen:
                    unioned.append(leaf)
                    seen.add(leaf.lower())
            merged[key] = unioned
        else:
            # Either a new key or a non-list shape — fall back to vault wins.
            merged[key] = vault_value
    return merged


def load_ontology(path: Path | None = None) -> dict[str, list[str]]:
    """Load domain → [concepts] from the ontology YAML file.

    Excludes reserved keys (e.g. ``tag_vocabulary``) so the result is
    purely the concept ontology — safe to iterate when generating hub
    pages, building keep sets, or computing concept→domain reverse maps.
    """
    raw = _parse_ontology_file(path)
    return {k: v for k, v in raw.items() if k not in _RESERVED_ONTOLOGY_KEYS}


def load_tag_vocabulary(path: Path | None = None) -> set[str]:
    """Load the canonical tag vocabulary from ontology.yaml's
    ``tag_vocabulary`` key.

    Returns an empty set if the key is absent. Tags in vault notes that
    fall outside this set are surfaced as drift by ``mem doctor``.
    """
    raw = _parse_ontology_file(path)
    return set(raw.get("tag_vocabulary", []))


def build_keep_set(ontology: dict[str, list[str]]) -> set[str]:
    """Build the set of all concepts referenced in the ontology.

    Includes both **domain keys** (top-level entries like ``swe-python``,
    ``ml-deep-learning``) and **leaf concepts** (the items under each
    domain). Domain keys are themselves valid coarse-grained concepts —
    the dual-level hierarchy lets a note tag at the domain when no
    finer-grained leaf concept fits.
    """
    keep: set[str] = set()
    for domain, concepts in ontology.items():
        keep.add(domain.lower())
        keep.update(c.lower() for c in concepts)
    return keep


def build_leaf_set(ontology: dict[str, list[str]]) -> set[str]:
    """Just the leaf concepts — domain keys excluded.

    Use for "is this term *substantively* covered" checks like
    dead-vocabulary detection, where structural domain headers shouldn't
    count as terms expected to have note coverage.
    """
    leaves: set[str] = set()
    for concepts in ontology.values():
        leaves.update(c.lower() for c in concepts)
    return leaves


def concept_to_domains(ontology: dict[str, list[str]]) -> dict[str, list[str]]:
    """Build reverse map: concept → [domain1, domain2, ...]."""
    reverse: dict[str, list[str]] = defaultdict(list)
    for domain, concepts in ontology.items():
        for c in concepts:
            reverse[c.lower()].append(domain)
    return dict(reverse)


# Substring markers for "domain-relevant" concepts kept by the singleton
# prune even when they appear in only one note. These are domain-specific
# vocabulary fragments — math/ML/finance/fitness/physics/tools — where a
# single occurrence is more likely a real-but-rare term than enrichment
# noise. The seed list grew out of two ad-hoc prune scripts that were run
# manually before the lift; revisions go here. Concept matches if any
# marker is a substring of the concept name (case-insensitive).
DOMAIN_MARKERS: frozenset[str] = frozenset({
    # Math
    "theorem", "lemma", "proof", "equation", "polynomial", "matrix",
    "vector", "integral", "derivative", "convergence", "distribution",
    "variance", "eigenvalue", "factorization", "decomposition",
    "approximation", "coefficient", "exponent", "logarithm", "algebra",
    "calculus", "topology", "manifold", "subspace", "orthogonal",
    "gaussian", "binomial", "poisson", "bayesian", "stochastic",
    "combinatorics", "permutation", "combinations", "probability",
    "fourier", "laplace", "markov", "monte-carlo", "mcmc",
    "stirling", "cauchy", "riemann", "seminorm", "semidefinite",
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
    "gastrointestinal", "digestion", "musculoskeletal",
    # Physics
    "quantum", "qubit", "photon", "particle", "antimatter", "cern",
    # Specific tools / proper nouns worth keeping
    "beveridge", "graphql", "keras", "networkx", "spark",
    "itertools", "geojson", "vitest", "typescript",
})

# Files at vault top-level that are landing docs / synthesis outputs and
# must not have their concept frontmatter rewritten by a prune pass.
_PRUNE_SKIP_FILENAMES: frozenset[str] = frozenset({
    "DECISIONS.md", "BACKLOG.md", "STATE.md", "RESEARCH_FOCUS.md",
    "THEMES.md",
})


_DOMAIN_PREFIXES: frozenset[str] = frozenset({
    "swe", "ai", "ml", "math", "finance",
})


def _domain_label(domain: str) -> str:
    """Friendly title for a domain wikilink — drop the recognised prefix
    segment and title-case the remainder.

    ``swe-python`` → ``Python``. ``ai-agents`` → ``Agents``.
    Falls through unchanged when the domain doesn't start with a known
    prefix (e.g. ``thinkmesh``, ``fitness``) — those become ``Thinkmesh``
    / ``Fitness``.
    """
    parts = domain.split("-", 1)
    if len(parts) == 2 and parts[0] in _DOMAIN_PREFIXES:
        return parts[1].replace("-", " ").title()
    return domain.replace("-", " ").title()


def load_domain_markers() -> frozenset[str]:
    """Return the effective domain-marker substring set.

    Built-in :data:`DOMAIN_MARKERS` (math/ML/finance/fitness/physics/tools)
    union the vault override at ``<vault>/.mem/ontology.yaml::domain_markers``.
    Vault list extends — never replaces — the built-ins so personal_mem
    upgrades keep the curated set intact while users add their own domains
    (chemistry, music, law, …) without code edits.
    """
    parsed = _parse_ontology_file()
    extra = parsed.get("domain_markers") or []
    if not extra:
        return DOMAIN_MARKERS
    return DOMAIN_MARKERS | frozenset(m.lower() for m in extra if m)


def is_domain_concept(concept: str, *, markers: frozenset[str] | None = None) -> bool:
    """True if any domain-marker substring appears in the concept (lowercased)."""
    cl = concept.lower()
    effective = markers if markers is not None else load_domain_markers()
    return any(marker in cl for marker in effective)


def split_concepts_by_ontology(
    candidate: list[str] | None,
    *,
    proposed: list[str] | None = None,
    ontology_keep: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Partition a concept list into (canonical, proposed) by ontology membership.

    Strict policy enforcement: terms in ``candidate`` that exist in the
    merged ontology (seed + vault override) go to ``canonical``; everything
    else — plus anything already in ``proposed`` — flows to ``proposed``.
    Each output list is lowercased, stripped, and deduped (preserving
    order). When ``ontology_keep`` is omitted, the merged ontology is
    loaded fresh.

    Used at every concept-write surface (mem_extract, enrich, importers)
    so non-ontology vocabulary can never reach canonical ``concepts:``
    without explicit promotion via ``/mem-resolve-concepts``.
    """
    if ontology_keep is None:
        ontology_keep = build_keep_set(load_ontology())

    canonical: list[str] = []
    proposed_out: list[str] = []
    seen_canonical: set[str] = set()
    seen_proposed: set[str] = set()

    for raw in candidate or []:
        if not raw:
            continue
        c = raw.lower().strip()
        if not c:
            continue
        if c in ontology_keep:
            if c not in seen_canonical:
                canonical.append(c)
                seen_canonical.add(c)
        else:
            if c not in seen_proposed:
                proposed_out.append(c)
                seen_proposed.add(c)

    for raw in proposed or []:
        if not raw:
            continue
        c = raw.lower().strip()
        if not c or c in seen_canonical:
            continue
        if c not in seen_proposed:
            proposed_out.append(c)
            seen_proposed.add(c)

    return canonical, proposed_out


def prune_noisy_singletons(
    config: Config,
    *,
    dry_run: bool = False,
) -> dict:
    """Strip count=1 *canonical* concepts when they're neither in the
    ontology nor matched by any DOMAIN_MARKERS substring.

    Operates on `concepts:` only. **`proposed_concepts:` is sanctuary
    by design** — emergent terms enter at count=1, that's their natural
    starting state, not noise. Cleaning the proposed pool happens via
    promotion (graduation to canonical) or explicit review inside
    ``/mem-resolve-concepts``, never via automated count-based pruning.
    Treating combined counts here would conflict with the strict
    creation policy by stripping terms the demotion sweep just moved.

    The keep heuristic preserves: (a) ontology entries (seed + vault
    override merged), (b) domain-vocabulary substrings (see
    ``DOMAIN_MARKERS``). Under the strict policy, canonical singletons
    are rare (non-ontology can't enter ``concepts:`` from any write
    surface) — this prune is mostly a guardrail against pre-policy
    leftovers and direct vault edits.

    Returns stats dict::

        {
            "singletons": int,         # canonical count==1 terms
            "kept_ontology": int,      # preserved by ontology match
            "kept_domain": int,        # preserved by DOMAIN_MARKERS
            "removed": list[str],      # singleton terms being pruned
            "files_modified": int,     # notes whose `concepts:` was edited
            "instances_removed": int,  # canonical occurrences stripped
        }

    Hub pages, the archive directory, and landing docs are skipped. On
    ``dry_run=True`` no files are written and the index is not rebuilt;
    stats describe the would-be prune.
    """
    from personal_mem.core.indexer import Indexer
    from personal_mem.core.vault import parse_frontmatter, render_frontmatter

    idx = Indexer(config=config)
    canonical_counts = get_all_concepts(idx.db)
    idx.close()

    ontology_keep = build_keep_set(load_ontology())
    domain_markers = load_domain_markers()
    singletons = {c for c, n in canonical_counts.items() if n == 1}

    remove: set[str] = set()
    kept_ontology = 0
    kept_domain = 0
    for c in singletons:
        if c in ontology_keep:
            kept_ontology += 1
        elif is_domain_concept(c, markers=domain_markers):
            kept_domain += 1
        else:
            remove.add(c)

    files_modified = 0
    instances_removed = 0

    for path in config.vault_root.rglob("*.md"):
        rel = str(path.relative_to(config.vault_root))
        if rel.startswith("concepts/") or rel.startswith(".archive/"):
            continue
        if path.name in _PRUNE_SKIP_FILENAMES:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
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

        if filtered:
            fm["concepts"] = filtered
        else:
            fm.pop("concepts", None)
        path.write_text(render_frontmatter(fm) + "\n" + body, encoding="utf-8")

    if not dry_run and files_modified > 0:
        idx = Indexer(config=config)
        idx.rebuild(full=True)
        idx.close()

    return {
        "singletons": len(singletons),
        "kept_ontology": kept_ontology,
        "kept_domain": kept_domain,
        "removed": sorted(remove),
        "files_modified": files_modified,
        "instances_removed": instances_removed,
    }


def get_all_proposed_concepts(db) -> dict[str, int]:
    """Aggregate `proposed_concepts:` across every indexed note.

    `proposed_concepts:` is not materialized in `note_concepts` (which
    indexes only canonical assignments), so we read it from the
    `notes.frontmatter` JSON column. Returns `{concept: count}` keyed
    by lowercased term, sorted at the call site if needed.
    """
    counts: dict[str, int] = {}
    for row in db.execute("SELECT frontmatter FROM notes"):
        fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
        proposed = fm.get("proposed_concepts") or []
        if isinstance(proposed, str):
            proposed = [c.strip() for c in proposed.split(",") if c.strip()]
        for c in proposed:
            if not c:
                continue
            term = c.lower().strip()
            counts[term] = counts.get(term, 0) + 1
    return counts


def promote_proposed_concept(
    config: Config,
    concept: str,
    *,
    domain: str,
    rebuild_index: bool = True,
) -> dict:
    """Promote a proposed concept to canonical ontology status.

    Three actions in one pass:

    1. Add the term to ``vault/.mem/ontology.yaml`` under ``domain``
       (creating the override file and the domain key if missing).
    2. Walk every note carrying the term in ``proposed_concepts:`` and
       move it to ``concepts:`` (preserving existing canonical entries).
    3. Ensure a concept-hub skeleton exists at
       ``vault/concepts/topics/{concept}.md`` and rebuild the index.

    Returns stats::

        {
            "notes_modified": int,    # notes whose frontmatter shifted
            "ontology_updated": bool, # True if the term was newly added
            "hub_created": bool,      # True if a new hub skeleton was written
        }

    Idempotent: re-running with a term already canonical is a no-op
    (zero modifications, ontology untouched, hub already present).

    Set ``rebuild_index=False`` when batching multiple promotions in one
    pass (e.g. the dream cycle's apply phase) and rebuild once at the
    end. Default ``True`` preserves the standalone-call contract.
    """
    from personal_mem.core.indexer import Indexer
    from personal_mem.core.vault import parse_frontmatter, render_frontmatter
    from personal_mem.synthesis.concept_hub import (
        concept_hub_path,
        ensure_concept_hub_skeleton,
    )

    term = concept.lower().strip()
    if not term:
        raise ValueError("concept must be a non-empty string")
    if not domain:
        raise ValueError("domain must be specified for promotion")

    domain_key = domain.lower().strip()
    ontology = load_ontology()
    ontology_updated = False

    if term not in build_keep_set(ontology):
        # Add to vault override (the user-editable layer). Don't touch
        # the shipped seed.
        override_path = _vault_ontology_path()
        if override_path is None:
            raise RuntimeError("Vault override path is unavailable; cannot promote.")
        override_path.parent.mkdir(parents=True, exist_ok=True)

        existing = (
            _parse_yaml_file(override_path) if override_path.exists() else {}
        )
        existing.setdefault(domain_key, [])
        if term not in [c.lower() for c in existing[domain_key]]:
            existing[domain_key] = sorted({*existing[domain_key], term})
        # Re-emit as YAML preserving inline-list format (compact).
        lines: list[str] = []
        for d in sorted(existing):
            items = sorted({c.lower() for c in existing[d]})
            if items:
                lines.append(f"{d}: [{', '.join(items)}]")
            else:
                lines.append(f"{d}: []")
        override_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ontology_updated = True

    # Walk the vault and shift the term from proposed → canonical.
    notes_modified = 0
    for path in config.vault_root.rglob("*.md"):
        rel = str(path.relative_to(config.vault_root))
        if rel.startswith("concepts/") or rel.startswith(".archive/"):
            continue
        if path.name in _PRUNE_SKIP_FILENAMES:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "proposed_concepts:" not in text:
            continue

        fm, body = parse_frontmatter(text)
        proposed = fm.get("proposed_concepts") or []
        if isinstance(proposed, str):
            proposed = [c.strip() for c in proposed.split(",") if c.strip()]
        if not proposed:
            continue

        if term not in [c.lower() for c in proposed]:
            continue

        new_proposed = [c for c in proposed if c.lower() != term]
        canonical = fm.get("concepts") or []
        if isinstance(canonical, str):
            canonical = [c.strip() for c in canonical.split(",") if c.strip()]
        if term not in [c.lower() for c in canonical]:
            canonical = list(canonical) + [term]

        fm["concepts"] = canonical
        if new_proposed:
            fm["proposed_concepts"] = new_proposed
        else:
            fm.pop("proposed_concepts", None)

        path.write_text(render_frontmatter(fm) + "\n" + body, encoding="utf-8")
        notes_modified += 1

    # Ensure a hub skeleton.
    hub_path = concept_hub_path(config, term)
    hub_created = not hub_path.exists()
    ensure_concept_hub_skeleton(config, term, domains=[domain_key])

    if rebuild_index and (notes_modified > 0 or ontology_updated):
        idx = Indexer(config=config)
        idx.rebuild(full=True)
        idx.close()

    return {
        "notes_modified": notes_modified,
        "ontology_updated": ontology_updated,
        "hub_created": hub_created,
    }


def demote_non_ontology_concepts(
    config: Config,
    *,
    dry_run: bool = False,
) -> dict:
    """One-shot vault sweep: move non-ontology terms from `concepts:` to
    `proposed_concepts:` on every note.

    Retroactively applies the strict creation policy to the existing
    population. Pure deterministic operation — no LLM, no API. Each note's
    `concepts:` list is partitioned against the merged ontology
    (seed + vault override): matches stay in `concepts:`, non-matches
    flow into `proposed_concepts:` (preserving any existing entries
    there). Order is preserved within each output list; duplicates are
    deduped.

    Hub pages, the archive directory, and landing docs are skipped — the
    same exclusion list as ``prune_noisy_singletons``.

    Returns::

        {
            "files_modified": int,
            "concepts_demoted": int,    # total occurrences moved
            "terms_demoted": list[str], # distinct terms (sorted)
        }

    On ``dry_run=True`` no files are written and the index is not rebuilt;
    stats describe what *would* have happened.
    """
    from personal_mem.core.indexer import Indexer
    from personal_mem.core.vault import parse_frontmatter, render_frontmatter

    ontology_keep = build_keep_set(load_ontology())

    files_modified = 0
    concepts_demoted = 0
    terms_demoted: set[str] = set()

    for path in config.vault_root.rglob("*.md"):
        rel = str(path.relative_to(config.vault_root))
        if rel.startswith("concepts/") or rel.startswith(".archive/"):
            continue
        if path.name in _PRUNE_SKIP_FILENAMES:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
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

        existing_proposed = fm.get("proposed_concepts") or []
        if isinstance(existing_proposed, str):
            existing_proposed = [
                c.strip() for c in existing_proposed.split(",") if c.strip()
            ]

        canonical, proposed = split_concepts_by_ontology(
            existing,
            proposed=existing_proposed,
            ontology_keep=ontology_keep,
        )

        # No-op when the canonical list equals the existing list (case-
        # insensitive) and no items moved into proposed beyond what was
        # already there.
        moved_terms = {c for c in (existing or []) if c.lower() not in ontology_keep}
        if not moved_terms:
            continue

        terms_demoted.update(t.lower() for t in moved_terms)
        concepts_demoted += len(moved_terms)
        files_modified += 1

        if dry_run:
            continue

        if canonical:
            fm["concepts"] = canonical
        else:
            fm.pop("concepts", None)
        if proposed:
            fm["proposed_concepts"] = proposed
        elif "proposed_concepts" in fm:
            fm.pop("proposed_concepts", None)

        path.write_text(render_frontmatter(fm) + "\n" + body, encoding="utf-8")

    hubs_archived: list[str] = []
    if not dry_run and files_modified > 0:
        idx = Indexer(config=config)
        idx.rebuild(full=True)
        idx.close()
        # Doctrine: a term that just left the canonical pool can leave a
        # stranded hub file behind (its notes no longer cite it as
        # canonical, the term isn't in the ontology). Relocate any such
        # hubs to topics/_archive/ in the same operation so the live
        # surface stays aligned with the canonical vocabulary. Lossless —
        # the synthesis work is preserved for possible re-promotion.
        archived = archive_orphan_hubs(config)
        hubs_archived = sorted(c for c, _ in archived)

    return {
        "files_modified": files_modified,
        "concepts_demoted": concepts_demoted,
        "terms_demoted": sorted(terms_demoted),
        "hubs_archived": hubs_archived,
    }


def consolidate_parent_leaf_concepts(
    config: Config,
    *,
    dry_run: bool = False,
) -> dict:
    """One-shot vault sweep: drop a domain concept from ``concepts:`` when
    any of its leaves is also present on the same note.

    Counterpart to the strict ontology gate — the gate runs at *write*
    time, this runs at cleanup time. The 2-tier ontology gives us
    parent/child semantics: top-level keys (``swe-python``, ``ai-agents``)
    are parents; listed entries underneath are children. A note carrying
    both a parent and one of its children is asserting two facts where
    one is strictly more specific; drop the parent.

    Skips the same set of paths as ``demote_non_ontology_concepts`` —
    hub pages, archive, landing docs.

    Returns::

        {
            "files_modified": int,
            "occurrences_dropped": int,   # total parent-occurrences removed
            "domains_touched": list[str], # distinct parents pruned (sorted)
        }

    On ``dry_run=True`` no files are written and the index is not
    rebuilt; stats describe what *would* have happened.
    """
    from personal_mem.core.indexer import Indexer
    from personal_mem.core.vault import parse_frontmatter, render_frontmatter

    ontology = load_ontology()
    domain_to_leaves: dict[str, set[str]] = {
        d.lower(): {leaf.lower() for leaf in leaves}
        for d, leaves in ontology.items()
    }

    files_modified = 0
    occurrences_dropped = 0
    domains_touched: set[str] = set()

    for path in config.vault_root.rglob("*.md"):
        rel = str(path.relative_to(config.vault_root))
        if rel.startswith("concepts/") or rel.startswith(".archive/"):
            continue
        if path.name in _PRUNE_SKIP_FILENAMES:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
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

        existing_lc = {c.lower() for c in existing}
        to_drop: set[str] = set()
        for c in existing:
            cl = c.lower()
            leaves = domain_to_leaves.get(cl)
            if leaves and existing_lc & leaves:
                to_drop.add(cl)

        if not to_drop:
            continue

        new_concepts = [c for c in existing if c.lower() not in to_drop]
        domains_touched.update(to_drop)
        occurrences_dropped += len(to_drop)
        files_modified += 1

        if dry_run:
            continue

        if new_concepts:
            fm["concepts"] = new_concepts
        else:
            fm.pop("concepts", None)

        path.write_text(render_frontmatter(fm) + "\n" + body, encoding="utf-8")

    if not dry_run and files_modified > 0:
        idx = Indexer(config=config)
        idx.rebuild(full=True)
        idx.close()

    return {
        "files_modified": files_modified,
        "occurrences_dropped": occurrences_dropped,
        "domains_touched": sorted(domains_touched),
    }


def prune_concepts(
    vault_root: Path,
    keep_set: set[str],
) -> dict:
    """Remove concepts not in keep_set from all vault notes.

    Returns stats dict: {files_modified, concepts_removed}.
    """
    from personal_mem.core.vault import parse_frontmatter, render_frontmatter

    stats = {"files_modified": 0, "concepts_removed": 0}

    for md_file in vault_root.rglob("*.md"):
        text = md_file.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        if not fm:
            continue

        concepts = fm.get("concepts", [])
        if isinstance(concepts, str):
            concepts = [c.strip() for c in concepts.split(",") if c.strip()]
        if not concepts:
            continue

        filtered = [c for c in concepts if c.lower() in keep_set]
        removed = len(concepts) - len(filtered)

        if removed > 0:
            if filtered:
                fm["concepts"] = filtered
            else:
                del fm["concepts"]
            new_content = render_frontmatter(fm) + "\n\n" + body
            md_file.write_text(new_content, encoding="utf-8")
            stats["files_modified"] += 1
            stats["concepts_removed"] += removed

    return stats


def generate_domain_hubs(
    config: Config,
    ontology: dict[str, list[str]] | None = None,
) -> dict[str, Path]:
    """Generate navigation-only domain hub pages in ``vault/concepts/``.

    Each domain hub is a thin directory page listing its child concepts,
    each linking to its concept hub at ``concepts/topics/{concept}.md``.
    Domain hubs carry no synthesis content — that lives on concept hubs.
    They are fully regenerable; hand-edits to the body are not preserved.

    Returns ``{domain: path}`` for generated files.
    """
    ontology = ontology or load_ontology()
    if not ontology:
        return {}

    concepts_dir = config.vault_root / "concepts"
    concepts_dir.mkdir(parents=True, exist_ok=True)

    generated: dict[str, Path] = {}
    now = datetime.now(timezone.utc).isoformat()

    category_display = {
        "math": "Mathematics",
        "ml": "Machine Learning",
        "ai": "AI & LLMs",
        "finance": "Finance",
        "swe": "Software Engineering",
    }

    for domain, domain_concepts in sorted(ontology.items()):
        display_name = domain.split("/")[-1].replace("-", " ").title()
        category = domain.split("/")[0] if "/" in domain else ""
        category_label = category_display.get(category, category.title())

        lines = [
            "---",
            "type: domain-hub",
            f"domain: {domain}",
            f"concepts: [{', '.join(domain_concepts)}]",
            "auto_generated: true",
            f"updated: \"{now}\"",
            "---",
            "",
            f"# {display_name}",
            "",
        ]

        if category_label:
            lines.append(f"*{category_label}*")
            lines.append("")

        lines.append(
            "Navigation for concepts in this domain. Each concept has its "
            "own hub page with an essence (working mental model) and a "
            "learning log (append-only artifacts extracted from vault notes)."
        )
        lines.append("")
        lines.append(f"## Concepts ({len(domain_concepts)})")
        lines.append("")
        if domain_concepts:
            for concept in domain_concepts:
                lines.append(f"- [[concepts/topics/{concept}|{concept}]]")
        else:
            lines.append("*No concepts listed.*")
        lines.append("")

        # Domain names are dash-form (e.g. ``swe-python``); filename mirrors
        # the canonical name 1:1, no slug munging required.
        hub_path = concepts_dir / f"{domain}.md"
        hub_path.write_text("\n".join(lines), encoding="utf-8")
        generated[domain] = hub_path

    return generated


def generate_concept_hub_skeletons(
    config: Config,
    ontology: dict[str, list[str]] | None = None,
) -> dict[str, Path]:
    """Create empty concept-hub stubs at ``vault/concepts/topics/{concept}.md``.

    Never overwrites existing files — ``ensure_concept_hub_skeleton`` is
    a no-op when the hub already exists. This means running it repeatedly
    is safe, and concept hubs with LLM-written content are preserved.

    Returns ``{concept: path}`` for every concept referenced by the
    ontology, whether it was newly created or already existed.
    """
    from personal_mem.synthesis.concept_hub import ensure_concept_hub_skeleton

    ontology = ontology or load_ontology()
    if not ontology:
        return {}

    c2d = concept_to_domains(ontology)
    created: dict[str, Path] = {}
    for concept, domains in c2d.items():
        path = ensure_concept_hub_skeleton(config, concept, domains=domains)
        created[concept] = path
    return created


def generate_hub_pages(
    config: Config,
    ontology: dict[str, list[str]] | None = None,
) -> dict[str, Path]:
    """Back-compat wrapper: generate both domain hubs and concept skeletons.

    Returns the union of domain paths (keyed by domain) and concept hub
    paths (keyed by concept). Used by ``mem concepts hubs``.
    """
    result: dict[str, Path] = {}
    result.update(generate_domain_hubs(config, ontology))
    result.update(generate_concept_hub_skeletons(config, ontology))
    return result


def add_hub_wikilinks(
    config: Config,
    ontology: dict[str, list[str]] | None = None,
) -> int:
    """Add wikilinks to domain hub pages in each note's body.

    Appends a '## Domains' section with links like [[concepts/math--linear-algebra]].
    Returns count of modified files.
    """
    from personal_mem.core.vault import parse_frontmatter, render_frontmatter

    ontology = ontology or load_ontology()
    c2d = concept_to_domains(ontology)

    vm_root = config.vault_root
    modified = 0

    from personal_mem.synthesis.landing import landing_filename_set

    landing_skip = landing_filename_set(config.vault_root)
    for md_file in vm_root.rglob("*.md"):
        # Skip hub pages and landing pages
        if md_file.parent.name == "concepts":
            continue
        if md_file.name in landing_skip:
            continue

        text = md_file.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        if not fm or not fm.get("id"):
            continue

        concepts = fm.get("concepts", [])
        if isinstance(concepts, str):
            concepts = [c.strip() for c in concepts.split(",") if c.strip()]
        if not concepts:
            continue

        # Find domains for this note
        note_domains: set[str] = set()
        for c in concepts:
            for domain in c2d.get(c.lower(), []):
                note_domains.add(domain)

        if not note_domains:
            continue

        # Build wikilinks section. Domain names are dash-form (e.g.
        # ``swe-python``); the friendly label drops the domain prefix and
        # title-cases the remainder (``swe-python`` → ``Python``).
        links = sorted(note_domains)
        link_lines = [
            f"[[concepts/{d}|{_domain_label(d)}]]" for d in links
        ]
        domains_section = "\n## Domains\n" + " · ".join(link_lines)

        # Replace existing domains section or append
        if "\n## Domains\n" in body:
            # Find and replace existing section (up to next ## or end)
            import re
            body = re.sub(
                r"\n## Domains\n.*?(?=\n## |\Z)",
                domains_section,
                body,
                flags=re.DOTALL,
            )
        else:
            body = body.rstrip() + "\n" + domains_section

        new_content = render_frontmatter(fm) + "\n\n" + body + "\n"
        md_file.write_text(new_content, encoding="utf-8")
        modified += 1

    return modified


def delete_concept_hub(config: Config, concept: str) -> bool:
    """Remove a concept hub page from disk if it exists.

    Used after ``mem concepts merge`` so the renamed concept's hub
    doesn't linger as a stale ledger. Safe to call when the file is
    missing — returns False then. Does not touch the index; callers
    should rebuild after a batch of deletions.
    """
    from personal_mem.synthesis.concept_hub import concept_hub_path

    path = concept_hub_path(config, concept)
    if path.exists():
        path.unlink()
        return True
    return False


HUB_ARCHIVE_DIRNAME = "_archive"


def hub_archive_dir(config: Config) -> Path:
    """Directory holding archived (formerly canonical, now orphan) concept hubs.

    Mirrors ``themes/_candidates/_archive/``. Lives *inside* ``topics/`` so
    non-recursive ``topics.glob('*.md')`` scans (status, link, repair) skip
    archived files automatically — no filter logic required.
    """
    from personal_mem.synthesis.concept_hub import topics_dir

    return topics_dir(config) / HUB_ARCHIVE_DIRNAME


def archive_concept_hub(config: Config, concept: str) -> Path | None:
    """Move a concept hub file to ``topics/_archive/`` if it exists.

    Reversible counterpart to :func:`delete_concept_hub`. Used when a
    concept gets demoted out of the canonical ontology — the hub's log
    entries cite real notes that still exist, so we keep the synthesis
    work for possible re-promotion rather than discarding it.

    Returns the new archived path, or ``None`` if no hub file existed.
    On filename collision (rare — only if the same concept was archived
    before and re-promoted then re-demoted) we suffix the existing file
    with ``.bak`` to preserve both copies; the just-moved file takes the
    canonical archive name.
    """
    import shutil

    from personal_mem.synthesis.concept_hub import concept_hub_path

    src = concept_hub_path(config, concept)
    if not src.exists():
        return None

    archive_dir = hub_archive_dir(config)
    archive_dir.mkdir(parents=True, exist_ok=True)
    dst = archive_dir / src.name
    if dst.exists():
        # Preserve the prior archive copy under a .bak suffix, then take
        # the canonical name for the just-moved file.
        bak = dst.with_suffix(dst.suffix + ".bak")
        if bak.exists():
            bak.unlink()
        dst.rename(bak)
    shutil.move(str(src), str(dst))
    return dst


def archive_orphan_hubs(
    config: Config,
    *,
    dry_run: bool = False,
) -> list[tuple[str, Path]]:
    """Move every orphan hub (concept absent from ontology *and* zero notes)
    into ``topics/_archive/``. Returns ``[(concept, archived_path), ...]``
    for the paths that moved (or *would* move on ``dry_run=True``).

    Idempotent: safe to call repeatedly. Use after edits to ``ontology.yaml``
    or after ``demote_non_ontology_concepts`` to keep the live ``topics/``
    surface aligned with the current canonical vocabulary.
    """
    orphans = find_orphan_hubs(config)
    moved: list[tuple[str, Path]] = []
    if dry_run:
        archive_dir = hub_archive_dir(config)
        for concept, src in orphans:
            moved.append((concept, archive_dir / src.name))
        return moved
    for concept, _src in orphans:
        new_path = archive_concept_hub(config, concept)
        if new_path is not None:
            moved.append((concept, new_path))
    return moved


def find_orphan_hubs(config: Config) -> list[tuple[str, Path]]:
    """Find concept hub pages whose underlying concept has zero vault
    assignments and is not in ``ontology.yaml``.

    Returns ``[(concept, path), ...]``. Read-only — caller decides whether
    to delete. A hub for a concept with notes (even if the concept itself
    isn't in the ontology) is kept; orphan = no notes AND not in ontology.
    """
    import sqlite3

    from personal_mem.synthesis.concept_hub import topics_dir

    topics = topics_dir(config)
    if not topics.exists():
        return []

    ontology = load_ontology()
    keep = build_keep_set(ontology)

    counts: dict[str, int] = {}
    if config.index_db.exists():
        db = sqlite3.connect(str(config.index_db))
        db.row_factory = sqlite3.Row
        try:
            for row in db.execute(
                "SELECT concept, COUNT(*) AS cnt FROM note_concepts GROUP BY concept"
            ):
                counts[row["concept"].lower()] = row["cnt"]
        finally:
            db.close()

    orphans: list[tuple[str, Path]] = []
    for path in sorted(topics.glob("*.md")):
        concept = path.stem.lower()
        if counts.get(concept, 0) > 0:
            continue
        if concept in keep:
            continue
        orphans.append((concept, path))
    return orphans


def find_redundant_hub_candidates(
    config: Config,
    *,
    min_essence_chars: int = 80,
    min_jaccard: float = 0.4,
) -> list[tuple[str, str, float]]:
    """Pre-filter concept-hub pairs likely to be redundant.

    Cheap structural check before the (expensive) LLM redundancy pass.
    Compares the **content** of each hub's essence — token-set Jaccard
    similarity over normalized words — and returns pairs above the
    threshold. The actual LLM judgment runs on this candidate list, not
    on every pair (which is quadratic).

    Returns ``[(concept_a, concept_b, jaccard), ...]`` sorted by Jaccard
    descending. Hubs with empty or short essences are skipped.
    """
    from personal_mem.synthesis.concept_hub import parse_concept_hub, topics_dir

    topics = topics_dir(config)
    if not topics.exists():
        return []

    essences: dict[str, set[str]] = {}
    for path in topics.glob("*.md"):
        hub = parse_concept_hub(path)
        if not hub.essence or len(hub.essence) < min_essence_chars:
            continue
        words = {
            w.lower().strip(".,;:!?()[]\"'")
            for w in hub.essence.split()
            if len(w) >= 4
        }
        if words:
            essences[hub.concept] = words

    concepts = sorted(essences)
    candidates: list[tuple[str, str, float]] = []
    for i, a in enumerate(concepts):
        for b in concepts[i + 1:]:
            wa, wb = essences[a], essences[b]
            if not wa or not wb:
                continue
            inter = len(wa & wb)
            union = len(wa | wb)
            if union == 0:
                continue
            jaccard = inter / union
            if jaccard >= min_jaccard:
                candidates.append((a, b, jaccard))

    candidates.sort(key=lambda r: r[2], reverse=True)
    return candidates


def merge_concept_in_notes(
    vault_root: Path,
    from_concept: str,
    to_concept: str,
) -> int:
    """Rename a concept across all vault notes. Returns count of modified files."""
    from personal_mem.core.vault import parse_frontmatter, render_frontmatter

    from_lower = from_concept.lower()
    to_lower = to_concept.lower()
    changed = 0

    for md_file in vault_root.rglob("*.md"):
        text = md_file.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        if not fm:
            continue

        concepts = fm.get("concepts", [])
        if isinstance(concepts, str):
            concepts = [c.strip() for c in concepts.split(",") if c.strip()]
        if not concepts:
            continue

        new_concepts = []
        modified = False
        for c in concepts:
            if c.lower() == from_lower:
                if to_lower not in [nc.lower() for nc in new_concepts]:
                    new_concepts.append(to_lower)
                modified = True
            elif c.lower() not in [nc.lower() for nc in new_concepts]:
                new_concepts.append(c)

        if modified:
            fm["concepts"] = new_concepts
            new_content = render_frontmatter(fm) + "\n\n" + body
            md_file.write_text(new_content, encoding="utf-8")
            changed += 1

    return changed


# ---------------------------------------------------------------------------
# Drift report — advisory, read-only, mem-wrap step 7.5
# ---------------------------------------------------------------------------

# When a concept crosses this count, it's worth considering for the ontology.
DRIFT_COUNT_THRESHOLD = 5

# Marker file: mtime records when `mem concepts hubs` last ran. If
# ontology.yaml is newer, hub pages are stale.
_HUBS_MARKER_NAME = "hubs_last_run"


def hubs_marker_path(config: Config) -> Path:
    return config.mem_dir / _HUBS_MARKER_NAME


def drift_report(
    config: Config,
    project: str = "",
    *,
    threshold: int = DRIFT_COUNT_THRESHOLD,
    max_items: int = 5,
) -> dict:
    """Read-only advisory drift report for mem-wrap.

    Returns a dict with three keys:

    - ``near_duplicates``: [(a, b, reason), ...] up to ``max_items``
    - ``new_concept_candidates``: [(concept, count), ...] concepts with
      count >= threshold that are NOT currently listed in ontology.yaml
    - ``ontology_stale``: bool. True if ``ontology.yaml`` mtime is newer
      than the ``hubs_last_run`` marker file (or if the marker is missing).

    Never modifies anything. Callers display the report and let the user
    decide whether to act.
    """
    import sqlite3

    result: dict = {
        "near_duplicates": [],
        "new_concept_candidates": [],
        "ontology_stale": False,
    }

    # --- 1. Near-duplicates among all vault concepts ---
    if config.index_db.exists():
        db = sqlite3.connect(str(config.index_db))
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                """
                SELECT DISTINCT concept
                FROM note_concepts
                WHERE (? = '' OR note_id IN (
                    SELECT id FROM notes WHERE project = ?
                ))
                """,
                (project, project),
            ).fetchall()
            concepts_list = [r["concept"] for r in rows]
        finally:
            db.close()

        dupes = find_near_duplicates(concepts_list)
        result["near_duplicates"] = dupes[:max_items]

    # --- 2. New concept candidates (crossed threshold, not in ontology) ---
    ontology = load_ontology()
    ontology_concepts = build_keep_set(ontology)

    if config.index_db.exists():
        db = sqlite3.connect(str(config.index_db))
        db.row_factory = sqlite3.Row
        try:
            if project:
                rows = db.execute(
                    """
                    SELECT nc.concept, COUNT(*) AS cnt
                    FROM note_concepts nc
                    JOIN notes n ON n.id = nc.note_id
                    WHERE n.project = ?
                    GROUP BY nc.concept
                    HAVING cnt >= ?
                    ORDER BY cnt DESC
                    """,
                    (project, threshold),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT concept, COUNT(*) AS cnt
                    FROM note_concepts
                    GROUP BY concept
                    HAVING cnt >= ?
                    ORDER BY cnt DESC
                    """,
                    (threshold,),
                ).fetchall()
        finally:
            db.close()

        candidates = [
            (row["concept"], row["cnt"])
            for row in rows
            if row["concept"].lower() not in ontology_concepts
        ]
        result["new_concept_candidates"] = candidates[:max_items]

    # --- 3. Ontology staleness ---
    ontology_path = _ontology_path()
    marker = hubs_marker_path(config)
    if ontology_path.exists():
        ontology_mtime = ontology_path.stat().st_mtime
        if marker.exists():
            marker_mtime = marker.stat().st_mtime
            result["ontology_stale"] = ontology_mtime > marker_mtime
        else:
            # No marker → hubs never generated → stale by default if ontology exists
            result["ontology_stale"] = True

    return result


def format_drift_report(report: dict) -> str:
    """Format a drift_report() dict as human-readable text for mem-wrap output.

    Kept separate from drift_report so programmatic callers can consume
    the structured data without re-parsing strings.
    """
    lines: list[str] = []

    dupes = report.get("near_duplicates", [])
    if dupes:
        lines.append("Near-duplicate concepts:")
        for a, b, reason in dupes:
            lines.append(
                f"  drift: '{a}' ≈ '{b}' ({reason}) — suggest: `mem concepts merge {a} {b}`"
            )

    candidates = report.get("new_concept_candidates", [])
    if candidates:
        lines.append("New ontology candidates (count ≥ {}):".format(DRIFT_COUNT_THRESHOLD))
        for concept, cnt in candidates:
            lines.append(
                f"  drift: '{concept}' has {cnt} note(s) — consider adding to ontology.yaml"
            )

    if report.get("ontology_stale"):
        lines.append(
            "hint: ontology.yaml is newer than the last hub generation — "
            "run `mem concepts hubs` to refresh hub pages"
        )

    if not lines:
        return "No drift detected."
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Doctor — broader vault coherence linter (`mem doctor`)
# ---------------------------------------------------------------------------

# Concepts in the ontology assigned to fewer than this many notes are flagged
# as dead vocabulary. The threshold matches DRIFT_COUNT_THRESHOLD's posture
# (advisory, not enforcing) but uses a lower bar — once a concept clears 5
# notes it's a candidate for the ontology; below 2 it's a dead entry.
DEAD_VOCAB_THRESHOLD = 2


def find_tag_concept_overlap(db) -> list[tuple[str, int, int]]:
    """Find strings used as both a tag and a concept somewhere in the vault.

    Returns ``[(term, tag_count, concept_count), ...]`` sorted by combined
    count descending. Empty list if no overlap.
    """
    tag_counts: dict[str, int] = {}
    for row in db.execute(
        "SELECT tag, COUNT(*) AS cnt FROM note_tags GROUP BY tag"
    ):
        tag_counts[row["tag"].lower()] = row["cnt"]

    concept_counts: dict[str, int] = {}
    for row in db.execute(
        "SELECT concept, COUNT(*) AS cnt FROM note_concepts GROUP BY concept"
    ):
        concept_counts[row["concept"].lower()] = row["cnt"]

    overlap = sorted(set(tag_counts) & set(concept_counts))
    rows = [(term, tag_counts[term], concept_counts[term]) for term in overlap]
    rows.sort(key=lambda r: r[1] + r[2], reverse=True)
    return rows


def find_unknown_tags(db, vocabulary: set[str]) -> list[tuple[str, int]]:
    """Find tags in use that aren't in the canonical vocabulary.

    Returns ``[(tag, count), ...]`` sorted by count descending. Empty list
    if every used tag is in vocabulary or vocabulary is empty (in which
    case linting is disabled).
    """
    if not vocabulary:
        return []

    rows: list[tuple[str, int]] = []
    for row in db.execute(
        "SELECT tag, COUNT(*) AS cnt FROM note_tags GROUP BY tag ORDER BY cnt DESC"
    ):
        tag = row["tag"].lower()
        if tag not in vocabulary:
            rows.append((tag, row["cnt"]))
    return rows


def find_dead_vocabulary(
    db,
    ontology: dict[str, list[str]],
    *,
    min_count: int = DEAD_VOCAB_THRESHOLD,
) -> list[tuple[str, int]]:
    """Find ontology concepts assigned to fewer than ``min_count`` notes.

    Returns ``[(concept, count), ...]`` sorted by count ascending (deadest
    first). Concepts in the ontology with zero vault assignments are
    included with count=0.
    """
    # Dead-vocabulary checks the *leaves* — domain headers like `swe-python`
    # are structural and shouldn't be flagged as dead just because no note
    # tags them directly (their leaf concepts may still have full coverage).
    leaves = build_leaf_set(ontology)
    if not leaves:
        return []

    counts: dict[str, int] = {}
    for row in db.execute(
        "SELECT concept, COUNT(*) AS cnt FROM note_concepts GROUP BY concept"
    ):
        counts[row["concept"].lower()] = row["cnt"]

    dead: list[tuple[str, int]] = []
    for concept in sorted(leaves):
        cnt = counts.get(concept, 0)
        if cnt < min_count:
            dead.append((concept, cnt))
    dead.sort(key=lambda r: r[1])
    return dead


_PHANTOM_NAME_RE = re.compile(r"^(n|dec|src|ses|thm)-[a-z0-9]+\.md$")


def find_phantom_note_files(vault_root: Path) -> list[Path]:
    """Return zero-byte note-id-named files outside session folders.

    These are the residue of Obsidian wikilink misresolution: when a
    [[n-XXX]] / [[dec-XXX]] / [[src-XXX]] click cannot resolve to an
    existing filename or alias, Obsidian creates an empty file at the
    vault root with the wikilink target as filename. Real notes never
    look like this — they live under their slug filename and carry the
    id in frontmatter — so a zero-byte file with this shape is always
    phantom residue and safe to delete.
    """
    if not vault_root.exists():
        return []
    phantoms: list[Path] = []
    for path in vault_root.rglob("*.md"):
        if not _PHANTOM_NAME_RE.match(path.name):
            continue
        # Notes inside a session folder are legitimate (sessions filename
        # their session.md but extracted notes inside the folder may be
        # named after their id).
        if "/sessions/" in str(path):
            continue
        if path.stat().st_size == 0:
            phantoms.append(path)
    return phantoms


def doctor_report(config: Config) -> dict:
    """Run all coherence checks and return a structured report.

    Read-only. Never modifies anything. Returns a dict with keys:

    - ``tag_concept_overlap``: list of (term, tag_count, concept_count)
    - ``unknown_tags``: list of (tag, count) — tags outside the canonical
      vocabulary
    - ``dead_vocabulary``: list of (concept, count) — ontology concepts
      with fewer than DEAD_VOCAB_THRESHOLD vault assignments
    - ``vocabulary_size``: int — size of the canonical tag vocabulary
      (0 = linting disabled because no vocabulary is declared)
    - ``phantom_note_files``: list of Path — zero-byte n-*/dec-*/src-*
      files at vault root, residue of unresolved wikilink clicks
    """
    import sqlite3

    result: dict = {
        "tag_concept_overlap": [],
        "unknown_tags": [],
        "dead_vocabulary": [],
        "vocabulary_size": 0,
        "phantom_note_files": [],
    }

    result["phantom_note_files"] = find_phantom_note_files(config.vault_root)

    if not config.index_db.exists():
        return result

    vocabulary = load_tag_vocabulary()
    ontology = load_ontology()
    result["vocabulary_size"] = len(vocabulary)

    db = sqlite3.connect(str(config.index_db))
    db.row_factory = sqlite3.Row
    try:
        result["tag_concept_overlap"] = find_tag_concept_overlap(db)
        result["unknown_tags"] = find_unknown_tags(db, vocabulary)
        result["dead_vocabulary"] = find_dead_vocabulary(db, ontology)
    finally:
        db.close()

    return result


def format_doctor_report(report: dict) -> str:
    """Human-readable rendering of doctor_report()."""
    lines: list[str] = []

    overlap = report.get("tag_concept_overlap", [])
    if overlap:
        lines.append("Tag/concept overlap (a term should live in one field, never both):")
        for term, tag_cnt, concept_cnt in overlap:
            lines.append(
                f"  '{term}' — used as tag on {tag_cnt} note(s), "
                f"as concept on {concept_cnt} note(s)"
            )
        lines.append("")

    unknown = report.get("unknown_tags", [])
    if unknown:
        lines.append(
            "Unknown tags (not in tag_vocabulary; add to ontology.yaml or rename):"
        )
        for tag, cnt in unknown:
            lines.append(f"  '{tag}' — used on {cnt} note(s)")
        lines.append("")
    elif report.get("vocabulary_size", 0) == 0:
        lines.append(
            "Tag vocabulary is empty — add a `tag_vocabulary:` block to "
            "ontology.yaml to enable unknown-tag detection."
        )
        lines.append("")

    dead = report.get("dead_vocabulary", [])
    if dead:
        lines.append(
            f"Dead vocabulary (ontology concepts with < {DEAD_VOCAB_THRESHOLD} notes):"
        )
        for concept, cnt in dead:
            suffix = "0 notes — never used" if cnt == 0 else f"{cnt} note(s)"
            lines.append(f"  '{concept}' — {suffix}")
        lines.append("")

    phantoms = report.get("phantom_note_files", [])
    if phantoms:
        lines.append(
            f"Phantom note files ({len(phantoms)}; zero-byte residue of "
            "unresolved wikilink clicks; safe to delete with --fix-phantoms):"
        )
        for path in phantoms[:20]:
            lines.append(f"  {path}")
        if len(phantoms) > 20:
            lines.append(f"  … and {len(phantoms) - 20} more")
        lines.append("")

    if not lines:
        return "No coherence issues detected."
    return "\n".join(lines).rstrip()
