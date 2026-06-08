"""Tests for structural decision judgment."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.synthesis.judge import (
    _check_blame_survival,
    _check_re_edited,
    _check_tested,
    evaluate_decision,
    find_decisions,
)
from personal_mem.core.schemas import NoteMeta, NoteType
from personal_mem.core.vault import VaultManager


def _make_decision(
    id: str = "dec-test1",
    date: str = "2026-04-01",
    file_paths: list | None = None,
    committed: bool = False,
    **extra_fm,
) -> NoteMeta:
    fm = {
        "id": id,
        "type": "decision",
        "status": "proposed",
        "committed": committed,
        "file_paths": file_paths or [],
        **extra_fm,
    }
    return NoteMeta(
        id=id,
        type=NoteType.DECISION,
        title=f"Decision {id}",
        path=f"projects/test/decisions/{id}.md",
        date=date,
        project="test",
        frontmatter=fm,
    )


def _make_session(
    id: str = "ses-test1",
    test_runs: list | None = None,
) -> NoteMeta:
    fm = {
        "id": id,
        "type": "session",
        "test_runs": test_runs or [],
    }
    return NoteMeta(
        id=id,
        type=NoteType.SESSION,
        title="Test Session",
        path=f"projects/test/sessions/{id}.md",
        date="2026-04-01",
        project="test",
        frontmatter=fm,
    )


class TestCheckReEdited:
    def test_no_overlap(self):
        dec1 = _make_decision("dec-1", date="2026-04-01", file_paths=["a.py"])
        dec2 = _make_decision("dec-2", date="2026-04-02", file_paths=["b.py"])
        assert _check_re_edited(dec1, ["a.py"], [dec1, dec2]) is None

    def test_overlap_later(self):
        dec1 = _make_decision("dec-1", date="2026-04-01", file_paths=["a.py"])
        dec2 = _make_decision("dec-2", date="2026-04-02", file_paths=["a.py"])
        assert _check_re_edited(dec1, ["a.py"], [dec1, dec2]) == "dec-2"

    def test_overlap_earlier_not_counted(self):
        dec1 = _make_decision("dec-1", date="2026-04-02", file_paths=["a.py"])
        dec2 = _make_decision("dec-2", date="2026-04-01", file_paths=["a.py"])
        assert _check_re_edited(dec1, ["a.py"], [dec1, dec2]) is None

    def test_empty_files(self):
        dec1 = _make_decision("dec-1", date="2026-04-01")
        dec2 = _make_decision("dec-2", date="2026-04-02", file_paths=["a.py"])
        assert _check_re_edited(dec1, [], [dec1, dec2]) is None


class TestCheckTested:
    def test_passing_tests(self):
        session = _make_session(test_runs=[{"passed": 12, "failed": 0}])
        assert _check_tested(session, ["a.py"]) is True

    def test_failing_tests(self):
        session = _make_session(test_runs=[{"passed": 10, "failed": 2}])
        assert _check_tested(session, ["a.py"]) is False

    def test_no_tests(self):
        session = _make_session(test_runs=[])
        assert _check_tested(session, ["a.py"]) is False

    def test_no_session(self):
        assert _check_tested(None, ["a.py"]) is False


class TestEvaluateDecision:
    def test_committed_and_tested(self, tmp_path):
        existing_file = tmp_path / "a.py"
        existing_file.write_text("pass")
        dec = _make_decision(committed=True, file_paths=[str(existing_file)])
        session = _make_session(test_runs=[{"passed": 5, "failed": 0}])
        result = evaluate_decision(dec, [dec], session_meta=session)
        assert result["verdict"] == "kept"
        assert result["confidence"] == 0.9

    def test_committed_not_tested(self, tmp_path):
        # File must exist on disk or judge thinks it was reverted
        existing_file = tmp_path / "a.py"
        existing_file.write_text("pass")
        dec = _make_decision(committed=True, file_paths=[str(existing_file)])
        session = _make_session(test_runs=[])
        result = evaluate_decision(dec, [dec], session_meta=session)
        assert result["verdict"] == "kept"
        assert result["confidence"] == 0.6

    def test_not_committed(self):
        dec = _make_decision(committed=False, file_paths=[])
        result = evaluate_decision(dec, [dec])
        assert result["verdict"] == "unknown"
        assert result["confidence"] == 0.0

    def test_superseded(self, tmp_path):
        existing_file = tmp_path / "a.py"
        existing_file.write_text("pass")
        fp = str(existing_file)
        dec1 = _make_decision("dec-1", date="2026-04-01", committed=True, file_paths=[fp])
        dec2 = _make_decision("dec-2", date="2026-04-02", committed=True, file_paths=[fp])
        result = evaluate_decision(dec1, [dec1, dec2])
        assert result["verdict"] == "superseded"
        assert result["confidence"] == 0.7

    @patch("personal_mem.synthesis.judge._check_committed_via_git")
    def test_git_reconciliation(self, mock_git, tmp_path):
        """Decision starts as uncommitted but git shows it was committed later."""
        existing_file = tmp_path / "a.py"
        existing_file.write_text("pass")
        fp = str(existing_file)
        mock_git.return_value = {"abc1234": [fp]}
        dec = _make_decision(committed=False, file_paths=[fp])
        result = evaluate_decision(dec, [dec])
        assert result["verdict"] == "kept"
        assert result["confidence"] == 0.6
        assert "abc1234" in result["commit_refs"]
        mock_git.assert_called_once()

    def test_committed_file_removed(self, tmp_path):
        """Decision's file was committed but later deleted."""
        dec = _make_decision(
            committed=True,
            file_paths=[str(tmp_path / "nonexistent.py")],
        )
        result = evaluate_decision(dec, [dec])
        assert result["verdict"] == "reverted"
        assert result["confidence"] == 0.6

    def test_all_verdicts_include_commit_refs(self, tmp_path):
        """Every verdict path must include commit_refs in the result."""
        existing = tmp_path / "a.py"
        existing.write_text("pass")
        fp = str(existing)

        # kept (committed + tested)
        dec = _make_decision(committed=True, file_paths=[fp])
        session = _make_session(test_runs=[{"passed": 5, "failed": 0}])
        assert "commit_refs" in evaluate_decision(dec, [dec], session)

        # kept (committed, not tested)
        assert "commit_refs" in evaluate_decision(dec, [dec])

        # unknown (not committed, no file_paths)
        dec2 = _make_decision(committed=False, file_paths=[])
        assert "commit_refs" in evaluate_decision(dec2, [dec2])

        # superseded
        dec_old = _make_decision("d1", date="2026-04-01", committed=True, file_paths=[fp])
        dec_new = _make_decision("d2", date="2026-04-02", committed=True, file_paths=[fp])
        assert "commit_refs" in evaluate_decision(dec_old, [dec_old, dec_new])

    @patch("personal_mem.synthesis.judge._check_committed_via_git")
    def test_git_reconciliation_stores_multiple_refs(self, mock_git, tmp_path):
        """Judge stores all discovered commit hashes."""
        existing = tmp_path / "a.py"
        existing.write_text("pass")
        fp = str(existing)
        mock_git.return_value = {"aaa1111": [fp], "bbb2222": [fp]}
        dec = _make_decision(committed=False, file_paths=[fp])
        result = evaluate_decision(dec, [dec])
        assert result["commit_refs"] == ["aaa1111", "bbb2222"]

    @patch("personal_mem.synthesis.judge._check_committed_via_git")
    def test_existing_commit_refs_merged(self, mock_git, tmp_path):
        """Existing commit_refs from frontmatter are merged with discovered refs."""
        existing = tmp_path / "a.py"
        existing.write_text("pass")
        fp = str(existing)
        mock_git.return_value = {"new1234": [fp]}
        dec = _make_decision(
            committed=True, file_paths=[fp], commit_refs=["old5678"],
        )
        result = evaluate_decision(dec, [dec])
        assert "old5678" in result["commit_refs"]
        assert "new1234" in result["commit_refs"]

    @patch("personal_mem.synthesis.judge._check_committed_via_git")
    def test_commit_refs_deduped(self, mock_git, tmp_path):
        """Duplicate refs from frontmatter and git discovery are deduplicated."""
        existing = tmp_path / "a.py"
        existing.write_text("pass")
        fp = str(existing)
        mock_git.return_value = {"dup1234": [fp]}
        dec = _make_decision(
            committed=True, file_paths=[fp], commit_refs=["dup1234"],
        )
        result = evaluate_decision(dec, [dec])
        assert result["commit_refs"].count("dup1234") == 1

    def test_judged_at_present_in_all_verdicts(self, tmp_path):
        """Every verdict includes a judged_at ISO timestamp."""
        existing = tmp_path / "a.py"
        existing.write_text("pass")
        # kept path
        dec = _make_decision(committed=True, file_paths=[str(existing)])
        result = evaluate_decision(dec, [dec])
        assert "judged_at" in result
        assert "T" in result["judged_at"]  # ISO format has T separator
        # unknown path
        dec2 = _make_decision(committed=False, file_paths=[])
        assert "judged_at" in evaluate_decision(dec2, [dec2])

    def test_blame_lines_present_in_result(self, tmp_path):
        """Verdict includes blame_lines count."""
        existing = tmp_path / "a.py"
        existing.write_text("pass")
        dec = _make_decision(committed=True, file_paths=[str(existing)])
        result = evaluate_decision(dec, [dec])
        assert "blame_lines" in result
        assert isinstance(result["blame_lines"], int)

    def test_str_shaped_file_paths_and_commit_refs(self, tmp_path):
        """Regression: ``file_paths`` / ``commit_refs`` arriving as a YAML
        scalar (single string instead of list) must not iterate
        char-by-char. The 2026-06-07 ``as_list`` migration coerces scalar
        → ``[scalar]`` at read time; pre-migration this iterated each
        char and produced bogus single-char "file paths" / "commit
        hashes" that downstream git checks then mangled.

        K2-item-5 coverage: the str-shape input is the load-bearing
        contract; without this guard, a decision frontmatter mis-shape
        silently degrades to "no verdict" rather than crashing visibly.
        """
        existing = tmp_path / "a.py"
        existing.write_text("pass")
        fp = str(existing)
        dec = _make_decision(
            committed=True,
            file_paths=fp,            # scalar, not list
            commit_refs="abc1234",    # scalar, not list
        )
        # If as_list isn't applied, file_paths iterates as individual
        # chars (one per character of the absolute path) and the
        # blame/re-edit checks see garbage, but the verdict-shape
        # contract should still hold.
        result = evaluate_decision(dec, [dec])
        assert "verdict" in result
        assert "commit_refs" in result
        # The seed scalar must be preserved (would be stripped to chars
        # without the guard).
        assert "abc1234" in result["commit_refs"]

    @patch("personal_mem.synthesis.judge._check_blame_survival", return_value=15)
    @patch("personal_mem.synthesis.judge._check_committed_via_git", return_value={})
    def test_superseded_with_surviving_lines_becomes_kept(self, mock_git, mock_blame, tmp_path):
        """Decision with surviving blame lines is co-contributor, not superseded."""
        existing = tmp_path / "a.py"
        existing.write_text("pass")
        fp = str(existing)
        dec_old = _make_decision("d1", date="2026-04-01", committed=True, file_paths=[fp])
        dec_new = _make_decision("d2", date="2026-04-02", committed=True, file_paths=[fp])
        result = evaluate_decision(dec_old, [dec_old, dec_new])
        assert result["verdict"] == "kept"
        assert result["confidence"] == 0.5
        assert "15 lines survive" in result["evidence"]
        assert result["blame_lines"] == 15

    @patch("personal_mem.synthesis.judge._check_blame_survival", return_value=0)
    @patch("personal_mem.synthesis.judge._check_committed_via_git", return_value={})
    def test_superseded_with_zero_lines_stays_superseded(self, mock_git, mock_blame, tmp_path):
        """Decision with zero surviving lines is truly superseded."""
        existing = tmp_path / "a.py"
        existing.write_text("pass")
        fp = str(existing)
        dec_old = _make_decision("d1", date="2026-04-01", committed=True, file_paths=[fp])
        dec_new = _make_decision("d2", date="2026-04-02", committed=True, file_paths=[fp])
        result = evaluate_decision(dec_old, [dec_old, dec_new])
        assert result["verdict"] == "superseded"
        assert result["confidence"] == 0.7


