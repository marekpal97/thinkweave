"""Tests for Claude Code hook handler and installer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from personal_mem.config import Config
from personal_mem.hooks.handler import (
    _buffer_event,
    _build_auto_summary,
    _build_event,
    _detect_project,
    _diff_context,
    _extract_insight_blocks,
    _first_meaningful_line,
    _get_commit_files,
    _is_git_commit,
    _is_internal,
    _is_significant_command,
    _is_test_command,
    _log_error,
    _parse_commit_from_output,
    _parse_push_branch,
    _parse_test_result,
    _read_buffer,
    _summarize_events,
    archive_buffer,
    cleanup_buffer,
)
from personal_mem.hooks.install import install_hooks, uninstall_hooks


class TestHookHelpers:
    def test_is_internal(self):
        assert _is_internal("/home/user/.claude/settings.json")
        assert _is_internal("/project/CLAUDE.md")
        assert _is_internal("/project/.mem/index.db")
        assert not _is_internal("/project/src/main.py")
        assert not _is_internal("/project/README.md")

    def test_is_significant_command(self):
        assert _is_significant_command("git commit -m 'fix'")
        assert _is_significant_command("pytest tests/")
        assert _is_significant_command("python3 script.py")
        assert _is_significant_command("uv run pytest")
        assert not _is_significant_command("ls -la")
        assert not _is_significant_command("cat file.txt")
        assert not _is_significant_command("echo hello")

    def test_detect_project(self, tmp_path: Path):
        # Env var takes priority
        with patch.dict("os.environ", {"PERSONAL_MEM_PROJECT": "from-env"}):
            assert _detect_project({"cwd": "/anywhere"}) == "from-env"

        # Git repo detection: walk up to .git
        repo = tmp_path / "my_project" / "src" / "pkg"
        repo.mkdir(parents=True)
        (tmp_path / "my_project" / ".git").mkdir()
        assert _detect_project({"cwd": str(repo)}) == "my_project"

        # Fallback to cwd directory name
        no_git = tmp_path / "random_dir"
        no_git.mkdir()
        assert _detect_project({"cwd": str(no_git)}) == "random_dir"

    def test_extract_insight_blocks(self):
        text = """Some text before.
★ Insight ─────────────────────────────────────
Key point 1: FTS5 is fast.
Key point 2: WAL enables concurrency.
─────────────────────────────────────────────────
Some text after."""
        insights = _extract_insight_blocks(text)
        assert len(insights) == 1
        assert "FTS5 is fast" in insights[0]
        assert "WAL enables concurrency" in insights[0]

    def test_extract_no_insights(self):
        assert _extract_insight_blocks("No insights here.") == []


class TestHookInstaller:
    def test_install_fresh(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))

        settings_path = project_dir / ".claude" / "settings.local.json"
        assert settings_path.exists()

        settings = json.loads(settings_path.read_text())
        assert "hooks" in settings
        assert "PreToolUse" in settings["hooks"]
        assert "PostToolUse" in settings["hooks"]

        # Verify hook structure
        pre = settings["hooks"]["PreToolUse"][0]
        assert pre["matcher"] == "Write|Edit"
        assert pre["hooks"][0]["timeout"] == 5

        post = settings["hooks"]["PostToolUse"][0]
        assert post["matcher"] == "Write|Edit|Bash"

    def test_install_preserves_existing(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)

        # Pre-existing settings
        existing = {
            "permissions": {"allow": ["Bash(ls)"]},
            "hooks": {
                "PreToolUse": [
                    {"matcher": "SomeOther", "hooks": [{"type": "command", "command": "echo hi"}]}
                ]
            },
        }
        (claude_dir / "settings.local.json").write_text(json.dumps(existing))

        install_hooks(project_dir=str(project_dir))

        settings = json.loads((claude_dir / "settings.local.json").read_text())
        # Existing permission preserved
        assert "Bash(ls)" in settings["permissions"]["allow"]
        # Existing hook preserved
        assert len(settings["hooks"]["PreToolUse"]) == 2
        # Our hook added
        assert any("personal_mem" in str(entry) for entry in settings["hooks"]["PreToolUse"])

    def test_install_idempotent(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))
        install_hooks(project_dir=str(project_dir))

        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        # Should not duplicate
        assert len(settings["hooks"]["PreToolUse"]) == 1
        assert len(settings["hooks"]["PostToolUse"]) == 1

    def test_install_includes_stop_hook(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))

        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        assert "Stop" in settings["hooks"]
        stop = settings["hooks"]["Stop"][0]
        assert "stop" in stop["hooks"][0]["command"]

    def test_install_includes_session_start_hook(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))

        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        assert "SessionStart" in settings["hooks"]
        ss = settings["hooks"]["SessionStart"][0]
        assert "session_start" in ss["hooks"][0]["command"]
        # No matcher is used for SessionStart
        assert ss["matcher"] == ""

    def test_uninstall(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))
        uninstall_hooks(project_dir=str(project_dir))

        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        assert "hooks" not in settings

    def test_uninstall_removes_session_start(self, tmp_path: Path):
        """SessionStart must be round-trippable like every other hook type."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        install_hooks(project_dir=str(project_dir))

        # Sanity — installed
        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        assert "SessionStart" in settings["hooks"]

        uninstall_hooks(project_dir=str(project_dir))
        settings = json.loads(
            (project_dir / ".claude" / "settings.local.json").read_text()
        )
        # Either the whole hooks dict is gone or SessionStart specifically is empty/missing
        assert "hooks" not in settings or "SessionStart" not in settings.get("hooks", {})


