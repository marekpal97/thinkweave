"""Tests for the ``focus_research`` discover strategy.

Covers the three legs of a gap descriptor — substrate exemplars from
``context_served``, probe-tied exemplars from sibling ``events.jsonl``,
and source-coverage counts from ``type=source`` notes — plus the
fail-open paths (empty PRIORITIES, no index, no project).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager
from thinkweave.acquisition.discover.strategies import focus_research


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


def _write_priorities(vault_dir: Path, concepts: list[str]) -> None:
    cfg_dir = vault_dir / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    body = "focus:\n  research_concepts: [" + ", ".join(concepts) + "]\n"
    (cfg_dir / "PRIORITIES.yaml").write_text(body, encoding="utf-8")


def _seed_session_with_log(
    vault: VaultManager,
    project: str,
    log_lines: list[dict],
) -> str:
    """Create a session note with a sibling retrieval_log.jsonl and return its id."""
    sess_path = vault.create_note(
        NoteType.SESSION,
        "S",
        body="## Summary\nseed\n",
        project=project,
        extra_frontmatter={"processed": True, "source_session": "uuid-stub"},
    )
    if log_lines:
        (sess_path.parent / "retrieval_log.jsonl").write_text(
            "\n".join(json.dumps(ln) for ln in log_lines) + "\n",
            encoding="utf-8",
        )
    return vault.read_note(sess_path).id


def _seed_session_with_events(
    vault: VaultManager,
    project: str,
    events: list[dict],
    retrieval_log: list[dict] | None = None,
) -> str:
    """Create a session note with both events.jsonl + (optional) retrieval_log.jsonl."""
    uuid_stub = "uuid-" + str(len(events))
    sess_path = vault.create_note(
        NoteType.SESSION,
        "S",
        body="## Summary\nseed\n",
        project=project,
        extra_frontmatter={"processed": True, "source_session": uuid_stub},
    )
    if events:
        (sess_path.parent / "events.jsonl").write_text(
            "\n".join(json.dumps(ev) for ev in events) + "\n",
            encoding="utf-8",
        )
    if retrieval_log:
        (sess_path.parent / "retrieval_log.jsonl").write_text(
            "\n".join(json.dumps(ln) for ln in retrieval_log) + "\n",
            encoding="utf-8",
        )
    return vault.read_note(sess_path).id


# --- fail-open paths --------------------------------------------------------


class TestFailOpen:
    def test_no_priorities_returns_empty(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ):
        indexer.rebuild(full=True)
        assert focus_research.STRATEGY.run(config, "test", {}) == []

    def test_empty_research_concepts_returns_empty(
        self, vault: VaultManager, indexer: Indexer, config: Config, vault_dir: Path
    ):
        _write_priorities(vault_dir, [])
        indexer.rebuild(full=True)
        assert focus_research.STRATEGY.run(config, "test", {}) == []

    def test_missing_index_returns_empty(
        self, vault: VaultManager, config: Config, vault_dir: Path
    ):
        _write_priorities(vault_dir, ["agent-harness"])
        # No indexer.rebuild() — index_db doesn't exist
        assert focus_research.STRATEGY.run(config, "test", {}) == []


# --- substrate exemplars ----------------------------------------------------


class TestSubstrateExemplars:
    def test_top_n_by_served_count(
        self, vault: VaultManager, indexer: Indexer, config: Config, vault_dir: Path
    ):
        _write_priorities(vault_dir, ["agent-harness"])
        # Three notes tagged with agent-harness, served different counts
        n1 = vault.create_note(
            NoteType.NOTE, "n1", project="test",
            extra_frontmatter={"concepts": ["agent-harness"]},
        )
        n2 = vault.create_note(
            NoteType.NOTE, "n2", project="test",
            extra_frontmatter={"concepts": ["agent-harness"]},
        )
        n3 = vault.create_note(
            NoteType.NOTE, "n3", project="test",
            extra_frontmatter={"concepts": ["agent-harness"]},
        )
        id1 = vault.read_note(n1).id
        id2 = vault.read_note(n2).id
        id3 = vault.read_note(n3).id

        # n2 served 3x, n1 served 1x, n3 never served — n2 should rank first.
        _seed_session_with_log(vault, "test", [
            {"ts": datetime.now(timezone.utc).isoformat(), "type": "startup",
             "returned_ids": [id1, id2]},
        ])
        _seed_session_with_log(vault, "test", [
            {"ts": datetime.now(timezone.utc).isoformat(), "type": "retrieval",
             "tool": "weave_search", "returned_ids": [id2]},
        ])
        _seed_session_with_log(vault, "test", [
            {"ts": datetime.now(timezone.utc).isoformat(), "type": "retrieval",
             "tool": "weave_search", "returned_ids": [id2]},
        ])
        indexer.rebuild(full=True)

        result = focus_research.STRATEGY.run(config, "test", {})
        assert len(result) == 1
        desc = result[0]
        assert desc["concept"] == "agent-harness"
        # n2 served most, n1 served once, n3 never — n3 excluded.
        assert desc["exemplar_served"][0] == id2
        assert id1 in desc["exemplar_served"]
        assert id3 not in desc["exemplar_served"]

    def test_prompttime_boost_breaks_ties(
        self, vault: VaultManager, indexer: Indexer, config: Config, vault_dir: Path
    ):
        _write_priorities(vault_dir, ["x"])
        nA = vault.read_note(vault.create_note(
            NoteType.NOTE, "A", project="test",
            extra_frontmatter={"concepts": ["x"]},
        )).id
        nB = vault.read_note(vault.create_note(
            NoteType.NOTE, "B", project="test",
            extra_frontmatter={"concepts": ["x"]},
        )).id

        # Same total served count (1 each), but A came through prompttime.
        _seed_session_with_log(vault, "test", [
            {"ts": datetime.now(timezone.utc).isoformat(), "type": "retrieval",
             "tool": "prompt_time_retrieval", "returned_ids": [nA]},
        ])
        _seed_session_with_log(vault, "test", [
            {"ts": datetime.now(timezone.utc).isoformat(), "type": "retrieval",
             "tool": "weave_search", "returned_ids": [nB]},
        ])
        indexer.rebuild(full=True)

        result = focus_research.STRATEGY.run(config, "test", {})
        assert result[0]["exemplar_served"][0] == nA


# --- source coverage --------------------------------------------------------


class TestSourceCoverage:
    def test_counts_by_source_type(
        self, vault: VaultManager, indexer: Indexer, config: Config, vault_dir: Path
    ):
        _write_priorities(vault_dir, ["orchestration"])
        vault.create_note(
            NoteType.SOURCE, "paper-1", project="test",
            extra_frontmatter={"concepts": ["orchestration"], "source_type": "paper"},
        )
        vault.create_note(
            NoteType.SOURCE, "paper-2", project="test",
            extra_frontmatter={"concepts": ["orchestration"], "source_type": "paper"},
        )
        vault.create_note(
            NoteType.SOURCE, "repo-1", project="test",
            extra_frontmatter={"concepts": ["orchestration"], "source_type": "repo"},
        )
        indexer.rebuild(full=True)

        result = focus_research.STRATEGY.run(config, "test", {})
        coverage = result[0]["source_coverage"]
        assert coverage.get("paper") == 2
        assert coverage.get("repo") == 1


# --- probe-tied exemplars ---------------------------------------------------


class TestNoProbeLeg:
    """focus_research is the declared-floor rail — it carries NO probe
    input. The probe→research path is owned by the /dream probe-distillation
    worker; the probe-tightening leg (exemplar_probed / probe_texts) was
    removed 2026-06-17 so the behavioural and declared rails don't
    duplicate. Substrate exemplars + source coverage remain."""

    def test_descriptor_carries_no_probe_fields(
        self, vault: VaultManager, indexer: Indexer, config: Config, vault_dir: Path
    ):
        _write_priorities(vault_dir, ["dense-retrieval"])
        tagged = vault.read_note(vault.create_note(
            NoteType.NOTE, "tagged", project="test",
            extra_frontmatter={"concepts": ["dense-retrieval"]},
        )).id

        ts = datetime.now(timezone.utc).isoformat()
        # A session with a probe mentioning the concept AND a retrieval
        # that served the tagged note — under the old leg this produced
        # exemplar_probed + probe_texts; now it must produce neither.
        _seed_session_with_events(
            vault,
            "test",
            events=[
                {"type": "prompt", "ts": ts, "session_id": "uuid-1",
                 "text": "What is dense-retrieval and how does it differ?"},
            ],
            retrieval_log=[
                {"ts": ts, "type": "retrieval", "tool": "weave_search",
                 "returned_ids": [tagged]},
            ],
        )
        indexer.rebuild(full=True)

        result = focus_research.STRATEGY.run(config, "test", {})
        d0 = result[0]
        # Substrate exemplar still surfaces (served once)...
        assert tagged in d0["exemplar_served"]
        # ...but the probe leg is gone — no probe fields on the descriptor.
        assert "exemplar_probed" not in d0
        assert "probe_texts" not in d0


# --- registry wiring --------------------------------------------------------


def test_strategy_is_registered():
    from thinkweave.acquisition.discover import get, names

    assert "focus_research" in names()
    s = get("focus_research")
    assert s.name == "focus_research"
