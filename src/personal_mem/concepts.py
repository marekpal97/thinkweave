"""Concept tightening utilities — aliases, near-duplicate detection, merge,
pruning, and Obsidian hub page generation.

No external dependencies. Uses simple string similarity heuristics
to find near-duplicate concepts and a YAML aliases file for canonical mappings.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
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
    """Path to the read-only ontology seed shipped with the package."""
    return Path(__file__).parent / "ontology.yaml"


def _vault_ontology_path() -> Path | None:
    """Path to the vault-local override, or None if no vault is configured."""
    try:
        from personal_mem.config import load_config

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
_RESERVED_ONTOLOGY_KEYS = frozenset({"tag_vocabulary"})


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
    """Parse the effective ontology — seed layered beneath the vault override.

    With ``path=None`` (the canonical call): read the shipped seed, then
    layer the vault override on top. The vault wins per top-level key, so
    any key the vault explicitly defines (even as an empty list) shadows
    the seed; keys the vault never defined fall through to the seed. This
    means new ontology keys shipped with the package are picked up by
    older vaults without a manual migration.

    With ``path`` given: read only that file, no layering. Useful for
    tests and tooling that want a single source of truth.
    """
    if path is not None:
        return _parse_yaml_file(path)

    layered = _parse_yaml_file(_seed_ontology_path())
    override_path = _vault_ontology_path()
    if override_path is not None and override_path.exists():
        layered.update(_parse_yaml_file(override_path))
    return layered


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
    """Build the set of all concepts referenced in the ontology."""
    keep: set[str] = set()
    for concepts in ontology.values():
        keep.update(c.lower() for c in concepts)
    return keep


def concept_to_domains(ontology: dict[str, list[str]]) -> dict[str, list[str]]:
    """Build reverse map: concept → [domain1, domain2, ...]."""
    reverse: dict[str, list[str]] = defaultdict(list)
    for domain, concepts in ontology.items():
        for c in concepts:
            reverse[c.lower()].append(domain)
    return dict(reverse)


def prune_concepts(
    vault_root: Path,
    keep_set: set[str],
) -> dict:
    """Remove concepts not in keep_set from all vault notes.

    Returns stats dict: {files_modified, concepts_removed}.
    """
    from personal_mem.vault import parse_frontmatter, render_frontmatter

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

        safe_name = domain.replace("/", "--") + ".md"
        hub_path = concepts_dir / safe_name
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
    from personal_mem.hubs import ensure_concept_hub_skeleton

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
    from personal_mem.vault import parse_frontmatter, render_frontmatter

    ontology = ontology or load_ontology()
    c2d = concept_to_domains(ontology)

    vm_root = config.vault_root
    modified = 0

    for md_file in vm_root.rglob("*.md"):
        # Skip hub pages and landing pages
        if md_file.parent.name == "concepts":
            continue
        if md_file.name in ("DECISIONS.md", "BACKLOG.md", "STATE.md"):
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

        # Build wikilinks section
        links = sorted(note_domains)
        link_lines = [f"[[concepts/{d.replace('/', '--')}|{d.split('/')[-1].replace('-', ' ').title()}]]" for d in links]
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
    from personal_mem.hubs import concept_hub_path

    path = concept_hub_path(config, concept)
    if path.exists():
        path.unlink()
        return True
    return False


def find_orphan_hubs(config: Config) -> list[tuple[str, Path]]:
    """Find concept hub pages whose underlying concept has zero vault
    assignments and is not in ``ontology.yaml``.

    Returns ``[(concept, path), ...]``. Read-only — caller decides whether
    to delete. A hub for a concept with notes (even if the concept itself
    isn't in the ontology) is kept; orphan = no notes AND not in ontology.
    """
    import sqlite3

    from personal_mem.hubs import topics_dir

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
    from personal_mem.hubs import parse_concept_hub, topics_dir

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
    keep = build_keep_set(ontology)
    if not keep:
        return []

    counts: dict[str, int] = {}
    for row in db.execute(
        "SELECT concept, COUNT(*) AS cnt FROM note_concepts GROUP BY concept"
    ):
        counts[row["concept"].lower()] = row["cnt"]

    dead: list[tuple[str, int]] = []
    for concept in sorted(keep):
        cnt = counts.get(concept, 0)
        if cnt < min_count:
            dead.append((concept, cnt))
    dead.sort(key=lambda r: r[1])
    return dead


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
    """
    import sqlite3

    result: dict = {
        "tag_concept_overlap": [],
        "unknown_tags": [],
        "dead_vocabulary": [],
        "vocabulary_size": 0,
    }

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

    if not lines:
        return "No coherence issues detected."
    return "\n".join(lines).rstrip()