class TestBuildEventEnrichment:
    """Tests for _build_event metadata enrichment (commit, test, insights)."""

    def test_bash_with_commit(self):
        output = "[main abc1234] Fix bug\n 2 files changed\n"
        event = _build_event("Bash", {"command": "git commit -m 'Fix bug'"}, output, "14:00")
        assert event is not None
        assert "commit" in event
        assert event["commit"]["hash"] == "abc1234"

    def test_bash_with_test_result(self):
        output = "====== 12 passed in 1.5s ======"
        event = _build_event("Bash", {"command": "uv run pytest"}, output, "14:00")
        assert event is not None
        assert "test_run" in event
        assert event["test_run"]["passed"] == 12

    def test_bash_with_git_push(self):
        event = _build_event("Bash", {"command": "git push origin feature/x"}, "", "14:00")
        assert event is not None
        assert event["git_branch"] == "feature/x"

    def test_insight_captured_in_event(self):
        output = "Some text\n★ Insight ─────────────────────────────────────\nKey point here.\n─────────────────────────────────────────────────\n"
        event = _build_event("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"}, output, "14:00")
        assert event is not None
        assert "insights" in event
        assert "Key point here." in event["insights"][0]

    def test_no_insight_no_field(self):
        event = _build_event("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"}, "", "14:00")
        assert event is not None
        assert "insights" not in event


class TestDiffContext:
    def test_edit_context(self):
        ctx = _diff_context("Edit", {"old_string": "foo = 1", "new_string": "foo = 2"})
        assert "foo = 1" in ctx
        assert "foo = 2" in ctx
        assert "→" in ctx

    def test_edit_truncates_long_strings(self):
        ctx = _diff_context("Edit", {"old_string": "x" * 200, "new_string": "y" * 200})
        # Should truncate to ~80 chars each
        assert len(ctx) < 250

    def test_write_context(self):
        ctx = _diff_context("Write", {"content": "def main():\n    pass\n"})
        assert "def main():" in ctx

    def test_write_skips_comments(self):
        ctx = _diff_context("Write", {"content": "# comment\n# another\ndef real():\n"})
        assert "def real():" in ctx

    def test_empty_input(self):
        assert _diff_context("Edit", {}) == ""
        assert _diff_context("Write", {}) == ""


