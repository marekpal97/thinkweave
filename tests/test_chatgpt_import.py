"""Tests for ChatGPT conversation importer."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from personal_mem.importers.chatgpt import (
    Message,
    Thread,
    _build_body,
    _parse_summary_response,
    import_chatgpt,
    parse_conversations,
    parse_thread,
)


# ── Fixtures ───────────────────────────────────────────────────────


def _make_mapping(*messages: tuple[str, str]) -> dict:
    """Build a minimal ChatGPT mapping dict from (role, text) pairs."""
    mapping = {
        "client-created-root": {
            "id": "client-created-root",
            "message": None,
            "parent": None,
            "children": [],
        }
    }

    prev_id = "client-created-root"
    for i, (role, text) in enumerate(messages):
        node_id = f"node-{i}"
        mapping[node_id] = {
            "id": node_id,
            "message": {
                "author": {"role": role},
                "content": {
                    "content_type": "text",
                    "parts": [text],
                },
                "create_time": 1700000000 + i * 60,
            },
            "parent": prev_id,
            "children": [],
        }
        mapping[prev_id]["children"].append(node_id)
        prev_id = node_id

    return mapping


def _make_conversation(
    title: str = "Test Conversation",
    messages: list[tuple[str, str]] | None = None,
    conversation_id: str = "conv-001",
    model: str = "gpt-4o-mini",
    create_time: float = 1700000000,
) -> dict:
    """Build a minimal ChatGPT conversation object."""
    if messages is None:
        messages = [
            ("user", "What is Python?"),
            ("assistant", "Python is a programming language."),
        ]
    return {
        "title": title,
        "conversation_id": conversation_id,
        "id": conversation_id,
        "create_time": create_time,
        "update_time": create_time + 3600,
        "default_model_slug": model,
        "mapping": _make_mapping(*messages),
    }


@pytest.fixture
def conversations_file(tmp_path: Path) -> Path:
    """Create a temp conversations.json with 3 conversations."""
    convos = [
        _make_conversation(
            title="Python Basics",
            conversation_id="conv-001",
            create_time=1700000000,
            messages=[
                ("user", "What is Python?"),
                ("assistant", "Python is a versatile programming language."),
                ("user", "What about type hints?"),
                ("assistant", "Type hints were introduced in Python 3.5."),
            ],
        ),
        _make_conversation(
            title="SQL Optimization",
            conversation_id="conv-002",
            create_time=1700100000,
            messages=[
                ("user", "How do I optimize SQL queries?"),
                ("assistant", "Use indexes, avoid SELECT *, and analyze query plans."),
            ],
        ),
        _make_conversation(
            title="Empty Convo",
            conversation_id="conv-003",
            create_time=1700200000,
            messages=[],  # No messages — should be skipped
        ),
    ]
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(convos), encoding="utf-8")
    return path


@pytest.fixture
def vault_config(tmp_path: Path):
    """Create a minimal vault config pointing to a temp directory."""
    from personal_mem.config import Config

    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    return Config(vault_root=vault_root)


# ── Parser tests ───────────────────────────────────────────────────


class TestParseThread:
    def test_basic_conversation(self):
        raw = _make_conversation(
            title="Test",
            messages=[
                ("user", "Hello"),
                ("assistant", "Hi there!"),
            ],
        )
        thread = parse_thread(raw)
        assert thread.title == "Test"
        assert thread.id == "conv-001"
        assert thread.model == "gpt-4o-mini"
        assert len(thread.messages) == 2
        assert thread.messages[0].role == "user"
        assert thread.messages[0].text == "Hello"
        assert thread.messages[1].role == "assistant"

    def test_filters_system_messages(self):
        raw = _make_conversation(
            messages=[
                ("system", "You are a helpful assistant."),
                ("user", "Hello"),
                ("assistant", "Hi!"),
            ],
        )
        thread = parse_thread(raw)
        # System messages should be filtered out
        assert len(thread.messages) == 2
        assert thread.messages[0].role == "user"

    def test_filters_tool_messages(self):
        raw = _make_conversation(messages=[("user", "Search for X")])
        # Manually add a tool message with non-text content_type
        mapping = raw["mapping"]
        last_id = [k for k in mapping if mapping[k]["children"] == []][-1]
        mapping["tool-node"] = {
            "id": "tool-node",
            "message": {
                "author": {"role": "tool"},
                "content": {"content_type": "tether_browsing_display", "parts": []},
                "create_time": 1700000100,
            },
            "parent": last_id,
            "children": [],
        }
        mapping[last_id]["children"].append("tool-node")

        thread = parse_thread(raw)
        assert len(thread.messages) == 1
        assert thread.messages[0].role == "user"

    def test_empty_conversation(self):
        raw = _make_conversation(messages=[])
        thread = parse_thread(raw)
        assert len(thread.messages) == 0

    def test_skips_empty_text_parts(self):
        raw = _make_conversation(
            messages=[("user", ""), ("assistant", "Real response")],
        )
        thread = parse_thread(raw)
        # Empty text should be filtered
        assert len(thread.messages) == 1
        assert thread.messages[0].text == "Real response"

    def test_handles_dict_parts(self):
        """Parts can contain dicts (images, etc.) — should be ignored."""
        raw = _make_conversation(messages=[("user", "Look at this")])
        # Replace parts with mixed content
        mapping = raw["mapping"]
        for node in mapping.values():
            msg = node.get("message")
            if msg and msg["author"]["role"] == "user":
                msg["content"]["parts"] = [
                    "Look at this ",
                    {"type": "image", "url": "..."},
                    "image please",
                ]
        thread = parse_thread(raw)
        assert thread.messages[0].text == "Look at this image please"


class TestParseConversations:
    def test_loads_and_filters(self, conversations_file: Path):
        threads = parse_conversations(conversations_file)
        # Should skip the empty conversation
        assert len(threads) == 2
        assert threads[0].title == "Python Basics"
        assert threads[1].title == "SQL Optimization"

    def test_sorted_by_date(self, conversations_file: Path):
        threads = parse_conversations(conversations_file)
        assert threads[0].created < threads[1].created


# ── Summary parser tests ──────────────────────────────────────────


class TestParseSummaryResponse:
    def test_parses_all_sections(self):
        content = """## Summary
