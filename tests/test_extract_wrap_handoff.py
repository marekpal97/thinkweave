"""Tests for the weave_extract → wrap-finalize identifier handoff (issue: 2026-05-14).

When a caller passes a non-``ses-XXX`` value as ``session_id`` (e.g. a Claude
Code UUID), ``weave_extract`` auto-mints a session note whose own ``id:`` is a
fresh ``ses-XXX`` but whose ``source_session:`` frontmatter is the input value.
Decisions written for that session inherit ``source_session = <input>``.

Since #181 the finalize hint prints the **minted ses-id**: a forced
re-extract leaves two folders claiming the same source UUID, and only the
session-note id unambiguously names the one just written. ``finalize_wrap``
maps a ses- id back to ``source_session`` for the judge step, so the old
2026-05-14 trap (ses-id → 0 decisions judged) no longer applies.

These tests pin:

- ``ExtractOutcome.session_note_id`` exists and carries the canonical
  ``ses-XXX`` distinct from ``session_id`` when they diverge
- The MCP format report distinguishes them in the header
- The format report ends with a ``▶ To finalize: weave wrap-finalize ...``
  line that uses the **minted ses-id**, and following it verbatim after a
  forced re-extract matches prompts (#181 AC2)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager
from thinkweave.operations.extract import extract_session


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
        # UUID-shaped input → weave_extract auto-mints a ses-XXX note.
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
    def test_archive_failure_is_reported_and_buffer_is_preserved(
        self, config: Config, monkeypatch
    ):
        from thinkweave.surfaces.mcp.tools.extract import _format_extract_report

        _index(config)
        session_id = "ses-archive-denied"
        buf = config.weave_dir / "buffer" / f"{session_id}.jsonl"
        buf.parent.mkdir(parents=True, exist_ok=True)
        buf.write_text('{"type":"prompt","text":"keep"}\n', encoding="utf-8")
        monkeypatch.setattr(
            "thinkweave.operations.extract.archive_buffer",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                PermissionError("archive denied")
            ),
        )

        out = extract_session(
            config,
            session_id=session_id,
            project="t",
            summary="captured",
            insights=[],
            decisions=[],
        )

        assert buf.exists()
        assert out.warnings
        report = _format_extract_report(out)
        assert "Warning:" in report
        assert "buffer was preserved" in report

    def test_header_distinguishes_diverged_ids(
        self, config: Config, vault: VaultManager
    ):
        from thinkweave.surfaces.mcp.tools.extract import _format_extract_report

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
        from thinkweave.surfaces.mcp.tools.extract import _format_extract_report

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

    def test_finalize_hint_uses_minted_ses_id(
        self, config: Config, vault: VaultManager
    ):
        # #181: the finalize hint MUST use the minted ses-id — after a
        # forced re-extract the source UUID is claimed by two folders, and
        # only the note id names the one this extract just wrote.
        from thinkweave.surfaces.mcp.tools.extract import _format_extract_report

        _index(config)
        cc_uuid = "043708d8-1eb8-4aa3-a9ff-7d8bdad37951"
        out = extract_session(
            config,
            session_id=cc_uuid,
            project="thinkweave",
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
        assert f"weave wrap-finalize {out.session_note_id}" in report
        assert f"weave wrap-finalize {cc_uuid}" not in report
        # And includes the project so the agent can copy-paste verbatim.
        assert "--project thinkweave" in report

    def test_force_reextract_hint_followed_verbatim_matches_prompts(
        self, config: Config, vault: VaultManager
    ):
        # #181 AC2 — live incident 2026-08-22: weave_extract(force=true) on
        # an already-processed session mints a NEW folder and archives the
        # buffer there. The printed hint must name an id that resolves the
        # freshly archived events (prompts found, verdicts matched).
        import json

        from thinkweave.operations.wrap import finalize_wrap
        from thinkweave.surfaces.mcp.tools.extract import _format_extract_report

        _index(config)
        cc_uuid = "aaaa1111-2222-4333-8444-555566667777"
        row = json.dumps({
            "ts": "2026-08-22T09:00:00+00:00", "type": "prompt",
            "text": "collapse the duplicate rows", "session_id": cc_uuid,
        }) + "\n"
        buf = config.weave_dir / "buffer" / f"{cc_uuid}.jsonl"
        buf.parent.mkdir(parents=True, exist_ok=True)
        buf.write_text(row, encoding="utf-8")

        first = extract_session(
            config, session_id=cc_uuid, project="t",
            summary="first pass", insights=[], decisions=[],
        )
        assert first.error == ""
        # Hooks keep capturing after the first archive.
        buf.write_text(row, encoding="utf-8")

        out = extract_session(
            config, session_id=cc_uuid, project="t",
            summary="forced redo", insights=[], decisions=[], force=True,
        )
        assert out.error == ""
        assert out.skipped_reason == ""
        report = _format_extract_report(out)
        assert f"weave wrap-finalize {out.session_note_id}" in report

        # Follow the hint verbatim: the id it names must match prompts.
        result = finalize_wrap(
            config, session_id=out.session_note_id, project="t", prune=False,
            verdicts=[{
                "prompt": "collapse the duplicate", "register": "correction",
            }],
        )
        assert result.verdicts_written == 1
        assert result.verdicts_unmatched == 0

    def test_finalize_hint_without_project_when_unknown(
        self, config: Config, vault: VaultManager
    ):
        # Defensive: if nothing was created (rare), the hint still appears
        # but without a --project flag — caller can fill it in.
        from thinkweave.surfaces.mcp.tools.extract import _format_extract_report

        _index(config)
        out = extract_session(
            config, session_id="ses-99999999", project="t",
            summary="empty", insights=[], decisions=[],
        )
        report = _format_extract_report(out)
        assert "▶ To finalize:" in report
        # No created notes/decisions → no project surfaced from those — fine.
        assert f"weave wrap-finalize {out.session_note_id}" in report
        assert "--project" not in report
