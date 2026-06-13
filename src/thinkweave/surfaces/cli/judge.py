"""``weave judge`` — drain the rejudge queue, list pending decisions, manual rejudge.

The Python side of the ``/judge-prediction`` skill. Three flavours:

- ``weave judge --drain --json`` — pull the supersession-triggered queue,
  merge in cron-style ``pending_due`` items, emit a worklist JSON array
  on stdout for the LLM half of ``/judge-prediction`` to consume.
- ``weave judge --rejudge <dec-id>`` — enqueue one decision manually, then
  shell to ``claude -p "/judge-prediction --decision <dec-id>"``.
- ``weave judge --list-pending`` — read-only enumeration of decisions whose
  ``prediction_match == 'pending'``. One id per line on stdout.

All worklist shapes mirror ``commands/judge-prediction.md``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

from thinkweave.core.config import load_config
from thinkweave.core.vault import VaultManager, parse_frontmatter


# Pattern in queue items' ``reason`` field — populated by the supersession
# enqueue path with "superseded by <successor_id>". Used to lift the
# successor into the worklist payload's ``successor_decision_id``.
_SUCC_RE = re.compile(r"superseded by (dec-[A-Za-z0-9_-]+)")


def cmd_judge(args: argparse.Namespace) -> None:
    """Dispatch the ``weave judge`` subcommand on which flag was set."""
    cfg = load_config()

    if args.list_pending:
        _cmd_list_pending(cfg, args)
        return
    if args.rejudge:
        _cmd_rejudge(cfg, args)
        return
    if args.drain:
        _cmd_drain(cfg, args)
        return

    print(
        "usage: weave judge {--drain | --rejudge DEC | --list-pending} [--max N] [--json]",
        file=sys.stderr,
    )
    sys.exit(2)


def _cmd_list_pending(cfg, args: argparse.Namespace) -> None:
    from thinkweave.operations import rejudge_queue

    ids = rejudge_queue.pending_due(cfg, age_days=0)
    if args.json:
        print(json.dumps(ids))
        return
    for note_id in ids:
        print(note_id)


def _cmd_rejudge(cfg, args: argparse.Namespace) -> None:
    """Enqueue ``--rejudge <dec-id>`` and shell to ``/judge-prediction``.

    The subprocess inherits stdin/stdout/stderr so the LLM output streams
    straight to the caller's terminal (or cron log). We exit with the
    subprocess's exit code so cron picks up real failures.
    """
    from thinkweave.core.indexer import Indexer
    from thinkweave.operations import rejudge_queue

    dec_id = args.rejudge
    idx = Indexer(config=cfg)
    try:
        row = idx.db.execute(
            "SELECT id FROM notes WHERE id = ? AND type = 'decision'", (dec_id,)
        ).fetchone()
    finally:
        idx.close()
    if not row:
        print(f"error: decision {dec_id} not found", file=sys.stderr)
        sys.exit(2)

    rejudge_queue.enqueue(
        cfg, decision_id=dec_id, reason="manual rejudge", source="manual"
    )

    # Shell to the LLM skill. ``claude -p`` is headless; inheriting the
    # parent's stdio means the verdict line streams live to the user.
    result = subprocess.run(
        ["claude", "-p", f"/judge-prediction --decision {dec_id}"],
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    sys.exit(result.returncode)


def _cmd_drain(cfg, args: argparse.Namespace) -> None:
    """Drain the supersession queue, merge ``pending_due``, emit JSON.

    Order: supersession-triggered items first (they're the cause of the
    drain), then cron stragglers from ``pending_due``. Deduped by
    ``decision_id`` (first-wins on metadata). Capped at ``--max``.
    """
    from thinkweave.operations import rejudge_queue

    drained = rejudge_queue.drain_all(cfg)
    pending = rejudge_queue.pending_due(cfg, age_days=1)

    # Merge: drained items keep their richer metadata (source, reason).
    # pending_due items are bare ids; synthesize a queue-shaped wrapper
    # so the worklist builder treats them uniformly.
    seen: set[str] = set()
    merged: list[dict] = []
    for item in drained:
        dec_id = item.get("decision_id", "")
        if not dec_id or dec_id in seen:
            continue
        seen.add(dec_id)
        merged.append(item)
    for dec_id in pending:
        if dec_id in seen:
            continue
        seen.add(dec_id)
        merged.append(
            {
                "decision_id": dec_id,
                "reason": "stale verdict",
                "source": "cron",
                "enqueued_at": "",
            }
        )

    cap = max(0, int(args.max))
    if cap:
        merged = merged[:cap]

    worklist = _build_worklist(cfg, merged)
    print(json.dumps(worklist))


def _build_worklist(cfg, items: list[dict]) -> list[dict]:
    """Resolve each queue item into the worklist shape ``/judge-prediction`` consumes.

    Per ``commands/judge-prediction.md``, each entry carries:

    - ``decision_id``, ``decision_path`` (absolute), ``predicted_outcome``
    - ``supersedes`` (list from fm), ``supersedes_history`` (the full
      ``prediction_history`` list)
    - ``successor_decision_id`` (extracted from queue item reason if
      ``source == 'supersession'``)
    - ``source_session``, ``trigger``, ``file_paths``

    Decisions whose markdown no longer resolves get dropped from the
    worklist silently — the queue item is already consumed (we drained
    above), so a missing file just means "skip, no work to do".
    """
    vm = VaultManager(config=cfg)
    from thinkweave.core.indexer import Indexer

    idx = Indexer(config=cfg)
    out: list[dict] = []
    try:
        for item in items:
            dec_id = item.get("decision_id", "")
            if not dec_id:
                continue
            row = idx.db.execute(
                "SELECT path FROM notes WHERE id = ? AND type = 'decision'",
                (dec_id,),
            ).fetchone()
            if not row:
                continue
            rel_path = row["path"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
            abs_path = vm.root / rel_path
            if not abs_path.exists():
                continue
            try:
                fm, _ = parse_frontmatter(abs_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            successor = ""
            source = item.get("source", "")
            reason = item.get("reason", "") or ""
            if source == "supersession":
                m = _SUCC_RE.search(reason)
                if m:
                    successor = m.group(1)
            trigger = source if source in {"supersession", "cron", "manual"} else "manual"
            supersedes = fm.get("supersedes") or []
            if isinstance(supersedes, str):
                supersedes = [supersedes]
            history = fm.get("prediction_history") or []
            if not isinstance(history, list):
                history = []
            file_paths = fm.get("file_paths") or []
            if isinstance(file_paths, str):
                file_paths = [file_paths]
            out.append(
                {
                    "decision_id": dec_id,
                    "decision_path": str(abs_path),
                    "predicted_outcome": fm.get("predicted_outcome", "") or "",
                    "supersedes": [str(s) for s in supersedes if s],
                    "supersedes_history": history,
                    "successor_decision_id": successor or None,
                    "source_session": fm.get("source_session", "") or "",
                    "trigger": trigger,
                    "file_paths": [str(f) for f in file_paths if f],
                }
            )
    finally:
        idx.close()
    return out
