"""``mem news-stats`` — per-outlet drain stats from processed-archive JSONLs.

The triage + writer pipeline stamps every drained item with ``status`` and
``status_reason`` into ``vault/.mem/queues/_processed/<YYYY-MM-DD>/news.jsonl``.
This command aggregates those rows by outlet over a window so the operator
can prune the feed registry on evidence rather than guessing.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from personal_mem.core.config import load_config


STATUS_ACCEPTED = "done"
STATUS_DROPPED = "rejected"
STATUS_FAIL = {"failed", "worker_bug"}


def cmd_news_stats(args: argparse.Namespace) -> None:
    cfg = load_config()
    days = max(1, int(args.days))
    today = date.today()
    window_start = today - timedelta(days=days - 1)

    processed_root = cfg.vault_root / ".mem" / "queues" / "_processed"
    active_queue = cfg.vault_root / ".mem" / "queues" / "news.jsonl"

    rows = _collect_processed(processed_root, window_start, today)
    pending = _collect_pending(active_queue)

    if not rows and not pending:
        print(
            f"No news activity in the last {days} day(s) "
            f"({window_start.isoformat()} → {today.isoformat()})."
        )
        return

    stats: dict[str, dict] = defaultdict(
        lambda: {
            "enq": 0,
            "drop": 0,
            "accept": 0,
            "fail": 0,
            "pend": 0,
            "drop_reasons": Counter(),
            "fail_reasons": Counter(),
        }
    )

    for row in rows:
        outlet = row.get("outlet") or "(unknown)"
        s = stats[outlet]
        s["enq"] += 1
        status = (row.get("status") or "").strip()
        reason = (row.get("status_reason") or "").strip()
        if status == STATUS_DROPPED:
            s["drop"] += 1
            if reason:
                s["drop_reasons"][_normalize_reason(reason)] += 1
        elif status == STATUS_ACCEPTED:
            s["accept"] += 1
        elif status in STATUS_FAIL:
            s["fail"] += 1
            if reason:
                s["fail_reasons"][_normalize_reason(reason)] += 1

    for outlet, count in pending.items():
        stats[outlet]["enq"] += count
        stats[outlet]["pend"] = count

    sorted_rows = sorted(
        stats.items(), key=lambda kv: kv[1]["enq"], reverse=True
    )

    if args.json:
        out = []
        for outlet, s in sorted_rows:
            out.append(
                {
                    "outlet": outlet,
                    "enqueued": s["enq"],
                    "dropped": s["drop"],
                    "accepted": s["accept"],
                    "failed": s["fail"],
                    "pending": s["pend"],
                    "top_drop_reason": _top_reason(s["drop_reasons"]),
                    "top_fail_reason": _top_reason(s["fail_reasons"]),
                }
            )
        print(json.dumps(out, indent=2))
        return

    total_processed = sum(s["drop"] + s["accept"] + s["fail"] for _, s in sorted_rows)
    total_pending = sum(s["pend"] for _, s in sorted_rows)
    print(
        f"News stats — last {days} day(s) "
        f"({window_start.isoformat()} → {today.isoformat()})"
    )
    print(
        f"Processed: {total_processed} items across "
        f"{sum(1 for _, s in sorted_rows if s['enq'] > 0)} outlets. "
        f"Pending: {total_pending}.\n"
    )
    print(
        f"{'OUTLET':<22} {'ENQ':>5} {'DROP%':>6} {'ACPT%':>6} {'FAIL%':>6} {'PEND':>5}  TOP DROP REASON"
    )
    print("-" * 80)
    for outlet, s in sorted_rows:
        processed = s["drop"] + s["accept"] + s["fail"]
        drop_pct = _pct(s["drop"], processed)
        acpt_pct = _pct(s["accept"], processed)
        fail_pct = _pct(s["fail"], processed)
        top_reason = _top_reason(s["drop_reasons"]) or "—"
        if len(top_reason) > 30:
            top_reason = top_reason[:27] + "..."
        print(
            f"{outlet:<22} {s['enq']:>5} {drop_pct:>5}% {acpt_pct:>5}% "
            f"{fail_pct:>5}% {s['pend']:>5}  {top_reason}"
        )

    print()
    print("Hints (action thresholds when ENQ ≥ 10):")
    print("  DROP% > 60   → mostly noise; lower daily_cap or drop outlet")
    print("  FAIL% > 30   → mostly unreachable; check paywall / Cloudflare")
    print("  ACPT% < 20   → rarely yields a brief; reconsider keeping")


def _collect_processed(
    processed_root: Path, start: date, end: date
) -> list[dict]:
    if not processed_root.exists():
        return []
    rows: list[dict] = []
    d = start
    while d <= end:
        day_file = processed_root / d.isoformat() / "news.jsonl"
        if day_file.exists():
            for line in day_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        d += timedelta(days=1)
    return rows


def _collect_pending(queue_file: Path) -> dict[str, int]:
    if not queue_file.exists():
        return {}
    counts: Counter[str] = Counter()
    for line in queue_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        outlet = item.get("outlet") or "(unknown)"
        counts[outlet] += 1
    return dict(counts)


def _pct(n: int, total: int) -> int:
    return round(100 * n / total) if total else 0


def _top_reason(c: Counter) -> str:
    if not c:
        return ""
    return c.most_common(1)[0][0]


def _normalize_reason(reason: str) -> str:
    """Collapse triage reasons to a shared bucket for the histogram.

    Triage emits free-form ≤120-char reasons. We want the *category* not
    the per-item phrasing, so strip the ``triage drop: `` prefix and keep
    the short label that follows.
    """
    r = reason.strip()
    if r.lower().startswith("triage drop:"):
        r = r[len("triage drop:"):].strip()
    return r[:60]
