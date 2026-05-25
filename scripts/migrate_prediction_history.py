"""One-off: migrate legacy prediction frontmatter to `prediction_history:` shape.

Pre-Phase-6 decision notes carried one of three legacy shapes:

  1. Bare-string ``predicted_outcome:`` + flat ``prediction_match:``.
  2. Structured dict ``predicted_outcome: {family, text, polarity}``.
  3. ``prediction_match: partial`` — verdict dropped in the redesign.
  4. ``prediction_match: unevaluable`` — test/commit-family false negatives
     in the user's vault (per call #6: rejudge via skill).

The new shape (Phase 1+):

  predicted_outcome: "<prose claim + manifestation pointer>"
  prediction_history:
    - {match, judged_at, reason}
  prediction_match: "<tail's match>"
  judged_at: "<tail's judged_at>"

Verdict enum: confirmed | contradicted | pending | unevaluable | stale.

Verdict mapping (legacy → new):
  - confirmed       → confirmed
  - contradicted    → contradicted
  - pending         → pending
  - partial         → contradicted    (closest preserved-semantics)
  - unevaluable     → stale           (per user call #6)
  - missing/empty   → pending

Idempotent: skipped if ``prediction_history`` is already a non-empty
list of dicts. Dry-run by default; ``--apply`` writes.

Also enqueues every migrated decision for re-judging via
``operations/rejudge_queue.enqueue`` (if importable). In dry-run, the
intent is logged but no queue file is written.

Style note: mirrors ``scripts/promote_and_backfill.py`` (CLI shape, summary
report on stdout, SQLite-indexer discovery instead of fs walk).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from personal_mem.core.config import Config, load_config
from personal_mem.core.indexer import Indexer
from personal_mem.core.vault import VaultManager, parse_frontmatter


# Map legacy prediction_match values → new history-entry match.
LEGACY_VERDICT_MAP = {
    "confirmed": "confirmed",
    "contradicted": "contradicted",
    "pending": "pending",
    "partial": "contradicted",
    "unevaluable": "stale",
}

STALE_REASON = "legacy test/commit-family false negative; rejudging via skill"
DEFAULT_REASON = "migrated from MVP family-evaluator"


def _decision_paths(cfg: Config) -> list[str]:
    """Look up vault-relative paths of every ``type: decision`` note."""
    if not cfg.index_db.exists():
        print(
            f"ERROR: index DB not found at {cfg.index_db} — run `mem index` first.",
            file=sys.stderr,
        )
        return []
    conn = sqlite3.connect(cfg.index_db)
    try:
        rows = conn.execute(
            "SELECT path FROM notes WHERE type = 'decision' ORDER BY path"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def _flatten_predicted_outcome(
    raw: object,
    decision_id: str,
) -> tuple[str | None, bool]:
    """Return (prose_string, was_dict_flattened).

    - String → unchanged.
    - Dict with ``text`` → flattened, was_dict_flattened=True.
    - Missing → (None, False) — caller skips entirely.
    """
    if raw is None or raw == "":
        return None, False
    if isinstance(raw, str):
        return raw, False
    if isinstance(raw, dict):
        text = raw.get("text", "")
        if not isinstance(text, str):
            text = str(text or "")
        print(
            f"  [flatten] {decision_id}: dict-shape predicted_outcome → "
            f"text={text!r}",
            file=sys.stderr,
        )
        return text, True
    # Unknown shape — coerce defensively.
    print(
        f"  [warn] {decision_id}: predicted_outcome has unexpected type "
        f"{type(raw).__name__}; coercing to str",
        file=sys.stderr,
    )
    return str(raw), False


def _migrate_one(
    vault: VaultManager,
    rel_path: str,
    *,
    dry_run: bool,
) -> dict:
    """Migrate a single decision note. Returns a status dict.

    Status keys: kind ∈ {skipped_no_predicted, skipped_already_migrated,
    migrated}; legacy_match; new_match; was_dict; decision_id; rel_path.
    """
    abs_path = vault.root / rel_path
    text = abs_path.read_text(encoding="utf-8")
    fm, _body = parse_frontmatter(text)

    decision_id = fm.get("id", "") or rel_path

    # Idempotency: already-migrated notes carry a non-empty list-of-dicts.
    existing_history = fm.get("prediction_history")
    if (
        isinstance(existing_history, list)
        and existing_history
        and all(isinstance(e, dict) for e in existing_history)
    ):
        return {
            "kind": "skipped_already_migrated",
            "decision_id": decision_id,
            "rel_path": rel_path,
        }

    raw_predicted = fm.get("predicted_outcome")
    prose, was_dict = _flatten_predicted_outcome(raw_predicted, decision_id)
    if prose is None:
        return {
            "kind": "skipped_no_predicted",
            "decision_id": decision_id,
            "rel_path": rel_path,
        }

    legacy_match = (fm.get("prediction_match") or "").strip()
    if legacy_match:
        new_match = LEGACY_VERDICT_MAP.get(legacy_match, "unevaluable")
    else:
        new_match = "pending"

    judged_at = fm.get("judged_at") or fm.get("date") or ""
    if not isinstance(judged_at, str):
        judged_at = str(judged_at)

    reason = STALE_REASON if new_match == "stale" else DEFAULT_REASON

    history_entry = {
        "match": new_match,
        "judged_at": judged_at,
        "reason": reason,
    }

    delta = {
        "predicted_outcome": prose,
        "prediction_history": [history_entry],
        "prediction_match": new_match,
        "judged_at": judged_at,
    }

    if not dry_run:
        vault.update_note(abs_path, frontmatter_updates=delta)

    return {
        "kind": "migrated",
        "decision_id": decision_id,
        "rel_path": rel_path,
        "legacy_match": legacy_match or "(missing)",
        "new_match": new_match,
        "was_dict": was_dict,
    }


def _try_enqueue(
    cfg: Config,
    decision_ids: list[str],
    *,
    dry_run: bool,
) -> str:
    """Enqueue migrated decisions for re-judging. Returns a status string.

    Imports lazily — `operations/rejudge_queue.py` is shipped by a
    parallel subagent and may not exist yet.
    """
    if not decision_ids:
        return "no-op (0 decisions to enqueue)"
    if dry_run:
        return f"dry-run: would enqueue {len(decision_ids)} decisions"
    try:
        from personal_mem.operations import rejudge_queue  # type: ignore
    except ImportError:
        print(
            "  [warn] rejudge_queue module not available; "
            "skipping enqueue step. Fallback: `mem judge --rejudge <id>`.",
            file=sys.stderr,
        )
        return "skipped (rejudge_queue not importable)"
    enqueued = 0
    for dec_id in decision_ids:
        try:
            rejudge_queue.enqueue(
                cfg,
                decision_id=dec_id,
                reason="migration",
                source="migration",
            )
            enqueued += 1
        except Exception as exc:  # noqa: BLE001
            print(
                f"  [warn] enqueue failed for {dec_id}: {exc}",
                file=sys.stderr,
            )
    return f"enqueued {enqueued}/{len(decision_ids)}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Report what would change, write nothing (default).",
    )
    grp.add_argument(
        "--apply",
        action="store_true",
        help="Write migrated frontmatter to disk and enqueue for re-judging.",
    )
    ap.add_argument(
        "--project",
        default="",
        help="Reserved for parity with other scripts; not used "
        "(every decision is migrated regardless of project).",
    )
    args = ap.parse_args()

    dry_run = not args.apply

    cfg = load_config()
    vault = VaultManager(config=cfg)

    print(f"=== migrate_prediction_history (mode={'dry-run' if dry_run else 'apply'}) ===")
    print(f"vault: {cfg.vault_root}")
    print(f"index_db: {cfg.index_db}")
    print()

    paths = _decision_paths(cfg)
    print(f"Scanning {len(paths)} decision notes from index...")

    statuses: list[dict] = []
    for rel_path in paths:
        abs_path = vault.root / rel_path
        if not abs_path.exists():
            print(f"  [missing] {rel_path} (in index but file gone)", file=sys.stderr)
            continue
        try:
            status = _migrate_one(vault, rel_path, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            print(f"  [error] {rel_path}: {exc}", file=sys.stderr)
            continue
        statuses.append(status)

    total = len(statuses)
    with_predicted = sum(
        1 for s in statuses if s["kind"] != "skipped_no_predicted"
    )
    already = sum(1 for s in statuses if s["kind"] == "skipped_already_migrated")
    migrated = [s for s in statuses if s["kind"] == "migrated"]
    n_migrated = len(migrated)
    n_flattened = sum(1 for s in migrated if s["was_dict"])

    # Verdict-transition breakdown.
    transitions = Counter(
        (s["legacy_match"], s["new_match"]) for s in migrated
    )

    # Re-index touched paths + enqueue.
    enqueue_status = _try_enqueue(
        cfg,
        [s["decision_id"] for s in migrated],
        dry_run=dry_run,
    )

    reindex_status: str
    if not dry_run and migrated:
        idx = Indexer(config=cfg)
        try:
            modified = [vault.root / s["rel_path"] for s in migrated]
            stats = idx.index_paths(modified)
            reindex_status = (
                f"indexed={stats.get('indexed', 0)}, "
                f"skipped={stats.get('skipped', 0)}, "
                f"removed={stats.get('removed', 0)}"
            )
        finally:
            idx.close()
    elif dry_run:
        reindex_status = "dry-run: skipped"
    else:
        reindex_status = "no-op (nothing migrated)"

    # --- Summary report ---
    print()
    print("=== Summary ===")
    print(f"  Decisions scanned:                {total}")
    print(f"  With predicted_outcome:           {with_predicted}")
    print(f"  Skipped (already migrated):       {already}")
    print(f"  Newly migrated:                   {n_migrated}")
    print(f"  Dict-shape flattenings:           {n_flattened}")
    print()
    if transitions:
        print("  Verdict transitions (legacy → new):")
        for (legacy, new), count in sorted(transitions.items()):
            print(f"    {legacy:>14}  →  {new:<14}  {count}")
    else:
        print("  Verdict transitions: (none)")
    print()
    print(f"  Re-index:    {reindex_status}")
    print(f"  Re-judge:    {enqueue_status}")
    print()
    if dry_run:
        print("Mode: DRY-RUN. Re-run with --apply to write.")
    else:
        print("Mode: APPLY complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