class TestFirstMeaningfulLine:
    def test_skips_blanks_and_comments(self):
        assert _first_meaningful_line("\n\n# comment\n//js comment\nactual code") == "actual code"

    def test_empty(self):
        assert _first_meaningful_line("") == ""
        assert _first_meaningful_line("\n\n\n") == ""


class TestGitCommitDetection:
    def test_is_git_commit(self):
        assert _is_git_commit("git commit -m 'fix'")
        assert _is_git_commit("git commit -am 'fix'")
        assert not _is_git_commit("git commit --amend")
        assert not _is_git_commit("git log")
        assert not _is_git_commit("echo git commit")

    def test_parse_commit_output(self):
        output = '[main abc1234] Fix parser bug\n 2 files changed, 15 insertions(+), 3 deletions(-)\n'
        result = _parse_commit_from_output("git commit -m 'Fix parser bug'", output)
        assert result is not None
        assert result["hash"] == "abc1234"
        assert result["message"] == "Fix parser bug"
        assert result["files_changed"] == 2

    def test_parse_commit_from_output_line(self):
        output = '[feature/x 1a2b3c4] Refactor module\n 1 file changed, 5 insertions(+)\n'
        result = _parse_commit_from_output("git commit", output)
        assert result is not None
        assert result["hash"] == "1a2b3c4"
        assert "Refactor module" in result["message"]

    def test_parse_commit_empty_output(self):
        assert _parse_commit_from_output("git commit", "") is None


class TestGetCommitFiles:
    @patch("personal_mem.hooks.handler.subprocess.run")
    def test_returns_file_list(self, mock_run):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "src/a.py\nsrc/b.py\n"
        result = _get_commit_files("abc1234")
        assert result == ["src/a.py", "src/b.py"]
        mock_run.assert_called_once()

    @patch("personal_mem.hooks.handler.subprocess.run")
    def test_returns_empty_on_error(self, mock_run):
        mock_run.side_effect = FileNotFoundError("git not found")
        assert _get_commit_files("abc1234") == []

    @patch("personal_mem.hooks.handler.subprocess.run")
    def test_returns_empty_on_timeout(self, mock_run):
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired("git", 5)
        assert _get_commit_files("abc1234") == []

    @patch("personal_mem.hooks.handler.subprocess.run")
    def test_returns_empty_on_nonzero_exit(self, mock_run):
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        assert _get_commit_files("abc1234") == []

    @patch("personal_mem.hooks.handler.subprocess.run")
    def test_filters_blank_lines(self, mock_run):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "src/a.py\n\n  \nsrc/b.py\n"
        result = _get_commit_files("abc1234")
        assert result == ["src/a.py", "src/b.py"]


class TestBuildEventCommitFiles:
    @patch("personal_mem.hooks.handler._get_commit_files", return_value=["src/a.py", "src/b.py"])
    def test_commit_event_includes_files(self, mock_files):
        output = "[main abc1234] Fix bug\n 2 files changed\n"
        event = _build_event("Bash", {"command": "git commit -m 'Fix bug'"}, output, "14:00")
        assert event["commit"]["files"] == ["src/a.py", "src/b.py"]

    @patch("personal_mem.hooks.handler._get_commit_files", return_value=[])
    def test_commit_event_no_files_key_when_empty(self, mock_files):
        output = "[main abc1234] Fix bug\n 2 files changed\n"
        event = _build_event("Bash", {"command": "git commit -m 'Fix bug'"}, output, "14:00")
        assert "files" not in event["commit"]