Discussed Python basics and type systems.

## Key Questions
- What is Python?
- How do type hints work?

## Key Insights
- Python is dynamically typed
- Type hints improve code quality

## Concepts
python, type-hints, dynamic-typing
"""
        result = _parse_summary_response(content)
        assert "Python basics" in result["summary"]
        assert "What is Python?" in result["key_questions"]
        assert "dynamically typed" in result["key_insights"]
        assert result["concepts"] == ["python", "type-hints", "dynamic-typing"]

    def test_handles_missing_sections(self):
        content = """## Summary
Just a summary.
"""
        result = _parse_summary_response(content)
        assert result["summary"] == "Just a summary."
        assert result["key_questions"] == ""
        assert result["concepts"] == []

    def test_normalizes_concepts(self):
        content = """## Concepts
Machine Learning, Neural Networks, GPT 4o Mini
"""
        result = _parse_summary_response(content)
        assert "machine-learning" in result["concepts"]
        assert "neural-networks" in result["concepts"]
        assert "gpt-4o-mini" in result["concepts"]


# ── Body builder tests ─────────────────────────────────────────────


class TestBuildBody:
    def test_includes_metadata(self):
        thread = Thread(
            id="test",
            title="Test",
            created=datetime(2024, 1, 15, tzinfo=timezone.utc),
            updated=datetime(2024, 1, 15, tzinfo=timezone.utc),
            model="gpt-4o-mini",
            messages=[
                Message(role="user", text="Q1"),
                Message(role="assistant", text="A1"),
            ],
        )
        summary = {"summary": "A test conversation.", "key_questions": "", "key_insights": ""}
        body = _build_body(thread, summary)
        assert "**Messages**: 2 (1 user, 1 assistant)" in body
        assert "gpt-4o-mini" in body
        assert "A test conversation." in body


# ── Transcript tests ───────────────────────────────────────────────


class TestTranscript:
    def test_basic_transcript(self):
        thread = Thread(
            id="t", title="T",
            created=datetime(2024, 1, 1, tzinfo=timezone.utc),
            updated=datetime(2024, 1, 1, tzinfo=timezone.utc),
            model="gpt-4o-mini",
            messages=[
                Message(role="user", text="Hello"),
                Message(role="assistant", text="Hi!"),
            ],
        )
        transcript = thread.transcript()
        assert "User: Hello" in transcript
        assert "Assistant: Hi!" in transcript

    def test_truncation(self):
        long_msg = "x" * 50_000
        thread = Thread(
            id="t", title="T",
            created=datetime(2024, 1, 1, tzinfo=timezone.utc),
            updated=datetime(2024, 1, 1, tzinfo=timezone.utc),
            model="m",
            messages=[
                Message(role="user", text=long_msg),
                Message(role="assistant", text=long_msg),
                Message(role="user", text="This should be cut"),
            ],
        )
        transcript = thread.transcript(max_chars=60_000)
        assert len(transcript) <= 65_000  # Some overhead for labels
        assert "This should be cut" not in transcript


# ── Integration tests ──────────────────────────────────────────────


class TestImportChatgpt:
    def test_dry_run(self, conversations_file: Path, vault_config, capsys):
        stats = import_chatgpt(
            config=vault_config,
            conversations_path=conversations_file,
            dry_run=True,
        )
        assert stats["dry_run"] is True
        assert stats["total"] == 2  # Empty convo filtered out
        output = capsys.readouterr().out
        assert "Dry Run" in output

    def test_missing_file(self, vault_config):
        stats = import_chatgpt(
            config=vault_config,
            conversations_path=Path("/nonexistent/file.json"),
        )
        assert "error" in stats

    def test_missing_api_key(self, conversations_file: Path, vault_config):
        with patch.dict("os.environ", {}, clear=True):
            stats = import_chatgpt(
                config=vault_config,
                conversations_path=conversations_file,
            )
            assert "error" in stats
            assert "OPENAI_API_KEY" in stats["error"]

    def test_limit_flag(self, conversations_file: Path, vault_config):
        stats = import_chatgpt(
            config=vault_config,
            conversations_path=conversations_file,
            dry_run=True,
            limit=1,
        )
        assert stats["total"] == 1

    def test_since_filter(self, conversations_file: Path, vault_config):
        # conv-001 is at 2023-11-14, conv-002 is at 2023-11-16
        stats = import_chatgpt(
            config=vault_config,
            conversations_path=conversations_file,
            dry_run=True,
            since="2023-11-15",
        )
        assert stats["total"] == 1  # Only conv-002

    @patch("personal_mem.importers.chatgpt.summarize_thread")
    def test_full_import(self, mock_summarize, conversations_file, vault_config):
        mock_summarize.return_value = {
            "summary": "Test summary.",
            "key_questions": "- Q1",
            "key_insights": "- I1",
            "concepts": ["python", "testing"],
        }

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            stats = import_chatgpt(
                config=vault_config,
                conversations_path=conversations_file,
            )

        assert stats["imported"] == 2
        assert stats["errors"] == 0

        # Check files were created (source notes now live in subdirectories)
        sources = list((vault_config.vault_root / "sources").glob("*/source.md"))
        assert len(sources) == 2

        # Check content of first note
        content = sources[0].read_text(encoding="utf-8")
        assert "source_type: conversation" in content
        assert "imported_from: chatgpt" in content
        assert "Test summary." in content

    @patch("personal_mem.importers.chatgpt.summarize_thread")
    def test_idempotency(self, mock_summarize, conversations_file, vault_config):
        mock_summarize.return_value = {
            "summary": "S", "key_questions": "", "key_insights": "", "concepts": [],
        }

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            # First import
            stats1 = import_chatgpt(config=vault_config, conversations_path=conversations_file)
            assert stats1["imported"] == 2

            # Second import — should skip all
            stats2 = import_chatgpt(config=vault_config, conversations_path=conversations_file)
            assert stats2["imported"] == 0
            assert stats2["skipped"] == 2

        # Still only 2 source directories
        sources = list((vault_config.vault_root / "sources").glob("*/source.md"))
        assert len(sources) == 2
