"""Tests for ``scripts/migrate_prediction_history.py``.

Covers verdict mapping, dict-shape flattening, idempotency, dry-run safety,
and the no-predicted-outcome skip path. Runs the script via its ``main()``
function with a monkeypatched ``sys.argv``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the `scripts/` directory importable as a flat module.
_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import migrate_prediction_history as mig  # noqa: E402

from personal_mem.core.config import Config  # noqa: E402
from personal_mem.core.indexer import Indexer  # noqa: E402
from personal_mem.core.schemas import NoteType  # noqa: E402
from personal_mem.core.vault import VaultManager, parse_frontmatter  # noqa: E402


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


@pytest.fixture
def indexer(config: Config):
    idx = Indexer(config=config)
    yield idx
    idx.close()


def _make_decision(
    vault: VaultManager,
    title: str,
    *,
    extra: dict,
) -> Path:
    """Create a decision note with the given extra frontmatter, return its path."""
    return vault.create_note(
        note_type=NoteType.DECISION,
        title=title,
        body=f"# {title}\n\nbody",
        project="test",
        extra_frontmatter=extra,
    )


def _read_fm(path: Path) -> dict:
    fm, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
    return fm


def _run(config: Config, indexer: Indexer, *, apply: bool, monkeypatch) -> None:
    """Run the script's main() with the given mode against the test vault."""
    indexer.rebuild()

    # Force load_config / Indexer inside the script to see our test vault.
    monkeypatch.setattr(mig, "load_config", lambda: config)

    argv = ["migrate_prediction_history.py"]
    if apply:
        argv.append("--apply")
    monkeypatch.setattr(sys, "argv", argv)
    rc = mig.main()
    assert rc == 0


class TestVerdictMapping:
    def test_bare_string_confirmed_carried_over(
        self, vault: VaultManager, config: Config, indexer: Indexer, monkeypatch
    ) -> None:
        path = _make_decision(
            vault,
            "C1",
            extra={
                "predicted_outcome": "tests will pass after refactor",
                "prediction_match": "confirmed",
            },
        )
        _run(config, indexer, apply=True, monkeypatch=monkeypatch)

        fm = _read_fm(path)
        assert isinstance(fm["prediction_history"], list)
        assert len(fm["prediction_history"]) == 1
        entry = fm["prediction_history"][0]
        assert entry["match"] == "confirmed"
        assert entry["reason"] == mig.DEFAULT_REASON
        assert fm["prediction_match"] == "confirmed"
        assert fm["predicted_outcome"] == "tests will pass after refactor"

    def test_unevaluable_maps_to_stale_with_legacy_reason(
        self, vault: VaultManager, config: Config, indexer: Indexer, monkeypatch
    ) -> None:
        path = _make_decision(
            vault,
            "U1",
            extra={
                "predicted_outcome": "tests pass: see commit",
                "prediction_match": "unevaluable",
            },
        )
        _run(config, indexer, apply=True, monkeypatch=monkeypatch)

        fm = _read_fm(path)
        entry = fm["prediction_history"][0]
        assert entry["match"] == "stale"
        assert entry["reason"] == mig.STALE_REASON
        assert fm["prediction_match"] == "stale"


class TestDictFlattening:
    """The vault's `parse_frontmatter` is YAML-lite and can't decode a
    top-level nested dict — by the time the script sees the fm, a dict
    shape only arises in-memory (e.g. ``mem_create`` callers that pass
    structured outcomes). Test the helper directly, plus a defensive
    `str(raw)` coercion for unknown shapes.
    """

    def test_helper_flattens_dict_to_text(self) -> None:
        prose, was_dict = mig._flatten_predicted_outcome(
            {"family": "test", "text": "tests will pass", "polarity": "positive"},
            "dec-x",
        )
        assert prose == "tests will pass"
        assert was_dict is True

    def test_helper_flattens_dict_with_empty_text(self) -> None:
        prose, was_dict = mig._flatten_predicted_outcome(
            {"family": "test", "text": "", "polarity": None},
            "dec-x",
        )
        assert prose == ""
        assert was_dict is True

    def test_helper_passes_string_through(self) -> None:
        prose, was_dict = mig._flatten_predicted_outcome(
            "tests will pass", "dec-x"
        )
        assert prose == "tests will pass"
        assert was_dict is False

    def test_helper_skips_when_missing(self) -> None:
        assert mig._flatten_predicted_outcome(None, "dec-x") == (None, False)
        assert mig._flatten_predicted_outcome("", "dec-x") == (None, False)


class TestSkipPaths:
    def test_no_predicted_outcome_skipped(
        self, vault: VaultManager, config: Config, indexer: Indexer, monkeypatch
    ) -> None:
        path = _make_decision(vault, "Plain", extra={"status": "proposed"})
        _run(config, indexer, apply=True, monkeypatch=monkeypatch)

        fm = _read_fm(path)
        # No prediction_history was injected.
        assert "prediction_history" not in fm or not fm.get("prediction_history")
        assert "predicted_outcome" not in fm

    def test_dry_run_writes_nothing(
        self, vault: VaultManager, config: Config, indexer: Indexer, monkeypatch, capsys
    ) -> None:
        path = _make_decision(
            vault,
            "DR1",
            extra={
                "predicted_outcome": "tests will pass",
                "prediction_match": "partial",
            },
        )
        before = path.read_text(encoding="utf-8")
        _run(config, indexer, apply=False, monkeypatch=monkeypatch)
        after = path.read_text(encoding="utf-8")
        assert before == after

        out = capsys.readouterr().out
        # Dry-run reports `partial → contradicted` transition without writing.
        assert "partial" in out and "contradicted" in out
        assert "DRY-RUN" in out


class TestIdempotency:
    def test_running_twice_matches_running_once(
        self, vault: VaultManager, config: Config, indexer: Indexer, monkeypatch
    ) -> None:
        path = _make_decision(
            vault,
            "I1",
            extra={
                "predicted_outcome": "tests will pass",
                "prediction_match": "confirmed",
            },
        )

        _run(config, indexer, apply=True, monkeypatch=monkeypatch)
        after_first = path.read_text(encoding="utf-8")
        fm_first = _read_fm(path)

        _run(config, indexer, apply=True, monkeypatch=monkeypatch)
        after_second = path.read_text(encoding="utf-8")
        fm_second = _read_fm(path)

        # Bytes-identical: idempotent rewrite.
        assert after_first == after_second
        # History still has exactly one entry (no double-append).
        assert len(fm_first["prediction_history"]) == 1
        assert len(fm_second["prediction_history"]) == 1
        assert fm_first["prediction_history"] == fm_second["prediction_history"]
