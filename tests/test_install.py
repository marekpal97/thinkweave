"""Regression tests for ``weave install`` and the project-scope ``.mcp.json``.

The three MCP-registration paths — project-scope ``.mcp.json``, machine-
scope ``~/.claude.json`` (written by ``weave install``), and the plugin
manifests under ``.claude-plugin/`` — must all produce equivalent server
entries so that Claude Code's MCP launcher sees the same invocation
regardless of how the user installed the package.

"Equivalent" here means: same ``args`` list, same ``env`` keys, and
command resolves to ``uv`` (the absolute path baked into
``~/.claude.json`` and the bare ``"uv"`` used in checked-in manifests
are both legitimate forms).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thinkweave.surfaces.cli import install as install_mod
from thinkweave.surfaces.cli.install import (
    CLAUDE_MD_BLOCK_BODY,
    CLAUDE_MD_BLOCK_END,
    CLAUDE_MD_BLOCK_START,
    SERVER_NAME,
    _build_server_entry,
    _check_pyproject_reachable,
    _check_uv_available,
    _detect_project_root,
    _extract_claude_md_block,
    _plugin_provides_mcp,
    _render_claude_md_block,
    _splice_claude_md_block,
    cmd_dev_link,
    cmd_dev_unlink,
    cmd_install,
    cmd_uninstall,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT_MCP_JSON = REPO_ROOT / ".mcp.json"
PLUGIN_MANIFEST_ROOT = REPO_ROOT / ".claude-plugin" / "plugin.json"


def _command_basename(cmd: str) -> str:
    return Path(cmd).name


def _normalise_args_for_compare(args: list[str], placeholder_for_project: str) -> list[str]:
    """Replace the ``--project <value>`` slot with a sentinel so absolute,
    relative, and ``${CLAUDE_PLUGIN_ROOT}`` forms compare equal.
    """
    out: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--project" and i + 1 < len(args):
            out.extend(["--project", placeholder_for_project])
            i += 2
            continue
        out.append(args[i])
        i += 1
    return out


def _entry_from_install() -> dict:
    project_root = _detect_project_root()
    return _build_server_entry(project_root, vault_root=None)


def _entry_from_mcp_json() -> dict:
    data = json.loads(PROJECT_MCP_JSON.read_text(encoding="utf-8"))
    return data["mcpServers"]["thinkweave"]


def _entry_from_plugin_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "mcpServers" in data, f"{path} must declare mcpServers inline"
    return data["mcpServers"]["thinkweave"]


class TestMcpInvocationConsistency:
    """All three scopes resolve to the same launcher + args shape."""

    def test_mcp_json_uses_uv_run_shape(self):
        entry = _entry_from_mcp_json()
        assert _command_basename(entry["command"]) == "uv"
        assert entry["args"][:2] == ["run", "--project"]
        assert "--extra" in entry["args"]
        assert "mcp" in entry["args"]
        assert entry["args"][-1] == "weave-mcp"

    def test_weave_install_uses_uv_run_shape(self):
        entry = _entry_from_install()
        assert _command_basename(entry["command"]) == "uv"
        assert entry["args"][:2] == ["run", "--project"]
        assert "--extra" in entry["args"]
        assert "mcp" in entry["args"]
        assert entry["args"][-1] == "weave-mcp"

    def test_plugin_manifest_root_uses_uv_run_shape(self):
        entry = _entry_from_plugin_manifest(PLUGIN_MANIFEST_ROOT)
        assert _command_basename(entry["command"]) == "uv"
        assert entry["args"][:2] == ["run", "--project"]
        assert "--extra" in entry["args"]
        assert "mcp" in entry["args"]
        assert entry["args"][-1] == "weave-mcp"

    def test_all_scopes_normalise_to_same_args_shape(self):
        """Once the per-scope project path is replaced with a sentinel,
        every config produces exactly the same args list and env keys."""
        sentinel = "<PROJECT_PATH>"
        install_entry = _entry_from_install()
        mcp_entry = _entry_from_mcp_json()
        plugin_root_entry = _entry_from_plugin_manifest(PLUGIN_MANIFEST_ROOT)

        norm_install = _normalise_args_for_compare(install_entry["args"], sentinel)
        norm_mcp = _normalise_args_for_compare(mcp_entry["args"], sentinel)
        norm_root = _normalise_args_for_compare(plugin_root_entry["args"], sentinel)

        assert norm_install == norm_mcp == norm_root, (
            f"args shape diverged:\n"
            f"  install={norm_install}\n"
            f"  mcp.json={norm_mcp}\n"
            f"  plugin/root={norm_root}"
        )

        # env keys (not values — install may inject THINKWEAVE_VAULT)
        for entry in (mcp_entry, plugin_root_entry):
            assert entry.get("env", {}) == {}, (
                f"checked-in manifest must not bake env vars: {entry}"
            )
        # install with vault_root=None matches
        assert install_entry.get("env", {}) == {}


class TestClaudeMdBlock:
    """The user-global CLAUDE.md splice must be idempotent, must never
    edit bytes outside its sentinels, and must survive every degenerate
    initial state (empty file, no sentinels, only one sentinel)."""

    def test_render_contains_sentinels_and_body(self):
        rendered = _render_claude_md_block()
        assert rendered.startswith(CLAUDE_MD_BLOCK_START)
        assert rendered.endswith(CLAUDE_MD_BLOCK_END)
        assert CLAUDE_MD_BLOCK_BODY in rendered

    def test_extract_returns_none_when_absent(self):
        assert _extract_claude_md_block("# my notes\n\nsome content") is None

    def test_extract_returns_none_when_only_start_sentinel(self):
        text = f"# notes\n{CLAUDE_MD_BLOCK_START}\nbody\n(no end)\n"
        assert _extract_claude_md_block(text) is None

    def test_extract_round_trips_rendered_block(self):
        block = _render_claude_md_block()
        text = f"# my notes\n\n{block}\n\n# more\n"
        assert _extract_claude_md_block(text) == block

    def test_splice_appends_when_absent(self):
        block = _render_claude_md_block()
        result = _splice_claude_md_block("# my notes\n", block)
        assert "# my notes\n" in result
        assert block in result
        assert result.count(CLAUDE_MD_BLOCK_START) == 1
        assert result.count(CLAUDE_MD_BLOCK_END) == 1

    def test_splice_into_empty_file(self):
        block = _render_claude_md_block()
        result = _splice_claude_md_block("", block)
        assert block in result
        assert result.count(CLAUDE_MD_BLOCK_START) == 1

    def test_splice_is_idempotent_when_block_already_present(self):
        block = _render_claude_md_block()
        first = _splice_claude_md_block("# notes\n", block)
        second = _splice_claude_md_block(first, block)
        assert first == second
        assert second.count(CLAUDE_MD_BLOCK_START) == 1

    def test_splice_replaces_stale_block_in_place(self):
        stale = f"{CLAUDE_MD_BLOCK_START}\nold body that drifted\n{CLAUDE_MD_BLOCK_END}"
        text = f"# my notes\n\n{stale}\n\n# trailing content\n"
        fresh = _render_claude_md_block()
        result = _splice_claude_md_block(text, fresh)
        assert fresh in result
        assert "old body that drifted" not in result
        # bytes outside the sentinels survive untouched
        assert "# my notes\n" in result
        assert "# trailing content\n" in result
        assert result.count(CLAUDE_MD_BLOCK_START) == 1

    def test_splice_appends_when_only_start_sentinel_present(self):
        """Corrupt initial state: a dangling start with no end. We append
        a fresh block rather than try to repair the corrupt one — keeps
        the splice rule 'never modify bytes outside the sentinels' intact."""
        corrupt = f"# notes\n{CLAUDE_MD_BLOCK_START}\nhalf-written body\n"
        block = _render_claude_md_block()
        result = _splice_claude_md_block(corrupt, block)
        assert block in result
        assert "half-written body" in result  # preserved
        assert result.count(CLAUDE_MD_BLOCK_START) == 2  # the dangling + the fresh


class TestMcpJsonSyntax:
    """Basic sanity: every checked-in MCP-bearing JSON file is valid JSON
    and parses to a dict with the expected top-level shape."""

    @pytest.mark.parametrize(
        "path",
        [PROJECT_MCP_JSON, PLUGIN_MANIFEST_ROOT],
        ids=lambda p: str(p.relative_to(REPO_ROOT)),
    )
    def test_file_is_valid_json(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "mcpServers" in data
        assert "thinkweave" in data["mcpServers"]


@pytest.fixture
def stub_install_validators(monkeypatch):
    """Stub the three install-time validators (`_check_uv_available`,
    `_check_pyproject_reachable`, `_uv_sync`) so cmd_install tests don't
    require uv on PATH, a real pyproject in the sandbox, or pay sync time.
    Tests that specifically validate these helpers don't use this fixture."""
    from thinkweave.surfaces.cli import install as inst
    monkeypatch.setattr(inst, "_check_uv_available", lambda: None)
    monkeypatch.setattr(inst, "_check_pyproject_reachable", lambda root: None)
    monkeypatch.setattr(inst, "_uv_sync", lambda root: None)


