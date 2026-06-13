"""Tests for the user-overridable sources config loader.

Covers the three behaviours of ``load_user_config``:
  1. Missing file → returns deep-copied defaults.
  2. Partial override → merges per key, leaves untouched defaults intact.
  3. List/scalar overwrites → wholesale replacement (not merge).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thinkweave.acquisition.sources import DEFAULT_CONFIG, load_user_config
from thinkweave.acquisition.sources.config import _parse_simple_yaml


def test_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = load_user_config(tmp_path)
    assert cfg["auto_todo_extraction"] is True
    assert cfg["sources"]["paper"]["queue"] == "vault/.weave/queues/papers.jsonl"
    assert cfg["landing_files"]["state"] == "STATE.md"


def test_returns_deep_copy(tmp_path: Path) -> None:
    cfg = load_user_config(tmp_path)
    cfg["sources"]["paper"]["queue"] = "mutated"
    assert DEFAULT_CONFIG["sources"]["paper"]["queue"] == "vault/.weave/queues/papers.jsonl"


def test_user_override_merges_with_defaults(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "sources.yaml").write_text(
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
    assert cfg["sources"]["repo"]["queue"] == "vault/.weave/queues/repos.jsonl"


def test_list_overwrites_wholesale(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "sources.yaml").write_text(
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
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    # Tab-indented + missing colon — definitely fails parsing.
    (cfg_dir / "sources.yaml").write_text("not valid yaml here\n", encoding="utf-8")

    cfg = load_user_config(tmp_path)
    assert cfg["sources"]["paper"]["queue"] == "vault/.weave/queues/papers.jsonl"


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


# ---------------------------------------------------------------------------
# Newsletter source-type config — pins the per-type shape in DEFAULT_CONFIG
# and proves the shipped vault_templates/.weave/sources.yaml still parses with
# the two newsletter blocks in place.
# ---------------------------------------------------------------------------


def test_default_config_has_newsletter_events() -> None:
    cfg = DEFAULT_CONFIG["sources"]["newsletter-events"]
    assert cfg["drain_strategy"] == "subagent"
    assert cfg["subagent_type"] == "research-newsletter-worker"
    assert cfg["mail_provider"] == "gmail"
    assert cfg["processed_label"] == "weave-processed"
    assert cfg["lookback_days"] == 30
    assert "message_id" in cfg["dedup_keys"]
    # senders: [] is the canonical allowlist — must exist as an empty list
    # so the skill's halt guard ("no senders + no mail_query → refuse to
    # fetch") works without a KeyError on a fresh config.
    assert cfg["senders"] == []
    assert cfg["mail_query"] == ""


def test_default_config_has_newsletter_concepts() -> None:
    cfg = DEFAULT_CONFIG["sources"]["newsletter-concepts"]
    assert cfg["drain_strategy"] == "subagent"
    assert cfg["subagent_type"] == "research-newsletter-worker"
    assert cfg["mail_provider"] == "gmail"
    # Concept-grain newsletters get a longer lookback — technical posts age slower.
    assert cfg["lookback_days"] == 90
    assert "message_id" in cfg["dedup_keys"]
    assert cfg["senders"] == []
    assert cfg["mail_query"] == ""


def test_shipped_template_parses_with_newsletter_blocks() -> None:
    """The shipped vault_templates/config/sources.yaml must still load — and
    contain both newsletter blocks. Phase 3.1: senders list moved out of
    this file into PRIORITIES.yaml — assertion narrows to the processing
    knobs that still live here. Guards against accidental YAML breakage
    when the template is hand-edited."""
    import thinkweave

    pkg_root = Path(thinkweave.__file__).parent
    template = pkg_root / "vault_templates" / "config" / "sources.yaml"
    parsed = _parse_simple_yaml(template.read_text(encoding="utf-8"))
    sources = parsed["sources"]
    assert "newsletter-events" in sources
    assert "newsletter-concepts" in sources
    assert sources["newsletter-events"]["subagent_type"] == "research-newsletter-worker"
    assert sources["newsletter-concepts"]["lookback_days"] == 90
    # Senders allowlist now lives in PRIORITIES.yaml::intake.newsletter_*.senders,
    # not in this file. Confirm it's *not* present (would be a config-drift bug).
    assert "senders" not in sources["newsletter-events"]
    assert "senders" not in sources["newsletter-concepts"]


def test_user_override_can_populate_senders(tmp_path: Path) -> None:
    """Round-trip: a user file populating `senders:` merges cleanly with
    the shipped empty default."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "sources.yaml").write_text(
        """
sources:
  newsletter-events:
    senders: [alerts@bloomberg.com, levine@bloomberg.net, stratechery.com]
""",
        encoding="utf-8",
    )

    cfg = load_user_config(tmp_path)
    senders = cfg["sources"]["newsletter-events"]["senders"]
    assert senders == ["alerts@bloomberg.com", "levine@bloomberg.net", "stratechery.com"]
    # Other defaults preserved.
    assert cfg["sources"]["newsletter-events"]["mail_provider"] == "gmail"
    assert cfg["sources"]["newsletter-events"]["processed_label"] == "weave-processed"
