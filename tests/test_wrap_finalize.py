"""Tests for the deterministic wrap-finalize tail (operations/wrap.py + CLI).

``weave wrap-finalize`` is phase 2 of ``/wrap`` — after ``weave_extract`` has
written a session's insights/decisions, this bundles prune → index → judge →
landing → drift-advisory into one process. These tests build a tmp vault that
looks like a just-extracted session and assert the chain runs cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager
from thinkweave.operations.wrap import WrapFinalizeResult, finalize_wrap


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


def _seed_session_with_decision(vm: VaultManager) -> str:
    """Create a session note + a decision derived from it (mimics weave_extract output).

    Returns the session note ID.
    """
    sess_path = vm.create_note(
        NoteType.SESSION,
        "Did some work",
        body="## Summary\nDid some work.\n",
        project="t",
        extra_frontmatter={"processed": True, "processed_at": "2026-05-13"},
    )
    session_id = vm.read_note(sess_path).id
    vm.create_note(
        NoteType.DECISION,
        "Use SQLite for the index",
        body="## Context\n\nNeeded a derived index.\n\n## Decision\n\nUse SQLite.",
        project="t",
        extra_frontmatter={
            "status": "accepted",
            "committed": True,
            "source_session": session_id,
            "derived_from": [session_id],
            "concepts": ["sqlite", "memory-system"],
        },
        output_dir=sess_path.parent,
    )
    return session_id


class TestFinalizeWrap:
    def test_runs_end_to_end(self, config: Config, vault: VaultManager):
        session_id = _seed_session_with_decision(vault)
        _index(config)

        result = finalize_wrap(config, session_id=session_id, project="t")

        assert isinstance(result, WrapFinalizeResult)
        assert result.errors == []
        assert result.decisions_judged == 1
        assert sum(result.verdicts.values()) == 1
        assert len(result.landing_written) >= 1
        assert any("DECISION" in name.upper() for name in result.landing_written)
        assert any("BACKLOG" in name.upper() for name in result.landing_written)
        # P1-9 — every step contributes a timing entry (even if the step is a
        # no-op or errors out; the `finally` blocks stamp wall time regardless).
        assert set(result.timings) == {
            "prune", "index", "judge", "landing", "drift",
        }
        assert all(v >= 0.0 for v in result.timings.values())

    def test_judge_writes_verdict_to_decision_frontmatter(
        self, config: Config, vault: VaultManager
    ):
        session_id = _seed_session_with_decision(vault)
        _index(config)

        finalize_wrap(config, session_id=session_id, project="t")

        from thinkweave.synthesis.judge import find_decisions

        idx = Indexer(config=config)
        try:
            decs = find_decisions(idx.db, vault, session_id=session_id)
        finally:
            idx.close()
        assert len(decs) == 1
        assert "verdict" in decs[0].frontmatter
        assert "judged_at" in decs[0].frontmatter

    def test_judge_finds_decisions_when_given_minted_ses_id(
        self, config: Config, vault: VaultManager
    ):
        # #181: the extract report now hints the minted ses- id, but
        # decisions are stamped with the extract INPUT (``source_session``,
        # often a Claude Code UUID). The judge step must fall back to that
        # id instead of silently judging 0 (the 2026-05-14 trap).
        cc_uuid = "d4e07b2a-0000-4aa3-a9ff-7d8bdad37951"
        sess_path = vault.create_note(
            NoteType.SESSION,
            "Did some work",
            body="## Summary\nDid some work.\n",
            project="t",
            extra_frontmatter={"processed": True, "source_session": cc_uuid},
        )
        ses_id = vault.read_note(sess_path).id
        vault.create_note(
            NoteType.DECISION,
            "Use SQLite for the index",
            body="## Context\n\nNeeded an index.\n\n## Decision\n\nSQLite.",
            project="t",
            extra_frontmatter={
                "status": "proposed",
                "committed": True,
                "source_session": cc_uuid,
                "derived_from": [cc_uuid],
                "concepts": ["sqlite", "memory-system"],
            },
            output_dir=sess_path.parent,
        )
        _index(config)

        result = finalize_wrap(config, session_id=ses_id, project="t")
        assert result.decisions_judged == 1

    def test_no_decisions_is_fine(self, config: Config, vault: VaultManager):
        sess_path = vault.create_note(
            NoteType.SESSION,
            "Empty session",
            body="## Summary\nNothing happened.\n",
            project="t",
            extra_frontmatter={"processed": True},
        )
        session_id = vault.read_note(sess_path).id
        _index(config)

        result = finalize_wrap(config, session_id=session_id, project="t")
        assert result.errors == []
        assert result.decisions_judged == 0

    def test_missing_project_is_recorded_as_error(
        self, config: Config, vault: VaultManager
    ):
        sess_path = vault.create_note(
            NoteType.SESSION, "S", body="## Summary\n", project="t"
        )
        session_id = vault.read_note(sess_path).id
        _index(config)

        result = finalize_wrap(config, session_id=session_id, project="")
        assert any("landing" in e for e in result.errors)

    def test_prune_removes_orphan_folder(self, config: Config, vault: VaultManager):
        orphan = config.vault_root / "projects" / "t" / "sessions" / "orphan-old"
        orphan.mkdir(parents=True, exist_ok=True)
        (orphan / "session.md").write_text(
            "---\ntype: session\nid: ses-orphan1\ndate: '2020-01-01'\nproject: t\n"
            "files_touched: []\ncommits: []\n---\n\n# orphan stub\n",
            encoding="utf-8",
        )
        session_id = _seed_session_with_decision(vault)
        _index(config)

        result = finalize_wrap(config, session_id=session_id, project="t", prune=True)
        assert result.orphans_pruned == 1
        assert not orphan.exists()

    def test_no_prune_keeps_orphan_folder(self, config: Config, vault: VaultManager):
        orphan = config.vault_root / "projects" / "t" / "sessions" / "orphan-old"
        orphan.mkdir(parents=True, exist_ok=True)
        (orphan / "session.md").write_text(
            "---\ntype: session\nid: ses-orphan2\ndate: '2020-01-01'\nproject: t\n"
            "files_touched: []\ncommits: []\n---\n\n# orphan stub\n",
            encoding="utf-8",
        )
        session_id = _seed_session_with_decision(vault)
        _index(config)

        result = finalize_wrap(config, session_id=session_id, project="t", prune=False)
        assert result.orphans_pruned == 0
        assert orphan.exists()


class TestWrapFinalizeCLI:
    def test_json_output_parses(
        self, config: Config, vault: VaultManager, monkeypatch, capsys
    ):
        session_id = _seed_session_with_decision(vault)
        _index(config)

        # Point load_config at our tmp vault.
        monkeypatch.setenv("THINKWEAVE_VAULT", str(config.vault_root))
        monkeypatch.setenv("THINKWEAVE_PROJECT", "t")

        from thinkweave.surfaces.cli.wrap import cmd_wrap_finalize

        args = type(
            "Args",
            (),
            {"session_id": session_id, "project": "t", "json": True, "no_prune": True},
        )()
        with pytest.raises(SystemExit) as exc:
            cmd_wrap_finalize(args)
        assert exc.value.code == 0  # no errors

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["session_id"] == session_id
        assert payload["decisions_judged"] == 1
        assert payload["errors"] == []


class TestVerdictStep:
    """#101 — the deterministic half of the async prompt labeler."""

    def _write_buffer(self, config: Config, session_id: str, rows: list[dict]):
        buf_dir = config.weave_dir / "buffer"
        buf_dir.mkdir(parents=True, exist_ok=True)
        f = buf_dir / f"{session_id}.jsonl"
        f.write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
        return f

    def _rows(self, f: Path) -> list[dict]:
        return [
            json.loads(ln)
            for ln in f.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]

    def test_verdicts_append_frozen_shape_events(
        self, config: Config, vault: VaultManager
    ):
        f = self._write_buffer(config, "cc-uuid-1", [
            {"ts": "2026-08-03T10:00:00+00:00", "type": "prompt",
             "text": "no, that's wrong — use a dict instead",
             "session_id": "cc-uuid-1", "cwd": "/p"},
            {"ts": "2026-08-03T10:05:00+00:00", "type": "prompt",
             "text": "looks good, ship it", "session_id": "cc-uuid-1",
             "cwd": "/p"},
        ])
        result = finalize_wrap(
            config, session_id="cc-uuid-1", project="t", prune=False,
            verdicts=[
                {"prompt": "no, that's wrong", "register": "correction"},
                {"prompt": "looks good", "register": "confirmation"},
            ],
        )
        assert result.verdicts_written == 2
        assert result.verdicts_unmatched == 0
        fb = [r for r in self._rows(f) if r.get("type") == "feedback"]
        assert [r["register"] for r in fb] == ["correction", "confirmation"]
        # Frozen schema: exactly the keys the pre-#101 hook labeler wrote,
        # and ts reuses the prompt event's own timestamp (exact join).
        assert set(fb[0]) == {"ts", "type", "register", "session_id", "prompt_ref"}
        assert fb[0]["ts"] == "2026-08-03T10:00:00+00:00"
        assert fb[0]["prompt_ref"].startswith("no, that's wrong")
        assert "verdicts" in result.timings

    def test_rewrap_is_idempotent(self, config: Config, vault: VaultManager):
        f = self._write_buffer(config, "cc-uuid-2", [
            {"ts": "2026-08-03T10:00:00+00:00", "type": "prompt",
             "text": "revert that change", "session_id": "cc-uuid-2"},
        ])
        verdicts = [{"prompt": "revert that", "register": "correction"}]
        finalize_wrap(config, session_id="cc-uuid-2", project="t",
                      prune=False, verdicts=verdicts)
        result = finalize_wrap(config, session_id="cc-uuid-2", project="t",
                               prune=False, verdicts=verdicts)
        assert result.verdicts_written == 0
        assert result.verdicts_skipped == 1
        fb = [r for r in self._rows(f) if r.get("type") == "feedback"]
        assert len(fb) == 1

    def test_unmatched_and_invalid_verdicts(
        self, config: Config, vault: VaultManager
    ):
        self._write_buffer(config, "cc-uuid-3", [
            {"ts": "2026-08-03T10:00:00+00:00", "type": "prompt",
             "text": "do the thing", "session_id": "cc-uuid-3"},
        ])
        result = finalize_wrap(
            config, session_id="cc-uuid-3", project="t", prune=False,
            verdicts=[
                {"prompt": "never said this", "register": "correction"},
                {"prompt": "do the thing", "register": "neutral"},
            ],
        )
        assert result.verdicts_written == 0
        assert result.verdicts_unmatched == 1
        assert any("invalid register" in e for e in result.errors)

    def test_echoed_prompts_yield_one_event(
        self, config: Config, vault: VaultManager
    ):
        # Multi-registration triple-write (#161): three echoes of the same
        # prompt milliseconds apart must produce ONE feedback event.
        f = self._write_buffer(config, "cc-uuid-4", [
            {"ts": f"2026-08-03T10:00:00.0{i}0000+00:00", "type": "prompt",
             "text": "no, wrong approach", "session_id": "cc-uuid-4"}
            for i in range(3)
        ])
        result = finalize_wrap(
            config, session_id="cc-uuid-4", project="t", prune=False,
            verdicts=[{"prompt": "no, wrong", "register": "correction"}],
        )
        assert result.verdicts_written == 1
        fb = [r for r in self._rows(f) if r.get("type") == "feedback"]
        assert len(fb) == 1

    def test_duplicate_prompt_rows_yield_one_event(
        self, config: Config, vault: VaultManager
    ):
        # #181 AC1: triple hook registration could leave duplicate prompt
        # rows farther apart than the echo-collapse window. One verdict must
        # still write exactly ONE feedback event — and a re-wrap must not
        # sneak a second event onto a surviving duplicate.
        f = self._write_buffer(config, "cc-uuid-6", [
            {"ts": f"2026-08-22T10:00:{s:02d}+00:00", "type": "prompt",
             "text": "no, use the queue primitive", "session_id": "cc-uuid-6"}
            for s in (0, 10, 20)
        ])
        verdicts = [{"prompt": "no, use the queue", "register": "correction"}]
        result = finalize_wrap(config, session_id="cc-uuid-6", project="t",
                               prune=False, verdicts=verdicts)
        assert result.verdicts_written == 1
        fb = [r for r in self._rows(f) if r.get("type") == "feedback"]
        assert len(fb) == 1

        rewrap = finalize_wrap(config, session_id="cc-uuid-6", project="t",
                               prune=False, verdicts=verdicts)
        assert rewrap.verdicts_written == 0
        assert rewrap.verdicts_skipped == 1
        fb = [r for r in self._rows(f) if r.get("type") == "feedback"]
        assert len(fb) == 1

    def test_verdict_labels_single_best_of_distinct_prefix_matches(
        self, config: Config, vault: VaultManager
    ):
        # Two distinct prompts share the verdict's prefix — the verdict
        # labels one (the first unlabeled), never fans out to both.
        f = self._write_buffer(config, "cc-uuid-7", [
            {"ts": "2026-08-22T10:00:00+00:00", "type": "prompt",
             "text": "fix the parser bug", "session_id": "cc-uuid-7"},
            {"ts": "2026-08-22T11:00:00+00:00", "type": "prompt",
             "text": "fix the parser tests too", "session_id": "cc-uuid-7"},
        ])
        verdicts = [{"prompt": "fix the parser", "register": "correction"}]
        result = finalize_wrap(
            config, session_id="cc-uuid-7", project="t", prune=False,
            verdicts=verdicts,
        )
        assert result.verdicts_written == 1
        fb = [r for r in self._rows(f) if r.get("type") == "feedback"]
        assert len(fb) == 1
        assert fb[0]["ts"] == "2026-08-22T10:00:00+00:00"

        # #181 review: a re-wrap of the SAME verdict must not walk onto the
        # next unlabeled candidate — that would label a prompt the user
        # never judged. Skip outright instead.
        rewrap = finalize_wrap(
            config, session_id="cc-uuid-7", project="t", prune=False,
            verdicts=verdicts,
        )
        assert rewrap.verdicts_written == 0
        assert rewrap.verdicts_skipped == 1
        fb = [r for r in self._rows(f) if r.get("type") == "feedback"]
        assert len(fb) == 1
        assert fb[0]["ts"] == "2026-08-22T10:00:00+00:00"

    @pytest.mark.parametrize("reverse", [False, True])
    def test_forced_reextract_prefers_newest_source_session_folder(
        self, config: Config, vault: VaultManager, monkeypatch, reverse: bool
    ):
        # #181: weave_extract(force=true) mints a SECOND folder claiming the
        # same source UUID. Resolution must pick the most recently written
        # events file, not whichever folder iterdir yields first — pinned
        # by forcing BOTH directory orders instead of trusting the
        # filesystem's readdir order.
        import os

        real_iterdir = Path.iterdir
        monkeypatch.setattr(
            Path,
            "iterdir",
            lambda self: iter(
                sorted(real_iterdir(self), key=lambda p: p.name,
                       reverse=reverse)
            ),
        )

        base = config.vault_root / "projects" / "t" / "sessions"
        row = json.dumps({
            "ts": "2026-08-22T09:00:00+00:00", "type": "prompt",
            "text": "wire the verdict rail", "session_id": "cc-uuid-8",
        })
        events_files: dict[str, Path] = {}
        for name, sid, mtime in [
            ("ses-stale111-2026-08-20", "ses-stale111", 1_000_000_000),
            ("ses-fresh222-2026-08-22", "ses-fresh222", 2_000_000_000),
        ]:
            d = base / name
            d.mkdir(parents=True)
            (d / "session.md").write_text(
                f"---\ntype: session\nid: {sid}\n"
                "source_session: cc-uuid-8\n---\n",
                encoding="utf-8",
            )
            ev = d / "events.jsonl"
            ev.write_text(row + "\n", encoding="utf-8")
            os.utime(ev, (mtime, mtime))
            events_files[sid] = ev

        result = finalize_wrap(
            config, session_id="cc-uuid-8", project="t", prune=False,
            verdicts=[{"prompt": "wire the verdict", "register": "correction"}],
        )
        assert result.verdicts_written == 1
        new_fb = [
            r for r in self._rows(events_files["ses-fresh222"])
            if r.get("type") == "feedback"
        ]
        old_fb = [
            r for r in self._rows(events_files["ses-stale111"])
            if r.get("type") == "feedback"
        ]
        assert len(new_fb) == 1
        assert old_fb == []

    def test_archived_events_jsonl_is_found(
        self, config: Config, vault: VaultManager
    ):
        sess_dir = (
            config.vault_root / "projects" / "t" / "sessions"
            / "ses-arch1-2026-08-03"
        )
        sess_dir.mkdir(parents=True)
        events = sess_dir / "events.jsonl"
        events.write_text(
            json.dumps({
                "ts": "2026-08-03T09:00:00+00:00", "type": "prompt",
                "text": "actually, undo that", "session_id": "ses-arch1",
            }) + "\n",
            encoding="utf-8",
        )
        result = finalize_wrap(
            config, session_id="ses-arch1", project="t", prune=False,
            verdicts=[{"prompt": "actually, undo", "register": "correction"}],
        )
        assert result.verdicts_written == 1
        fb = [
            r for r in self._rows(events) if r.get("type") == "feedback"
        ]
        assert len(fb) == 1
        assert fb[0]["session_id"] == "ses-arch1"

    def _hijack_fixture(self, config: Config, ses_id: str, uuid: str):
        """Archived UUID-named folder with one prompt + a recreated buffer
        holding only a post-archive action row. Returns the archive path."""
        sess_dir = (
            config.vault_root / "projects" / "t" / "sessions"
            / f"{uuid}-2026-08-22"
        )
        sess_dir.mkdir(parents=True)
        (sess_dir / "session.md").write_text(
            f"---\ntype: session\nid: {ses_id}\n"
            f"source_session: {uuid}\nproject: t\n---\n",
            encoding="utf-8",
        )
        events = sess_dir / "events.jsonl"
        events.write_text(
            json.dumps({
                "ts": "2026-08-22T10:00:00+00:00", "type": "prompt",
                "text": "ship the archive", "session_id": uuid,
            }) + "\n",
            encoding="utf-8",
        )
        self._write_buffer(config, uuid, [
            {"ts": "2026-08-22T10:05:00+00:00", "type": "action",
             "tool": "Bash", "session_id": uuid},
        ])
        _index(config)
        return events

    def test_recreated_buffer_does_not_hijack_archived_events(
        self, config: Config, vault: VaultManager
    ):
        # #181 review: hooks recreate the buffer the moment any event fires
        # after weave_extract archived it. Resolution keyed by the hinted
        # ses- id must still hit the archived events.jsonl (the live folder
        # is UUID-named with the ses- id in frontmatter) — the buffer is
        # the events file only when no archive exists.
        events = self._hijack_fixture(config, "ses-hijack1", "cc-uuid-10")
        result = finalize_wrap(
            config, session_id="ses-hijack1", project="t", prune=False,
            verdicts=[{"prompt": "ship the archive", "register": "correction"}],
        )
        assert result.verdicts_written == 1
        assert result.verdicts_unmatched == 0
        fb = [r for r in self._rows(events) if r.get("type") == "feedback"]
        assert len(fb) == 1

    def test_recreated_buffer_does_not_hijack_for_uuid_id(
        self, config: Config, vault: VaultManager
    ):
        # Same hijack, UUID-keyed finalize: the direct buffer probe must
        # not shadow the archived folder either.
        events = self._hijack_fixture(config, "ses-hijack2", "cc-uuid-11")
        result = finalize_wrap(
            config, session_id="cc-uuid-11", project="t", prune=False,
            verdicts=[{"prompt": "ship the archive", "register": "correction"}],
        )
        assert result.verdicts_written == 1
        assert result.verdicts_unmatched == 0
        fb = [r for r in self._rows(events) if r.get("type") == "feedback"]
        assert len(fb) == 1

    def test_specific_verdict_beats_broad_same_batch(
        self, config: Config, vault: VaultManager
    ):
        # #181 review: in-batch assignment must not be verdict-order
        # dependent — the most specific (longest) needle claims its prompt
        # first, and a starved verdict is surfaced, not silently skipped.
        f = self._write_buffer(config, "cc-uuid-12", [
            {"ts": "2026-08-22T10:00:00+00:00", "type": "prompt",
             "text": "fix the parser bug", "session_id": "cc-uuid-12"},
            {"ts": "2026-08-22T11:00:00+00:00", "type": "prompt",
             "text": "fix the parser tests too", "session_id": "cc-uuid-12"},
        ])
        result = finalize_wrap(
            config, session_id="cc-uuid-12", project="t", prune=False,
            verdicts=[
                {"prompt": "fix the parser", "register": "correction",
                 "about": "broad one"},
                {"prompt": "fix the parser", "register": "correction",
                 "about": "broad two"},
                {"prompt": "fix the parser tests", "register": "correction",
                 "about": "narrow"},
            ],
        )
        assert result.verdicts_written == 2
        assert result.verdicts_skipped == 1
        fb = {
            r["ts"]: r for r in self._rows(f) if r.get("type") == "feedback"
        }
        # The narrow verdict got its precise referent; one broad duplicate
        # starved — surfaced as a warning, NOT an error (two verdicts
        # resolving to one prompt is a benign LLM-output shape and must not
        # flip the wrap-finalize exit code).
        assert fb["2026-08-22T11:00:00+00:00"]["about"] == "narrow"
        assert any("no unlabeled prompt" in w for w in result.warnings)
        assert result.errors == []

    def test_mixed_register_verdicts_do_not_double_label_one_prompt(
        self, config: Config, vault: VaultManager
    ):
        # #181 review round 4: a prompt claimed by one verdict must not be
        # "free" for a verdict of a DIFFERENT register (contradictory
        # labels on one prompt), and an exact-text match outranks a mere
        # prefix match — here the confirmation needle IS p2's text.
        f = self._write_buffer(config, "cc-uuid-13", [
            {"ts": "2026-08-29T09:00:00+00:00", "type": "prompt",
             "text": "carry on with the refactor", "session_id": "cc-uuid-13"},
            {"ts": "2026-08-29T09:30:00+00:00", "type": "prompt",
             "text": "carry on", "session_id": "cc-uuid-13"},
        ])
        result = finalize_wrap(
            config, session_id="cc-uuid-13", project="t", prune=False,
            verdicts=[
                {"prompt": "carry on with the refactor",
                 "register": "correction"},
                {"prompt": "carry on", "register": "confirmation"},
            ],
        )
        assert result.verdicts_written == 2
        fb = [r for r in self._rows(f) if r.get("type") == "feedback"]
        assert {r["ts"]: r["register"] for r in fb} == {
            "2026-08-29T09:00:00+00:00": "correction",
            "2026-08-29T09:30:00+00:00": "confirmation",
        }

    def test_ses_id_falls_back_to_source_uuid_buffer(
        self, config: Config, vault: VaultManager
    ):
        # #181 review: the hint names the minted ses- id, but the live
        # buffer is keyed by the source UUID. If archive_buffer failed, the
        # buffer is the only events file — the ses- id must still find it.
        sess_path = vault.create_note(
            NoteType.SESSION, "S", body="## Summary\n", project="t",
            extra_frontmatter={"source_session": "cc-uuid-9"},
        )
        ses_id = vault.read_note(sess_path).id
        _index(config)
        f = self._write_buffer(config, "cc-uuid-9", [
            {"ts": "2026-08-22T10:00:00+00:00", "type": "prompt",
             "text": "archive failed but capture lives",
             "session_id": "cc-uuid-9"},
        ])
        result = finalize_wrap(
            config, session_id=ses_id, project="t", prune=False,
            verdicts=[{"prompt": "archive failed", "register": "correction"}],
        )
        assert result.verdicts_written == 1
        fb = [r for r in self._rows(f) if r.get("type") == "feedback"]
        assert len(fb) == 1

    def test_no_events_file_reports_error(
        self, config: Config, vault: VaultManager
    ):
        result = finalize_wrap(
            config, session_id="cc-none", project="t", prune=False,
            verdicts=[{"prompt": "anything", "register": "correction"}],
        )
        assert result.verdicts_written == 0
        assert result.verdicts_unmatched == 1
        assert any("no events file" in e for e in result.errors)

    def test_probe_verdict_writes_probe_event(
        self, config: Config, vault: VaultManager
    ):
        from thinkweave.core.events import extract_prompts

        f = self._write_buffer(config, "cc-uuid-5", [
            {"ts": "2026-08-03T10:00:00+00:00", "type": "prompt",
             "text": "how does the drift judge decide?",
             "session_id": "cc-uuid-5"},
        ])
        result = finalize_wrap(
            config, session_id="cc-uuid-5", project="t", prune=False,
            verdicts=[{"prompt": "how does the drift", "register": "probe"}],
        )
        assert result.verdicts_written == 1
        rows = self._rows(f)
        probe = [r for r in rows if r.get("type") == "probe"]
        assert len(probe) == 1
        assert probe[0]["ts"] == "2026-08-03T10:00:00+00:00"
        assert "register" not in probe[0]
        # The join seam folds it back onto the prompt.
        prompts = extract_prompts(f)
        assert prompts[0].classification == "probe"
        # Idempotent on re-wrap.
        again = finalize_wrap(
            config, session_id="cc-uuid-5", project="t", prune=False,
            verdicts=[{"prompt": "how does the drift", "register": "probe"}],
        )
        assert again.verdicts_written == 0
        assert again.verdicts_skipped == 1

    def test_about_clause_rides_the_event(
        self, config: Config, vault: VaultManager
    ):
        # Grounding (#101): the wrap LLM's `about` referent clause is
        # carried onto both event types; absent about → no key (base
        # feedback schema stays frozen).
        f = self._write_buffer(config, "cc-uuid-6", [
            {"ts": "2026-08-04T10:00:00+00:00", "type": "prompt",
             "text": "no, revert the parser change", "session_id": "cc-uuid-6"},
            {"ts": "2026-08-04T10:05:00+00:00", "type": "prompt",
             "text": "how does the echo collapse window work?",
             "session_id": "cc-uuid-6"},
        ])
        result = finalize_wrap(
            config, session_id="cc-uuid-6", project="t", prune=False,
            verdicts=[
                {"prompt": "no, revert the parser", "register": "correction",
                 "about": "rejected the regex-based parser rewrite"},
                {"prompt": "how does the echo collapse", "register": "probe"},
            ],
        )
        assert result.verdicts_written == 2
        rows = self._rows(f)
        fb = [r for r in rows if r.get("type") == "feedback"]
        assert fb[0]["about"] == "rejected the regex-based parser rewrite"
        probe = [r for r in rows if r.get("type") == "probe"]
        assert "about" not in probe[0]