@pytest.fixture
def fake_claude_home(tmp_path, monkeypatch):
    """Sandbox the four install touchpoints (CLAUDE_JSON, CLAUDE_MD,
    MARKER, PLUGINS_ROOT) so the cmd_* tests never reach the real
    ``~/.claude``."""
    fake = tmp_path / "claude_home"
    fake.mkdir()
    claude_json = fake / ".claude.json"
    claude_md = fake / ".claude" / "CLAUDE.md"
    marker = fake / ".claude" / "thinkweave_paused.json"
    plugins_root = fake / ".claude" / "plugins"
    monkeypatch.setattr(install_mod, "CLAUDE_JSON", claude_json)
    monkeypatch.setattr(install_mod, "CLAUDE_MD", claude_md)
    monkeypatch.setattr(install_mod, "MARKER", marker)
    monkeypatch.setattr(install_mod, "PLUGINS_ROOT", plugins_root)
    return {
        "claude_json": claude_json,
        "claude_md": claude_md,
        "marker": marker,
        "plugins_root": plugins_root,
    }


def _write_plugin_manifest(plugins_root: Path, name: str, declares_thinkweave: bool):
    """Helper: stub a plugin manifest under
    ``<plugins_root>/<name>/.claude-plugin/plugin.json``."""
    manifest_dir = plugins_root / name / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = manifest_dir / "plugin.json"
    data: dict = {"name": name, "version": "0.0.1"}
    if declares_thinkweave:
        data["mcpServers"] = {SERVER_NAME: {"type": "stdio", "command": "uv", "args": []}}
    manifest.write_text(json.dumps(data), encoding="utf-8")
    return manifest


