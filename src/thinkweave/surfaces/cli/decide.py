"""``weave decide`` — System 1 decision sites: a smoke call, the ex-post sweep,
and the verdicts readout."""

from __future__ import annotations

import argparse
import json
import sys

from thinkweave.core.config import load_config
from thinkweave.core.decision_provider import build_from_vault
from thinkweave.core.indexer import Indexer
from thinkweave.operations.decisions import EX_POST, readout, run_site, sweep

_SMOKE_SLOTS = {
    "prompt": "How do I keep a sourdough starter alive while travelling?",
    "candidate": "Sourdough starter care: feed 1:1:1 flour and water daily; "
    "refrigerate to slow it down for up to two weeks.",
}


def cmd_decide(args: argparse.Namespace) -> None:
    """Dispatch ``weave decide <action>``."""
    cfg = load_config()
    if args.decide_action == "sweep":
        _sweep(cfg, args)
    elif args.decide_action == "readout":
        _readout(cfg, args)
    else:
        _smoke(cfg, args)


def _sweep(cfg, args: argparse.Namespace) -> None:
    """Judge every session the ex-post judge has not; exit 1 when the site is off."""
    if not cfg.decision_site(EX_POST).enabled:
        print(
            "weave decide sweep: the ex_post decision site is off; turn it on "
            "with [decisions.ex_post] enabled = true in the rail's thinkweave/config.toml",
            file=sys.stderr,
        )
        sys.exit(1)
    result = sweep(cfg, project=args.project, since=args.since)
    if args.json:
        print(json.dumps(result.__dict__, indent=2))
        return
    print(f"ex-post sweep: {result.sessions} session(s), {result.calls} call(s), "
          f"{result.rows} verdict row(s), {len(result.misses)} miss(es)")
    for miss in result.misses:
        print(f"  miss: {miss}")


def _readout(cfg, args: argparse.Namespace) -> None:
    """Print the verdicts per site and source."""
    threshold = cfg.decision_site(EX_POST).accept if args.threshold is None else args.threshold
    sites = readout(cfg, project=args.project, since=args.since, threshold=threshold)
    if args.json:
        print(json.dumps({"threshold": threshold, "sites": [s.as_dict() for s in sites]},
                         indent=2))
        return
    print(f"threshold: {threshold}")
    for s in sites:
        print(f"{s.site}/{s.source}: {s.rows} rows, {s.calls} calls, {s.misses} misses "
              f"(rate {s.miss_rate}), latency p50 {s.latency_ms_p50} ms p95 "
              f"{s.latency_ms_p95} ms, cost {s.cost}")
        for action, counts in s.usage.items():
            print(f"  {action}: used {counts['used']}, unused {counts['unused']}")
        for row in s.calibration:
            if row["n"]:
                print(f"  ex-ante {row['ex_ante']}: n {row['n']}, "
                      f"mean ex-post {row['mean_ex_post']}")


def _smoke(cfg, args: argparse.Namespace) -> None:
    """Run the ``smoke`` site against the real backend and show its verdicts row."""
    provider = build_from_vault(cfg.vault_root, args.backend, args.model)
    decision = run_site(cfg, "smoke", _SMOKE_SLOTS, provider=provider, subject_id="smoke")
    print(f"vault:   {cfg.vault_root}")
    print(f"backend: {decision.backend}  model: {decision.model}")
    print(f"cost:    {decision.cost}  latency_ms: {decision.latency_ms}")
    for answer in decision.answers:
        print(f"answer:  {answer.question} = {answer.value!r}  probs={json.dumps(answer.probs)}")

    idx = Indexer(config=cfg)
    try:
        idx.project_vault_verdicts()
        row = idx.db.execute(
            "SELECT * FROM verdicts WHERE site = 'smoke' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    finally:
        idx.close()
    print("verdicts row:", json.dumps(dict(row)))
