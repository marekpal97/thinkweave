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
        # archive_buffer UNLINKED the buffer, so what hooks recreate holds
        # only post-archive rows — the prompts are unreachable from it.
        # (#181 review round 3: re-seeding the prompt row here masked the
        # real state.)
        buf.write_text(
            json.dumps({
                "ts": "2026-08-22T09:10:00+00:00", "type": "action",
                "tool": "Bash", "session_id": cc_uuid,
            }) + "\n",
            encoding="utf-8",
        )

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


    def test_skip_reason_is_actionable_not_terse(
        self, config: Config, vault: VaultManager
    ):
        """Rail A (2026-10-10). The old terse 'Use force=true to re-extract'
        read as a risky escape hatch, and a hesitant model skipped the whole
        extraction (silently). The skip reason must now state that force=true
        is the NORMAL /wrap catch-up path."""
        _index(config)
        sid = "sess-rail-a"
        first = extract_session(
            config, session_id=sid, project="t",
            summary="seed", insights=[], decisions=[],
        )
        assert first.skipped_reason == ""  # first pass marks processed
        second = extract_session(
            config, session_id=sid, project="t",
            summary="redo", decisions=[],
            insights=[{"title": "x", "body": "y",
                        "concepts": ["mcp", "harness"]}],
        )
        assert second.skipped_reason
        assert "force=true" in second.skipped_reason
        assert "CATCH-UP" in second.skipped_reason.upper()
        assert "does not duplicate" in second.skipped_reason


class TestResolvesExistingNoteBySourceSession:
    """A raw harness UUID must land on the note the hooks already stamped.

    Under concurrency the alternative — minting a second note, or the skill
    falling back to a "most recent session in this project" search — writes
    one session's insights onto another session's note.
    """

    def _hook_created_note(self, vault: VaultManager, cc_uuid: str) -> Path:
        return vault.create_note(
            NoteType.SESSION,
            "hooked",
            body="## Summary\n\n## Events\n",
            project="t",
            extra_frontmatter={"source_session": cc_uuid, "processed": False},
        )

    def test_uuid_resolves_indexed_note_instead_of_minting(
        self, config: Config, vault: VaultManager
    ):
        cc_uuid = "11111111-2222-3333-4444-555555555555"
        existing = self._hook_created_note(vault, cc_uuid)
        existing_id = vault.read_note(existing).id
        _index(config)

        out = extract_session(
            config, session_id=cc_uuid, project="t",
            summary="ok", insights=[], decisions=[],
        )
        assert out.error == ""
        assert out.session_note_id == existing_id
        assert len(list((vault.root / "projects/t/sessions").iterdir())) == 1

    def test_uuid_resolves_via_glob_when_index_is_stale(
        self, config: Config, vault: VaultManager
    ):
        # The just-created-not-yet-indexed window the hooks routinely leave.
        cc_uuid = "66666666-7777-8888-9999-000000000000"
        existing = self._hook_created_note(vault, cc_uuid)
        existing_id = vault.read_note(existing).id
        _index(config)
        # Drop the session row so only the bounded glob can answer.
        idx = Indexer(config=config)
        idx.db.execute("DELETE FROM notes WHERE type='session'")
        idx.db.commit()
        idx.close()

        out = extract_session(
            config, session_id=cc_uuid, project="t",
            summary="ok", insights=[], decisions=[],
        )
        assert out.error == ""
        assert out.session_note_id == existing_id

    def test_unknown_uuid_still_mints(self, config: Config, vault: VaultManager):
        self._hook_created_note(vault, "aaaaaaaa-0000-0000-0000-000000000001")
        _index(config)

        out = extract_session(
            config, session_id="bbbbbbbb-0000-0000-0000-000000000002",
            project="t", summary="ok", insights=[], decisions=[],
        )
        assert out.error == ""
        assert len(list((vault.root / "projects/t/sessions").iterdir())) == 2

    def test_weave_read_resolves_a_raw_session_uuid(
        self, config: Config, vault: VaultManager
    ):
        from thinkweave.operations.notes import read_note

        cc_uuid = "cccccccc-0000-0000-0000-000000000003"
        existing_id = vault.read_note(self._hook_created_note(vault, cc_uuid)).id
        _index(config)

        note, raw = read_note(config, cc_uuid)
        assert note is not None and note.id == existing_id
        assert raw is not None and cc_uuid in raw
        assert read_note(config, "dddddddd-0000-0000-0000-000000000004") == (None, None)