def _ns(**kw):
    import argparse

    return argparse.Namespace(
        **{"yes": False, "vault": None, "no_claude_md": False, **kw}
    )


class TestUninstall:
    """`weave uninstall` is the inverse of `weave install`. It must:
    no-op cleanly when nothing was installed; require --yes before
    touching files; preserve sibling MCP servers and surrounding
    CLAUDE.md content; and clean up any leftover pause marker."""

    def test_noop_when_nothing_installed(self, fake_claude_home, capsys):
        cmd_uninstall(_ns(yes=True))
        out = capsys.readouterr().out
        assert "Nothing to remove" in out

    def test_dry_run_without_yes_exits_and_writes_nothing(
        self, fake_claude_home, capsys
    ):
        install_mod._restore_mcp_entry()
        before = fake_claude_home["claude_json"].read_text()
        with pytest.raises(SystemExit):
            cmd_uninstall(_ns(yes=False))
        # file unchanged after preview-only invocation
        assert fake_claude_home["claude_json"].read_text() == before
        out = capsys.readouterr().out
        assert "will remove" in out
        assert "Re-run with --yes" in out

    def test_removes_mcp_entry_and_preserves_siblings(
        self, fake_claude_home, capsys
    ):
        cj = fake_claude_home["claude_json"]
        install_mod._restore_mcp_entry()
        # bolt on a sibling server + a top-level field
        cfg = json.loads(cj.read_text())
        cfg["mcpServers"]["other"] = {"command": "x"}
        cfg["someOther"] = "preserved"
        cj.write_text(json.dumps(cfg), encoding="utf-8")

        cmd_uninstall(_ns(yes=True))

        cfg_after = json.loads(cj.read_text())
        assert install_mod.SERVER_NAME not in cfg_after["mcpServers"]
        assert "other" in cfg_after["mcpServers"]
        assert cfg_after["someOther"] == "preserved"

    def test_removes_claude_md_block_preserves_surrounding(
        self, fake_claude_home
    ):
        md = fake_claude_home["claude_md"]
        md.parent.mkdir(parents=True, exist_ok=True)
        original = "# my notes\n\nbefore\n"
        spliced = install_mod._splice_claude_md_block(
            original, install_mod._render_claude_md_block()
        ) + "after\n"
        md.write_text(spliced, encoding="utf-8")

        cmd_uninstall(_ns(yes=True))

        text = md.read_text()
        assert install_mod.CLAUDE_MD_BLOCK_START not in text
        assert "# my notes" in text
        assert "before" in text
        assert "after" in text

    def test_clears_leftover_pause_marker(self, fake_claude_home, capsys):
        # simulate: user paused (marker exists) then ran uninstall
        marker = fake_claude_home["marker"]
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")

        cmd_uninstall(_ns(yes=True))
        assert not marker.exists()

    def test_full_round_trip_install_then_uninstall(
        self, fake_claude_home
    ):
        # install both surfaces
        install_mod._restore_mcp_entry()
        fake_claude_home["claude_md"].parent.mkdir(parents=True, exist_ok=True)
        fake_claude_home["claude_md"].write_text(
            install_mod._splice_claude_md_block(
                "", install_mod._render_claude_md_block()
            ),
            encoding="utf-8",
        )

        cmd_uninstall(_ns(yes=True))

        cfg = json.loads(fake_claude_home["claude_json"].read_text())
        assert install_mod.SERVER_NAME not in cfg.get("mcpServers", {})
        assert install_mod.CLAUDE_MD_BLOCK_START not in (
            fake_claude_home["claude_md"].read_text()
        )


