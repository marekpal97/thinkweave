"""Tests for the CC-seed importer's cwd → project normalization.

Focus: the path parsing must be separator-agnostic so a Windows-originated
session cwd (``C:\\Users\\x\\repo``) and a POSIX one normalize identically,
regardless of which OS runs the import.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thinkweave.onboarding.claude_code_seed import (
    UNSCOPED_PROJECT,
    normalize_project,
)


class TestNormalizeProjectPosix:
    def test_simple_repo(self):
        assert normalize_project("/home/u/projects/myrepo") == "myrepo"

    def test_trailing_slash(self):
        assert normalize_project("/home/u/projects/myrepo/") == "myrepo"

    def test_dash_to_underscore(self):
        assert normalize_project("/home/u/My-Cool-Repo") == "my_cool_repo"

    def test_worktree_stripped(self):
        assert (
            normalize_project("/home/u/myrepo/.claude/worktrees/feature-x")
            == "myrepo"
        )

    def test_empty(self):
        assert normalize_project("") == UNSCOPED_PROJECT

    def test_none(self):
        assert normalize_project(None) == UNSCOPED_PROJECT  # type: ignore[arg-type]

    def test_root(self):
        assert normalize_project("/") == UNSCOPED_PROJECT


class TestNormalizeProjectWindows:
    def test_backslash_repo(self):
        assert normalize_project("C:\\Users\\u\\projects\\MyRepo") == "myrepo"

    def test_forward_slash_drive(self):
        assert normalize_project("C:/Users/u/repo-name") == "repo_name"

    def test_trailing_backslash(self):
        assert normalize_project("C:\\Users\\u\\MyRepo\\") == "myrepo"

    def test_worktree_stripped_backslash(self):
        assert (
            normalize_project("C:\\Users\\u\\myrepo\\.claude\\worktrees\\feat")
            == "myrepo"
        )

    def test_bare_drive_root(self):
        assert normalize_project("C:\\") == UNSCOPED_PROJECT


class TestNormalizeProjectHomeGuards:
    def test_homedir_is_unscoped(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: Path("/home/u"))
        assert normalize_project("/home/u") == UNSCOPED_PROJECT

    def test_dot_claude_under_home_is_unscoped(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: Path("/home/u"))
        assert normalize_project("/home/u/.claude") == UNSCOPED_PROJECT

    def test_dotfile_dir_is_unscoped(self):
        assert normalize_project("/home/u/.config") == UNSCOPED_PROJECT
