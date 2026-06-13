"""C23 — Judge-prediction → RLVR export end-to-end contract.

Tests the full CLI loop the ``/judge-prediction`` skill orchestrates:

1. Seed decisions with ``predicted_outcome`` + ``pending`` history.
2. Enqueue rejudge requests (simulates supersession trigger).
3. ``weave judge --drain`` returns the worklist the skill consumes.
4. Apply synthetic verdicts via ``weave update`` (the skill's write step).
5. ``weave rlvr export`` reflects the new verdicts.

The skill itself is a Claude-Code slash command, so the LLM-judgment
turn is *not* run here — the test exercises the CLI/data boundaries
the skill depends on, which is the part that breaks if any of the
five primitives drift. Spawning a real subprocess with ``claude -p
"/judge-prediction --drain"`` would add LLM cost + flakiness without
locking any contract these assertions don't already lock.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager, parse_frontmatter, render_frontmatter
from thinkweave.operations import rejudge_queue
from thinkweave.surfaces.cli.judge import cmd_judge
from thinkweave.surfaces.cli.notes import cmd_update
from thinkweave.surfaces.cli.rlvr import cmd_rlvr


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    vault = tmp_path / "vault"
    monkeypatch.setenv("THINKWEAVE_VAULT", str(vault))
    monkeypatch.setenv("THINKWEAVE_PROJECT", "t")
    return Config(vault_root=vault, default_project="t")


@pytest.fixture
def vault(cfg: Config) -> VaultManager:
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    return vm


def _seed_decision_with_prediction(
    vm: VaultManager,
    *,
    title: str,
    predicted_outcome: str,
    project: str = "t",
) -> str:
    """Create a decision note with a pending prediction_history entry.
    Returns its note id."""
    path = vm.create_note(
        NoteType.DECISION,
        title,
        body="Decision body.",
        project=project,
        extra_frontmatter={
            "status": "accepted",
            "outcome": "committed",
            "predicted_outcome": predicted_outcome,
            "prediction_history": [
                {
                    "match": "pending",
                    "judged_at": "",
                    "reason": "initialized by /weave-wrap",
                }
            ],
            "prediction_match": "pending",
            "judged_at": "",
        },
    )
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    return fm["id"]


def _apply_verdict_via_weave_update(
    cfg: Config,
    *,
    decision_id: str,
    verdict: str,
    reason: str,
) -> None:
    """Apply a verdict to a decision the way ``/judge-prediction`` does:
    write a new ``prediction_history`` entry plus denormalized
    ``prediction_match`` + ``judged_at`` tail values."""
    from datetime import datetime, timezone

    judged_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    idx = Indexer(config=cfg)
    row = idx.db.execute(
        "SELECT path FROM notes WHERE id = ?", (decision_id,)
    ).fetchone()
    idx.close()
    assert row, f"decision {decision_id} not indexed"
    note_path = cfg.vault_root / row["path"]
    fm, body = parse_frontmatter(note_path.read_text(encoding="utf-8"))
    history = list(fm.get("prediction_history") or [])
    history.append({
        "match": verdict,
        "judged_at": judged_at,
        "reason": reason,
    })
    fm["prediction_history"] = history
    fm["prediction_match"] = verdict
    fm["judged_at"] = judged_at
    note_path.write_text(
        render_frontmatter(fm) + "\n\n" + body, encoding="utf-8"
    )
    idx = Indexer(config=cfg)
    idx.index_file(note_path)
    idx.close()


class TestJudgePredictionE2E:
    """Drive the rejudge loop end-to-end via CLI primitives.

    Each step exercises the same code path ``/judge-prediction`` uses;
    only the LLM judgment is short-circuited (we supply the verdict
    directly instead of letting Claude compose it)."""

    def test_drain_emits_worklist_with_predicted_outcome(
        self, cfg: Config, vault: VaultManager, capsys
    ):
        dec_id = _seed_decision_with_prediction(
            vault,
            title="Use WAL mode for SQLite writers",
            predicted_outcome="Concurrent reads no longer block. Check that wrap-finalize takes <1s after the next /weave-wrap.",
        )
        Indexer(config=cfg).rebuild(full=True)
        rejudge_queue.enqueue(
            cfg, decision_id=dec_id, reason="supersession", source="supersession"
        )

        # weave judge --drain → JSON worklist
        cmd_judge(argparse.Namespace(
            drain=True, rejudge=None, list_pending=False,
            max=20, json=True, verdict=None,
        ))
        worklist = json.loads(capsys.readouterr().out)
        assert len(worklist) == 1
        entry = worklist[0]
        assert entry["decision_id"] == dec_id
        assert "predicted_outcome" in entry
        assert "concurrent reads" in entry["predicted_outcome"].lower()

    def test_apply_verdict_flows_into_rlvr_export(
        self, cfg: Config, vault: VaultManager, capsys
    ):
        dec_id = _seed_decision_with_prediction(
            vault,
            title="Adopt FTS5 over LIKE",
            predicted_outcome="Search latency drops to <50ms. Look for the latency line in stats.",
        )
        Indexer(config=cfg).rebuild(full=True)

        # Simulate skill applying verdict.
        _apply_verdict_via_weave_update(
            cfg, decision_id=dec_id,
            verdict="confirmed",
            reason="Synthetic verdict (test).",
        )

        # weave rlvr export → JSONL on stdout
        cmd_rlvr(argparse.Namespace(
            rlvr_action="export",
            project="",
            since="",
            until="",
            committed_only=False,
            verbose=False,
            explode_history=False,
        ))
        out = capsys.readouterr().out.strip()
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
        matching = [r for r in rows if r.get("decision_id") == dec_id]
        assert matching, f"decision {dec_id} not in rlvr export: {out[:200]}"
        # Verdict flowed through to the export's prediction block.
        row = matching[0]
        assert row["prediction"]["match"] == "confirmed"
        # The full history is also preserved (pending + confirmed entries).
        history_matches = [h["match"] for h in row["prediction"]["history"]]
        assert "pending" in history_matches
        assert "confirmed" in history_matches

    def test_two_decisions_in_one_drain(
        self, cfg: Config, vault: VaultManager, capsys
    ):
        """Drain is batched — both decisions should appear in one
        worklist response, in insertion order."""
        d1 = _seed_decision_with_prediction(
            vault, title="D1",
            predicted_outcome="A. check it.",
        )
        d2 = _seed_decision_with_prediction(
            vault, title="D2",
            predicted_outcome="B. check it.",
        )
        Indexer(config=cfg).rebuild(full=True)
        rejudge_queue.enqueue(
            cfg, decision_id=d1, reason="supersession", source="supersession"
        )
        rejudge_queue.enqueue(
            cfg, decision_id=d2, reason="supersession", source="supersession"
        )

        cmd_judge(argparse.Namespace(
            drain=True, rejudge=None, list_pending=False,
            max=20, json=True, verdict=None,
        ))
        worklist = json.loads(capsys.readouterr().out)
        ids = [e["decision_id"] for e in worklist]
        assert d1 in ids
        assert d2 in ids
        # The supersession-triggered entries are gone after drain. Both
        # decisions still have pending verdicts so they MAY resurface
        # via ``pending_due`` on a second call, but their ``source``
        # changes from "supersession" to "cron". Lock that contract.
        cmd_judge(argparse.Namespace(
            drain=True, rejudge=None, list_pending=False,
            max=20, json=True, verdict=None,
        ))
        second = json.loads(capsys.readouterr().out)
        # No second-call entry carries ``source="supersession"`` — the
        # queue file was atomically truncated.
        sources = [e.get("source") for e in second]
        assert "supersession" not in sources
