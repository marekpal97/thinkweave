"""Tests for deterministic extraction from session events."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from thinkweave.core.events import (
    ExtractResult,
    Prompt,
    Todo,
    assign_concepts_from_paths,
    classify_feedback,
    classify_probe,
    extract_deterministic,
    extract_prompts,
    extract_todos,
    feedback_events,
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
        ontology = {"ml-deep-learning": ["pytorch", "neural-networks"]}
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


class TestClassifyFeedback:
    """Issue #70 — deterministic feedback register (correction/confirmation/neutral)."""

    def test_leading_no_is_correction(self):
        assert classify_feedback("no, that's wrong — use a dict instead") == "correction"

    def test_thats_wrong_phrase_is_correction(self):
        # No leading marker, but the strong phrase fires anywhere.
        assert classify_feedback("hmm, that's wrong, revisit the parser") == "correction"

    def test_actually_lead_is_correction(self):
        assert classify_feedback("actually, let's revert that change") == "correction"

    def test_revert_lead_is_correction(self):
        # Kept as a lead: in a coding-agent session a leading revert/undo is
        # overwhelmingly corrective of prior agent work.
        assert classify_feedback("revert the last commit") == "correction"

    def test_undo_lead_is_correction(self):
        assert classify_feedback("undo that change") == "correction"

    def test_correct_imperative_is_neutral(self):
        # "correct" dropped from confirmation leads — imperative verb, not
        # endorsement.
        assert classify_feedback("correct the typo in line 5") == "neutral"

    def test_wait_lead_is_neutral(self):
        # "wait" dropped from correction leads — too task-common as a lead.
        assert classify_feedback("wait for the build then run tests") == "neutral"

    def test_dont_forget_lead_is_neutral(self):
        # "don't" dropped from correction leads.
        assert classify_feedback("don't forget to update the changelog") == "neutral"

    def test_stop_lead_is_neutral(self):
        # "stop" dropped from correction leads.
        assert classify_feedback("stop the server before deploying") == "neutral"

    def test_no_problem_is_neutral(self):
        # Neutral-override courtesy phrase beats the leading-"no" rule.
        assert classify_feedback("no problem, take your time") == "neutral"

    def test_no_worries_is_neutral(self):
        assert classify_feedback("no worries, whenever you get to it") == "neutral"

    def test_no_rush_is_neutral(self):
        assert classify_feedback("no rush on this one") == "neutral"

    def test_hedged_looks_good_is_neutral(self):
        # A hedged partial-correction dressed as confirmation is the worst
        # mislabel for the reward channel — suppress to neutral.
        assert classify_feedback("looks good, but the naming is off") == "neutral"

    def test_hedged_except_is_neutral(self):
        assert classify_feedback("that's right except for the edge case") == "neutral"

    def test_hedged_although_is_neutral(self):
        assert classify_feedback("looks good although I'd rename it") == "neutral"

    def test_hedge_before_signal_still_confirmation(self):
        # The hedge must FOLLOW the confirming signal to suppress; a leading
        # hedge that resolves affirmatively stays confirmation.
        assert classify_feedback("but that looks good overall") == "confirmation"

    def test_emoji_lead_is_confirmation(self):
        # Leading non-alpha (emoji, whitespace) is skipped before the lead word.
        assert classify_feedback("👍 yes") == "confirmation"

    def test_punctuation_only_is_neutral(self):
        assert classify_feedback("!!!") == "neutral"
        assert classify_feedback("...") == "neutral"

    def test_leading_yes_is_confirmation(self):
        assert classify_feedback("yes, exactly right") == "confirmation"

    def test_perfect_lead_is_confirmation(self):
        assert classify_feedback("perfect, keep going") == "confirmation"

    def test_lgtm_is_confirmation(self):
        assert classify_feedback("lgtm, ship it") == "confirmation"

    def test_looks_good_phrase_is_confirmation(self):
        assert classify_feedback("that looks good to me") == "confirmation"

    def test_neutral_task_prompt(self):
        assert classify_feedback(
            "Add a feedback register to the UserPromptSubmit hook"
        ) == "neutral"

    def test_neutral_question(self):
        assert classify_feedback("What does the indexer skip?") == "neutral"

    def test_empty_is_neutral(self):
        assert classify_feedback("") == "neutral"
        assert classify_feedback("   ") == "neutral"

    def test_word_boundary_no_false_positive(self):
        # "yesterday"/"note"/"nothing" must not trip the yes/no leads.
        assert classify_feedback("yesterday's run finished cleanly") == "neutral"
        assert classify_feedback("note the failing test in module X") == "neutral"
        assert classify_feedback("nothing changed in the output") == "neutral"

    def test_correction_wins_over_confirmation(self):
        # A prompt carrying both signals resolves to correction (stronger
        # improvement signal). Leading "no" dominates the trailing "perfect".
        assert classify_feedback("no, the earlier version was perfect") == "correction"


