"""Tests for ``predicted_outcome`` → ``prediction_match`` lifecycle.

Phase 2 of the prediction-judge redesign moved the family/polarity
evaluator out of `synthesis/judge.py` and into the `/judge-prediction`
Claude Code skill. The Python side now only:

- Seeds a ``pending`` entry into ``prediction_history`` at decision
  creation time (``operations/extract.py``) when ``predicted_outcome``
  is set.
- Lets the structural judge writeback (``mem_judge_and_writeback``) run
  without touching ``prediction_match`` at all.

The skill (out of scope for these tests) is what eventually appends
``confirmed``/``contradicted``/``unevaluable``/``stale`` entries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def config(vault_dir: Path) -> Config:
    return Config(vault_root=vault_dir)


@pytest.fixture
def vault(config: Config) -> VaultManager:
    vm = VaultManager(config=config)
    vm.ensure_dirs()
    return vm


def _seed(vm: VaultManager, *, predicted: str | None = None,
          test_runs: list | None = None) -> str:
    sess_path = vm.create_note(
        NoteType.SESSION,
        "S",
        body="## Summary\n",
        project="t",
        extra_frontmatter={
            "processed": True,
            "test_runs": test_runs or [],
        },
    )
    session_id = vm.read_note(sess_path).id
    dec_fm = {
        "status": "accepted",
        "committed": True,
        "source_session": session_id,
        "derived_from": [session_id],
        "concepts": ["a", "b"],
    }
    if predicted is not None:
        dec_fm["predicted_outcome"] = predicted
    vm.create_note(
        NoteType.DECISION,
        "D",
        body="## Context\n\n## Decision\n",
        project="t",
        extra_frontmatter=dec_fm,
        output_dir=sess_path.parent,
    )
    return session_id


def _index(config: Config) -> None:
    idx = Indexer(config=config)
    idx.rebuild(full=True)
    idx.close()


def _reload_decision(config: Config, session_id: str):
    from personal_mem.synthesis.judge import find_decisions

    idx = Indexer(config=config)
    try:
        vm = VaultManager(config=config)
        return find_decisions(idx.db, vm, session_id=session_id)
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# Writeback no longer touches prediction_match
# ---------------------------------------------------------------------------


class TestJudgeWritebackPrediction:
    def test_writeback_does_not_emit_prediction_match(
        self, config: Config, vault: VaultManager
    ):
        """``mem_judge_and_writeback`` only handles structural verdict now.

        The decision carries a ``predicted_outcome``, but the structural
        judge must not write ``prediction_match`` — that's the
        /judge-prediction skill's job. The structural verdict (status,
        committed, judged_at) still gets written.
        """
        from personal_mem.operations.decisions import judge_and_writeback

        session_id = _seed(
            vault,
            predicted="next CI run on this branch shows all judge tests green",
        )
        _index(config)
        results = judge_and_writeback(config, session_id=session_id)
        assert len(results) == 1
        _, result = results[0]
        # Structural verdict still emitted.
        assert "verdict" in result
        assert "prediction_match" not in result

        decs = _reload_decision(config, session_id)
        assert len(decs) == 1
        fm = decs[0].frontmatter
        # The structural writeback may not touch prediction_match at all.
        # The seed entry from extract.py was a "pending" — and since this
        # decision was created directly via VaultManager (not extract),
        # it has no seed. So prediction_match must be absent.
        assert "prediction_match" not in fm
        # But the structural verdict made it through.
        assert fm.get("verdict") in {"kept", "superseded", "reverted", "unknown"}

    def test_writeback_runs_without_predicted_outcome(
        self, config: Config, vault: VaultManager
    ):
        from personal_mem.operations.decisions import judge_and_writeback

        session_id = _seed(vault, predicted=None)
        _index(config)
        judge_and_writeback(config, session_id=session_id)

        decs = _reload_decision(config, session_id)
        assert len(decs) == 1
        assert "prediction_match" not in decs[0].frontmatter


# ---------------------------------------------------------------------------
# Extract integration — predicted_outcome seeds a pending history entry
# ---------------------------------------------------------------------------


class TestExtractPredictionPassthrough:
    def test_predicted_outcome_seeds_pending_history(
        self, config: Config, vault: VaultManager
    ):
        """``mem_extract`` with ``predicted_outcome`` seeds ``pending``.

        The cron-driven /judge-prediction skill scans for `pending`
        entries; the decision file must land with that seed already
        present so there's something to find. ``predicted_outcome``
        itself roundtrips unchanged.
        """
        sess_path = vault.create_note(
            NoteType.SESSION,
            "S",
            body="## Summary\nDid work.\n",
            project="t",
            extra_frontmatter={"processed": False, "files_touched": ["x.py"]},
        )
        session_id = vault.read_note(sess_path).id

        _index(config)

        from personal_mem.operations.extract import extract_session

        predicted = "tests will pass on next CI run for this branch"
        out = extract_session(
            config,
            session_id=session_id,
            project="t",
            summary="ok",
            insights=[],
            decisions=[{
                "title": "Use SQLite",
                "rationale": "derived index",
                "outcome": "committed",
                "concepts": ["sqlite", "memory-system"],
                "predicted_outcome": predicted,
            }],
        )
        assert out.error == ""
        assert len(out.created_decisions) == 1
        fm = out.created_decisions[0].frontmatter

        # predicted_outcome string preserved as-is.
        assert fm.get("predicted_outcome") == predicted

        # The pending initializer fires and round-trips through the
        # frontmatter renderer/parser as a structured dict.
        assert fm.get("prediction_match") == "pending"
        history = fm.get("prediction_history")
        assert isinstance(history, list) and len(history) == 1
        entry = history[0]
        assert isinstance(entry, dict)
        assert entry["match"] == "pending"
        assert entry["reason"] == "awaiting evidence"
        assert entry["judged_at"]
        # Top-level judged_at denormalized for cheap reads.
        assert fm.get("judged_at")
        assert "T" in fm["judged_at"]  # ISO timestamp

    def test_no_predicted_outcome_no_pending_seed(
        self, config: Config, vault: VaultManager
    ):
        """Decisions without ``predicted_outcome`` get no history entry."""
        sess_path = vault.create_note(
            NoteType.SESSION,
            "S",
            body="## Summary\nDid work.\n",
            project="t",
            extra_frontmatter={"processed": False, "files_touched": ["x.py"]},
        )
        session_id = vault.read_note(sess_path).id

        _index(config)

        from personal_mem.operations.extract import extract_session

        out = extract_session(
            config,
            session_id=session_id,
            project="t",
            summary="ok",
            insights=[],
            decisions=[{
                "title": "Use SQLite",
                "rationale": "derived index",
                "outcome": "committed",
                "concepts": ["sqlite", "memory-system"],
            }],
        )
        assert out.error == ""
        fm = out.created_decisions[0].frontmatter
        assert "predicted_outcome" not in fm
        assert "prediction_match" not in fm
        assert "prediction_history" not in fm
