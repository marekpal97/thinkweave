"""Seam-link queue — hubs whose folded logs await cross-parent linkage.

When a hub merge folds one catalyst log into another (concept merge via
``/dream`` apply or ``weave concepts merge``; theme merge via
``merge_theme_into``), the merged file holds two histories whose
``extends/agrees/contradicts`` edges were only ever computed *within*
each parent. The fold stamps ``fold_pending_from`` /
``fold_pending_dates`` frontmatter on the winner and enqueues it here;
the ``dream-seam-link-worker`` (phase 2 of ``/dream``) drains the queue,
judges only the cross-parent entry pairs (fold dates × the rest), writes
the revisions through ``weave hubs apply-linkage`` (which validates via
``validate_linkage_revision``), and clears the stamp.

Same shape and guarantees as ``operations/rejudge_queue.py`` — one
per-vault JSONL at ``vault/.weave/seam_link_queue.jsonl``, idempotent
enqueue, atomic rewrites (tempfile + rename). Item shape::

    {
      "hub_kind": "concept" | "theme",
      "hub_id": "derivatives" | "thm-...",
      "folded_from": "derivative" | "thm-...",
      "fold_dates": ["YYYY-MM-DD", ...],
      "reason": "concept_merged" | "theme_merged",
      "enqueued_at": "<iso-utc>"
    }
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from thinkweave.core.config import Config

_QUEUE_PATH = ".weave/seam_link_queue.jsonl"


def _queue_path(config: Config) -> Path:
    return Path(config.vault_root) / _QUEUE_PATH


def _read_all(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            # Corrupt rows skipped, not fatal — drain proceeds with what's valid.
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _write_all(path: Path, items: list[dict[str, Any]]) -> None:
    """Atomic rewrite via tempfile + rename in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not items:
        with path.open("w", encoding="utf-8"):
            return
    fd, tmp_name = tempfile.mkstemp(
        prefix=".seam_link_queue.", suffix=".jsonl", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for item in items:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise


def enqueue(
    config: Config,
    *,
    hub_kind: str,
    hub_id: str,
    folded_from: str = "",
    fold_dates: list[str] | None = None,
    reason: str = "concept_merged",
) -> None:
    """Append one item. Idempotent on ``(hub_kind, hub_id)`` — with a twist:

    re-enqueueing a hub that's already queued *unions the fold dates* into
    the existing item instead of no-opping. Two merges into the same
    winner before a drain runs must not lose the second merge's seam
    (mirrors how :func:`synthesis.hub.fold_hub_logs` unions the
    ``fold_pending_dates`` stamp on the file itself).
    """
    if not hub_id:
        return
    path = _queue_path(config)
    items = _read_all(path)
    for it in items:
        if it.get("hub_id") == hub_id and it.get("hub_kind") == hub_kind:
            dates = sorted(set(it.get("fold_dates") or []) | set(fold_dates or []))
            if dates != (it.get("fold_dates") or []):
                it["fold_dates"] = dates
                _write_all(path, items)
            return
    items.append(
        {
            "hub_kind": hub_kind,
            "hub_id": hub_id,
            "folded_from": folded_from,
            "fold_dates": sorted(set(fold_dates or [])),
            "reason": reason,
            "enqueued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    _write_all(path, items)


def drain(config: Config, *, cap: int = 0) -> list[dict[str, Any]]:
    """Return up to ``cap`` queued items and remove them from the queue.

    ``cap=0`` drains everything. FIFO — oldest enqueues first, so a hub
    that's been waiting longest gets its seam linked first.
    """
    path = _queue_path(config)
    items = _read_all(path)
    if not items:
        return []
    if cap and cap < len(items):
        taken, rest = items[:cap], items[cap:]
    else:
        taken, rest = items, []
    _write_all(path, rest)
    return taken


def peek(config: Config) -> list[dict[str, Any]]:
    """Return every queued item without clearing. Read-only."""
    return _read_all(_queue_path(config))


def dequeue(config: Config, *, hub_kind: str, hub_id: str) -> bool:
    """Remove one hub's item from the queue. Returns True if it was present.

    Called by ``weave hubs apply-linkage --clear-fold`` — clearing the
    ``fold_pending_*`` stamps and retiring the queue item are one atomic
    notion of "this seam is stitched"; splitting them across caller and
    orchestrator invites drift.
    """
    path = _queue_path(config)
    items = _read_all(path)
    rest = [
        it
        for it in items
        if not (it.get("hub_id") == hub_id and it.get("hub_kind") == hub_kind)
    ]
    if len(rest) == len(items):
        return False
    _write_all(path, rest)
    return True