class TestFeedbackEvents:
    """Issue #70 — enumerate feedback events from a session's events JSONL."""

    def test_returns_only_feedback_rows(self, tmp_path: Path):
        f = tmp_path / "events.jsonl"
        _write_jsonl(f, [
            {"ts": "2026-05-02T15:00:00+00:00", "type": "prompt",
             "text": "no, wrong", "session_id": "s"},
            {"ts": "2026-05-02T15:00:00+00:00", "type": "feedback",
             "register": "correction", "session_id": "s",
             "prompt_ref": "no, wrong"},
            {"ts": "2026-05-02T15:01:00+00:00", "tool": "Edit", "file": "x.py"},
            {"ts": "2026-05-02T15:02:00+00:00", "type": "feedback",
             "register": "confirmation", "session_id": "s",
             "prompt_ref": "lgtm"},
        ])
        evs = feedback_events(f)
        assert [e["register"] for e in evs] == ["correction", "confirmation"]
        assert evs[0]["session_id"] == "s"
        assert evs[0]["ts"] == "2026-05-02T15:00:00+00:00"

    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert feedback_events(tmp_path / "missing.jsonl") == []

    def test_skips_malformed_lines(self, tmp_path: Path):
        f = tmp_path / "events.jsonl"
        f.write_text(
            '{"type":"feedback","register":"correction","session_id":"s"}\n'
            "not json\n"
            "{}\n",
            encoding="utf-8",
        )
        evs = feedback_events(f)
        assert len(evs) == 1
        assert evs[0]["register"] == "correction"


class TestExtractTodos:
    """Phase 4 E5 — auto-todo lifting from session text."""

    def test_explicit_todo_marker(self):
        text = "Some intro.\nTODO: wire up the discover skill.\nMore prose."
        todos = extract_todos(text)
        assert len(todos) == 1
        assert todos[0].text.startswith("wire up the discover")

    def test_soft_we_should_no_longer_matches(self):
        # The soft "we should …" pattern was removed — it fired on rhetorical
        # prose ("we should note that…") and was almost all noise.
        text = "We should refactor the queue module before shipping."
        assert extract_todos(text) == []

    def test_compound_word_does_not_fire(self):
        # Regression: a hyphenated compound like "todo-tag" must NOT match —
        # this was the false positive that spawned a garbage backlog item from
        # a decision rationale ("legacy todo-tag queue → JSONL queue + /drain").
        text = "Removed the legacy todo-tag queue in favour of JSONL queues."
        assert extract_todos(text) == []

    def test_rationale_prose_mentioning_todo_does_not_fire(self):
        # Prose that merely discusses the word "todo" without an explicit
        # "TODO: <action>" marker produces nothing.
        text = (
            "The auto-todo extractor used to mine decision rationales; "
            "any rationale discussing todo handling produced spurious todos."
        )
        assert extract_todos(text) == []

    def test_explicit_marker_still_fires_after_hardening(self):
        # Positive control: a real marker with ':' + whitespace still works.
        todos = extract_todos("TODO: wire the cron job")
        assert len(todos) == 1
        assert "wire the cron job" in todos[0].text

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


