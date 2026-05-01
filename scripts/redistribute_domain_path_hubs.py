"""Redistribute domain-path topic hubs into proper concept hubs (n-4dd8ad62).

A historical bug let domain strings like `swe/python` leak into note
`concepts:` arrays. The hub-skeleton generator turned each into a
topic-hub at `vault/concepts/topics/swe-python.md` etc. The result:
~20 oversized topic hubs whose filenames are domain paths, holding ~1k
learning-log entries that *should* live on the specific-concept hubs
that the source notes also touched (`pytest`, `pydantic`, `fastapi`,
`statusline`, `model-architecture`, …).

This script walks each domain-path hub, looks up each entry's source
note in the SQLite index, picks the most-established non-domain-path
concept the source note also carries, and appends the entry to that
concept's hub via `append_log_entries` (which handles dedup on
citation id). Entries whose source note has no specific concept get
dropped — the information lives in the source note; the topic-hub
synthesis isn't load-bearing for those.

Routing rule (mechanical, no LLM):

    For an entry citing source note S living on hub H:
    1. Load S's concepts: list from the SQLite index.
    2. Drop any concept that is a domain path (contains "/").
    3. Drop H's own dotted slug (e.g. for swe-python.md, drop "swe/python").
    4. Drop H's own dashed slug (e.g. drop "swe-python" too).
    5. If the remaining set is empty → drop the entry.
    6. Otherwise pick the concept with the highest vault-wide count
       (most-established target). Tiebreaker: alphabetical.

After redistribution: delete the origin hub file. Reindex changed hubs.

Run AFTER you've reviewed the dry-run output. Run with --apply to commit.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from personal_mem.config import load_config
from personal_mem.hubs import (
    LogEntry,
    append_log_entries,
    concept_hub_path,
    parse_concept_hub,
    topics_dir,
)
from personal_mem.indexer import Indexer


# Allowlist from n-4dd8ad62 — these are the topic-hub slugs that match
# (current or historical) ontology domain paths and shouldn't exist as
# topic hubs.
DOMAIN_PATH_HUB_SLUGS = [
    "swe-python",
    "ml-deep-learning",
    "ai-agents",
    "ai-tools",
    "swe-data",
    "swe-infra",
    "ml-training",
    "finance-markets",
    "finance-options",
    "finance-quant",
    "math-calculus",
    "math-linear-algebra",
    "math-numerical-linear-algebra",
    "math-probability",
    "ml-classical",
    "ml-embeddings",
    "ml-optimization",
    "ml-transformers",
    "ai-llms",
    "ai-infra",
]


def _build_concept_index(idx: Indexer) -> tuple[dict[str, set[str]], Counter]:
    """Build (note_id → set[concepts], concept → vault count)."""
    note_concepts: dict[str, set[str]] = defaultdict(set)
    counts: Counter = Counter()
    for row in idx.db.execute("SELECT note_id, concept FROM note_concepts"):
        note_id, concept = row[0], row[1]
        note_concepts[note_id].add(concept)
        counts[concept] += 1
    return note_concepts, counts


def pick_target_concept(
    source_concepts: set[str],
    hub_slug: str,
    concept_counts: Counter,
) -> str | None:
    """Apply the routing rule to a single source-note → entry pairing.

    Returns the chosen target concept or None if the entry should be
    dropped.
    """
    own_dotted = hub_slug.replace("-", "/")
    candidates = {
        c
        for c in source_concepts
        if "/" not in c and c != hub_slug and c != own_dotted
    }
    if not candidates:
        return None
    # Highest vault count wins; alphabetical tiebreaker.
    return min(candidates, key=lambda c: (-concept_counts.get(c, 0), c))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="commit changes (default: dry run)"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="print per-hub redistribution map"
    )
    parser.add_argument(
        "--limit-hubs",
        type=int,
        default=0,
        help="process only the first N hubs (0 = all)",
    )
    args = parser.parse_args()

    cfg = load_config()
    topics = topics_dir(cfg)

    idx = Indexer(config=cfg)
    note_concepts, concept_counts = _build_concept_index(idx)
    idx.close()

    grand_routed = 0
    grand_dropped_no_concept = 0
    grand_dropped_no_citation = 0
    grand_dedup = 0
    grand_target_counts: Counter = Counter()
    deleted_hubs: list[Path] = []
    # (origin_hub_slug, note_id, entry_date, entry_text) for the manifest.
    dropped_records: list[tuple[str, str, str, str]] = []

    hubs_to_process = DOMAIN_PATH_HUB_SLUGS
    if args.limit_hubs > 0:
        hubs_to_process = hubs_to_process[: args.limit_hubs]

    for slug in hubs_to_process:
        hub_path = topics / f"{slug}.md"
        if not hub_path.exists():
            print(f"  {slug}: missing — skipping")
            continue

        hub = parse_concept_hub(hub_path)
        if not hub.log_entries:
            print(f"  {slug}: empty — would delete (no redistribution needed)")
            if args.apply:
                hub_path.unlink()
                deleted_hubs.append(hub_path)
            continue

        # Group entries by target concept for this hub.
        per_target: dict[str, list[LogEntry]] = defaultdict(list)
        dropped_no_concept = 0
        dropped_no_citation = 0
        for e in hub.log_entries:
            if not e.citation:
                dropped_no_citation += 1
                continue
            concepts = note_concepts.get(e.citation, set())
            target = pick_target_concept(concepts, slug, concept_counts)
            if target is None:
                dropped_no_concept += 1
                dropped_records.append((slug, e.citation, e.date, e.text))
                continue
            per_target[target].append(e)

        routed_count = sum(len(v) for v in per_target.values())
        grand_routed += routed_count
        grand_dropped_no_concept += dropped_no_concept
        grand_dropped_no_citation += dropped_no_citation
        for t, es in per_target.items():
            grand_target_counts[t] += len(es)

        if args.verbose:
            top = sorted(per_target.items(), key=lambda kv: -len(kv[1]))[:6]
            top_str = ", ".join(f"{t}={len(es)}" for t, es in top)
            print(
                f"  {slug}: {len(hub.log_entries)} entries → "
                f"{routed_count} routed across {len(per_target)} concept(s); "
                f"dropped: {dropped_no_concept} (no specific concept), "
                f"{dropped_no_citation} (no citation)"
                + (f" — top: {top_str}" if top else "")
            )

        if args.apply:
            for target, entries in per_target.items():
                # Dedup against existing hub citations is handled by
                # append_log_entries.
                target_path = concept_hub_path(cfg, target)
                pre_existing = (
                    parse_concept_hub(target_path).cited_ids if target_path.exists() else set()
                )
                pre_dedup = sum(1 for e in entries if e.citation in pre_existing)
                grand_dedup += pre_dedup
                append_log_entries(cfg, target, entries)
            # Delete the origin hub now that its entries have been re-homed.
            hub_path.unlink()
            deleted_hubs.append(hub_path)

    print()
    print(f"{'APPLIED' if args.apply else 'WOULD CHANGE'}:")
    print(f"  routed entries:                {grand_routed}")
    print(f"  dropped (no specific concept): {grand_dropped_no_concept}")
    print(f"  dropped (no citation):         {grand_dropped_no_citation}")
    if args.apply:
        print(f"  dedup'd against existing:      {grand_dedup}")
        print(f"  hubs deleted:                  {len(deleted_hubs)}")
    print(
        f"  distinct target concepts:      {len(grand_target_counts)}"
    )
    if grand_target_counts:
        print("  top 10 targets:")
        for c, n in grand_target_counts.most_common(10):
            print(f"    {c}: {n}")

    # Write the dropped-citations manifest so the user can later enrich
    # the source notes with specific concepts and re-run the LLM
    # backfill to recover the synthesis. TSV format for easy grep.
    if args.apply and dropped_records:
        manifest_path = cfg.mem_dir / "redistribution_dropped.tsv"
        lines = [
            "# Domain-path topic hub redistribution — dropped entries",
            "# Source notes are preserved; only the topic-hub synthesis was lost.",
            "# To recover: enrich the source note with a specific (non-domain-path)",
            "# concept, then re-run the concept-hub backfill.",
            "#",
            "# origin_hub\tnote_id\tentry_date\tentry_text_snippet",
        ]
        for slug, note_id, date, text in dropped_records:
            snippet = text.replace("\t", " ").replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:197] + "..."
            lines.append(f"{slug}\t{note_id}\t{date}\t{snippet}")
        manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nWrote dropped-citations manifest: {manifest_path}")
        print(f"  ({len(dropped_records)} entries — review with `grep` or open in your editor)")

    if args.apply and deleted_hubs:
        print("\nReindexing... (full rebuild — many hubs touched)")
        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()
        print("Done.")
    elif not args.apply:
        print("\n[dry run; pass --apply to commit]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
