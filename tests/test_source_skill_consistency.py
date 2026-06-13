"""Tests that every ``research_skill`` referenced from the source config
points at a real ``commands/*.md`` (or ``commands/research/*.md``) skill
file, and that every ``SourceTypeSpec.skills`` entry likewise resolves.

Catches the historical "research-news" dangling reference where
``sources/config.py`` and ``sources/registry.py`` cited a skill file
that nobody had created. Failing this test means a vault user typing
``/research <url>`` (or watching ``mem drain`` log a per-item skill
name) would hit an unknown-command error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.acquisition.sources import DEFAULT_CONFIG
from personal_mem.acquisition.sources.registry import all_specs

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_DIR = REPO_ROOT / "commands"


def _known_skill_names() -> set[str]:
    """Set of every skill filename (sans .md) under commands/ and
    commands/research/. Includes both the top-level skills (news, drain,
    research, …) and the subskills (research-paper, research-repo,
    research-article)."""
    names: set[str] = set()
    if not COMMANDS_DIR.exists():
        return names
    for path in COMMANDS_DIR.rglob("*.md"):
        if path.name.startswith("_"):  # _source_template.md etc.
            continue
        names.add(path.stem)
    return names


def test_every_research_skill_in_config_has_command_file() -> None:
    """Every ``sources.<slug>.research_skill`` must resolve to a real
    skill file under ``commands/``. Otherwise ``/research <url>`` and
    ``mem drain --source-type <slug>`` would dispatch to nothing."""
    known = _known_skill_names()
    missing: list[tuple[str, str]] = []
    for slug, source_cfg in DEFAULT_CONFIG["sources"].items():
        skill = source_cfg.get("research_skill")
        if not skill:
            continue
        if skill not in known:
            missing.append((slug, skill))
    assert not missing, (
        "DEFAULT_CONFIG references research_skill values without a "
        f"matching commands/*.md file: {missing}. Available skills: "
        f"{sorted(known)}"
    )


def test_every_registry_skill_has_command_file() -> None:
    """Every ``SourceTypeSpec.skills`` entry must resolve to a real
    skill file. The registry is read by ``mem sources show`` as
    cross-reference; broken entries surface as ghost links."""
    known = _known_skill_names()
    missing: list[tuple[str, str]] = []
    for spec in all_specs():
        for skill in spec.skills:
            if skill not in known:
                missing.append((spec.slug, skill))
    assert not missing, (
        "SourceTypeSpec.skills references non-existent skill files: "
        f"{missing}. Available skills: {sorted(known)}"
    )


def test_news_routes_to_news_skill_not_dangling_research_news() -> None:
    """Regression: ``news`` source type must NOT reference ``research-news``.

    Either the skill exists (option A — file under commands/research/)
    or the references drop to ``news`` (option B — current shape). This
    test pins the chosen shape so the dangling reference can't reappear.
    """
    known = _known_skill_names()

    news_cfg = DEFAULT_CONFIG["sources"]["news"]
    research_skill = news_cfg.get("research_skill")
    assert research_skill in known, (
        f"news.research_skill='{research_skill}' must resolve to a real "
        f"skill file (got {known})."
    )

    news_spec = next(s for s in all_specs() if s.slug == "news")
    for skill in news_spec.skills:
        assert skill in known, (
            f"news SourceTypeSpec lists skill='{skill}' that doesn't "
            f"exist on disk. Available: {sorted(known)}"
        )
