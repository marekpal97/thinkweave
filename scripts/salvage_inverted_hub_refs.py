"""Salvage inverted refs in concept hubs (n-8645c889).

The 2026-04-23 `mem hubs link` batch produced 51 entries whose `ref`
points to a date later than the entry's own date — the symptom of the
subject/object inversion bug fixed in n-c9614ce7.

The information those refs carry is real (the model identified a
relationship between two entries on the same hub); only the *direction*
is wrong. This script walks every hub and rewrites the inversion
mechanically — no LLM call, no semantic guessing.

Symmetric swap rule:
    pre:  X.flag = "extends" | "contradicts" | "agrees"
          X.ref  = Y.date    where Y.date > X.date
    post: X.flag = "new"
          X.ref  = ""
          Y.flag = X.flag    (only if Y.flag was "new")
          Y.ref  = X.date

Fallbacks (clear X to "new" with empty ref):
    - same-day ref (ref == date): not a temporal edge
    - no entry-Y matches X.ref date: target missing
    - multiple entries-Y share that date: ambiguous, no semantic guess
    - Y is already classified (flag != "new"): don't overwrite real signal

Run AFTER the inversion-bug fix lands (commit 078f0f1 in main) — without
it, future runs would keep producing more inverted refs.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from personal_mem.config import load_config
from personal_mem.hubs import (
    LogEntry,
    parse_concept_hub,
    topics_dir,
    write_concept_hub,
)
from personal_mem.indexer import Indexer


def salvage_hub(
    entries: list[LogEntry],
) -> tuple[int, int, dict[str, int]]:
    """Apply the swap rule to one hub's log entries in place.

    Returns ``(swapped, cleared, reasons)`` where ``swapped`` is the
    number of entry pairs successfully swapped, ``cleared`` is the
    number of entries whose invalid ref was just dropped, and
    ``reasons`` breaks ``cleared`` down by fallback cause.
    """
    by_date: dict[str, list[LogEntry]] = defaultdict(list)
    for e in entries:
        by_date[e.date].append(e)

    swapped = 0
    cleared = 0
    reasons: dict[str, int] = defaultdict(int)

    for x in entries:
        if not x.ref:
            continue
        if x.ref < x.date:
            continue  # valid backward-pointing ref — leave alone

        if x.ref == x.date:
            x.flag = "new"
            x.ref = ""
            cleared += 1
            reasons["same_day"] += 1
            continue

        candidates = by_date.get(x.ref, [])
        if not candidates:
            x.flag = "new"
            x.ref = ""
            cleared += 1
            reasons["target_missing"] += 1
            continue
        if len(candidates) > 1:
            x.flag = "new"
            x.ref = ""
            cleared += 1
            reasons["target_ambiguous"] += 1
            continue

        y = candidates[0]
        if y.flag != "new":
            x.flag = "new"
            x.ref = ""
            cleared += 1
            reasons["target_already_classified"] += 1
            continue

        # Symmetric swap.
        y.flag = x.flag
        y.ref = x.date
        x.flag = "new"
        x.ref = ""
        swapped += 1

    return swapped, cleared, dict(reasons)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="commit changes (default: dry run)"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="print per-hub stats"
    )
    args = parser.parse_args()

    cfg = load_config()
    topics = topics_dir(cfg)

    total_swapped = 0
    total_cleared = 0
    grand_reasons: dict[str, int] = defaultdict(int)
    hubs_changed: list[Path] = []

    for hub_path in sorted(topics.glob("*.md")):
        hub = parse_concept_hub(hub_path)
        if not hub.log_entries:
            continue

        # Operate on a copy so a dry-run leaves the parsed entries intact
        # for the verbose printer.
        entries_copy = [
            LogEntry(date=e.date, flag=e.flag, ref=e.ref, text=e.text, citation=e.citation)
            for e in hub.log_entries
        ]
        swapped, cleared, reasons = salvage_hub(entries_copy)

        if swapped == 0 and cleared == 0:
            continue

        hubs_changed.append(hub_path)
        total_swapped += swapped
        total_cleared += cleared
        for k, v in reasons.items():
            grand_reasons[k] += v

        if args.verbose:
            print(
                f"  {hub_path.name}: swapped={swapped} cleared={cleared} "
                f"reasons={reasons}"
            )

        if args.apply:
            hub.log_entries = entries_copy
            write_concept_hub(hub)

    print(
        f"\n{'APPLIED' if args.apply else 'WOULD CHANGE'}: "
        f"{len(hubs_changed)} hub(s), {total_swapped} swapped, "
        f"{total_cleared} cleared."
    )
    if grand_reasons:
        print(f"Cleared reasons: {dict(grand_reasons)}")

    if args.apply and hubs_changed:
        print("\nReindexing changed hubs...")
        idx = Indexer(config=cfg)
        for p in hubs_changed:
            try:
                idx.index_file(p)
            except Exception as e:
                print(f"  warning: reindex failed for {p.name}: {e}")
        idx.close()
        print("Done.")
    elif not args.apply:
        print("\n[dry run; pass --apply to commit]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
