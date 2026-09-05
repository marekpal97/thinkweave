"""Pi's machine wiring through ``weave install`` / ``uninstall`` /
``doctor --mcp`` — the E3 posture landed by #114's follow-up.

Three facts drive every test here, all measured or read on 2026-09-03/05
(vault note n-fb74c7d0; pi-mcp-adapter 2.32.1 source):

* Pi core has NO MCP client. A ``mcpServers`` block in ``settings.json``
  parses and is silently ignored; the community ``pi-mcp-adapter`` extension
  reads the standard shape from ``~/.pi/agent/mcp.json`` / project
  ``.mcp.json`` instead. So the installer writes there, sweeps the legacy
  ``settings.json`` entry, and the doctor checks the adapter is installed.
* Pi discovers root ``*.md`` files in ``~/.pi/agent/skills`` as skills. The
  installer links the canonical ``commands/*.md`` there by name — NOT the
  Codex ``skills/`` bundle, whose ``../../docs`` pointer resolves to nothing
  from the Pi skills dir (the breakage that motivated this work).
* Worker-backed commands are not linked: Pi has no subagents.

Everything runs against a throwaway home; the real ``~/.pi`` is never read.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from thinkweave.core import harness
from thinkweave.surfaces.cli import install as inst
from thinkweave.surfaces.cli import mcp_doctor as md

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def pi_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Activate the Pi profile against a throwaway home; returns the
    ``~/.pi/agent`` dir every path assertion wants."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(harness, "_OVERRIDE", harness.pi(home=home))
    return home / ".pi" / "agent"


@pytest.fixture
def installable(monkeypatch: pytest.MonkeyPatch, stub_install_validators) -> None:
    """Neutralise the environment probes so ``cmd_install`` is deterministic.
    The project root is the REAL checkout: the skill links must point at the
    canonical ``commands/*.md`` files, so a fake root would test nothing."""
    monkeypatch.setattr(
        inst, "_check_scripts", lambda: inst.ScriptsCheck("ok", [], Path("/unused"))
    )
    monkeypatch.setattr(inst, "_detect_uv_path", lambda: "/uv")
    monkeypatch.setattr(inst, "_detect_project_root", lambda: REPO_ROOT)


def _install(**kw) -> None:
    inst.cmd_install(
        argparse.Namespace(**{"yes": True, "vault": None, "no_claude_md": True, **kw})
    )


def _uninstall() -> None:
    inst.cmd_uninstall(argparse.Namespace(yes=True))


def _worker_less_commands() -> set[str]:
    from thinkweave.core.skill_projection import iter_command_contracts

    return {
        c.name
        for c in iter_command_contracts(REPO_ROOT / "commands", REPO_ROOT / "agents")
        if not c.workers
    }


# --------------------------------------------------------------------------- #
# MCP registration: mcp.json, extras, legacy settings.json sweep
# --------------------------------------------------------------------------- #


class TestMcpRegistration:
    def test_entry_lands_in_the_adapters_global_file_with_its_keys(
        self, pi_home: Path, installable, requires_symlinks
    ):
        _install()
        doc = json.loads((pi_home / "mcp.json").read_text(encoding="utf-8"))
        entry = doc["mcpServers"]["thinkweave"]
        assert entry["command"] == "/uv"
        assert entry["args"][-1] == "thinkweave.surfaces.mcp.server"
        # The three adapter-only keys: direct bare `weave_*` tools, eager.
        assert entry["lifecycle"] == "eager"
        assert entry["directTools"] is True
        assert entry["toolPrefix"] == "none"
        # settings.json — the location the harness never reads — is NOT written.
        assert not (pi_home / "settings.json").exists()

    def test_install_sweeps_the_dead_settings_json_entry(
        self, pi_home: Path, installable, requires_symlinks, capsys
    ):
        settings = pi_home / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps(
                {
                    "defaultModel": "claude-opus-4-7",
                    "mcpServers": {
                        "thinkweave": {"command": "uv", "args": []},
                        "other": {"command": "npx", "args": ["-y", "x"]},
                    },
                    "packages": ["npm:pi-mcp-adapter"],
                }
            ),
            encoding="utf-8",
        )
        _install()
        after = json.loads(settings.read_text(encoding="utf-8"))
        assert "thinkweave" not in after["mcpServers"]
        # Everything that is not ours survives — this is the user's file.
        assert after["mcpServers"]["other"]["command"] == "npx"
        assert after["defaultModel"] == "claude-opus-4-7"
        assert after["packages"] == ["npm:pi-mcp-adapter"]
        assert "Removed the dead thinkweave MCP entry" in capsys.readouterr().out
        # …and the live registration went where the adapter reads it.
        assert (pi_home / "mcp.json").exists()

    def test_uninstall_sweeps_a_legacy_entry_even_with_no_live_one(
        self, pi_home: Path, installable, capsys
    ):
        settings = pi_home / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({"mcpServers": {"thinkweave": {"command": "uv"}}, "theme": "dark"}),
            encoding="utf-8",
        )
        _uninstall()
        out = capsys.readouterr().out
        assert f"legacy thinkweave MCP entry in {settings}" in out
        after = json.loads(settings.read_text(encoding="utf-8"))
        assert "thinkweave" not in after.get("mcpServers", {})
        assert after["theme"] == "dark"

    def test_a_malformed_settings_json_is_left_alone(
        self, pi_home: Path, installable, requires_symlinks
    ):
        settings = pi_home / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text("{not json", encoding="utf-8")
        _install()  # must not raise
        assert settings.read_text(encoding="utf-8") == "{not json"


# --------------------------------------------------------------------------- #
# root-file skill links
# --------------------------------------------------------------------------- #


class TestRootFileSkills:
    def test_every_worker_less_command_is_linked_by_name(
        self, pi_home: Path, installable, requires_symlinks
    ):
        _install()
        skills = pi_home / "skills"
        linked = {p.stem for p in skills.iterdir() if p.is_symlink()}
        assert linked == _worker_less_commands()
        assert (skills / "wrap.md").readlink() == REPO_ROOT / "commands" / "wrap.md"
        # Nested commands link flat, by their own name.
        assert (skills / "research-article.md").readlink() == (
            REPO_ROOT / "commands" / "research" / "research-article.md"
        )
        # The two that had no `name:` before (Pi would have named both from
        # the parent dir and collided) are linked under their own names.
        assert (skills / "hubs-link.md").is_symlink()
        assert (skills / "import-chatgpt.md").is_symlink()

    def test_worker_backed_commands_are_not_linked(
        self, pi_home: Path, installable, requires_symlinks
    ):
        _install()
        skills = pi_home / "skills"
        for name in ("drain", "dream", "news", "newsletter", "podcast", "youtube", "seed-enrich"):
            assert not (skills / f"{name}.md").exists(), name
        assert not (skills / "_source_template.md").exists()

    def test_links_carry_the_frontmatter_pi_needs(
        self, pi_home: Path, installable, requires_symlinks
    ):
        from thinkweave.core.vault import parse_frontmatter

        _install()
        for link in (pi_home / "skills").iterdir():
            meta, _ = parse_frontmatter(link.read_text(encoding="utf-8"))
            assert meta.get("name") == link.stem, link.name
            assert str(meta.get("description") or "").strip(), link.name

    def test_reinstall_is_a_no_op(
        self, pi_home: Path, installable, requires_symlinks, capsys
    ):
        _install()
        before = sorted((p.name, p.readlink()) for p in (pi_home / "skills").iterdir())
        capsys.readouterr()
        _install()
        after = sorted((p.name, p.readlink()) for p in (pi_home / "skills").iterdir())
        assert after == before
        assert "0 linked" in capsys.readouterr().out

    def test_codex_bundle_links_are_swept(
        self, pi_home: Path, installable, requires_symlinks, capsys
    ):
        """The breakage that motivated this: `thinkweave-*` dirs symlinked
        from the Codex projection bundle say "read ../../docs/…", which from
        ~/.pi/agent/skills is nothing."""
        skills = pi_home / "skills"
        skills.mkdir(parents=True)
        (skills / "thinkweave-wrap").symlink_to(REPO_ROOT / "skills" / "thinkweave-wrap")
        (skills / "thinkweave-gone").symlink_to(pi_home / "nowhere" / "thinkweave-gone")
        # A user's own skill dir named like ours but NOT a projection stays.
        mine = skills / "thinkweave-mine"
        mine.mkdir()
        (mine / "SKILL.md").write_text("---\nname: thinkweave-mine\ndescription: x\n---\n")
        _install()
        assert not (skills / "thinkweave-wrap").is_symlink()
        assert not (skills / "thinkweave-gone").is_symlink()
        assert (mine / "SKILL.md").exists()
        assert "2 Codex-bundle link(s) swept" in capsys.readouterr().out

    def test_stale_command_links_go_and_user_files_stay(
        self, pi_home: Path, installable, requires_symlinks, capsys
    ):
        skills = pi_home / "skills"
        skills.mkdir(parents=True)
        # A link to a command that no longer exists (renamed/removed).
        (skills / "bogus.md").symlink_to(REPO_ROOT / "commands" / "bogus.md")
        # A link aimed at an old checkout gets re-pointed.
        (skills / "wrap.md").symlink_to(Path("/old/checkout/commands/wrap.md"))
        # The user's own root-file skill, and a regular file in our way.
        (skills / "mine.md").write_text("---\nname: mine\ndescription: x\n---\n")
        (skills / "brief.md").write_text("not ours\n")
        _install()
        out = capsys.readouterr().out
        assert not (skills / "bogus.md").exists()
        assert (skills / "wrap.md").readlink() == REPO_ROOT / "commands" / "wrap.md"
        assert (skills / "mine.md").read_text().startswith("---\nname: mine")
        assert (skills / "brief.md").read_text() == "not ours\n"
        assert "skipped" in out and "brief.md" in out

    def test_uninstall_removes_only_our_links(
        self, pi_home: Path, installable, requires_symlinks, capsys
    ):
        _install()
        skills = pi_home / "skills"
        (skills / "mine.md").write_text("---\nname: mine\ndescription: x\n---\n")
        capsys.readouterr()
        _uninstall()
        out = capsys.readouterr().out
        assert "skill link(s) in" in out and "wrap.md" in out
        assert [p.name for p in skills.iterdir()] == ["mine.md"]

    def test_symlink_targets_are_worker_less_on_every_row_that_links(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The link set is profile-driven: a row with subagents would link
        the worker-backed contracts too. No such row exists today, so this
        pins the rule rather than an example."""
        monkeypatch.setattr(inst, "_detect_project_root", lambda: REPO_ROOT)
        monkeypatch.setattr(harness, "_OVERRIDE", harness.pi(home=tmp_path))
        names = {link.stem for link, _ in inst._root_file_skill_links()}
        assert names == _worker_less_commands()


# --------------------------------------------------------------------------- #
# doctor: the MCP client is an extension
# --------------------------------------------------------------------------- #


class TestDoctorAdapterCheck:
    def _settings(self, pi_home: Path, packages: list, project: Path | None = None) -> None:
        path = (project / ".pi" / "settings.json") if project else pi_home / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"packages": packages}), encoding="utf-8")

    def _unpack(self, pi_home: Path, name: str = "pi-mcp-adapter") -> Path:
        pkg = pi_home / "npm" / "node_modules" / name
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "%s"}' % name, encoding="utf-8")
        return pkg

    def test_absent_fails_with_the_install_command(self, pi_home: Path, tmp_path: Path):
        r = md.check_mcp_client_extension(tmp_path)
        assert not r.passed
        assert "pi-mcp-adapter is not listed" in r.detail
        assert "pi install npm:pi-mcp-adapter" in r.fix

    def test_listed_and_unpacked_passes(self, pi_home: Path, tmp_path: Path):
        self._settings(pi_home, ["npm:pi-mcp-adapter"])
        pkg = self._unpack(pi_home)
        r = md.check_mcp_client_extension(tmp_path)
        assert r.passed and str(pkg) in r.detail

    def test_listed_but_not_unpacked_fails(self, pi_home: Path, tmp_path: Path):
        self._settings(pi_home, ["npm:pi-mcp-adapter@2.32.1"])
        r = md.check_mcp_client_extension(tmp_path)
        assert not r.passed and "not unpacked" in r.detail
        assert "pi install npm:pi-mcp-adapter" in r.fix

    def test_scoped_and_pinned_specs_are_recognised(self, pi_home: Path, tmp_path: Path):
        self._settings(pi_home, [{"source": "npm:@fork/pi-mcp-adapter@1.0.0", "skills": []}])
        self._unpack(pi_home, "@fork/pi-mcp-adapter")
        assert md.check_mcp_client_extension(tmp_path).passed

    def test_project_settings_alone_pass(self, pi_home: Path, tmp_path: Path):
        project = tmp_path / "proj"
        self._settings(pi_home, ["npm:pi-mcp-adapter"], project=project)
        r = md.check_mcp_client_extension(project)
        assert r.passed and ".pi/settings.json" in r.detail.replace("\\", "/")

    def test_a_lookalike_does_not_count(self, pi_home: Path, tmp_path: Path):
        self._settings(pi_home, ["npm:pi-mcp-adapter-ui", "npm:notpi-mcp-adapter"])
        assert not md.check_mcp_client_extension(tmp_path).passed

    def test_the_check_leads_the_pi_report_and_is_absent_elsewhere(
        self, pi_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        monkeypatch.setattr(md, "_EXTRA_MODULES", (("json", "stdlib", "always"),))
        names = [c.name for c in md.run_mcp_doctor(tmp_path).checks]
        assert names[0] == "MCP client extension"
        monkeypatch.setattr(harness, "_OVERRIDE", harness.claude_code(home=tmp_path))
        names = [c.name for c in md.run_mcp_doctor(tmp_path).checks]
        assert "MCP client extension" not in names


# --------------------------------------------------------------------------- #
# what the user is told
# --------------------------------------------------------------------------- #


class TestPiNextStepsAndBlock:
    def test_next_steps_name_the_adapter_and_the_wrap_skill(self, pi_home: Path, capsys):
        inst._print_next_steps()
        out = capsys.readouterr().out
        assert "pi install npm:pi-mcp-adapter" in out
        assert "weave hooks install --scope user --harness pi" in out
        assert "weave import pi --enrich" in out
        assert "/skill:wrap" in out
        assert "/onboard" not in out
        assert "has no thinkweave skills" not in out

    def test_claude_code_and_codex_screens_carry_no_adapter_step(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        for factory in (harness.claude_code, harness.codex):
            monkeypatch.setattr(harness, "_OVERRIDE", factory(home=tmp_path))
            inst._print_next_steps()
            out = capsys.readouterr().out
            assert "pi install" not in out and "/skill:" not in out

    def test_rendered_block_is_the_e3_posture(
        self, pi_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(inst, "_detect_project_root", lambda: REPO_ROOT)
        block = inst._render_claude_md_block()
        assert "{weave}" not in block
        assert "NEVER call `weave_extract` mid-session or per turn" in block
        assert "/skill:wrap" in block
        assert "pi-mcp-adapter" in block
        assert f"`{REPO_ROOT / 'bin' / 'weave'} add" in block
