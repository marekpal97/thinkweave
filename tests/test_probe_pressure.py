"""Slice 1.2 — recent_probe_pressure helper tests.

The helper turns probe-classified prompts into per-concept pressure
that gap-emitter discover strategies (decision_review, prompt_gap)
read as an additive bias.
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
    return Config(vault_root=tmp_path / "vault", default_project="proj_a")


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
        events_file = _seed_session(cfg, vault, "proj_a", _recent_iso())
        _write_events(
            events_file,
            [
                {"type": "prompt", "text": "How does the llm reason?",
                 "session_id": "cc-1", "ts": _recent_iso(days_ago=1)},
            ],
        )

        pressure = recent_probe_pressure(cfg, project="proj_a", window_days=14)
        assert pressure.get("llm", 0) == 1

    def test_one_probe_pressures_multiple_matched_concepts(
        self, cfg: Config, vault: VaultManager
    ):
        """A probe touching two distinct concept slugs (substring match)
        contributes +1 to each — not double-counted per occurrence."""
        events_file = _seed_session(cfg, vault, "proj_a", _recent_iso())
        _write_events(
            events_file,
            [
                {"type": "prompt",
                 "text": "What is the relationship between llm training?",
                 "session_id": "cc-1", "ts": _recent_iso(days_ago=1)},
            ],
        )
        pressure = recent_probe_pressure(cfg, project="proj_a", window_days=14)
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
        events_file = _seed_session(cfg, vault, "proj_a", _recent_iso())
        _write_events(
            events_file,
            [
                {"type": "prompt", "text": "Refactor the llm pipeline",
                 "session_id": "cc-1", "ts": _recent_iso(days_ago=1)},
            ],
        )
        pressure = recent_probe_pressure(cfg, project="proj_a", window_days=14)
        assert "llm" not in pressure

    def test_window_excludes_old_probes(
        self, cfg: Config, vault: VaultManager
    ):
        events_file = _seed_session(cfg, vault, "proj_a", _recent_iso())
        _write_events(
            events_file,
            [
                {"type": "prompt", "text": "How does the llm work?",
                 "session_id": "cc-1", "ts": _recent_iso(days_ago=60)},
            ],
        )
        pressure = recent_probe_pressure(
            cfg, project="proj_a", window_days=14
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
            project="proj_a",
            extra_frontmatter={"proposed_concepts": ["frobnicate-widget"]},
        )
        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()

        events_file = _seed_session(cfg, vault, "proj_a", _recent_iso())
        _write_events(
            events_file,
            [
                {"type": "prompt",
                 "text": "How does the frobnicate-widget interact?",
                 "session_id": "cc-1", "ts": _recent_iso(days_ago=1)},
            ],
        )
        pressure = recent_probe_pressure(
            cfg, project="proj_a", window_days=14
        )
        assert pressure.get("frobnicate-widget", 0) == 1

    def test_repeated_probes_compound_pressure(
        self, cfg: Config, vault: VaultManager
    ):
        events_file = _seed_session(cfg, vault, "proj_a", _recent_iso())
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
            cfg, project="proj_a", window_days=14
        )
        assert pressure.get("llm", 0) == 3


class TestRecentProbePressureVaultWide:
    """K2 Item 4 — three-bug cascade fixes:

    (A) Empty ``cfg.default_project`` no longer short-circuits to ``{}``.
    (B) Vault-wide aggregation across every ``vault/projects/<p>/``.
    (C) Single/2-char concept slugs no longer match (drowns the garbage
        pool from the str-iter bug class).

    These tests cover the three failures together because they masked
    each other — fixing only (A) or (B) without (C) yields noisy
    vault-wide pressure dominated by single-letter pseudo-concepts.
    """

    @pytest.fixture
    def cfg_no_default(self, tmp_path: Path) -> Config:
        """Config with ``default_project=''`` — the vault-wide call path."""
        return Config(vault_root=tmp_path / "vault", default_project="")

    @pytest.fixture
    def vault_no_default(self, cfg_no_default: Config) -> VaultManager:
        vm = VaultManager(config=cfg_no_default)
        vm.ensure_dirs()
        return vm

    def test_empty_default_project_returns_vault_wide_pressure(
        self, cfg_no_default: Config, vault_no_default: VaultManager
    ):
        """(A) When neither ``project`` nor ``cfg.default_project`` is
        set, the helper must fall through to vault-wide aggregation
        rather than early-returning ``{}``."""
        # Seed a probe under a project; vault-wide call should still
        # surface it.
        sess_dir = (
            cfg_no_default.vault_root / "projects" / "proj_x"
            / "sessions" / "ses-1"
        )
        _write_events(
            sess_dir / "events.jsonl",
            [
                {"type": "prompt", "text": "How does the llm work?",
                 "session_id": "cc-1", "ts": _recent_iso(days_ago=1)},
            ],
        )

        # No ``project`` arg, no ``cfg.default_project`` — exercises
        # the vault-wide fallback. Pre-fix this returned ``{}``.
        pressure = recent_probe_pressure(cfg_no_default, window_days=14)
        assert pressure.get("llm", 0) == 1

    def test_vault_wide_aggregates_across_projects(
        self, cfg_no_default: Config, vault_no_default: VaultManager
    ):
        """(B) Explicit ``project=''`` must union probes from every
        project under ``vault/projects/`` — every other dream scan
        surface is vault-global, this should match."""
        # Two projects, each with one llm probe → pressure should be 2.
        for proj in ("proj_a", "proj_b"):
            sess_dir = (
                cfg_no_default.vault_root / "projects" / proj
                / "sessions" / "ses-1"
            )
            _write_events(
                sess_dir / "events.jsonl",
                [
                    {"type": "prompt", "text": "How does the llm work?",
                     "session_id": f"cc-{proj}",
                     "ts": _recent_iso(days_ago=1)},
                ],
            )

        pressure = recent_probe_pressure(
            cfg_no_default, project="", window_days=14
        )
        assert pressure.get("llm", 0) == 2

    def test_short_concept_slug_does_not_match(
        self, cfg_no_default: Config, vault_no_default: VaultManager
    ):
        """(C) A 1- or 2-char ``proposed_concepts`` entry (the
        single-letter garbage left by the 2026-06-07 str-iter bug)
        must not pressure every probe that happens to contain that
        letter. Without the len-guard, a stray ``-`` or single ``a``
        would match nearly every English probe."""
        # Seed a note carrying a single-char proposed concept — exactly
        # the shape ``_coerce_list_field`` was added to prevent at write
        # time, but we still defend at read time for historical pollution.
        vault_no_default.create_note(
            note_type=NoteType.NOTE,
            title="Polluted",
            body="Body",
            project="proj_a",
            extra_frontmatter={
                "proposed_concepts": ["a", "z", "-"],
            },
        )
        idx = Indexer(config=cfg_no_default)
        idx.rebuild(full=True)
        idx.close()

        sess_dir = (
            cfg_no_default.vault_root / "projects" / "proj_a"
            / "sessions" / "ses-1"
        )
        _write_events(
            sess_dir / "events.jsonl",
            [
                # English probe — contains ``a``, ``z`` (in "tokenize"),
                # ``-`` (in "well-known"), and is a probe (ends ``?``,
                # no follow-up edit).
                {"type": "prompt",
                 "text": "How does the well-known tokenizer work?",
                 "session_id": "cc-1", "ts": _recent_iso(days_ago=1)},
            ],
        )

        pressure = recent_probe_pressure(
            cfg_no_default, project="proj_a", window_days=14
        )
        # None of the 1-char pseudo-concepts should appear, even though
        # the letters are present in the probe text.
        for short in ("a", "z", "-"):
            assert short not in pressure, (
                f"short concept {short!r} matched but should be filtered"
            )

    def test_three_char_concept_still_matches(
        self, cfg_no_default: Config, vault_no_default: VaultManager
    ):
        """(C cont.) The len-guard is ``>= 3`` so real 3-char concepts
        (``llm``, ``mcp``, ``fts``) keep matching. ``llm`` is canonical
        in the shipped ontology — guard rails on the boundary."""
        sess_dir = (
            cfg_no_default.vault_root / "projects" / "proj_a"
            / "sessions" / "ses-1"
        )
        _write_events(
            sess_dir / "events.jsonl",
            [
                {"type": "prompt", "text": "How does the llm reason?",
                 "session_id": "cc-1", "ts": _recent_iso(days_ago=1)},
            ],
        )
        pressure = recent_probe_pressure(
            cfg_no_default, project="proj_a", window_days=14
        )
        assert pressure.get("llm", 0) == 1
