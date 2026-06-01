"""Tests for the strict ontology gate on ``operations.notes.update_note``.

The concept-write gate that ``create_note`` enforces (canonical terms in
``concepts:``, everything else routed to ``proposed_concepts:`` for later
promotion via ``/mem-resolve-concepts``) must also fire on the headless
``mem_update`` path. Without it, a caller passing ``frontmatter_updates=
{"concepts": [...]}`` can land arbitrary strings as canonical concepts —
the bypass A3 of the pre-shipping audit flagged.

Only the ``concepts`` field is gated. Other frontmatter keys (``tags``,
``status``, ``commit_refs``, …) must pass through untouched.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager
from personal_mem.operations import notes as ops_notes


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def cfg(vault_dir: Path) -> Config:
    return Config(vault_root=vault_dir)


@pytest.fixture
def vault(cfg: Config) -> VaultManager:
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    return vm


def _write_ontology(monkeypatch, content: str) -> Path:
    """Point every ontology loader at a temp YAML file (matches the test
    pattern in ``test_concepts_doctor.py``)."""
    path = Path(tempfile.mkdtemp()) / "ontology.yaml"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(
        "personal_mem.synthesis.concepts._seed_ontology_path", lambda: path
    )
    monkeypatch.setattr(
        "personal_mem.synthesis.concepts._vault_ontology_path", lambda: path
    )
    monkeypatch.setattr(
        "personal_mem.synthesis.concepts._ontology_path", lambda: path
    )
    return path


def _seed_note(vault: VaultManager, cfg: Config) -> str:
    """Create a plain note and index it. Returns the note id."""
    path = vault.create_note(
        NoteType.NOTE,
        "T",
        body="# T\n\nbody",
        project="t",
    )
    idx = Indexer(config=cfg)
    idx.index_file(path)
    idx.close()
    return vault.read_note(path).id


class TestUpdateNoteConceptGate:
    def test_canonical_concepts_land_in_concepts(
        self, cfg: Config, vault: VaultManager, monkeypatch
    ) -> None:
        _write_ontology(monkeypatch, "swe-python:\n  - python\n  - asyncio\n")
        note_id = _seed_note(vault, cfg)

        updated = ops_notes.update_note(
            cfg,
            note_id,
            frontmatter_updates={"concepts": ["python", "asyncio"]},
        )

        assert updated.frontmatter.get("concepts") == ["python", "asyncio"]
        assert not updated.frontmatter.get("proposed_concepts")

    def test_non_canonical_routed_to_proposed(
        self, cfg: Config, vault: VaultManager, monkeypatch
    ) -> None:
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        note_id = _seed_note(vault, cfg)

        updated = ops_notes.update_note(
            cfg,
            note_id,
            frontmatter_updates={
                "concepts": ["python", "made-up-thing", "another-novel-term"],
            },
        )

        assert updated.frontmatter.get("concepts") == ["python"]
        assert updated.frontmatter.get("proposed_concepts") == [
            "made-up-thing",
            "another-novel-term",
        ]

    def test_all_non_canonical_drops_concepts_field(
        self, cfg: Config, vault: VaultManager, monkeypatch
    ) -> None:
        """When zero canonical terms survive, `concepts:` is removed
        from the update payload rather than written as an empty list —
        otherwise vm.update_note's list-merge would persist `[]` and the
        note would carry a confusing empty canonical field."""
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        note_id = _seed_note(vault, cfg)

        updated = ops_notes.update_note(
            cfg,
            note_id,
            frontmatter_updates={"concepts": ["totally-novel"]},
        )

        assert not updated.frontmatter.get("concepts")
        assert updated.frontmatter.get("proposed_concepts") == ["totally-novel"]

    def test_existing_proposed_preserved_across_update(
        self, cfg: Config, vault: VaultManager, monkeypatch
    ) -> None:
        """Pre-existing `proposed_concepts:` on the note must survive an
        update that only touches `concepts:` — they get merged with any
        new non-canonical entries, deduped, not dropped."""
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        path = vault.create_note(
            NoteType.NOTE,
            "T",
            body="# T\n\nbody",
            project="t",
            extra_frontmatter={"proposed_concepts": ["already-here"]},
        )
        idx = Indexer(config=cfg)
        idx.index_file(path)
        idx.close()
        note_id = vault.read_note(path).id

        updated = ops_notes.update_note(
            cfg,
            note_id,
            frontmatter_updates={"concepts": ["python", "newly-proposed"]},
        )

        assert updated.frontmatter.get("concepts") == ["python"]
        proposed = updated.frontmatter.get("proposed_concepts")
        assert "already-here" in proposed
        assert "newly-proposed" in proposed

    def test_other_frontmatter_fields_pass_through_unchanged(
        self, cfg: Config, vault: VaultManager, monkeypatch
    ) -> None:
        """Only `concepts:` is gated. `tags`, `status`, `commit_refs`,
        etc. land verbatim."""
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        note_id = _seed_note(vault, cfg)

        updated = ops_notes.update_note(
            cfg,
            note_id,
            frontmatter_updates={
                "status": "accepted",
                "tags": ["urgent"],
                "commit_refs": ["abc1234"],
            },
        )

        assert updated.frontmatter.get("status") == "accepted"
        assert "urgent" in (updated.frontmatter.get("tags") or [])
        assert updated.frontmatter.get("commit_refs") == ["abc1234"]
        # The gate didn't touch concepts because we didn't pass it.
        assert not updated.frontmatter.get("concepts")
        assert not updated.frontmatter.get("proposed_concepts")

    def test_no_concepts_key_no_gate_invocation(
        self, cfg: Config, vault: VaultManager, monkeypatch
    ) -> None:
        """An update that doesn't touch `concepts:` must not synthesize
        a `proposed_concepts:` field. Regression guard against the gate
        firing on every update."""
        _write_ontology(monkeypatch, "swe-python:\n  - python\n")
        path = vault.create_note(
            NoteType.NOTE,
            "T",
            body="# T\n\nbody",
            project="t",
            extra_frontmatter={
                "concepts": ["python"],
                "proposed_concepts": ["already-here"],
            },
        )
        idx = Indexer(config=cfg)
        idx.index_file(path)
        idx.close()
        note_id = vault.read_note(path).id

        updated = ops_notes.update_note(
            cfg,
            note_id,
            frontmatter_updates={"status": "accepted"},
        )

        # Both lists are still there, untouched.
        assert updated.frontmatter.get("concepts") == ["python"]
        assert updated.frontmatter.get("proposed_concepts") == ["already-here"]
        assert updated.frontmatter.get("status") == "accepted"
