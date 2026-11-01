"""``weave decide smoke`` — one end-to-end System 1 decision call."""

from __future__ import annotations

import argparse
import json

from thinkweave.core.config import load_config
from thinkweave.core.decision_provider import build_from_vault
from thinkweave.core.indexer import Indexer
from thinkweave.operations.decisions import run_site

_SMOKE_SLOTS = {
    "prompt": "How do I keep a sourdough starter alive while travelling?",
    "candidate": "Sourdough starter care: feed 1:1:1 flour and water daily; "
    "refrigerate to slow it down for up to two weeks.",
}


def cmd_decide(args: argparse.Namespace) -> None:
    """Run the ``smoke`` site against the real backend and show its verdicts row."""
    cfg = load_config()
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