class TestRefEvidence:
    """The extract result lists the session's ref evidence for the task
    declaration: commit closing refs, the pushed branch's PRs, and open
    tasks whose rounds touched the same files."""

    def _session(self, vault: VaultManager, config: Config, **fm) -> str:
        path = vault.create_note(
            note_type=NoteType.SESSION,
            title="evidence",
            body="## Summary\n",
            project="t",
            extra_frontmatter=fm,
        )
        _index(config)
        return vault.read_note(path).id

    def _open_task(self, config: Config, vault: VaultManager, paths: list[str]):
        from thinkweave.core.task_contract import Round, SessionRef
        from thinkweave.operations.tasks import TaskStore

        task = TaskStore(config).mint("work", "ship the greet CLI", "t")
        task.put_round(
            Round(
                route="session",
                session_ref=SessionRef.session("claude-code", "earlier"),
                did={"paths": paths},
            )
        )
        task.save(vault)
        return task

    def test_evidence_names_commit_closes_and_touching_task(
        self, config: Config, vault: VaultManager, monkeypatch
    ):
        from thinkweave.surfaces.mcp.tools.extract import _format_extract_report

        task = self._open_task(config, vault, ["src/greet/cli.py"])
        self._open_task(config, vault, ["src/other.py"])
        sess = self._session(
            vault,
            config,
            commits=[
                {
                    "hash": "abc1234",
                    "message": "Fix greet typo. Closes #12",
                    "files": ["src/greet/cli.py"],
                },
                {"hash": "def5678", "message": "chore: bump", "files": ["README.md"]},
            ],
        )

        out = extract_session(
            config, session_id=sess, summary="ok", insights=[], decisions=[]
        )

        assert out.ref_evidence.commit_closes == {"#12": ["abc1234"]}
        assert list(out.ref_evidence.tasks) == [task.id]
        report = _format_extract_report(out)
        assert "#12" in report and "abc1234" in report
        assert task.id in report and "ship the greet CLI" in report

    def test_evidence_matches_absolute_touched_paths(
        self, config: Config, vault: VaultManager
    ):
        task = self._open_task(config, vault, ["src/greet/cli.py"])
        sess = self._session(
            vault, config, files_touched=["/home/u/repo/src/greet/cli.py"]
        )

        out = extract_session(
            config, session_id=sess, summary="ok", insights=[], decisions=[]
        )

        assert list(out.ref_evidence.tasks) == [task.id]

    def test_evidence_lists_pushed_branch_prs_and_their_closing_refs(
        self, config: Config, vault: VaultManager, monkeypatch
    ):
        from thinkweave.surfaces.mcp.tools.extract import _format_extract_report

        seen = []
        monkeypatch.setattr(
            "thinkweave.operations.tasks._gh_branch_prs",
            lambda branch: seen.append(branch)
            or {"https://github.com/o/r/pull/7": ["#12"]},
        )
        sess = self._session(vault, config, git_branch="feat/greet")

        out = extract_session(
            config, session_id=sess, summary="ok", insights=[], decisions=[]
        )

        assert seen == ["feat/greet"]
        assert out.ref_evidence.prs == {"https://github.com/o/r/pull/7": ["#12"]}
        assert "https://github.com/o/r/pull/7" in _format_extract_report(out)

    def test_evidence_pr_lookup_failure_is_announced(
        self, config: Config, vault: VaultManager, monkeypatch
    ):
        import subprocess

        def boom(branch):
            raise subprocess.CalledProcessError(1, "gh", stderr="gh: not logged in")

        monkeypatch.setattr("thinkweave.operations.tasks._gh_branch_prs", boom)
        sess = self._session(vault, config, git_branch="feat/greet")

        out = extract_session(
            config, session_id=sess, summary="ok", insights=[], decisions=[]
        )

        assert any("not logged in" in w for w in out.warnings)
