"""Slice 1.2 — recent_probe_pressure helper tests.

The helper turns probe-classified prompts into per-concept pressure
that every discover strategy (concept_coverage, decision_review,
theme_drift) reads as an additive bias.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager
from personal_mem.operations.prompts import recent_probe_pressure


def _write_events(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


def _seed_session(cfg: Config, vm: VaultManager, project: str, ts_iso: str) -> Path:
    sess_dir = cfg.vault_root / "projects" / project / "sessions" / "ses-1"
    return sess_dir / "events.jsonl"


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(vault_root=tmp_path / "vault", default_project="proj-a")


@pytest.fixture
def vault(cfg: Config) -> VaultManager:
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    return vm


def _recent_iso(days_ago: int = 0) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).isoformat()


class TestRecentProbePressure:
    def test_empty_vault_returns_empty(self, cfg: Config, vault: VaultManager):
        assert recent_probe_pressure(cfg) == {}

    def test_canonical_concept_match_scores_one(
        self, cfg: Config, vault: VaultManager
    ):
        """A single probe mentioning a canonical-ontology concept
        pressures that concept by 1. Uses ``llm`` which the shipped
        ontology guarantees as a canonical leaf."""
        events_file = _seed_session(cfg, vault, "proj-a", _recent_iso())
        _write_events(
            events_file,
            [
                {"type": "prompt", "text": "How does the llm reason?",
                 "session_id": "cc-1", "ts": _recent_iso(days_ago=1)},
            ],
        )

        pressure = recent_probe_pressure(cfg, project="proj-a", window_days=14)
        assert pressure.get("llm", 0) == 1

    def test_one_probe_pressures_multiple_matched_concepts(
        self, cfg: Config, vault: VaultManager
    ):
        """A probe touching two distinct concept slugs (substring match)
        contributes +1 to each — not double-counted per occurrence."""
        events_file = _seed_session(cfg, vault, "proj-a", _recent_iso())
        _write_events(
            events_file,
            [
                {"type": "prompt",
                 "text": "What is the relationship between llm training?",
                 "session_id": "cc-1", "ts": _recent_iso(days_ago=1)},
            ],
        )
        pressure = recent_probe_pressure(cfg, project="proj-a", window_days=14)
        # Both ``llm`` and ``training`` are canonical leaves in the
        # shipped ontology. Both should pressure by exactly 1 from a
        # single matching probe.
        assert pressure.get("llm", 0) == 1
        assert pressure.get("training", 0) == 1

    def test_non_probe_prompts_ignored(
        self, cfg: Config, vault: VaultManager
    ):
        """Instructions (no question mark, no hint phrase) must not
        contribute pressure."""
        events_file = _seed_session(cfg, vault, "proj-a", _recent_iso())
        _write_events(
            events_file,
            [
                {"type": "prompt", "text": "Refactor the llm pipeline",
                 "session_id": "cc-1", "ts": _recent_iso(days_ago=1)},
            ],
        )
        pressure = recent_probe_pressure(cfg, project="proj-a", window_days=14)
        assert "llm" not in pressure

    def test_window_excludes_old_probes(
        self, cfg: Config, vault: VaultManager
    ):
        events_file = _seed_session(cfg, vault, "proj-a", _recent_iso())
        _write_events(
            events_file,
            [
                {"type": "prompt", "text": "How does the llm work?",
                 "session_id": "cc-1", "ts": _recent_iso(days_ago=60)},
            ],
        )
        pressure = recent_probe_pressure(
            cfg, project="proj-a", window_days=14
        )
        assert pressure == {}

    def test_proposed_concepts_also_pressured(
        self, cfg: Config, vault: VaultManager
    ):
        """A term that's only in ``proposed_concepts:`` (not yet
        promoted into the canonical ontology) still gets pressure from
        matching probes — the index aggregates proposed terms across
        all notes and the helper includes them in its vocabulary."""
        # Seed a note that has ``frobnicate-widget`` in proposed_concepts.
        # (The strict ontology gate would route an unknown concept here
        # automatically; we set the frontmatter directly for the test.)
        vault.create_note(
            note_type=NoteType.NOTE,
            title="Stub",
            body="Body",
            project="proj-a",
            extra_frontmatter={"proposed_concepts": ["frobnicate-widget"]},
        )
        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()

        events_file = _seed_session(cfg, vault, "proj-a", _recent_iso())
        _write_events(
            events_file,
            [
                {"type": "prompt",
                 "text": "How does the frobnicate-widget interact?",
                 "session_id": "cc-1", "ts": _recent_iso(days_ago=1)},
            ],
        )
        pressure = recent_probe_pressure(
            cfg, project="proj-a", window_days=14
        )
        assert pressure.get("frobnicate-widget", 0) == 1

    def test_repeated_probes_compound_pressure(
        self, cfg: Config, vault: VaultManager
    ):
        events_file = _seed_session(cfg, vault, "proj-a", _recent_iso())
        _write_events(
            events_file,
            [
                {"type": "prompt", "text": "How does the llm work?",
                 "session_id": "cc-1", "ts": _recent_iso(days_ago=1)},
                {"type": "prompt", "text": "What does the llm output?",
                 "session_id": "cc-1", "ts": _recent_iso(days_ago=2)},
                {"type": "prompt", "text": "Where is llm config?",
                 "session_id": "cc-1", "ts": _recent_iso(days_ago=3)},
            ],
        )
        pressure = recent_probe_pressure(
            cfg, project="proj-a", window_days=14
        )
        assert pressure.get("llm", 0) == 3
