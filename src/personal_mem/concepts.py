"""Concept tightening utilities — aliases, near-duplicate detection, merge.

No external dependencies. Uses simple string similarity heuristics
to find near-duplicate concepts and a YAML aliases file for canonical mappings.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from personal_mem.config import Config


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
            # Levenshtein distance ≤ 2 (only for concepts of similar length)
            elif abs(len(a) - len(b)) <= 2 and _levenshtein(a, b) <= 2:
                reason = f"edit distance {_levenshtein(a, b)}"

            if reason:
                seen_pairs.add(pair)
                duplicates.append((a, b, reason))

    return duplicates


def get_all_concepts(db) -> dict[str, int]:
    """Get all concepts with counts from the index database."""
    concept_counts: dict[str, int] = defaultdict(int)
    for row in db.execute("SELECT frontmatter FROM notes"):
        fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
        concepts = fm.get("concepts", [])
        if isinstance(concepts, str):
            concepts = [c.strip() for c in concepts.split(",") if c.strip()]
        for c in concepts:
            concept_counts[c.lower()] += 1
    return dict(concept_counts)


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
        elif abs(len(new_lower) - len(ex_lower)) <= 2 and _levenshtein(new_lower, ex_lower) <= 2:
            suggestions.append(ex_lower)

    return suggestions[:max_suggestions]


def merge_concept_in_notes(
    vault_root: Path,
    from_concept: str,
    to_concept: str,
) -> int:
    """Rename a concept across all vault notes. Returns count of modified files."""
    from personal_mem.vault import parse_frontmatter, render_frontmatter

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