class TestPluginDetection:
    """`_plugin_provides_mcp` is the parity bridge: when an installed
    plugin manifest declares the thinkweave MCP server, `weave install`
    must skip the ~/.claude.json write to avoid duplicate registration,
    but still add the CLAUDE.md nudge."""

    def test_returns_none_when_plugins_root_missing(self, fake_claude_home):
        # plugins_root doesn't exist by default in the fixture
        assert _plugin_provides_mcp() is None

    def test_returns_none_when_no_manifest_declares_thinkweave(
        self, fake_claude_home
    ):
        _write_plugin_manifest(
            fake_claude_home["plugins_root"], "other-plugin",
            declares_thinkweave=False,
        )
        assert _plugin_provides_mcp() is None

    def test_returns_manifest_path_when_plugin_claims_thinkweave(
        self, fake_claude_home
    ):
        manifest = _write_plugin_manifest(
            fake_claude_home["plugins_root"], "thinkweave",
            declares_thinkweave=True,
        )
        result = _plugin_provides_mcp()
        assert result == manifest

    def test_finds_thinkweave_among_multiple_plugins(self, fake_claude_home):
        _write_plugin_manifest(
            fake_claude_home["plugins_root"], "other-1",
            declares_thinkweave=False,
        )
        target = _write_plugin_manifest(
            fake_claude_home["plugins_root"], "thinkweave",
            declares_thinkweave=True,
        )
        _write_plugin_manifest(
            fake_claude_home["plugins_root"], "other-2",
            declares_thinkweave=False,
        )
        assert _plugin_provides_mcp() == target

    def test_skips_corrupt_manifest_without_aborting(self, fake_claude_home):
        # one corrupt manifest, one valid declaring manifest — detector
        # must skip the corrupt and still find the valid one
        corrupt_dir = fake_claude_home["plugins_root"] / "broken" / ".claude-plugin"
        corrupt_dir.mkdir(parents=True)
        (corrupt_dir / "plugin.json").write_text("{ not valid json", encoding="utf-8")
        target = _write_plugin_manifest(
            fake_claude_home["plugins_root"], "thinkweave",
            declares_thinkweave=True,
        )
        assert _plugin_provides_mcp() == target