class TestCheckBlameSurvival:
    @patch("personal_mem.synthesis.judge.subprocess.run")
    def test_counts_matching_lines(self, mock_run, tmp_path):
        existing = tmp_path / "a.py"
        existing.write_text("x = 1\ny = 2\n")
        # Porcelain format: hash lines, then \t-prefixed content
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = (
            "abc1234def5678901234567890123456789012 1 1 1\n"
            "author Test\n"
            "\tx = 1\n"
            "abc1234def5678901234567890123456789012 2 2 1\n"
            "author Test\n"
            "\ty = 2\n"
        )
        result = _check_blame_survival([str(existing)], ["abc1234"])
        assert result == 2

    @patch("personal_mem.synthesis.judge.subprocess.run")
    def test_no_matching_lines(self, mock_run, tmp_path):
        existing = tmp_path / "a.py"
        existing.write_text("z = 3\n")
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = (
            "fff9999aaa1111222233334444555566667777 1 1 1\n"
            "\tz = 3\n"
        )
        result = _check_blame_survival([str(existing)], ["abc1234"])
        assert result == 0

    def test_returns_negative_for_missing_file(self):
        result = _check_blame_survival(["/nonexistent/path.py"], ["abc1234"])
        assert result == -1

    def test_returns_negative_for_empty_inputs(self):
        assert _check_blame_survival([], ["abc1234"]) == -1
        assert _check_blame_survival(["/some/file.py"], []) == -1

    @patch("personal_mem.synthesis.judge.subprocess.run")
    def test_handles_subprocess_error(self, mock_run, tmp_path):
        existing = tmp_path / "a.py"
        existing.write_text("pass")
        mock_run.side_effect = FileNotFoundError("git not found")
        result = _check_blame_survival([str(existing)], ["abc1234"])
        assert result == -1

    @patch("personal_mem.synthesis.judge.subprocess.run")
    def test_narrows_blame_to_relevant_files(self, mock_run, tmp_path):
        """With hash_to_files, blame only checks files that the commit touched."""
        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text("x = 1\n")
        b.write_text("y = 2\n")

        # abc only touched a.py, def only touched b.py
        hash_to_files = {"abc1234": [str(a)], "def5678": [str(b)]}

        def fake_blame(args, **kwargs):
            fp = args[-1]
            m = type("R", (), {"returncode": 0, "stdout": ""})()
            if fp == str(a):
                m.stdout = "abc1234aaa1111222233334444555566667777 1 1 1\n\tx = 1\n"
            elif fp == str(b):
                m.stdout = "def5678bbb1111222233334444555566667777 1 1 1\n\ty = 2\n"
            return m

        mock_run.side_effect = fake_blame
        # Both files, both refs, but narrowed by hash_to_files
        result = _check_blame_survival(
            [str(a), str(b)], ["abc1234", "def5678"], hash_to_files,
        )
        assert result == 2  # 1 line from a (abc) + 1 line from b (def)

    @patch("personal_mem.synthesis.judge.subprocess.run")
    def test_narrowing_skips_unrelated_file(self, mock_run, tmp_path):
        """Commit that didn't touch a file shouldn't count blame lines in it."""
        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text("x = 1\n")
        b.write_text("y = 2\n")

        # abc only touched a.py — b.py blame should be skipped entirely
        hash_to_files = {"abc1234": [str(a)]}

        def fake_blame(args, **kwargs):
            m = type("R", (), {"returncode": 0, "stdout": ""})()
            m.stdout = "abc1234aaa1111222233334444555566667777 1 1 1\n\tx = 1\n"
            return m

        mock_run.side_effect = fake_blame
        result = _check_blame_survival(
            [str(a), str(b)], ["abc1234"], hash_to_files,
        )
        # Only a.py counted, b.py skipped because abc1234 didn't touch it
        assert result == 1