class TestTestDetection:
    def test_is_test_command(self):
        assert _is_test_command("pytest tests/")
        assert _is_test_command("uv run pytest -x")
        assert _is_test_command("python -m pytest")
        assert not _is_test_command("python script.py")
        assert not _is_test_command("echo pytest")

    def test_is_test_command_chained(self):
        """Chained commands like 'cd foo && uv run pytest' should be detected."""
        assert _is_test_command("cd src && uv run pytest -x")
        assert _is_test_command("export FOO=1 && pytest tests/")
        assert _is_test_command("cd /tmp; pytest")
        assert _is_test_command("echo setup || uv run pytest --tb=short")
        assert not _is_test_command("cd src && echo pytest")

    def test_parse_test_result_passed(self):
        output = "====== 12 passed in 1.5s ======"
        result = _parse_test_result("uv run pytest", output)
        assert result is not None
        assert result["passed"] == 12
        assert "failed" not in result

    def test_parse_test_result_mixed(self):
        output = "====== 10 passed, 2 failed, 1 error in 3.2s ======"
        result = _parse_test_result("pytest", output)
        assert result is not None
        assert result["passed"] == 10
        assert result["failed"] == 2
        assert result["errors"] == 1

    def test_parse_test_result_empty(self):
        assert _parse_test_result("pytest", "") is None


class TestPushBranch:
    def test_git_push_with_remote_and_branch(self):
        assert _parse_push_branch("git push origin feature/x") == "feature/x"

    def test_git_push_with_remote_only(self):
        assert _parse_push_branch("git push origin") == "origin"

    def test_git_push_with_flags(self):
        assert _parse_push_branch("git push -u origin main") == "main"

    def test_not_a_push(self):
        assert _parse_push_branch("git pull origin main") is None


class TestEventBuffer:
    def test_buffer_event_creates_file(self, tmp_path):
        _buffer_event(tmp_path, "ses-test", {"ts": "14:00", "tool": "Edit", "file": "a.py"})
        buf_file = tmp_path / "buffer" / "ses-test.jsonl"
        assert buf_file.exists()
        lines = buf_file.read_text().splitlines()
        assert len(lines) == 1
        assert '"Edit"' in lines[0]

    def test_buffer_appends(self, tmp_path):
        _buffer_event(tmp_path, "ses-test", {"ts": "14:00", "tool": "Edit", "file": "a.py"})
        _buffer_event(tmp_path, "ses-test", {"ts": "14:05", "tool": "Write", "file": "b.py"})
        lines = (tmp_path / "buffer" / "ses-test.jsonl").read_text().splitlines()
        assert len(lines) == 2

    def test_build_event_edit(self):
        event = _build_event("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"}, "", "14:00")
        assert event is not None
        assert event["tool"] == "Edit"
        assert event["file"] == "a.py"

    def test_build_event_skips_internal(self):
        event = _build_event("Edit", {"file_path": ".claude/settings.json"}, "", "14:00")
        assert event is None

    def test_build_event_bash_significant(self):
        event = _build_event("Bash", {"command": "git commit -m 'fix'"}, "", "14:00")
        assert event is not None
        assert event["tool"] == "Bash"

    def test_build_event_bash_insignificant(self):
        event = _build_event("Bash", {"command": "ls -la"}, "", "14:00")
        assert event is None

    def test_cleanup_buffer(self, tmp_path):
        buf_dir = tmp_path / "buffer"
        buf_dir.mkdir()
        buf_file = buf_dir / "ses-test.jsonl"
        buf_file.write_text('{"ts":"14:00"}\n')
        cleanup_buffer(tmp_path, "ses-test")
        assert not buf_file.exists()

    def test_cleanup_buffer_missing_file(self, tmp_path):
        # Should not raise
        cleanup_buffer(tmp_path, "nonexistent")

    def test_archive_buffer(self, tmp_path):
        buf_dir = tmp_path / "buffer"
        buf_dir.mkdir()
        buf_file = buf_dir / "ses-test.jsonl"
        buf_file.write_text('{"ts":"14:00"}\n')

        session_dir = tmp_path / "session_dir"
        session_dir.mkdir()

        archive_buffer(tmp_path, "ses-test", session_dir)

        assert not buf_file.exists()
        assert (session_dir / "events.jsonl").exists()
        assert '{"ts":"14:00"}' in (session_dir / "events.jsonl").read_text()

    def test_archive_buffer_missing_file(self, tmp_path):
        session_dir = tmp_path / "session_dir"
        session_dir.mkdir()
        # Should not raise
        archive_buffer(tmp_path, "nonexistent", session_dir)


