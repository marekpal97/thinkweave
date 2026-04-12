"""Tests for deterministic extraction from session events."""

from __future__ import annotations

from personal_mem.extract import (
    ExtractResult,
    assign_concepts_from_paths,
    extract_deterministic,
)


class TestExtractDeterministic:
    def test_basic_extraction(self):
        events = [
            {"ts": "14:00", "tool": "Edit", "file": "src/main.py"},
            {"ts": "14:05", "tool": "Edit", "file": "src/utils.py"},
        ]
        result = extract_deterministic(events)
        assert isinstance(result, ExtractResult)
        assert result.files_touched == ["src/main.py", "src/utils.py"]
        assert "Edited 2 files" in result.summary

    def test_deduplicates_files(self):
        events = [
            {"ts": "14:00", "tool": "Edit", "file": "a.py"},
            {"ts": "14:05", "tool": "Edit", "file": "a.py"},
            {"ts": "14:10", "tool": "Edit", "file": "b.py"},
        ]
        result = extract_deterministic(events)
        assert result.files_touched == ["a.py", "b.py"]

    def test_extracts_commits(self):
        events = [
            {"ts": "14:00", "tool": "Bash", "command": "git commit",
             "commit": {"hash": "abc123", "message": "Fix bug", "files": ["a.py"]}},
        ]
        result = extract_deterministic(events)
        assert len(result.commits) == 1
        assert result.commits[0]["hash"] == "abc123"

    def test_decision_skeletons_from_large_commits(self):
        events = [
            {"ts": "14:00", "tool": "Bash", "command": "git commit",
             "commit": {"hash": "abc123", "message": "Refactor auth module",
                        "files": ["auth.py", "login.py", "middleware.py", "tests/test_auth.py"]}},
        ]
        result = extract_deterministic(events)
        assert len(result.decision_skeletons) == 1
        assert result.decision_skeletons[0].title == "Refactor auth module"
        assert len(result.decision_skeletons[0].file_paths) == 4

    def test_no_skeleton_for_small_commits(self):
        events = [
            {"ts": "14:00", "tool": "Bash", "command": "git commit",
             "commit": {"hash": "abc", "message": "Fix typo", "files": ["README.md"]}},
        ]
        result = extract_deterministic(events)
        assert len(result.decision_skeletons) == 0

    def test_failure_signals_from_insights(self):
        events = [
            {"ts": "14:00", "tool": "Edit", "file": "a.py",
             "insights": ["This approach failed because the regex was too greedy"]},
        ]
        result = extract_deterministic(events)
        assert len(result.failure_signals) == 1
        assert result.failure_signals[0].source == "insight"

    def test_failure_signals_from_test_failures(self):
        events = [
            {"ts": "14:00", "tool": "Bash", "command": "pytest",
             "test_run": {"passed": 10, "failed": 3, "command": "pytest tests/"}},
        ]
        result = extract_deterministic(events)
        assert len(result.failure_signals) == 1
        assert result.failure_signals[0].source == "test"

    def test_no_failure_signals_when_clean(self):
        events = [
            {"ts": "14:00", "tool": "Edit", "file": "a.py",
             "insights": ["Key point: FTS5 is fast"]},
            {"ts": "14:05", "tool": "Bash", "command": "pytest",
             "test_run": {"passed": 10, "failed": 0, "command": "pytest"}},
        ]
        result = extract_deterministic(events)
        assert len(result.failure_signals) == 0

    def test_empty_events(self):
        result = extract_deterministic([])
        assert result.files_touched == []
        assert result.commits == []
        assert result.decision_skeletons == []
        assert result.failure_signals == []

    def test_concepts_from_python_files(self):
        events = [
            {"ts": "14:00", "tool": "Edit", "file": "src/main.py"},
        ]
        result = extract_deterministic(events)
        assert "python" in result.concepts

    def test_concepts_from_test_files(self):
        events = [
            {"ts": "14:00", "tool": "Edit", "file": "tests/test_main.py"},
        ]
        result = extract_deterministic(events)
        assert "pytest" in result.concepts
        assert "python" in result.concepts


class TestAssignConceptsFromPaths:
    def test_python_files(self):
        concepts = assign_concepts_from_paths(["src/main.py", "src/utils.py"])
        assert "python" in concepts

    def test_docker_files(self):
        concepts = assign_concepts_from_paths(["Dockerfile", "docker-compose.yml"])
        assert "docker" in concepts

    def test_sqlite_files(self):
        concepts = assign_concepts_from_paths(["db/schema.sql", "index.db"])
        assert "sqlite" in concepts

    def test_with_ontology(self):
        ontology = {"ml/deep-learning": ["pytorch", "neural-networks"]}
        concepts = assign_concepts_from_paths(
            ["src/pytorch/model.py"], ontology
        )
        assert "pytorch" in concepts
        assert "python" in concepts

    def test_empty_paths(self):
        assert assign_concepts_from_paths([]) == []

    def test_no_matches(self):
        concepts = assign_concepts_from_paths(["README.md"])
        assert concepts == []  # .md doesn't match any pattern