class TestBatchedGitLog:
    """Regression for P0-9: single git log call, not per-file fanout.

    _check_committed_via_git must issue ONE subprocess call regardless
    of how many files the decision touches. Old impl looped over
    file_paths and ran `git log` per file (45s worst case for 3 files
    with 5+10s timeouts).
    """

    @patch("personal_mem.synthesis.judge.subprocess.run")
    def test_single_subprocess_call_for_multiple_files(self, mock_run):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = (
            "abc1234\n"
            "src/a.py\n"
            "src/b.py\n"
            "\n"
            "def5678\n"
            "src/c.py\n"
        )
        from personal_mem.synthesis.judge import _check_committed_via_git

        result = _check_committed_via_git(
            ["src/a.py", "src/b.py", "src/c.py"],
            "2026-04-01",
        )
        assert mock_run.call_count == 1, (
            f"expected exactly 1 subprocess call, got {mock_run.call_count}"
        )
        # First (and only) call: a single git log invocation, not per-file
        args = mock_run.call_args[0][0]
        assert args[0:2] == ["git", "log"]
        assert "--name-only" in args
        assert any(a.startswith("--since=") for a in args)
        # No `--` separator with file paths appended (no per-file scoping)
        assert "--" not in args

    @patch("personal_mem.synthesis.judge.subprocess.run")
    def test_parses_hash_to_files_map(self, mock_run):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = (
            "abc1234\n"
            "src/a.py\n"
            "src/b.py\n"
            "\n"
            "def5678\n"
            "src/b.py\n"
        )
        from personal_mem.synthesis.judge import _check_committed_via_git
        result = _check_committed_via_git(
            ["src/a.py", "src/b.py"], "2026-04-01",
        )
        # abc1234 touched both a and b; def5678 touched only b
        assert "abc1234" in result
        assert set(result["abc1234"]) == {"src/a.py", "src/b.py"}
        assert "def5678" in result
        assert result["def5678"] == ["src/b.py"]

    @patch("personal_mem.synthesis.judge.subprocess.run")
    def test_filters_to_target_files(self, mock_run):
        """Commit hashes with no overlap into file_paths should be dropped."""
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = (
            "abc1234\n"
            "src/a.py\n"
            "\n"
            "def5678\n"
            "unrelated/x.py\n"
        )
        from personal_mem.synthesis.judge import _check_committed_via_git
        result = _check_committed_via_git(["src/a.py"], "2026-04-01")
        assert "abc1234" in result
        # def5678 didn't touch any file in target set — excluded
        assert "def5678" not in result

    @patch("personal_mem.synthesis.judge.subprocess.run")
    def test_empty_inputs_no_subprocess(self, mock_run):
        from personal_mem.synthesis.judge import _check_committed_via_git
        # No file_paths → no subprocess call needed
        assert _check_committed_via_git([], "2026-04-01") == {}
        assert mock_run.call_count == 0
        # No since_date → no subprocess call
        assert _check_committed_via_git(["a.py"], "") == {}
        assert mock_run.call_count == 0

    @patch("personal_mem.synthesis.judge.subprocess.run")
    def test_handles_subprocess_failure(self, mock_run):
        from personal_mem.synthesis.judge import _check_committed_via_git
        mock_run.side_effect = FileNotFoundError("git not found")
        assert _check_committed_via_git(["a.py"], "2026-04-01") == {}

    @patch("personal_mem.synthesis.judge.subprocess.run")
    def test_handles_timeout(self, mock_run):
        from personal_mem.synthesis.judge import _check_committed_via_git
        import subprocess as _sp
        mock_run.side_effect = _sp.TimeoutExpired("git", 15)
        assert _check_committed_via_git(["a.py"], "2026-04-01") == {}


