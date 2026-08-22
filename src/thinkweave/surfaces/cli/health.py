"""``weave health`` — render the deterministic health report; exit 1 on any flag.

Headless-safe: no prompts, no LLM. ``--json`` emits the contract documented
in :mod:`thinkweave.operations.health` (read by ``/brief``, #170).
"""

from __future__ import annotations

import argparse
import json
import sys

from thinkweave.core.config import load_config
from thinkweave.operations import health


def cmd_health(args: argparse.Namespace) -> None:
    report = health.collect(load_config())
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
    else:
        _print_table(report)
    sys.exit(0 if report["ok"] else 1)


def _print_table(r: dict) -> None:
    print(f"Health: {'OK' if r['ok'] else 'FLAGGED'}  ({r['checked_at']})\n")
    print(f"{'JOB':<24} {'CADENCE':<14} {'LAST RUN':<26} STATUS")
    for j in r["jobs"]:
        status = "missing" if j["missing"] else "stale" if j["stale"] else "ok"
        print(f"{j['name']:<24} {j['cadence']:<14} {(j['last_run'] or '-')[:25]:<26} {status}")
    if not r["jobs"]:
        print("(no crontab lines found)")
    print(f"\n{'QUEUE':<24} {'DEPTH':>6} {'BACKLOG':>8}")
    for q in r["queues"]:
        print(f"{q['source_type']:<24} {q['depth']:>6} {q['backlog']:>8}")
    h, d = r["hooks"], r["digest"]
    print(f"\nHooks: {h['recent_errors']} error(s) in last 24h"
          + (f" — {h['last_error']}" if h["last_error"] else ""))
    print(f"Digest: latest {d['latest'] or '-'} ({d['age_days']}d old)"
          f"{' — STALE' if d['stale'] else ''}")
    if r["flags"]:
        print("\nFlags:")
        for f in r["flags"]:
            print(f"  - {f}")
