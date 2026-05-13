"""Tests for ``predicted_outcome`` → ``prediction_match`` flow.

Covers:

- ``synthesis.judge._evaluate_prediction_match`` — pure mapping logic, no I/O
  (every keyword family × every evidence shape)
- ``operations.decisions.judge_and_writeback`` — frontmatter writeback only
  emits ``prediction_match`` when ``predicted_outcome`` is present
- ``operations.extract.extract_session`` — ``predicted_outcome`` flows from
  the decision input dict into the new decision's frontmatter

The rules are deliberately conservative (see judge.py): anything outside the
``_TEST_KEYWORDS`` / ``_COMMIT_KEYWORDS`` families stays ``unevaluable``. These
tests pin that contract — widening it should require a deliberate test edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteMeta, NoteType
from personal_mem.core.vault import VaultManager
from personal_mem.synthesis.judge import _evaluate_prediction_match


# ---------------------------------------------------------------------------
# Pure mapping — no vault, just the rule table
# ---------------------------------------------------------------------------


def _sess(test_runs: list[dict] | None = None) -> NoteMeta:
    return NoteMeta(
        id="ses-test01",
        type=NoteType.SESSION,
        title="t",
        path="x",
        frontmatter={"test_runs": test_runs or []},
    )


class TestEvaluatePredictionMatch:
    def test_no_keyword_family_is_unevaluable(self):
        # Prose with no test/commit keywords — falls through.
        assert _evaluate_prediction_match(
            "this should make the system faster",
            verdict="kept",
            committed=True,
            tested=True,
            session_meta=_sess([{"passed": 3, "failed": 0}]),
        ) == "unevaluable"

    def test_test_pred_with_passing_session_is_confirmed(self):
        assert _evaluate_prediction_match(
            "tests will pass",
            verdict="kept",
            committed=True,
            tested=True,
            session_meta=_sess([{"passed": 3, "failed": 0}]),
        ) == "confirmed"

    def test_test_pred_with_failing_session_is_contradicted(self):
        # Even though the prediction was "tests will pass" — the rule doesn't
        # try to parse polarity; any failed test contradicts any test pred.
        assert _evaluate_prediction_match(
            "tests will pass",
            verdict="kept",
            committed=True,
            tested=False,
            session_meta=_sess([{"passed": 1, "failed": 2}]),
        ) == "contradicted"

    def test_test_pred_without_test_run_is_unevaluable(self):
        assert _evaluate_prediction_match(
            "tests will pass",
            verdict="kept",
            committed=True,
            tested=False,
            session_meta=_sess([]),
        ) == "unevaluable"

    def test_commit_pred_with_commit_is_confirmed(self):
        assert _evaluate_prediction_match(
            "this will land in one commit",
            verdict="kept",
            committed=True,
            tested=False,
            session_meta=_sess(),
        ) == "confirmed"

    def test_commit_pred_with_revert_is_contradicted(self):
        assert _evaluate_prediction_match(
            "ship it today",
            verdict="reverted",
            committed=True,
            tested=False,
            session_meta=_sess(),
        ) == "contradicted"

    def test_commit_pred_without_commit_is_unevaluable(self):
        assert _evaluate_prediction_match(
            "this will land",
            verdict="unknown",
            committed=False,
            tested=False,
            session_meta=_sess(),
        ) == "unevaluable"

    def test_test_family_takes_precedence_over_commit_family(self):
        # Prediction mentions both — test rule wins (first in dispatch chain).
        assert _evaluate_prediction_match(
            "tests pass and we ship",
            verdict="kept",
            committed=True,
            tested=True,
            session_meta=_sess([{"passed": 5, "failed": 0}]),
        ) == "confirmed"


# ---------------------------------------------------------------------------
# Writeback integration — judge_and_writeback emits/omits the field
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


class TestJudgeWritebackPrediction:
    def test_predicted_outcome_present_writes_match(
        self, config: Config, vault: VaultManager
    ):
        # Commit family — `committed: True` roundtrips cleanly (bool, not nested
        # dict). The test-family path is covered exhaustively by the unit tests
        # above; here we exercise the writeback wiring, not the rule table.
        from personal_mem.operations.decisions import judge_and_writeback

        session_id = _seed(
            vault,
            predicted="this will land in one commit",
        )
        _index(config)
        judge_and_writeback(config, session_id=session_id)

        decs = _reload_decision(config, session_id)
        assert len(decs) == 1
        assert decs[0].frontmatter.get("prediction_match") == "confirmed"

    def test_no_predicted_outcome_omits_field(
        self, config: Config, vault: VaultManager
    ):
        from personal_mem.operations.decisions import judge_and_writeback

        session_id = _seed(vault, predicted=None)
        _index(config)
        judge_and_writeback(config, session_id=session_id)

        decs = _reload_decision(config, session_id)
        assert len(decs) == 1
        # Absent — we don't write "unevaluable" by default to keep frontmatter clean.
        assert "prediction_match" not in decs[0].frontmatter

    def test_predicted_outcome_without_keyword_is_unevaluable(
        self, config: Config, vault: VaultManager
    ):
        from personal_mem.operations.decisions import judge_and_writeback

        session_id = _seed(vault, predicted="this should reduce memory usage")
        _index(config)
        judge_and_writeback(config, session_id=session_id)

        decs = _reload_decision(config, session_id)
        assert decs[0].frontmatter.get("prediction_match") == "unevaluable"


# ---------------------------------------------------------------------------
# Extract integration — predicted_outcome flows through extract_session
# ---------------------------------------------------------------------------


class TestExtractPredictionPassthrough:
    def test_predicted_outcome_lands_in_frontmatter(
        self, config: Config, vault: VaultManager, monkeypatch
    ):
        # Seed a session note with the body extract_session expects
        # (just the header; we pass decisions explicitly).
        sess_path = vault.create_note(
            NoteType.SESSION,
            "S",
            body="## Summary\nDid work.\n",
            project="t",
            extra_frontmatter={"processed": False, "files_touched": ["x.py"]},
        )
        session_id = vault.read_note(sess_path).id

        # Index so extract can find the session.
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
                "predicted_outcome": "tests will pass on next CI run",
            }],
        )
        assert out.error == ""
        assert len(out.created_decisions) == 1
        fm = out.created_decisions[0].frontmatter
        assert fm.get("predicted_outcome") == "tests will pass on next CI run"