class TestBlameSkipWhenUncommitted:
    """Regression for P0-9: skip blame when decision is uncommitted.

    `_check_blame_survival` is expensive (per-file git blame). When the
    decision isn't committed there's nothing to attribute blame against,
    so we must not invoke it at all.
    """

    @patch("personal_mem.synthesis.judge._check_blame_survival")
    @patch("personal_mem.synthesis.judge._check_committed_via_git", return_value={})
    def test_uncommitted_skips_blame(self, mock_git, mock_blame):
        """Uncommitted decision must NOT trigger _check_blame_survival."""
        dec = _make_decision(committed=False, file_paths=["a.py"])
        result = evaluate_decision(dec, [dec])
        assert result["verdict"] == "unknown"
        # The whole point: blame must not be invoked.
        mock_blame.assert_not_called()
        assert result["blame_lines"] == -1

    @patch("personal_mem.synthesis.judge._check_blame_survival", return_value=3)
    @patch("personal_mem.synthesis.judge._check_committed_via_git", return_value={})
    def test_committed_still_calls_blame(self, mock_git, mock_blame, tmp_path):
        """Committed decisions still pay the blame cost — that's the design."""
        existing = tmp_path / "a.py"
        existing.write_text("pass")
        dec = _make_decision(committed=True, file_paths=[str(existing)])
        result = evaluate_decision(dec, [dec])
        mock_blame.assert_called_once()
        assert result["blame_lines"] == 3