class TestProbeReachesIndex:
    """The probe → dream-priority → queue rail starts at the ``prompts``
    table. Found 2026-08-23: every session wrapped after the buffer-path
    change had ZERO prompt rows — extract indexed ``session.md`` before
    archiving the buffer into ``events.jsonl``, and finalize's verdicts
    landed in ``events.jsonl`` without re-projecting. This pins the
    end-to-end contract: wrap with a probe verdict → a ``probe`` row."""

    def test_probe_verdict_on_archived_session_lands_in_prompts_table(
        self, config: Config, vault: VaultManager
    ):
        import sqlite3

        # A wrapped session: note + archived events.jsonl (post-extract shape)
        sess_path = vault.create_note(
            NoteType.SESSION,
            "Asked about RL",
            body="## Summary\nExplored RLVR.\n",
            project="t",
            extra_frontmatter={"source_session": "cc-uuid-rl"},
        )
        sess_id = vault.read_note(sess_path).id
        events = sess_path.parent / "events.jsonl"
        events.write_text(
            json.dumps({
                "ts": "2026-08-23T10:00:00+00:00", "type": "prompt",
                "text": "how does GRPO differ from PPO for RLVR?",
                "session_id": "cc-uuid-rl", "project": "t",
            }) + "\n",
            encoding="utf-8",
        )
        _index(config)  # note indexed; no verdict yet → row exists, unclassified

        result = finalize_wrap(
            config, session_id="cc-uuid-rl", project="t", prune=False,
            verdicts=[{"prompt": "how does GRPO", "register": "probe",
                       "about": "GRPO vs PPO under RLVR"}],
        )
        assert result.verdicts_written == 1
        assert result.prompts_reprojected == 1
        assert result.errors == []

        db = sqlite3.connect(config.index_db)
        rows = db.execute(
            "SELECT classification FROM prompts WHERE session_id = ?", (sess_id,)
        ).fetchall()
        assert rows == [("probe",)]
