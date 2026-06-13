"""Tests for ``sources/priorities.py`` — PRIORITIES.yaml loader.

Covers the missing-file, malformed-file, and valid-file paths plus the
typed accessor helpers (``focus_active_projects``, ``focus_watch_themes``,
``intake_for``).
"""

from __future__ import annotations

from pathlib import Path

from personal_mem.acquisition.sources.priorities import (
    focus_active_projects,
    focus_watch_themes,
    intake_for,
    load_priorities,
    priorities_path,
)


def test_load_priorities_none_vault_returns_empty():
    assert load_priorities(None) == {}


def test_load_priorities_missing_file_returns_empty(tmp_path: Path):
    # No vault/config/PRIORITIES.yaml exists
    assert load_priorities(tmp_path) == {}


def test_priorities_path_returns_canonical(tmp_path: Path):
    p = priorities_path(tmp_path)
    assert p == tmp_path / "config" / "PRIORITIES.yaml"


def test_priorities_path_none_vault_returns_none():
    assert priorities_path(None) is None


def test_load_priorities_parses_focus_block(tmp_path: Path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "PRIORITIES.yaml").write_text(
        "focus:\n"
        "  active_projects: [personal_mem, research]\n"
        "  watch_themes: [thm-aaaa1111]\n",
        encoding="utf-8",
    )

    doc = load_priorities(tmp_path)
    assert "focus" in doc
    assert doc["focus"]["active_projects"] == ["personal_mem", "research"]
    assert doc["focus"]["watch_themes"] == ["thm-aaaa1111"]


def test_load_priorities_parses_intake_block(tmp_path: Path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "PRIORITIES.yaml").write_text(
        "intake:\n"
        "  news:\n"
        "    drain_window_days: 7\n"
        "    outlets:\n"
        "      reuters:\n"
        "        name: Reuters\n"
        "        feeds: [https://example.com/rss]\n"
        "        tier: 1\n"
        "  newsletter_events:\n"
        "    senders: [alerts@bloomberg.com, levine@bloomberg.net]\n",
        encoding="utf-8",
    )

    doc = load_priorities(tmp_path)
    news = doc["intake"]["news"]
    assert news["drain_window_days"] == 7
    assert "reuters" in news["outlets"]
    assert news["outlets"]["reuters"]["tier"] == 1

    newsletter = doc["intake"]["newsletter_events"]
    assert newsletter["senders"] == ["alerts@bloomberg.com", "levine@bloomberg.net"]


def test_focus_active_projects_and_watch_themes(tmp_path: Path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "PRIORITIES.yaml").write_text(
        "focus:\n"
        "  active_projects: [a, b]\n"
        "  watch_themes: [thm-1, thm-2]\n",
        encoding="utf-8",
    )

    p = load_priorities(tmp_path)
    assert focus_active_projects(p) == ["a", "b"]
    assert focus_watch_themes(p) == ["thm-1", "thm-2"]


def test_intake_for_normalises_dashes_to_underscores(tmp_path: Path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "PRIORITIES.yaml").write_text(
        "intake:\n"
        "  newsletter_events:\n"
        "    senders: [a@b.com]\n",
        encoding="utf-8",
    )

    p = load_priorities(tmp_path)
    # source-type slug uses dashes; intake key uses underscores
    block = intake_for(p, "newsletter-events")
    assert block.get("senders") == ["a@b.com"]


def test_intake_for_missing_returns_empty():
    assert intake_for({}, "news") == {}
    assert intake_for({"intake": {}}, "news") == {}
    assert intake_for({"intake": {"news": None}}, "news") == {}