class TestSupersedesStringCoercion:
    """Regression: ``supersedes: dec-X`` as bare string must not be
    iterated character-by-character into the rejudge queue.

    The pre-fix code did ``for target_id in dec.get("supersedes", []) or []:``
    which, when the YAML value was a bare string like ``"dec-71940"``, yielded
    9 single-character entries (``'d','e','c','-','7','1','9','4','0'``)
    instead of one ``"dec-71940"`` entry. The fix coerces string → list
    before the loop.
    """

    def test_bare_string_supersedes_enqueues_one_entry(self, tmp_path: Path):
        from thinkweave.core.config import Config
        from thinkweave.core.indexer import Indexer
        from thinkweave.core.schemas import NoteType
        from thinkweave.core.vault import VaultManager
        from thinkweave.operations import rejudge_queue
        from thinkweave.operations.extract import extract_session

        cfg = Config(vault_root=tmp_path / "vault")
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()

        # Create the predecessor decision so the supersession path has a
        # real target (otherwise the structural status-flip skips silently;
        # the enqueue happens regardless, which is the load-bearing bit).
        predecessor = vm.create_note(
            NoteType.DECISION,
            "Old decision",
            body="## Context\n\n## Decision\n",
            project="t",
            extra_frontmatter={"status": "accepted"},
        )
        predecessor_id = vm.read_note(predecessor).id
        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()

        # The bug shape: supersedes is a bare string, not a list.
        out = extract_session(
            cfg,
            session_id="ses-superstr-1",
            project="t",
            summary="x",
            insights=[],
            decisions=[{
                "title": "New decision",
                "rationale": "## Context\n\n## Decision\n",
                "outcome": "committed",
                "concepts": ["sqlite", "memory-system"],
                "supersedes": predecessor_id,  # BARE STRING — the bug shape
            }],
        )
        assert out.error == ""

        queued = rejudge_queue.peek(cfg)
        # Exactly one entry, with the full predecessor id intact — not 9
        # garbage single-char entries (the pre-fix behavior).
        assert len(queued) == 1, f"Expected 1 queue entry, got {len(queued)}: {queued}"
        assert queued[0]["decision_id"] == predecessor_id
        # Sanity: no single-character garbage decision_ids leaked through.
        assert all(len(q["decision_id"]) > 1 for q in queued)

    def test_list_supersedes_still_works(self, tmp_path: Path):
        # Symmetry check: the list shape (the canonical form) still produces
        # one entry per id and isn't accidentally broken by the coercion.
        from thinkweave.core.config import Config
        from thinkweave.core.indexer import Indexer
        from thinkweave.core.schemas import NoteType
        from thinkweave.core.vault import VaultManager
        from thinkweave.operations import rejudge_queue
        from thinkweave.operations.extract import extract_session

        cfg = Config(vault_root=tmp_path / "vault")
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()

        p1 = vm.read_note(vm.create_note(
            NoteType.DECISION, "Old A",
            body="## Context\n\n## Decision\n", project="t",
            extra_frontmatter={"status": "accepted"},
        )).id
        p2 = vm.read_note(vm.create_note(
            NoteType.DECISION, "Old B",
            body="## Context\n\n## Decision\n", project="t",
            extra_frontmatter={"status": "accepted"},
        )).id
        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()

        out = extract_session(
            cfg,
            session_id="ses-superlist-1",
            project="t",
            summary="x",
            insights=[],
            decisions=[{
                "title": "Multi-supersede",
                "rationale": "## Context\n\n## Decision\n",
                "outcome": "committed",
                "concepts": ["sqlite", "memory-system"],
                "supersedes": [p1, p2],
            }],
        )
        assert out.error == ""

        queued = rejudge_queue.peek(cfg)
        ids = {q["decision_id"] for q in queued}
        assert ids == {p1, p2}


class TestInsightsCap:
    """The per-extraction insight cap reads config ``extract.insights_cap``."""

    def _extract_with_insights(self, tmp_path: Path, n: int, cap: int | None):
        from thinkweave.core.config import Config
        from thinkweave.core.indexer import Indexer
        from thinkweave.core.vault import VaultManager
        from thinkweave.operations.extract import extract_session

        cfg = Config(vault_root=tmp_path / "vault")
        if cap is not None:
            cfg.extract_insights_cap = cap
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()

        insights = [
            {
                "title": f"Insight {i}",
                "body": f"Body {i}",
                "concepts": ["sqlite", "memory-system"],
            }
            for i in range(n)
        ]
        out = extract_session(
            cfg,
            session_id=f"ses-cap-{n}-{cap}",
            project="t",
            summary="x",
            insights=insights,
            decisions=[],
        )
        assert out.error == ""
        return out

    def test_default_cap_is_three(self, tmp_path: Path):
        out = self._extract_with_insights(tmp_path, n=5, cap=None)
        # Old literal: insights_in[:3].
        assert len(out.created_notes) == 3

    def test_config_override_reaches_extraction(self, tmp_path: Path):
        out = self._extract_with_insights(tmp_path, n=5, cap=1)
        assert len(out.created_notes) == 1
