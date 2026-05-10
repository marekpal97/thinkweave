"""Concept-bundle dedup helpers for news ingestion.

Two callers use this module:

* The ``research-news-worker`` subagent's per-item dedup step (24h window,
  Jaccard ≥0.8). The worker queries recent notes via ``mem_search`` and
  applies :func:`jaccard` against each candidate before ``mem_create``.

* The ``/drain --source-type news`` post-batch ``dedup_sweep`` hook. After
  the parallel workers commit, the orchestrator collects their note ids
  and calls :func:`find_near_duplicate_pairs` to detect within-batch
  race-condition duplicates that slipped through (because each worker
  dedups against committed state, so two workers in the same 30s window
  can both pass and create near-dupes).

The math lives here — not in the LLM's head — so it's deterministic,
testable, and not dependent on the worker's arithmetic.
"""

from __future__ import annotations

import argparse
import json
import sys
from itertools import combinations
from typing import Iterable


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """Jaccard similarity over two concept sets.

    Empty-vs-empty is 1.0 (identity). Empty-vs-nonempty is 0.0.
    Strings are compared case-folded — concept namespaces are
    canonicalised lowercase upstream, but folding here defends against
    sloppy frontmatter capitalisation.
    """
    a_set = {s.strip().lower() for s in a if s and s.strip()}
    b_set = {s.strip().lower() for s in b if s and s.strip()}
    if not a_set and not b_set:
        return 1.0
    union = a_set | b_set
    if not union:
        return 0.0
    return len(a_set & b_set) / len(union)


def find_near_duplicate_pairs(
    notes: list[dict],
    threshold: float = 0.8,
) -> list[tuple[str, str, float]]:
    """Return ``[(loser_id, winner_id, jaccard), ...]`` for near-duplicate pairs.

    Pairing rule:
      * Higher tier wins (tier 1 < tier 2; lower number = better outlet).
      * Tie on tier → earlier ``created_at`` wins (older note keeps its
        ground; the newer near-dupe is the loser).

    Each note dict needs: ``id``, ``concepts`` (list[str]), ``tier`` (int,
    default 2), ``created_at`` (ISO string, default empty).

    No transitive cleanup: if A≈B≈C but A<>C, we return (A,B) and (B,C)
    as separate pairs. Caller decides whether to chain-supersede.
    """
    pairs: list[tuple[str, str, float]] = []
    for a, b in combinations(notes, 2):
        j = jaccard(a.get("concepts") or [], b.get("concepts") or [])
        if j < threshold:
            continue
        a_tier = int(a.get("tier", 2))
        b_tier = int(b.get("tier", 2))
        if a_tier < b_tier:
            winner, loser = a, b
        elif b_tier < a_tier:
            winner, loser = b, a
        else:
            a_ct = a.get("created_at") or ""
            b_ct = b.get("created_at") or ""
            winner, loser = (a, b) if a_ct <= b_ct else (b, a)
        pairs.append((str(loser["id"]), str(winner["id"]), round(j, 4)))
    return pairs


def main() -> int:
    """CLI: read JSON-list of notes from stdin, print pair list as JSON.

    Used by ``/drain``'s dedup_sweep hook:

        echo '[{"id": "src-a", ...}, ...]' | \\
          uv run python -m personal_mem.operations.news_dedup --threshold 0.8
    """
    parser = argparse.ArgumentParser(
        description="Find near-duplicate news notes by concept-Jaccard."
    )
    parser.add_argument("--threshold", type=float, default=0.8)
    args = parser.parse_args()

    try:
        notes = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        print(f"Bad JSON on stdin: {exc}", file=sys.stderr)
        return 2
    if not isinstance(notes, list):
        print("Expected a JSON list of notes", file=sys.stderr)
        return 2

    pairs = find_near_duplicate_pairs(notes, threshold=args.threshold)
    print(json.dumps(
        [{"loser": l, "winner": w, "jaccard": j} for l, w, j in pairs],
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