class TestJudgeSubprocessCount:
    """Regression for P0-9: 3 decisions × 3 files should issue ≤ 3 git log
    calls (one per decision), not 9 (one per file × decision)."""

    @patch("personal_mem.synthesis.judge.subprocess.run")
    def test_three_decisions_three_files_each_uses_one_git_call_per_decision(
        self, mock_run, tmp_path,
    ):
        # Make blame return empty so we don't fan out into blame too.
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""  # no commits found
        files = []
        for name in ("a.py", "b.py", "c.py"):
            p = tmp_path / name
            p.write_text("pass")
            files.append(str(p))

        decisions = [
            _make_decision(f"d{i}", committed=True, file_paths=files)
            for i in range(3)
        ]
        for d in decisions:
            evaluate_decision(d, decisions)

        # 3 decisions × 1 git-log call each = 3.
        # Blame is skipped because no commit_refs produced (committed=True
        # in frontmatter, but git log returned nothing → commit_refs=[]).
        # _check_blame_survival is still called (committed=True) but its
        # internal early-return on empty refs costs zero subprocesses.
        assert mock_run.call_count == 3, (
            f"expected exactly 3 subprocess calls (1/decision), got {mock_run.call_count}"
        )


class TestFindDecisions:
    """Regression for n-a5a38892: session-scoped lookup previously walked the
    filesystem with `list_notes(limit=100)` and silently missed decisions
    past the limit or those only tagged via `derived_from`. The indexed
    lookup must find decisions by either frontmatter field, with no limit.
    """

    def _setup(self, tmp_path):
        cfg = Config(vault_root=tmp_path / "vault")
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        idx = Indexer(config=cfg)
        return cfg, vm, idx

    def _make_session(self, vm, idx, sid_label="ses-x"):
        ses_path = vm.create_note(
            note_type=NoteType.SESSION,
            title=sid_label,
            body="## Summary\n",
            project="p",
            extra_frontmatter={"source_session": sid_label},
        )
        idx.index_file(ses_path)
        return vm.read_note(ses_path).id

    def test_finds_by_source_session(self, tmp_path):
        _, vm, idx = self._setup(tmp_path)
        sid = self._make_session(vm, idx)
        ses_dir = next((vm.root / "projects/p/sessions").iterdir())
        dec_path = vm.create_note(
            note_type=NoteType.DECISION,
            title="FindMe-SourceSession",
            body="## Context\n\nX\n\n## Decision\n\nY",
            project="p",
            extra_frontmatter={
                "source_session": sid,
                "status": "accepted",
                "committed": True,
            },
            output_dir=ses_dir,
        )
        idx.index_file(dec_path)

        found = find_decisions(idx.db, vm, session_id=sid)
        idx.close()
        assert any(d.title == "FindMe-SourceSession" for d in found)

    def test_finds_by_derived_from(self, tmp_path):
        _, vm, idx = self._setup(tmp_path)
        sid = self._make_session(vm, idx)
        ses_dir = next((vm.root / "projects/p/sessions").iterdir())
        dec_path = vm.create_note(
            note_type=NoteType.DECISION,
            title="FindMe-DerivedFrom",
            body="## Context\n\nX\n\n## Decision\n\nY",
            project="p",
            extra_frontmatter={
                "derived_from": [sid],
                "status": "accepted",
                "committed": True,
            },
            output_dir=ses_dir,
        )
        idx.index_file(dec_path)

        found = find_decisions(idx.db, vm, session_id=sid)
        idx.close()
        assert any(d.title == "FindMe-DerivedFrom" for d in found)

    def test_ignores_non_matching_sessions(self, tmp_path):
        _, vm, idx = self._setup(tmp_path)
        sid_a = self._make_session(vm, idx, sid_label="ses-a")
        sid_b = self._make_session(vm, idx, sid_label="ses-b")

        ses_a_dir = (vm.root / "projects/p/sessions/ses-a")
        dec_path = vm.create_note(
            note_type=NoteType.DECISION,
            title="OnlyA",
            body="## Context\n\nX\n\n## Decision\n\nY",
            project="p",
            extra_frontmatter={
                "source_session": sid_a,
                "status": "accepted",
            },
            output_dir=ses_a_dir,
        )
        idx.index_file(dec_path)

        found_b = find_decisions(idx.db, vm, session_id=sid_b)
        idx.close()
        assert all(d.title != "OnlyA" for d in found_b)

    def test_no_limit_past_100_decisions(self, tmp_path):
        """Old implementation truncated at list_notes(limit=100)."""
        _, vm, idx = self._setup(tmp_path)
        sid = self._make_session(vm, idx)
        ses_dir = next((vm.root / "projects/p/sessions").iterdir())

        for i in range(120):
            p = vm.create_note(
                note_type=NoteType.DECISION,
                title=f"bulk-{i}",
                body="## Context\n\nX\n\n## Decision\n\nY",
                project="p",
                extra_frontmatter={
                    "source_session": sid,
                    "status": "accepted",
                },
                output_dir=ses_dir,
            )
            idx.index_file(p)

        found = find_decisions(idx.db, vm, session_id=sid)
        idx.close()
        assert len(found) == 120
