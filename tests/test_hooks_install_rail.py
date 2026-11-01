"""rsi-rail fan-out of the rule folders: ``rules/agents`` and
``rules/output-styles`` are linked whole into the profile folders that name
them (Claude Code only), and the doctor counts them (thinkweave#334,
dec-dc79eeeb). Then the rail's ``hooks/hooks.toml`` rendered into every
profile carrying thinkweave hooks, and the ``rsi-rail hooks`` doctor row
(dec-4f158b58 layers 2 and 3)."""

from __future__ import annotations

import json
import subprocess

import pytest

from thinkweave.core import harness
from thinkweave.surfaces.cli import mcp_doctor
from thinkweave.surfaces.hooks.install import (
    RAIL_RULES_DIR,
    install_hooks,
    rail_rules,
    rail_targets,
    uninstall_hooks,
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


# --------------------------------------------------------------------------- #
# hooks/hooks.toml rendered per profile (dec-4f158b58 layers 2 and 3)
# --------------------------------------------------------------------------- #

RAIL_TOML = """
[SubagentStart]
enabled = false

[[foreign]]
name = "note"
event = "Stop"
script = "note.sh"
"""

NOTE_SH = '#!/bin/sh\necho stop >> "$(dirname "$0")/note.log"\n'


def _write_rail_hooks(repo, toml=RAIL_TOML):
    hooks = repo / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "hooks.toml").write_text(toml)
    script = hooks / "note.sh"
    script.write_text(NOTE_SH)
    script.chmod(0o755)
    return hooks


def _install_everywhere(monkeypatch, home):
    """Install thinkweave hooks into all four profiles at user scope, the
    way a user does once per harness (`weave hooks install --harness X`)."""
    for make in harness.PROFILES.values():
        monkeypatch.setattr(harness, "_OVERRIDE", make(home=home))
        install_hooks(scope="user")
    monkeypatch.setattr(harness, "_OVERRIDE", harness.claude_code(home=home))


def _cli_install(*argv):
    from thinkweave.surfaces.cli.hooks import cmd_hooks
    from thinkweave.surfaces.cli.parser import build_parser

    cmd_hooks(build_parser().parse_args(["hooks", "install", *argv]))


def _settings(path):
    return json.loads(path.read_text())


def _commands(settings, event):
    return [h["command"] for e in settings.get("hooks", {}).get(event, []) for h in e["hooks"]]


@pytest.fixture
def four_profiles(rail_home, monkeypatch):
    repo, home = rail_home
    _install_everywhere(monkeypatch, home)
    paths = {
        "claude-code": harness.claude_code(home=home).user_settings,
        "codex": harness.codex(home=home).user_settings,
        "pi": harness.pi(home=home).plugins_root / "thinkweave.ts",
        "opencode": harness.opencode(home=home).plugins_root / "thinkweave.ts",
    }
    return repo, home, paths


def test_absent_rail_file_changes_nothing(four_profiles, capsys):
    repo, home, paths = four_profiles
    before = {k: p.read_text() for k, p in paths.items()}
    capsys.readouterr()

    _cli_install("--scope", "user")

    assert {k: p.read_text() for k, p in paths.items()} == before
    assert "rsi-rail hooks" not in capsys.readouterr().out
    assert 'export { default } from "' in before["pi"]
    result = mcp_doctor.check_rail_hooks()
    assert result.passed and "canonical registrations" in result.detail


def test_one_install_renders_every_profile(four_profiles, capsys):
    """Demo criterion 4: SubagentStart disabled, one foreign hook on Stop,
    `weave hooks install --scope user` under a temp HOME."""
    repo, home, paths = four_profiles
    hooks_dir = _write_rail_hooks(repo)
    note = (hooks_dir / "note.sh").as_posix()

    _cli_install("--scope", "user")

    out = capsys.readouterr().out
    assert f"rsi-rail hooks from {hooks_dir / 'hooks.toml'}" in out
    for kind in ("claude-code", "codex"):
        settings = _settings(paths[kind])
        assert "SubagentStart" not in settings["hooks"], kind
        assert "SubagentStop" in settings["hooks"], kind
        stops = _commands(settings, "Stop")
        assert f'"{note}"' in stops, kind
        assert any("weave-hook-launch" in c for c in stops), kind
    for kind in ("pi", "opencode"):
        stub = paths[kind].read_text()
        assert "withRail(" in stub, kind
        payload = json.loads(stub.split("withRail(", 1)[1].rsplit(");", 1)[0])
        assert "SubagentStart" not in payload["events"], kind
        assert "Stop" in payload["events"], kind
        assert payload["foreign"] == [
            {"event": "Stop", "matcher": "", "name": "note", "script": note}
        ], kind

    result = mcp_doctor.check_rail_hooks()
    assert result.name == "rsi-rail hooks"
    assert result.passed, result.detail
    assert "claude-code, codex, pi, opencode" in result.detail

    # The rendered command runs as a settings-file harness runs it.
    subprocess.run(["sh", "-c", f'"{note}"'], check=True)
    assert (hooks_dir / "note.log").read_text() == "stop\n"

    _cli_install("--scope", "user")
    assert "0 changed, 4 already current" in capsys.readouterr().out