class TestReadBuffer:
    def test_reads_events(self, tmp_path):
        buf_dir = tmp_path / "buffer"
        buf_dir.mkdir()
        (buf_dir / "ses-test.jsonl").write_text(
            '{"ts":"14:00","tool":"Edit","file":"a.py"}\n'
            '{"ts":"14:05","tool":"Bash","command":"pytest"}\n'
        )
        events = _read_buffer(tmp_path, "ses-test")
        assert len(events) == 2
        assert events[0]["tool"] == "Edit"
        assert events[1]["tool"] == "Bash"

    def test_missing_buffer(self, tmp_path):
        assert _read_buffer(tmp_path, "nonexistent") == []

    def test_skips_bad_json(self, tmp_path):
        buf_dir = tmp_path / "buffer"
        buf_dir.mkdir()
        (buf_dir / "ses-test.jsonl").write_text(
            '{"ts":"14:00","tool":"Edit"}\n'
            'not valid json\n'
            '{"ts":"14:05","tool":"Bash"}\n'
        )
        events = _read_buffer(tmp_path, "ses-test")
        assert len(events) == 2


class TestSummarizeEvents:
    def test_extracts_files(self):
        events = [
            {"ts": "14:00", "tool": "Edit", "file": "a.py"},
            {"ts": "14:05", "tool": "Edit", "file": "b.py"},
            {"ts": "14:10", "tool": "Edit", "file": "a.py"},  # duplicate
        ]
        meta = _summarize_events(events)
        assert meta["files_touched"] == ["a.py", "b.py"]  # deduped, order preserved

    def test_extracts_commits(self):
        events = [
            {"ts": "14:00", "tool": "Bash", "command": "git commit", "commit": {"hash": "abc", "message": "fix"}},
        ]
        meta = _summarize_events(events)
        assert len(meta["commits"]) == 1
        assert meta["commits"][0]["hash"] == "abc"

    def test_extracts_test_runs(self):
        events = [
            {"ts": "14:00", "tool": "Bash", "command": "pytest", "test_run": {"passed": 10, "failed": 0}},
        ]
        meta = _summarize_events(events)
        assert meta["test_runs"][0]["passed"] == 10

    def test_extracts_insights(self):
        events = [
            {"ts": "14:00", "tool": "Edit", "file": "a.py", "insights": ["Insight A"]},
            {"ts": "14:05", "tool": "Edit", "file": "b.py", "insights": ["Insight B", "Insight C"]},
        ]
        meta = _summarize_events(events)
        assert meta["insights"] == ["Insight A", "Insight B", "Insight C"]

    def test_extracts_git_branch(self):
        events = [
            {"ts": "14:00", "tool": "Bash", "command": "git push", "git_branch": "feature/x"},
        ]
        meta = _summarize_events(events)
        assert meta["git_branch"] == "feature/x"

    def test_empty_events(self):
        meta = _summarize_events([])
        assert meta["files_touched"] == []
        assert meta["commits"] == []
        assert meta["insights"] == []


class TestHookErrorLogging:
    def test_log_error_creates_file(self, tmp_path):
        cfg = Config(vault_root=tmp_path / "vault")
        with patch("personal_mem.config.load_config", return_value=cfg):
            _log_error("test_hook", ValueError("test error"))

        log_path = cfg.mem_dir / "hooks.log"
        assert log_path.exists()
        content = log_path.read_text()
        assert "test_hook" in content
        assert "test error" in content

    def test_log_error_appends(self, tmp_path):
        cfg = Config(vault_root=tmp_path / "vault")
        with patch("personal_mem.config.load_config", return_value=cfg):
            _log_error("hook1", ValueError("error1"))
            _log_error("hook2", RuntimeError("error2"))

        content = (cfg.mem_dir / "hooks.log").read_text()
        assert "error1" in content
        assert "error2" in content

    def test_log_error_never_raises(self):
        # Even if everything fails inside, _log_error should not raise
        # Force a failure by making the import path invalid
        with patch.dict("sys.modules", {"personal_mem.config": None}):
            _log_error("test", ValueError("err"))  # Should not raise


