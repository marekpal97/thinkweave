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
            # Levenshtein distance ≤ 2 (only for concepts of similar length)
            elif abs(len(a) - len(b)) <= 2 and _levenshtein(a, b) <= 2:
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
        elif abs(len(new_lower) - len(ex_lower)) <= 2 and _levenshtein(new_lower, ex_lower) <= 2:
            suggestions.append(ex_lower)

    return suggestions[:max_suggestions]


def _ontology_path() -> Path:
    """Path to the ontology YAML file (shipped with the package)."""
    return Path(__file__).parent / "ontology.yaml"


def load_ontology(path: Path | None = None) -> dict[str, list[str]]:
    """Load domain → [concepts] from the ontology YAML file.

    Uses the same minimal YAML parser as load_aliases.
    """
    path = path or _ontology_path()
    if not path.exists():
        return {}

    ontology: dict[str, list[str]] = {}
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
                ontology[current_key] = [i.lower() for i in items]
            else:
                ontology.setdefault(current_key, [])
        elif line.startswith(" ") and stripped.startswith("- "):
            item = stripped[2:].strip().strip("\"'").lower()
            if current_key:
                ontology.setdefault(current_key, []).append(item)
    return ontology


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


def generate_hub_pages(
    config: Config,
    ontology: dict[str, list[str]] | None = None,
) -> dict[str, Path]:
    """Generate Obsidian hub pages in vault/concepts/ for each domain.

    Each hub page wikilinks to all notes that have concepts in that domain.
    Returns {domain: path} for generated files.
    """
    from personal_mem.vault import VaultManager, parse_frontmatter

    ontology = ontology or load_ontology()
    if not ontology:
        return {}

    vm = VaultManager(config=config)
    c2d = concept_to_domains(ontology)

    # Scan all notes and group by domain
    domain_notes: dict[str, list[dict]] = defaultdict(list)

    for md_file in vm.root.rglob("*.md"):
        # Skip concept hub pages themselves
        if "concepts" in md_file.parts and md_file.parent.name == "concepts":
            continue
        # Skip landing pages
        if md_file.name in ("DECISIONS.md", "BACKLOG.md", "STATE.md"):
            continue

        text = md_file.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(text)
        if not fm or not fm.get("id"):
            continue

        concepts = fm.get("concepts", [])
        if isinstance(concepts, str):
            concepts = [c.strip() for c in concepts.split(",") if c.strip()]

        # Find which domains this note belongs to
        note_domains: set[str] = set()
        for c in concepts:
            for domain in c2d.get(c.lower(), []):
                note_domains.add(domain)

        note_info = {
            "title": fm.get("title", md_file.stem),
            "id": fm.get("id", ""),
            "type": fm.get("type", "note"),
            "date": fm.get("date", ""),
            "path": md_file,
            "filename": md_file.stem,
        }

        for domain in note_domains:
            domain_notes[domain].append(note_info)

    # Generate hub pages
    concepts_dir = vm.root / "concepts"
    concepts_dir.mkdir(parents=True, exist_ok=True)

    generated: dict[str, Path] = {}
    now = datetime.now(timezone.utc).isoformat()

    for domain, domain_concepts in sorted(ontology.items()):
        notes = domain_notes.get(domain, [])
        # Sort notes by date descending
        notes.sort(key=lambda n: n.get("date", ""), reverse=True)

        # Domain display name: "math/linear-algebra" → "Linear Algebra"
        display_name = domain.split("/")[-1].replace("-", " ").title()
        # Category prefix: "math/linear-algebra" → "Mathematics"
        category = domain.split("/")[0] if "/" in domain else ""
        category_display = {
            "math": "Mathematics",
            "ml": "Machine Learning",
            "ai": "AI & LLMs",
            "finance": "Finance",
            "swe": "Software Engineering",
        }.get(category, category.title())

        # Build page content
        lines = [
            "---",
            "type: concept-hub",
            f"domain: {domain}",
            f"concepts: [{', '.join(domain_concepts)}]",
            "auto_generated: true",
            f"updated: \"{now}\"",
            "---",
            "",
            f"# {display_name}",
            "",
        ]

        if category_display:
            lines.append(f"*{category_display}*")
            lines.append("")

        lines.append(f"**Concepts**: {', '.join(domain_concepts)}")
        lines.append("")

        if notes:
            lines.append(f"## Notes ({len(notes)})")
            lines.append("")
            for note in notes:
                type_badge = f"[{note['type']}]" if note["type"] != "note" else ""
                date_str = note["date"][:10] if note["date"] else ""
                lines.append(f"- [[{note['filename']}]] {type_badge} {date_str}".rstrip())
        else:
            lines.append("*No notes yet.*")

        lines.append("")

        # Write file — use domain path as filename (math/linear-algebra → math--linear-algebra.md)
        safe_name = domain.replace("/", "--") + ".md"
        hub_path = concepts_dir / safe_name
        hub_path.write_text("\n".join(lines), encoding="utf-8")
        generated[domain] = hub_path

    return generated


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
