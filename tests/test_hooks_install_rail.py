"""rsi-rail fan-out of the rule folders: ``rules/agents`` and
``rules/output-styles`` are linked whole into the profile folders that name
them (Claude Code only), and the doctor counts them (thinkweave#334,
dec-dc79eeeb)."""

from __future__ import annotations

import pytest

from thinkweave.core import harness
from thinkweave.surfaces.cli import mcp_doctor
from thinkweave.surfaces.hooks.install import (
    RAIL_RULES_DIR,
    install_hooks,
    rail_rules,
    rail_targets,
)


@pytest.fixture
def rail_with_folders(rail_home):
    repo, home = rail_home
    (repo / "rules" / "agents").mkdir()
    (repo / "rules" / "agents" / "reviewer.md").write_text("# reviewer\n")
    (repo / "rules" / "output-styles").mkdir()
    (repo / "rules" / "output-styles" / "terse.md").write_text("# terse\n")
    return repo, home


def test_rules_live_under_the_rules_folder(rail_home):
    repo, _ = rail_home
    assert RAIL_RULES_DIR == "rules"
    assert rail_rules(repo) == repo / "rules" / "RULES.md"


def test_only_the_claude_code_profile_names_the_rule_folders(tmp_path):
    cc = harness.claude_code(home=tmp_path)
    assert cc.agents_dir == tmp_path / ".claude" / "agents"
    assert cc.output_styles_dir == tmp_path / ".claude" / "output-styles"
    for pid, make in harness.PROFILES.items():
        if pid == "claude-code":
            continue
        profile = make()
        assert profile.agents_dir is None, pid
        assert profile.output_styles_dir is None, pid


def test_rule_folders_are_linked_and_counted(rail_with_folders, capsys):
    repo, home = rail_with_folders

    install_hooks(scope="user")

    for name in ("agents", "output-styles"):
        link = home / ".claude" / name
        assert link.is_symlink() and link.resolve() == repo / "rules" / name
    # No other profile reads these folders, so nothing lands there.
    assert not (home / ".codex" / "agents").exists()
    assert not (home / ".pi" / "agent" / "output-styles").exists()
    assert "14 changed" in capsys.readouterr().out

    result = mcp_doctor.check_rail_links()
    assert result.passed
    assert result.detail == f"14 links into {repo} resolve"

    install_hooks(scope="user")
    assert "0 changed, 14 already current" in capsys.readouterr().out


def test_a_repo_without_the_folders_links_nothing_there(rail_home):
    repo, home = rail_home
    kinds = {t.kind for t in rail_targets(repo)}
    assert "folder" not in kinds

    install_hooks(scope="user")

    assert not (home / ".claude" / "agents").exists()
    assert mcp_doctor.check_rail_links().passed


def test_a_link_left_dangling_by_the_rename_is_repointed(rail_with_folders, capsys):
    """The pre-rename hand link aimed at ``claude/agents``; after the move it
    dangles and the install re-aims it under ``rules/``."""
    repo, home = rail_with_folders
    old = home / ".claude" / "agents"
    old.parent.mkdir(parents=True)
    old.symlink_to(repo / "claude" / "agents")
    assert {t.link: t.state for t in rail_targets(repo)}[old] == "dangling"

    install_hooks(scope="user")

    assert old.resolve() == repo / "rules" / "agents"
    assert f"relink {old}" in capsys.readouterr().out


def test_the_users_own_folder_is_left_alone_and_named(rail_with_folders, capsys):
    repo, home = rail_with_folders
    own = home / ".claude" / "output-styles"
    own.mkdir(parents=True)
    (own / "mine.md").write_text("# mine\n")

    install_hooks(scope="user")

    assert own.is_dir() and not own.is_symlink()
    assert f"skipped {own}: not a thinkweave link" in capsys.readouterr().out
    result = mcp_doctor.check_rail_links()
    assert not result.passed
    assert f"{own} (foreign-file)" in result.detail
