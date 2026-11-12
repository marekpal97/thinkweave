"""``weave decide`` — System 1 question sets: a smoke call, the ex-post sweep,
the verdicts readout, the citation backfill and the news triage."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from thinkweave.core.config import load_config
from thinkweave.core.decision_provider import build_from_vault
from thinkweave.core.indexer import Indexer
from thinkweave.operations.verdicts import (
    CITATION,
    EX_POST,
    backfill_citations,
    readout,
    run_question_set,
    sweep,
)

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
    elif args.decide_action == "backfill-citations":
        _backfill_citations(cfg, args)
    elif args.decide_action == "triage":
        _triage(cfg, args)
    else:
        _smoke(cfg, args)


def _sweep(cfg, args: argparse.Namespace) -> None:
    """Judge every session the ex-post judge has not; exit 1 when the question set is off."""
    if not cfg.question_set(EX_POST).enabled:
        print(
            "weave decide sweep: the ex_post question set is off; turn it on "
            "with [verdicts.ex_post] enabled = true in the rail's thinkweave/config.toml",
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
    for malformed in result.malformed:
        print(f"  warning: {malformed}")


def _readout(cfg, args: argparse.Namespace) -> None:
    """Print the verdicts per question set and source."""
    threshold = cfg.question_set(EX_POST).accept if args.threshold is None else args.threshold
    question_sets = readout(cfg, project=args.project, since=args.since, threshold=threshold)
    if args.json:
        print(json.dumps({"threshold": threshold, "question_sets": [s.as_dict() for s in question_sets]},
                         indent=2))
        return
    print(f"threshold: {threshold}")
    for s in question_sets:
        print(f"{s.question_set}/{s.source}: {s.rows} rows, {s.calls} calls, {s.misses} misses "
              f"(rate {s.miss_rate}), latency p50 {s.latency_ms_p50} ms p95 "
              f"{s.latency_ms_p95} ms, cost {s.cost}")
        for action, counts in s.usage.items():
            print(f"  {action}: used {counts.used}, unused {counts.unused}")
        for b in s.calibration:
            if b.n:
                print(f"  ex-ante {b.ex_ante}: n {b.n}, mean ex-post {b.mean_ex_post}")


def _backfill_citations(cfg, args: argparse.Namespace) -> None:
    """Link each uncited decision to the served notes it relied on, or with
    ``--dry-run`` list them; exit 1 when the question set is off or a call failed."""
    if not cfg.question_set(CITATION).enabled:
        print(
            "weave decide backfill-citations: the citation question set is off; turn it "
            "on with [verdicts.citation] enabled = true in the rail's thinkweave/config.toml",
            file=sys.stderr,
        )
        sys.exit(1)
    result = backfill_citations(cfg, dry_run=args.dry_run)
    if args.json:
        print(json.dumps({"dry_run": args.dry_run, **result.as_dict()}, indent=2))
    else:
        for proposal in result.proposals:
            print(f"{proposal.decision_id} ({proposal.title}): "
                  f"{', '.join(proposal.cited) or 'no served note relied on'}")
        cited = sum(1 for proposal in result.proposals if proposal.cited)
        print(f"citation backfill{' (dry run)' if args.dry_run else ''}: "
              f"{len(result.proposals)} decision(s) judged, {cited} with a citation, "
              f"{result.written} written, {len(result.misses)} miss(es)")
        for miss in result.misses:
            print(f"  miss: {miss}")
    if result.misses:
        sys.exit(1)


def _triage(cfg, args: argparse.Namespace) -> None:
    """Print one stage-1 triage outcome per item at the head of a queue, as JSON."""
    from thinkweave.operations.news_triage import triage
    from thinkweave.operations.queue import peek

    outcomes = triage(cfg, peek(cfg, args.source_type, args.limit))
    print(json.dumps([asdict(outcome) for outcome in outcomes], indent=2))


def _smoke(cfg, args: argparse.Namespace) -> None:
    """Run the ``smoke`` question set against the real backend and show its verdicts row."""
    provider = build_from_vault(cfg.vault_root, args.backend, args.model)
    decision = run_question_set(cfg, "smoke", _SMOKE_SLOTS, provider=provider, subject_id="smoke")
    print(f"vault:   {cfg.vault_root}")
    print(f"backend: {decision.backend}  model: {decision.model}")
    print(f"cost:    {decision.cost}  latency_ms: {decision.latency_ms}")
    for answer in decision.answers:
        print(f"answer:  {answer.question} = {answer.value!r}  probs={json.dumps(answer.probs)}")

    idx = Indexer(config=cfg)
    try:
        idx.project_vault_verdicts()
        row = idx.db.execute(
            "SELECT * FROM verdicts WHERE question_set = 'smoke' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    finally:
        idx.close()
    print("verdicts row:", json.dumps(dict(row)))
