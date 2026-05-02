"""Tests for the user-overridable sources config loader.

Covers the three behaviours of ``load_user_config``:
  1. Missing file → returns deep-copied defaults.
  2. Partial override → merges per key, leaves untouched defaults intact.
  3. List/scalar overwrites → wholesale replacement (not merge).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.sources import DEFAULT_CONFIG, load_user_config
from personal_mem.sources.config import _parse_simple_yaml


def test_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = load_user_config(tmp_path)
    assert cfg["auto_todo_extraction"] is True
    assert cfg["sources"]["paper"]["queue"] == "vault/.mem/queues/papers.jsonl"
    assert cfg["landing_files"]["state"] == "STATE.md"


def test_returns_deep_copy(tmp_path: Path) -> None:
    cfg = load_user_config(tmp_path)
    cfg["sources"]["paper"]["queue"] = "mutated"
    assert DEFAULT_CONFIG["sources"]["paper"]["queue"] == "vault/.mem/queues/papers.jsonl"


def test_user_override_merges_with_defaults(tmp_path: Path) -> None:
    mem = tmp_path / ".mem"
    mem.mkdir()
    (mem / "sources.yaml").write_text(
        """
sources:
  paper:
    queue: custom/papers.jsonl
auto_todo_extraction: false
""",
        encoding="utf-8",
    )

    cfg = load_user_config(tmp_path)
    # Overridden:
    assert cfg["sources"]["paper"]["queue"] == "custom/papers.jsonl"
    assert cfg["auto_todo_extraction"] is False
    # Defaults preserved:
    assert cfg["sources"]["paper"]["research_skill"] == "research-paper"
    assert cfg["sources"]["repo"]["queue"] == "vault/.mem/queues/repos.jsonl"


def test_list_overwrites_wholesale(tmp_path: Path) -> None:
    mem = tmp_path / ".mem"
    mem.mkdir()
    (mem / "sources.yaml").write_text(
        """
sources:
  paper:
    dedup_keys: [arxiv_id]
""",
        encoding="utf-8",
    )

    cfg = load_user_config(tmp_path)
    assert cfg["sources"]["paper"]["dedup_keys"] == ["arxiv_id"]


def test_malformed_falls_back_to_defaults(tmp_path: Path) -> None:
    mem = tmp_path / ".mem"
    mem.mkdir()
    # Tab-indented + missing colon — definitely fails parsing.
    (mem / "sources.yaml").write_text("not valid yaml here\n", encoding="utf-8")

    cfg = load_user_config(tmp_path)
    assert cfg["sources"]["paper"]["queue"] == "vault/.mem/queues/papers.jsonl"


def test_parser_handles_inline_lists_and_scalars() -> None:
    parsed = _parse_simple_yaml(
        """
sources:
  paper:
    queue: foo.jsonl
    dedup_keys: [a, "b", c]
    drain_strategy: anthropic_batch
auto_todo_extraction: true
"""
    )
    assert parsed["sources"]["paper"]["queue"] == "foo.jsonl"
    assert parsed["sources"]["paper"]["dedup_keys"] == ["a", "b", "c"]
    assert parsed["auto_todo_extraction"] is True


def test_parser_strips_comments() -> None:
    parsed = _parse_simple_yaml(
        """
# top-level comment
sources:
  paper:
    queue: foo.jsonl  # trailing comment
"""
    )
    assert parsed["sources"]["paper"]["queue"] == "foo.jsonl"


def test_parser_rejects_block_indent_mismatch() -> None:
    with pytest.raises(ValueError):
        _parse_simple_yaml(
            """
sources:
  paper:
    queue: foo
   bad: line
"""
        )
