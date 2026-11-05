"""Tests for the weave_extract → wrap-finalize identifier handoff (issue: 2026-05-14).

When a caller passes a non-``ses-XXX`` value as ``session_id`` (e.g. a Claude
Code UUID), ``weave_extract`` auto-mints a session note whose own ``id:`` is a
fresh ``ses-XXX`` but whose ``source_session:`` frontmatter is the input value.
The minted ``ses-XXX`` is the identity every note written beside it is stamped
with — the harness id stays on the session note alone.

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
- A decision's ``source_session`` is the ``id`` of the ``session.md`` beside
  it — never the harness id, never a folder slug — so ``weave rlvr export``
  resolves the session chain, including for a session the Stop hook
  auto-extracted first
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager, parse_frontmatter
from thinkweave.operations.extract import extract_session


def _decision_spec(body: str = "Did the work because the old path missed.") -> dict:
    """One ``extract_session`` decision payload, in the shape /wrap composes."""
    return {
        "title": "Stamp the vault session id",
        "rationale": f"## Context\n\n{body}\n",
        "outcome": "committed",
        "concepts": ["session-management", "memory-system"],
    }


def _session_id_beside(config: Config, decision_relpath: str) -> str:
    """The ``id`` in the ``session.md`` of the folder holding this decision.

    Read straight off disk: the session note is the independent source of
    truth for what a co-located stamp must equal.
    """
    folder = config.vault_root / Path(decision_relpath).parent
    fm, _ = parse_frontmatter((folder / "session.md").read_text(encoding="utf-8"))
    return str(fm["id"])


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


class TestEmptyExtraction:
    def _session(self, config: Config, vault: VaultManager) -> tuple[Path, str]:
        path = vault.create_note(
            NoteType.SESSION,
            "Empty extraction",
            body="## Summary\n\n## Events\n",
            project="t",
            extra_frontmatter={"processed": False},
        )
        session_id = vault.read_note(path).id
        _index(config)
        return path, session_id

    def test_empty_extract_refuses_without_allow_empty(
        self, config: Config, vault: VaultManager
    ):
        path, session_id = self._session(config, vault)
        before = vault.read_note(path)

        out = extract_session(
            config,
            session_id=session_id,
            summary="",
            insights=[],
            decisions=[],
        )

        assert "Empty extraction refused" in out.error
        assert "allow_empty" in out.error
        after = vault.read_note(path)
        assert after.frontmatter.get("processed") is False
        assert "processed_at" not in after.frontmatter
        assert after.body == before.body

    def test_empty_extract_can_be_explicitly_allowed(
        self, config: Config, vault: VaultManager
    ):
        path, session_id = self._session(config, vault)

        out = extract_session(
            config,
            session_id=session_id,
            summary="",
            insights=[],
            decisions=[],
            allow_empty=True,
        )

        assert out.error == ""
        assert vault.read_note(path).frontmatter.get("processed") is True


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


class TestDecisionSourceSessionStamp:
    """A decision's ``source_session`` is the co-located session note's ``id``.

    ``context_served`` is keyed by the ``ses-`` id. A decision stamped with the
    harness id the caller passed in joins nothing, so ``weave rlvr export``
    emits an empty context block for it. The session note keeps the harness id
    under its own ``source_session``; the decision borrows the note's ``id``.
    """

    CC_UUID = "7a1b2c3d-1111-4aa3-a9ff-7d8bdad37951"

    def _hook_shaped_session(self, vault: VaultManager) -> tuple[Path, str]:
        """A session note as hooks leave it: harness id under ``source_session``."""
        path = vault.create_note(
            NoteType.SESSION,
            "hooked",
            body="## Summary\n\n## Events\n",
            project="t",
            extra_frontmatter={"source_session": self.CC_UUID, "processed": False},
        )
        return path, vault.read_note(path).id

    def test_decisions_carry_the_fixture_session_note_id(
        self, config: Config, vault: VaultManager
    ):
        self._hook_shaped_session(vault)
        _index(config)

        out = extract_session(
            config, session_id=self.CC_UUID, project="t", summary="did things",
            insights=[], decisions=[_decision_spec(), _decision_spec("Second one.")],
        )

        assert out.error == ""
        assert len(out.created_decisions) == 2
        for dec in out.created_decisions:
            # Disk truth: the id in the session.md beside the decision.
            assert dec.frontmatter["source_session"] == _session_id_beside(
                config, dec.path
            )
            assert dec.frontmatter["source_session"] != self.CC_UUID
            assert dec.frontmatter["source_session"].startswith("ses-")

    def test_decisions_carry_the_id_extract_mints_when_input_diverges(
        self, config: Config, vault: VaultManager
    ):
        # No session note yet: extract mints one, and the decision must name
        # that minted note, not the slug or the uuid it was handed.
        _index(config)

        out = extract_session(
            config, session_id=self.CC_UUID, project="t", summary="did things",
            insights=[], decisions=[_decision_spec()],
        )

        dec = out.created_decisions[0]
        assert dec.frontmatter["source_session"] == out.session_note_id
        assert dec.frontmatter["source_session"] == _session_id_beside(config, dec.path)

    def test_judge_still_resolves_the_decision_by_either_form(
        self, config: Config, vault: VaultManager
    ):
        # The judge lookup and the session resolver match on either form, so
        # stamping the note id must not cost a caller who passes the harness id.
        from thinkweave.synthesis.judge import find_decisions

        _, note_id = self._hook_shaped_session(vault)
        _index(config)

        out = extract_session(
            config, session_id=self.CC_UUID, project="t", summary="did things",
            insights=[], decisions=[_decision_spec()],
        )
        dec_id = out.created_decisions[0].id

        idx = Indexer(config=config)
        try:
            by_note = find_decisions(idx.db, vault, session_id=note_id)
            by_harness = find_decisions(idx.db, vault, session_id=self.CC_UUID)
        finally:
            idx.close()
        assert [d.id for d in by_note] == [dec_id]
        assert [d.id for d in by_harness] == [dec_id]

    def test_rlvr_export_resolves_the_session_chain(
        self, config: Config, vault: VaultManager, monkeypatch, capsys
    ):
        import json

        from thinkweave.surfaces.cli.rlvr import cmd_rlvr

        _index(config)
        out = extract_session(
            config, session_id=self.CC_UUID, project="t", summary="did things",
            insights=[],
            decisions=[_decision_spec(
                "Read [[n-boot0001]] at startup and pulled [[n-pull0002]]."
            )],
        )
        dec = out.created_decisions[0]
        session_dir = config.vault_root / Path(dec.path).parent
        (session_dir / "retrieval_log.jsonl").write_text(
            "\n".join(json.dumps(line) for line in [
                {"ts": "t1", "type": "startup", "surface": "",
                 "returned_ids": ["n-boot0001"], "token_est": 4321},
                {"ts": "t2", "type": "retrieval", "tool": "weave_search",
                 "returned_ids": ["n-pull0002"]},
            ]) + "\n",
            encoding="utf-8",
        )
        _index(config)

        monkeypatch.setenv("THINKWEAVE_VAULT", str(config.vault_root))
        args = type("Args", (), {
            "rlvr_action": "export", "project": "t", "since": "", "until": "",
            "committed_only": False, "verbose": False, "explode_history": False,
        })()
        cmd_rlvr(args)

        rows = [
            json.loads(line)
            for line in capsys.readouterr().out.splitlines() if line.strip()
        ]
        mine = [r for r in rows if r["decision_id"] == dec.id]
        assert len(mine) == 1
        row = mine[0]
        assert row["session_id"] == out.session_note_id
        assert row["context"]["cited_startup_only_ids"] == ["n-boot0001"]
        assert row["context"]["cited_onthefly_ids"] == ["n-pull0002"]
        assert row["context"]["n_retrievals_onthefly"] == 1
        assert row["context"]["startup_token_est"] == 4321


class TestStopHookAutoExtractStamp:
    """The Stop hook auto-extracts first; the wrap that follows stamps the same id.

    Stop marks the note ``processed`` + ``auto_extracted`` without minting
    decision notes, so the interactive ``/wrap`` catch-up runs
    ``extract_session(force=True)`` on the harness id. Its decisions land in
    the folder Stop materialised and must carry that note's ``ses-`` id.
    """

    CC_UUID = "9f8e7d6c-2222-4aa3-a9ff-7d8bdad37951"

    def test_forced_wrap_after_stop_carries_the_note_id(
        self, config: Config, vault: VaultManager, monkeypatch
    ):
        import json

        from thinkweave.surfaces.hooks import handler as handler_mod

        path = vault.create_note(
            NoteType.SESSION,
            "live",
            body="## Summary\n\n## Events\n",
            project="t",
            extra_frontmatter={"source_session": self.CC_UUID},
        )
        note_id = vault.read_note(path).id
        _index(config)

        buf = config.weave_dir / "buffer" / f"{self.CC_UUID}.jsonl"
        buf.parent.mkdir(parents=True, exist_ok=True)
        buf.write_text(
            json.dumps({
                "ts": "2026-10-14T09:00:00+00:00", "type": "prompt",
                "text": "stamp the session id", "session_id": self.CC_UUID,
            }) + "\n",
            encoding="utf-8",
        )

        monkeypatch.setattr("thinkweave.core.config.load_config", lambda: config)
        handler_mod._handle_stop({"session_id": self.CC_UUID})
        assert vault.read_note(path).frontmatter.get("auto_extracted") is True

        out = extract_session(
            config, session_id=self.CC_UUID, force=True, summary="wrapped",
            insights=[], decisions=[_decision_spec()],
        )

        assert out.skipped_reason == ""
        dec = out.created_decisions[0]
        assert dec.frontmatter["source_session"] == note_id
        assert dec.frontmatter["source_session"] == _session_id_beside(config, dec.path)
