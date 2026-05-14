"""Tests for the mem_extract → wrap-finalize identifier handoff (issue: 2026-05-14).

When a caller passes a non-``ses-XXX`` value as ``session_id`` (e.g. a Claude
Code UUID), ``mem_extract`` auto-mints a session note whose own ``id:`` is a
fresh ``ses-XXX`` but whose ``source_session:`` frontmatter is the input value.
Decisions written for that session inherit ``source_session = <input>``.

``mem wrap-finalize <input>`` then matches decisions correctly; passing
``<minted ses-XXX>`` instead silently returns 0 (judge writeback no-ops).

These tests pin:

- ``ExtractOutcome.session_note_id`` exists and carries the canonical
  ``ses-XXX`` distinct from ``session_id`` when they diverge
- The MCP format report distinguishes them in the header
- The format report ends with a ``▶ To finalize: mem wrap-finalize ...``
  line that uses the **input** ``session_id``, not the minted ses-id
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager
from personal_mem.operations.extract import extract_session


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


def _index(config: Config) -> None:
    idx = Indexer(config=config)
    idx.rebuild(full=True)
    idx.close()


class TestSessionNoteIdSurfacing:
    def test_diverges_when_input_is_uuid(self, config: Config, vault: VaultManager):
        # UUID-shaped input → mem_extract auto-mints a ses-XXX note.
        # session_id (input) stays the UUID; session_note_id is the minted id.
        _index(config)
        cc_uuid = "043708d8-1eb8-4aa3-a9ff-7d8bdad37951"
        out = extract_session(
            config,
            session_id=cc_uuid,
            project="t",
            summary="ok",
            insights=[],
            decisions=[],
        )
        assert out.error == ""
        assert out.session_id == cc_uuid
        assert out.session_note_id.startswith("ses-")
        assert out.session_note_id != cc_uuid

    def test_matches_when_input_is_already_ses_id(
        self, config: Config, vault: VaultManager
    ):
        # Caller passes a ses-XXX that already exists → session_id and
        # session_note_id are the same value.
        sess_path = vault.create_note(
            NoteType.SESSION,
            "S",
            body="## Summary\n",
            project="t",
            extra_frontmatter={"processed": False},
        )
        sess_id = vault.read_note(sess_path).id
        _index(config)

        out = extract_session(
            config, session_id=sess_id, project="t",
            summary="ok", insights=[], decisions=[],
        )
        assert out.error == ""
        assert out.session_id == sess_id
        assert out.session_note_id == sess_id


class TestExtractFormatReport:
    def test_header_distinguishes_diverged_ids(
        self, config: Config, vault: VaultManager
    ):
        from personal_mem.surfaces.mcp.tools.extract import _format_extract_report

        _index(config)
        out = extract_session(
            config,
            session_id="abc-not-a-ses-id",
            project="t",
            summary="did things",
            insights=[],
            decisions=[],
        )
        report = _format_extract_report(out)
        # Header explicitly carries both — the input and the minted ses-id.
        assert "abc-not-a-ses-id" in report
        assert out.session_note_id in report
        assert f"(session note: {out.session_note_id})" in report

    def test_header_unchanged_when_ids_match(
        self, config: Config, vault: VaultManager
    ):
        from personal_mem.surfaces.mcp.tools.extract import _format_extract_report

        sess_path = vault.create_note(
            NoteType.SESSION, "S", body="## Summary\n", project="t",
            extra_frontmatter={"processed": False},
        )
        sess_id = vault.read_note(sess_path).id
        _index(config)

        out = extract_session(
            config, session_id=sess_id, project="t",
            summary="ok", insights=[], decisions=[],
        )
        report = _format_extract_report(out)
        # No "(session note: ...)" annotation when they're equal.
        assert "(session note:" not in report
        assert sess_id in report

    def test_finalize_hint_uses_input_session_id(
        self, config: Config, vault: VaultManager
    ):
        # The load-bearing assertion: the finalize hint MUST use the input
        # session_id (what decisions are stamped with), not the minted ses-id.
        from personal_mem.surfaces.mcp.tools.extract import _format_extract_report

        _index(config)
        cc_uuid = "043708d8-1eb8-4aa3-a9ff-7d8bdad37951"
        out = extract_session(
            config,
            session_id=cc_uuid,
            project="personal_mem",
            summary="x",
            insights=[],
            decisions=[{
                "title": "T",
                "rationale": "## Context\n\n## Decision\n",
                "outcome": "committed",
                "concepts": ["sqlite", "memory-system"],
            }],
        )
        report = _format_extract_report(out)
        # The exact wrap-finalize hint line.
        assert "▶ To finalize:" in report
        # Critically: uses the UUID (input), not the minted ses-id.
        assert f"mem wrap-finalize {cc_uuid}" in report
        assert f"mem wrap-finalize {out.session_note_id}" not in report
        # And includes the project so the agent can copy-paste verbatim.
        assert "--project personal_mem" in report

    def test_finalize_hint_without_project_when_unknown(
        self, config: Config, vault: VaultManager
    ):
        # Defensive: if nothing was created (rare), the hint still appears
        # but without a --project flag — caller can fill it in.
        from personal_mem.surfaces.mcp.tools.extract import _format_extract_report

        _index(config)
        out = extract_session(
            config, session_id="ses-99999999", project="t",
            summary="empty", insights=[], decisions=[],
        )
        report = _format_extract_report(out)
        assert "▶ To finalize:" in report
        # No created notes/decisions → no project surfaced from those — fine.
        assert "mem wrap-finalize ses-99999999" in report
