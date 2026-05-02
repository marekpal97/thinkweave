"""One-shot vault vocabulary cleanup applying the 2026-05 ontology surgery slate.

Mappings encoded inline (no pyyaml dep). For the human-readable slate that
generated these decisions see scripts/ontology_cleanup_2026_05.config.yaml.

Order of operations per note:
  1. tag_renames     (left → right within tags array)
  2. tag_to_concept  (strip from tags, ensure in concepts)
  3. tag_deletes    (strip from tags)
  4. concept_deletes (strip from concepts)

TAG_TO_CONCEPT wins over TAG_DELETES for keys present in both — by ordering
to_concept before deletes, the tag is moved (not deleted), preserving
information. After all four steps both arrays are deduped (order-preserving)
and dropped from the frontmatter entirely if empty (matches the convention
used by scripts/cleanup_tag_concept_overlap.py).

Default mode is dry-run; pass ``--apply`` to commit changes.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from personal_mem.core.config import load_config
from personal_mem.core.vault import parse_frontmatter, render_frontmatter


SKIP_DIRS = {"templates", ".obsidian", ".trash"}


# ----------------------------------------------------------------------
# Operation tables (slate, encoded inline). Mirrors
# scripts/ontology_cleanup_2026_05.config.yaml — that YAML is reference
# documentation; this script does not read it at runtime.
# ----------------------------------------------------------------------


TAG_DELETES: set[str] = {
    # Section A — concept-wins overlaps:
    "novelty-detection", "documentation", "implementation", "options",
    "feature-extraction", "image-handling", "continual-learning",
    "deployment", "market-research", "contrastive-learning", "idempotency",
    "resilience",
    # Section E — DELETE-FROM-NOTES (lifecycle / noise):
    "budget-constraint", "coordinator-discipline", "cosmetic", "cron",
    "data-fix", "data-quality", "dedup", "design-choice",
    "developer-experience", "failure", "failure-mode", "feedback",
    "insight", "lesson", "lesson-learned", "meta", "plan-revision",
    "pivot", "pragmatic", "scope-control", "skipped", "source-types",
    # Section E — DELETE (too-generic / out-of-scope):
    "engineering", "framework", "philosophy", "policy", "processing",
    "scanner", "scheduling", "theory", "thesis", "mobile", "biotech",
    "cognitive-science", "medical-imaging", "tool",
}

TAG_RENAMES: dict[str, str] = {
    "benchmarking": "benchmark",
    "new-feature": "enhancement",
    "paradigm-shift": "paradigm",
    "paradigm-extension": "paradigm",
    "paradigm-decision": "paradigm",
    "reference-list": "reference",
}

TAG_TO_CONCEPT: dict[str, str] = {
    "agents": "agentic-systems",
    "continual-learning": "continual-learning",
    "contrastive-learning": "contrastive-learning",
    "deployment": "deployment",
    "feature-extraction": "feature-extraction",
    "idempotency": "idempotency",
    "inductive-bias": "inductive-bias",
    "labor": "labor-market",
    "labor-market": "labor-market",
    "macro": "macro-trading",
    "multi-agent": "multi-agent-systems",
    "novelty-detection": "novelty-detection",
    "resilience": "resilience",
    "rl": "reinforcement-learning",
    "ood-detection": "out-of-distribution-detection",
    "density-novelty": "density-estimation",
    "memory": "memory-system",
    "memory-systems": "memory-system",
    "agent-architecture": "agentic-systems",
    "agent-framework": "agentic-systems",
    "llm-agents": "agentic-systems",
}

CONCEPT_DELETES: set[str] = {
    # Section A — keep tag, drop too-generic concept:
    "framework", "infrastructure", "design", "planning", "mem-wrap",
    # Section E singletons that were also accidental concepts:
    "architecture-search", "baseline", "deep-learning", "feature-design",
    "financial-nlp", "high-dimensional", "hub-system", "implementation",
    "inference", "information-retrieval", "knowledge-base", "ledger-pattern",
    "local-inference", "local-llm", "real-time", "reasoning",
    "recommendation", "research-automation", "streaming-ml",
    "synthesis-layer", "system-design", "theory", "thesis",
    "verification", "indexer",
}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _normalize_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v) for v in value]


def _dedup_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ----------------------------------------------------------------------
# Per-note application
# ----------------------------------------------------------------------


@dataclass
class OpStats:
    """Running counters for a single operation class."""

    notes: int = 0
    total: int = 0  # total tag/concept items affected across all notes


@dataclass
class CleanupStats:
    notes_scanned: int = 0
    notes_modified: int = 0
    tag_renames: OpStats = field(default_factory=OpStats)
    tag_to_concept: OpStats = field(default_factory=OpStats)
    tag_deletes: OpStats = field(default_factory=OpStats)
    concept_deletes: OpStats = field(default_factory=OpStats)
    errors: list[tuple[Path, str]] = field(default_factory=list)
    samples: dict[str, list[Path]] = field(
        default_factory=lambda: {
            "tag_renames": [],
            "tag_to_concept": [],
            "tag_deletes": [],
            "concept_deletes": [],
        }
    )


def apply_operations(
    fm: dict,
    *,
    tag_renames: dict[str, str] = TAG_RENAMES,
    tag_to_concept: dict[str, str] = TAG_TO_CONCEPT,
    tag_deletes: set[str] = TAG_DELETES,
    concept_deletes: set[str] = CONCEPT_DELETES,
) -> tuple[dict, dict[str, int]]:
    """Apply the four cleanup operation classes to a frontmatter dict.

    Pure function — does not mutate the input. Returns a tuple of
    ``(new_fm, per_op_counts)``, where ``per_op_counts`` is a dict with
    keys ``tag_renames``, ``tag_to_concept``, ``tag_deletes``,
    ``concept_deletes`` mapping to the number of items affected on this
    specific note (0 means the rule did not fire here).

    Operation order — renames, then to_concept, then deletes — ensures
    TAG_TO_CONCEPT wins over TAG_DELETES for any key present in both
    (the tag is moved, not silently dropped).

    Empty ``tags``/``concepts`` arrays are removed from the output dict
    rather than serialised as ``tags: []`` — matches the convention used
    by ``scripts/cleanup_tag_concept_overlap.py``.

    The defaults bind the module-level slate tables; tests pass overrides.
    """
    tags = _normalize_list(fm.get("tags"))
    concepts = _normalize_list(fm.get("concepts"))

    counts = {
        "tag_renames": 0,
        "tag_to_concept": 0,
        "tag_deletes": 0,
        "concept_deletes": 0,
    }

    # 1. tag_renames — rewrite each tag string per the dict.
    if tag_renames:
        new_tags: list[str] = []
        for t in tags:
            if t in tag_renames:
                counts["tag_renames"] += 1
                new_tags.append(tag_renames[t])
            else:
                new_tags.append(t)
        tags = new_tags

    # 2. tag_to_concept — strip tag, ensure concept present in concepts.
    if tag_to_concept:
        moved_targets: list[str] = []
        kept_tags: list[str] = []
        for t in tags:
            if t in tag_to_concept:
                counts["tag_to_concept"] += 1
                moved_targets.append(tag_to_concept[t])
            else:
                kept_tags.append(t)
        tags = kept_tags
        for target in moved_targets:
            if target not in concepts:
                concepts.append(target)

    # 3. tag_deletes — drop matching tags.
    if tag_deletes:
        kept_tags = []
        for t in tags:
            if t in tag_deletes:
                counts["tag_deletes"] += 1
            else:
                kept_tags.append(t)
        tags = kept_tags

    # 4. concept_deletes — drop matching concepts.
    if concept_deletes:
        kept_concepts: list[str] = []
        for c in concepts:
            if c in concept_deletes:
                counts["concept_deletes"] += 1
            else:
                kept_concepts.append(c)
        concepts = kept_concepts

    # Final: dedup preserving order; drop empty keys entirely.
    tags = _dedup_preserve_order(tags)
    concepts = _dedup_preserve_order(concepts)

    new_fm = dict(fm)
    if tags:
        new_fm["tags"] = tags
    else:
        new_fm.pop("tags", None)
    if concepts:
        new_fm["concepts"] = concepts
    else:
        new_fm.pop("concepts", None)

    return new_fm, counts


def _frontmatter_changed(old_fm: dict, new_fm: dict) -> bool:
    """Did tags/concepts (the only fields we touch) actually change?"""
    return (
        _normalize_list(old_fm.get("tags")) != _normalize_list(new_fm.get("tags"))
        or _normalize_list(old_fm.get("concepts"))
        != _normalize_list(new_fm.get("concepts"))
        or ("tags" in old_fm) != ("tags" in new_fm)
        or ("concepts" in old_fm) != ("concepts" in new_fm)
    )


# ----------------------------------------------------------------------
# Vault walker
# ----------------------------------------------------------------------


def cleanup_vault(
    vault_root: Path,
    *,
    apply: bool,
    sample_size: int = 5,
) -> CleanupStats:
    """Walk the vault, apply operations, and accumulate stats.

    When ``apply=False`` (dry-run) no files are written, but counts and
    sample paths are still populated so callers can preview the impact.
    """
    stats = CleanupStats()

    for md in vault_root.rglob("*.md"):
        rel = md.relative_to(vault_root)
        if any(p in SKIP_DIRS for p in rel.parts):
            continue

        stats.notes_scanned += 1
        try:
            text = md.read_text(encoding="utf-8")
            old_fm, body = parse_frontmatter(text)
        except Exception as e:  # noqa: BLE001
            stats.errors.append((md, str(e)))
            continue

        new_fm, counts = apply_operations(old_fm)
        for op_name, count in counts.items():
            if count > 0:
                op_stats: OpStats = getattr(stats, op_name)
                op_stats.notes += 1
                op_stats.total += count
                samples = stats.samples[op_name]
                if len(samples) < sample_size:
                    samples.append(md)

        if not _frontmatter_changed(old_fm, new_fm):
            continue

        stats.notes_modified += 1
        if apply:
            try:
                md.write_text(
                    render_frontmatter(new_fm) + "\n\n" + body,
                    encoding="utf-8",
                )
            except Exception as e:  # noqa: BLE001
                stats.errors.append((md, f"write failed: {e}"))

    return stats


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------


_OP_UNITS = {
    "tag_renames": "total replacements",
    "tag_to_concept": "tags moved",
    "tag_deletes": "tags stripped",
    "concept_deletes": "concepts stripped",
}


def _format_report(
    vault_root: Path,
    stats: CleanupStats,
    *,
    apply: bool,
    sample_size: int,
) -> str:
    lines: list[str] = []
    lines.append(f"Vault root: {vault_root}")
    lines.append(f"Notes scanned: {stats.notes_scanned}")
    lines.append(f"Notes modified: {stats.notes_modified}")
    lines.append("")
    lines.append("Operations applied:")
    for op_name in ("tag_renames", "tag_to_concept", "tag_deletes", "concept_deletes"):
        op_stats: OpStats = getattr(stats, op_name)
        unit = _OP_UNITS[op_name]
        lines.append(
            f"  {op_name:<15} fired on {op_stats.notes} notes "
            f"({op_stats.total} {unit})"
        )

    if not apply:
        lines.append("")
        lines.append(
            f"[dry run; pass --apply to commit. "
            f"Showing up to {sample_size} sample notes per op]"
        )
        for op_name in ("tag_renames", "tag_to_concept", "tag_deletes", "concept_deletes"):
            samples = stats.samples[op_name]
            if not samples:
                continue
            lines.append(f"  {op_name}:")
            for path in samples:
                try:
                    rel = path.relative_to(vault_root)
                except ValueError:
                    rel = path
                lines.append(f"    {rel}")

    if stats.errors:
        lines.append("")
        lines.append(f"Errors: {len(stats.errors)}")
        for path, msg in stats.errors[:20]:
            try:
                rel = path.relative_to(vault_root)
            except ValueError:
                rel = path
            lines.append(f"  {rel}: {msg}")
        if len(stats.errors) > 20:
            lines.append(f"  ... +{len(stats.errors) - 20} more")
    else:
        lines.append("")
        lines.append("Errors: none")

    return "\n".join(lines)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit changes (default: dry run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicit dry-run flag (default behaviour; ignored if --apply also given)",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=5,
        help="how many sample affected paths to print per op (default: 5)",
    )
    args = parser.parse_args()

    apply = bool(args.apply)
    if args.dry_run and apply:
        # Explicit --apply wins; warn but proceed.
        print(
            "warning: both --dry-run and --apply given; --apply takes precedence",
            file=sys.stderr,
        )

    cfg = load_config()
    vault_root = cfg.vault_root

    stats = cleanup_vault(vault_root, apply=apply, sample_size=args.show)
    print(_format_report(vault_root, stats, apply=apply, sample_size=args.show))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