class TestBuildAutoSummary:
    def test_with_files(self):
        s = _build_auto_summary(["a.py", "b.py"], [], [], 5)
        assert "Edited 2 files" in s
        assert "a.py" in s

    def test_with_commits(self):
        s = _build_auto_summary([], [{"message": "Fix bug"}], [], 1)
        assert "Commits:" in s
        assert "Fix bug" in s

    def test_with_tests(self):
        s = _build_auto_summary([], [], [{"passed": 12, "failed": 1}], 1)
        assert "12 passed" in s
        assert "1 failed" in s

    def test_fallback_event_count(self):
        s = _build_auto_summary([], [], [], 7)
        assert "7 tool events recorded" in s

    def test_many_files_truncated(self):
        files = [f"file{i}.py" for i in range(10)]
        s = _build_auto_summary(files, [], [], 10)
        assert "+5 more" in s


class TestSessionStartHandler:
    """End-to-end: run _handle_session_start with a fresh vault and inspect stdout."""

    def _run(self, vault_dir: Path, project: str, monkeypatch) -> dict:
        """Invoke _handle_session_start with a stubbed config + project, capture stdout."""
        import io

        from personal_mem.config import Config
        from personal_mem.hooks import handler as handler_mod

        cfg = Config(vault_root=vault_dir)
        # Force our config through load_config so the handler uses the tmp vault
        monkeypatch.setattr(
            "personal_mem.config.load_config", lambda: cfg
        )
        monkeypatch.setattr(
            "personal_mem.hooks.handler._detect_project",
            lambda hook_input: project,
        )

        buf = io.StringIO()
        monkeypatch.setattr("sys.stdout", buf)
        handler_mod._handle_session_start({"session_id": "cc-test", "cwd": str(vault_dir)})
        return json.loads(buf.getvalue() or "{}")

    def test_empty_vault_emits_valid_response(self, tmp_path: Path, monkeypatch):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        (vault_dir / ".mem").mkdir()

        result = self._run(vault_dir, "ghost", monkeypatch)
        # Either empty (no payload) or contains hookSpecificOutput
        if result:
            assert "hookSpecificOutput" in result
            assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
            assert "additionalContext" in result["hookSpecificOutput"]

    def test_populated_vault_emits_additional_context(
        self, tmp_path: Path, monkeypatch
    ):
        from personal_mem.indexer import Indexer
        from personal_mem.schemas import NoteType
        from personal_mem.vault import VaultManager
        from personal_mem.config import Config

        vault_dir = tmp_path / "vault"
        cfg = Config(vault_root=vault_dir)
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()

        vm.create_note(
            NoteType.SESSION,
            "Populated session",
            body=(
                "## Summary\n"
                "We built SessionStart.\n"
                "\n## Candidate Insights\n"
                "\n- **An insight title** body\n"
            ),
            project="alpha",
            extra_frontmatter={
                "processed": True,
                "processed_at": "2026-04-07",
                "source_session": "cc-alpha",
            },
        )
        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()

        result = self._run(vault_dir, "alpha", monkeypatch)
        assert "hookSpecificOutput" in result
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Populated session" in ctx
        assert "An insight title" in ctx
        assert "## Header" in ctx
        assert "## Available MCP Tools" in ctx

    def test_failure_in_payload_does_not_block(self, tmp_path: Path, monkeypatch):
        """Hook must always exit cleanly, even if the payload builder raises."""
        import io

        from personal_mem.hooks import handler as handler_mod

        def boom(*args, **kwargs):
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(
            "personal_mem.context.build_project_context", boom
        )
        buf = io.StringIO()
        monkeypatch.setattr("sys.stdout", buf)

        # Should not raise
        handler_mod._handle_session_start(
            {"session_id": "cc-test", "cwd": str(tmp_path)}
        )
        # Stdout should still be valid JSON (empty dict)
        result = json.loads(buf.getvalue() or "{}")
        assert isinstance(result, dict)