class TestPluginRouteInstall:
    """When the plugin manifest already provides the MCP entry, `cmd_install`
    must (a) not touch ~/.claude.json, (b) still write the CLAUDE.md
    nudge — that's the parity bridge."""

    def test_plugin_route_skips_mcp_write_and_does_claude_md(
        self, fake_claude_home, stub_install_validators, monkeypatch, capsys
    ):
        # stub the script availability check (we don't install thinkweave
        # console scripts in CI's test env via this fixture)
        monkeypatch.setattr(install_mod, "_check_scripts", lambda: [])
        _write_plugin_manifest(
            fake_claude_home["plugins_root"], "thinkweave",
            declares_thinkweave=True,
        )

        cmd_install(_ns(yes=True))

        # MCP write skipped
        assert not fake_claude_home["claude_json"].exists()
        # CLAUDE.md nudge applied
        assert fake_claude_home["claude_md"].exists()
        assert install_mod.CLAUDE_MD_BLOCK_START in (
            fake_claude_home["claude_md"].read_text()
        )
        out = capsys.readouterr().out
        assert "plugin manifest" in out
        assert "Skipping" in out

    def test_plugin_route_respects_no_claude_md_flag(
        self, fake_claude_home, stub_install_validators, monkeypatch
    ):
        monkeypatch.setattr(install_mod, "_check_scripts", lambda: [])
        _write_plugin_manifest(
            fake_claude_home["plugins_root"], "thinkweave",
            declares_thinkweave=True,
        )

        cmd_install(_ns(yes=True, no_claude_md=True))

        assert not fake_claude_home["claude_json"].exists()
        assert not fake_claude_home["claude_md"].exists()

    def test_plugin_route_warns_on_vault_flag(
        self, fake_claude_home, stub_install_validators, monkeypatch, capsys
    ):
        """--vault is a no-op on plugin route because the MCP entry is
        plugin-owned. Surface that explicitly rather than silently
        ignoring."""
        monkeypatch.setattr(install_mod, "_check_scripts", lambda: [])
        _write_plugin_manifest(
            fake_claude_home["plugins_root"], "thinkweave",
            declares_thinkweave=True,
        )

        cmd_install(_ns(yes=True, vault="/some/vault"))
        out = capsys.readouterr().out
        assert "--vault is a no-op on the plugin route" in out

    def test_pip_route_writes_mcp_when_no_plugin_present(
        self, fake_claude_home, stub_install_validators, monkeypatch
    ):
        """Sanity: with no plugin manifest, the existing pip-route logic
        still kicks in — `cmd_install` writes both MCP entry and CLAUDE.md."""
        monkeypatch.setattr(install_mod, "_check_scripts", lambda: [])

        cmd_install(_ns(yes=True))

        cfg = json.loads(fake_claude_home["claude_json"].read_text())
        assert install_mod.SERVER_NAME in cfg["mcpServers"]
        assert fake_claude_home["claude_md"].exists()


class TestUvValidation:
    """`_check_uv_available` fails fast when uv is missing from PATH —
    without this check, `weave install` silently writes a config whose
    `command: "uv"` fails at every Claude Code session start."""

    def test_exits_when_uv_missing(self, monkeypatch, capsys):
        monkeypatch.setattr(install_mod.shutil, "which", lambda name: None)
        with pytest.raises(SystemExit) as exc:
            _check_uv_available()
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "uv` not found" in err
        assert "astral.sh/uv" in err  # points at install docs

    def test_passes_when_uv_present(self, monkeypatch):
        monkeypatch.setattr(install_mod.shutil, "which", lambda name: "/usr/local/bin/uv")
        # no exit, no print
        _check_uv_available()