def test_doctor_names_a_stale_profile_after_a_rail_commit(four_profiles):
    repo, home, paths = four_profiles
    _write_rail_hooks(repo)
    _cli_install("--scope", "user")
    assert mcp_doctor.check_rail_hooks().passed

    (repo / "hooks" / "hooks.toml").write_text(RAIL_TOML.replace("false", "true"))

    result = mcp_doctor.check_rail_hooks()
    assert not result.passed
    assert "claude-code: stale" in result.detail
    assert "SubagentStart is missing, the rail registers it" in result.detail
    assert "pi: stale" in result.detail
    assert result.fix == "weave hooks install --scope user"

    _cli_install("--scope", "user")
    assert mcp_doctor.check_rail_hooks().passed


def test_an_invalid_rail_file_stops_the_install_by_name(four_profiles, capsys):
    repo, home, paths = four_profiles
    _write_rail_hooks(repo, "[SubagentBegin]\nenabled = false\n")
    before = paths["codex"].read_text()

    with pytest.raises(SystemExit):
        _cli_install("--scope", "user")

    assert "unknown event 'SubagentBegin'" in capsys.readouterr().err
    assert paths["codex"].read_text() == before
    result = mcp_doctor.check_rail_hooks()
    assert not result.passed and "SubagentBegin" in result.detail


def test_install_then_uninstall_returns_each_profile_to_its_files(
    rail_home, monkeypatch
):
    repo, home = rail_home
    mine = {"type": "command", "command": "/usr/local/bin/mine"}
    own = {"model": "opus", "hooks": {"Stop": [{"matcher": "", "hooks": [mine]}]}}
    for make in (harness.claude_code, harness.codex):
        path = make(home=home).user_settings
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(own, indent=2) + "\n")
    _install_everywhere(monkeypatch, home)
    _write_rail_hooks(repo)
    _cli_install("--scope", "user")

    for make in harness.PROFILES.values():
        profile = make(home=home)
        monkeypatch.setattr(harness, "_OVERRIDE", profile)
        uninstall_hooks(scope="user")
        if profile.hook_mechanism == "extension":
            assert not (profile.plugins_root / "thinkweave.ts").exists()
        else:
            assert _settings(profile.user_settings) == own, profile.id


def test_plugin_route_carries_foreign_hooks_and_names_what_it_cannot(
    rail_home, monkeypatch, capsys
):
    """Claude Code with the plugin active: the plugin's hooks.json stays the
    canonical definitions and the user settings file gets only the foreign
    hook. The disabled event is renderable: Claude Code has no per-event
    suppression of a plugin hook, so the handler reads hooks.toml and exits
    early on it (tests/tasks/test_hook_behaviour.py); changed matchers are
    what stays not renderable."""
    repo, home = rail_home
    cc = harness.claude_code(home=home)
    cc.installed_plugins.parent.mkdir(parents=True)
    cc.installed_plugins.write_text('{"plugins": {"thinkweave@mp": []}}')
    hooks_dir = _write_rail_hooks(repo)

    _cli_install("--scope", "user")

    out = capsys.readouterr().out
    assert "not renderable" not in out
    settings = _settings(cc.user_settings)
    assert list(settings["hooks"]) == ["Stop"]
    assert _commands(settings, "Stop") == [f'"{(hooks_dir / "note.sh").as_posix()}"']
    assert mcp_doctor.check_rail_hooks().passed

    # Changed matchers stay inexpressible on the plugin route.
    _write_rail_hooks(repo, RAIL_TOML + '[PostToolUse]\nmatchers = ["Write|Edit"]\n')
    _cli_install("--scope", "user")
    assert "not renderable on claude-code: PostToolUse matchers" in capsys.readouterr().out
    result = mcp_doctor.check_rail_hooks()
    assert not result.passed
    assert "claude-code: not renderable: PostToolUse matchers" in result.detail


def test_rail_matchers_replace_the_post_tool_use_slots(rail_home, monkeypatch):
    repo, home = rail_home
    monkeypatch.setattr(harness, "_OVERRIDE", harness.codex(home=home))
    install_hooks(scope="user")
    _write_rail_hooks(repo, '[PostToolUse]\nmatchers = ["Write|Edit"]\n')

    install_hooks(scope="user")

    settings = _settings(harness.codex(home=home).user_settings)
    assert [e["matcher"] for e in settings["hooks"]["PostToolUse"]] == ["Write|Edit"]
    assert mcp_doctor.check_rail_hooks().passed
