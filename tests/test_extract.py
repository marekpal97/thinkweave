"""Tests for deterministic extraction from session events."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from personal_mem.extract import (
    ExtractResult,
    Prompt,
    Todo,
    assign_concepts_from_paths,
    classify_probe,
    extract_deterministic,
    extract_prompts,
    extract_todos,
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


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


class TestExtractPrompts:
    """Phase 4 E2 — Prompt primitive lifted from JSONL buffers."""

    def test_filters_to_prompt_type_only(self, tmp_path: Path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(f, [
            {"ts": "2026-05-02T15:00:00+00:00", "type": "prompt",
             "text": "What does X do?", "session_id": "ses-1", "cwd": "/p"},
            {"ts": "2026-05-02T15:01:00+00:00", "tool": "Edit",
             "file": "main.py"},  # not a prompt
            {"ts": "2026-05-02T15:02:00+00:00", "type": "prompt",
             "text": "Why is Y slow?", "session_id": "ses-1"},
        ])
        prompts = extract_prompts(f)
        assert len(prompts) == 2
        assert all(isinstance(p, Prompt) for p in prompts)
        assert prompts[0].text == "What does X do?"
        assert prompts[0].cwd == "/p"
        assert prompts[1].text == "Why is Y slow?"

    def test_skips_malformed_lines(self, tmp_path: Path):
        f = tmp_path / "events.jsonl"
        f.write_text(
            '{"type":"prompt","text":"valid","session_id":"s","ts":"2026-01-01T00:00:00+00:00"}\n'
            "not json\n"
            "{}\n"
            '{"type":"prompt","text":"also valid","session_id":"s","ts":"2026-01-02T00:00:00+00:00"}\n',
            encoding="utf-8",
        )
        prompts = extract_prompts(f)
        assert len(prompts) == 2
        assert prompts[0].text == "valid"
        assert prompts[1].text == "also valid"

    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert extract_prompts(tmp_path / "missing.jsonl") == []

    def test_skips_rows_missing_required_fields(self, tmp_path: Path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(f, [
            {"type": "prompt", "text": "", "session_id": "s"},  # empty text
            {"type": "prompt", "session_id": "s"},  # no text key
            {"type": "prompt", "text": "ok"},  # no session_id
        ])
        assert extract_prompts(f) == []


class TestClassifyProbe:
    """Phase 4 E2 — conservative probe classifier."""

    def test_question_no_followup_edit_classifies_as_probe(self):
        ts = datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)
        events = [
            {"ts": ts.isoformat(), "type": "prompt",
             "text": "What does the indexer skip?", "session_id": "s"},
            {"ts": "...", "tool": "Bash", "command": "uv run pytest"},
        ]
        prompt = Prompt(ts=ts, text="What does the indexer skip?",
                        session_id="s")
        assert classify_probe(prompt, events) is True

    def test_question_followed_by_edit_is_not_probe(self):
        ts = datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)
        events = [
            {"ts": ts.isoformat(), "type": "prompt",
             "text": "Should we rename foo?", "session_id": "s"},
            {"ts": "...", "tool": "Edit", "file": "foo.py"},
        ]
        prompt = Prompt(ts=ts, text="Should we rename foo?", session_id="s")
        assert classify_probe(prompt, events) is False

    def test_non_question_imperative_not_probe(self):
        ts = datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)
        events = [
            {"ts": ts.isoformat(), "type": "prompt",
             "text": "Refactor the indexer module.", "session_id": "s"},
        ]
        prompt = Prompt(ts=ts, text="Refactor the indexer module.",
                        session_id="s")
        assert classify_probe(prompt, events) is False

    def test_lead_phrase_classifies_as_probe(self):
        ts = datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)
        events = [
            {"ts": ts.isoformat(), "type": "prompt",
             "text": "Explain how the buffer is archived",
             "session_id": "s"},
        ]
        prompt = Prompt(ts=ts, text="Explain how the buffer is archived",
                        session_id="s")
        assert classify_probe(prompt, events) is True

    def test_empty_text_not_probe(self):
        ts = datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)
        prompt = Prompt(ts=ts, text="", session_id="s")
        assert classify_probe(prompt, []) is False


class TestExtractTodos:
    """Phase 4 E5 — auto-todo lifting from session text."""

    def test_explicit_todo_marker(self):
        text = "Some intro.\nTODO: wire up the discover skill.\nMore prose."
        todos = extract_todos(text)
        assert len(todos) == 1
        assert todos[0].text.startswith("wire up the discover")

    def test_we_should_pattern(self):
        text = "We should refactor the queue module before shipping."
        todos = extract_todos(text)
        assert len(todos) == 1
        assert "refactor the queue module" in todos[0].text

    def test_next_step_pattern(self):
        text = "Wrapped this up.\nNext step: add the Prompt primitive tests."
        todos = extract_todos(text)
        assert len(todos) == 1
        assert "add the Prompt primitive tests" in todos[0].text

    def test_dedup_same_todo(self):
        text = "TODO: sync docs.\nFixme: sync docs.\nFollow-up: sync docs."
        todos = extract_todos(text)
        # All three patterns hit the same target — only the first survives.
        assert len(todos) == 1

    def test_empty_text(self):
        assert extract_todos("") == []
        assert extract_todos("\n\n   \n") == []

    def test_session_id_propagates(self):
        todos = extract_todos("TODO: do the thing", source_session_id="ses-99")
        assert todos[0].source_session_id == "ses-99"

    def test_clamp_long_line(self):
        text = "TODO: " + "x" * 500
        todos = extract_todos(text)
        assert len(todos[0].text) <= 200

    def test_returns_todo_dataclass(self):
        todos = extract_todos("TODO: a thing")
        assert isinstance(todos[0], Todo)