class TestPyprojectReachable:
    """`_check_pyproject_reachable` catches the pipx install failure mode
    where `_detect_project_root` falls back to cwd and the install would
    silently bake the user's terminal directory into the MCP entry."""

    def test_exits_when_pyproject_missing(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc:
            _check_pyproject_reachable(tmp_path)
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "pyproject.toml" in err
        assert "pipx" in err  # points at the actual failure mode

    def test_passes_when_pyproject_present(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        _check_pyproject_reachable(tmp_path)


class TestUvSync:
    """`_uv_sync` invokes `uv sync` with the right args and surfaces
    failures clearly. The actual sync is mocked — we're testing the
    contract, not exercising uv."""

    def test_invokes_uv_sync_with_project_and_mcp_extra(
        self, tmp_path, monkeypatch
    ):
        calls: list[list[str]] = []

        def fake_run(cmd, check):
            calls.append(cmd)
            return subprocess_result(returncode=0)

        monkeypatch.setattr(install_mod.subprocess, "run", fake_run)
        install_mod._uv_sync(tmp_path)
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "uv"
        assert cmd[1] == "sync"
        assert "--project" in cmd
        assert str(tmp_path) in cmd
        assert "--extra" in cmd
        assert "mcp" in cmd

    def test_exits_on_nonzero_uv_sync(self, tmp_path, monkeypatch, capsys):
        def fake_run(cmd, check):
            return subprocess_result(returncode=2)

        monkeypatch.setattr(install_mod.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as exc:
            install_mod._uv_sync(tmp_path)
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "uv sync` exited 2" in err
        # error includes the manual-retry command
        assert "uv sync --project" in err


def subprocess_result(returncode: int):
    """Minimal stand-in for ``subprocess.CompletedProcess``."""
    class _R:
        pass
    r = _R()
    r.returncode = returncode
    return r


class TestPluginManifestContract:
    """The root `.claude-plugin/plugin.json` is the shipped packaging.

    Claude Code has no `post_install` manifest hook (verified against the
    plugin docs 2026-06-12 — the key is silently ignored), so nothing on
    the plugin route may depend on one. Hooks ship via `hooks/hooks.json`
    and agents via the auto-discovered root `agents/` dir; every launcher
    uses the canonical `uv run --project "${CLAUDE_PLUGIN_ROOT}" --extra
    mcp` shape because the plugin route never puts console scripts on
    PATH.
    """

    MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
    HOOK_LAUNCHER = 'uv run --project "${CLAUDE_PLUGIN_ROOT}" --extra mcp weave-hook '

    def test_no_post_install(self):
        data = json.loads(self.MANIFEST.read_text(encoding="utf-8"))
        assert "post_install" not in data, (
            "post_install is not a supported plugin.json field — Claude Code "
            "ignores it silently; don't ship steps that never run"
        )

    def test_hook_commands_use_plugin_root_launcher(self):
        hooks = json.loads(
            (REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )
        commands = [
            h["command"]
            for matchers in hooks["hooks"].values()
            for matcher in matchers
            for h in matcher["hooks"]
        ]
        assert commands, "plugin ships no hook commands"
        for cmd in commands:
            assert cmd.startswith(self.HOOK_LAUNCHER), (
                f"hook command {cmd!r} bypasses the canonical uv launcher — "
                "bare `weave-hook` is not on PATH for plugin-route users"
            )

    def test_agents_shipped_at_plugin_root(self):
        """Every worker the dream registry fans out to (plus the drain
        Path B writers) must exist under the auto-discovered `agents/`
        dir, or plugin users get 'Agent type not found' from /dream and
        every fan-out drain."""
        from thinkweave.operations.dream_tasks import REGISTRY

        shipped = {p.stem for p in (REPO_ROOT / "agents").glob("*.md")}
        assert shipped, "plugin ships no subagent workers"

        dream_workers = {spec.worker_name for spec in REGISTRY}
        missing = dream_workers - shipped
        assert not missing, f"dream workers missing from agents/: {sorted(missing)}"

        assert "news-triage-worker" in shipped
        assert {n for n in shipped if n.startswith("research-")}, (
            "no drain Path B writer agents shipped"
        )


class TestDevLink:
    """`weave dev-link` symlinks the checkout into ~/.claude/skills/ so
    Claude Code auto-loads it as the `thinkweave@skills-dir` plugin
    (flagless, namespaced, live edits). It writes no ~/.claude.json entry,
    refuses to shadow a marketplace install, and warns on a leftover raw
    MCP entry that would double-register the server."""

    @pytest.fixture
    def dev_link_env(self, fake_claude_home, tmp_path, monkeypatch):
        """Sandbox the dev-link touchpoints on top of fake_claude_home: a
        fake checkout carrying a plugin manifest, plus SKILLS_DIR / DEV_LINK
        under tmp, and a stubbed _detect_project_root pointing at it."""
        checkout = tmp_path / "checkout"
        (checkout / ".claude-plugin").mkdir(parents=True)
        (checkout / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": SERVER_NAME, "version": "0.1.0"}), encoding="utf-8"
        )
        skills_dir = tmp_path / "skills"
        dev_link = skills_dir / SERVER_NAME
        monkeypatch.setattr(install_mod, "SKILLS_DIR", skills_dir)
        monkeypatch.setattr(install_mod, "DEV_LINK", dev_link)
        monkeypatch.setattr(install_mod, "_detect_project_root", lambda: checkout)
        return {
            **fake_claude_home,
            "checkout": checkout,
            "skills_dir": skills_dir,
            "dev_link": dev_link,
        }

    def test_creates_symlink_to_checkout(self, dev_link_env, capsys):
        cmd_dev_link(_ns())
        link = dev_link_env["dev_link"]
        assert link.is_symlink()
        assert link.resolve() == dev_link_env["checkout"].resolve()
        assert "Dev-linked" in capsys.readouterr().out

    def test_refuses_when_marketplace_plugin_present(self, dev_link_env):
        _write_plugin_manifest(
            dev_link_env["plugins_root"], "thinkweave", declares_thinkweave=True
        )
        with pytest.raises(SystemExit):
            cmd_dev_link(_ns())
        assert not dev_link_env["dev_link"].is_symlink()

    def test_errors_without_plugin_manifest(self, dev_link_env, tmp_path, monkeypatch):
        bare = tmp_path / "not-a-checkout"
        bare.mkdir()
        monkeypatch.setattr(install_mod, "_detect_project_root", lambda: bare)
        with pytest.raises(SystemExit):
            cmd_dev_link(_ns())

    def test_idempotent_when_already_linked(self, dev_link_env, capsys):
        cmd_dev_link(_ns())
        capsys.readouterr()
        cmd_dev_link(_ns())
        assert "Already dev-linked" in capsys.readouterr().out

    def test_warns_on_leftover_raw_mcp_entry(self, dev_link_env, capsys):
        install_mod._restore_mcp_entry()  # simulate a `weave install` leftover
        cmd_dev_link(_ns())
        err = capsys.readouterr().err
        assert "twice" in err
        # still links despite the warning (non-fatal)
        assert dev_link_env["dev_link"].is_symlink()

    def test_dev_unlink_removes_symlink(self, dev_link_env, capsys):
        cmd_dev_link(_ns())
        capsys.readouterr()
        cmd_dev_unlink(_ns())
        assert not dev_link_env["dev_link"].is_symlink()
        assert "Removed dev-link" in capsys.readouterr().out

    def test_dev_unlink_noop_when_absent(self, dev_link_env, capsys):
        cmd_dev_unlink(_ns())
        assert "No dev-link" in capsys.readouterr().out
