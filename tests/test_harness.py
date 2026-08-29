"""Contract tests for the ``core.harness`` profile seam (issue #104, W1a).

Seams under test — the interfaces issue #104 names, nothing deeper:

1. ``core.harness`` itself: the profile registry, the ``claude-code`` entry's
   field values, and the capability flags.
2. The two consumers the acceptance criteria single out as having to *consult*
   a capability flag: the **seam gate** (``synthesis.memory_seam``) and the
   **cron renderer** (``scheduling.registry.resolve_command``).
3. The "adding a new profile requires no edits to consumer files" criterion —
   a synthetic profile is registered here, in the test, and the migrated
   consumers are asked where they would read/write.

Expected values are written by hand from Claude Code's documented install
topology (``~/.claude.json``, ``~/.claude/CLAUDE.md``, ``~/.claude/settings.json``,
…) and from the pre-refactor literals quoted in the issue — never recomputed
from the profile the code under test builds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thinkweave.core import harness


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``$HOME`` at a tmp dir and drop any active-profile override so
    ``harness.active()`` derives a fresh claude-code profile from it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("THINKWEAVE_HARNESS", raising=False)
    monkeypatch.setattr(harness, "_OVERRIDE", None)
    return home


def _probe_profile(home: Path) -> harness.HarnessProfile:
    """A second harness with a deliberately different topology — registered
    only inside tests, so the consumers below prove they read the profile."""
    return harness.HarnessProfile(
        id="probe",
        hooks=False,
        subagents=False,
        native_memory=False,
        headless_slash=False,
        instructions_file=home / "AGENTS.md",
        instructions_block_body="probe nudge",
        mcp_config=home / "config.toml",
        skills_dir=home / "prompts",
        plugins_root=home / "ext",
        plugins_cache=home / "ext" / "cache",
        installed_plugins=home / "ext" / "installed.json",
        user_settings=home / "settings.json",
        project_settings_relpath=Path(".probe/settings.json"),
        project_mcp_config_relpath=Path("probe.json"),
        project_plugins_relpath=Path(".probe/ext"),
        plugin_manifest_relpath=Path(".probe-ext/manifest.json"),
        pause_marker=home / "paused.json",
        memory_projects_root=home / "projects",
        memory_global_dir=home / "memory",
        cli_bin="probe-cli",
        model_flag="-m",
        prompt_flag="--exec",
        bypass_permissions_flag="",
        headless_model="probe-model",
    )


# --------------------------------------------------------------------------- #
# 1. the registry
# --------------------------------------------------------------------------- #


class TestRegistry:
    def test_registered_profiles(self):
        # W1a shipped `claude-code` alone; the Codex wave (#106) added the
        # second entry, and #191's dispatch-contract re-scope added the two
        # E0 blueprint rows (Pi, OpenCode).
        assert set(harness.PROFILES) == {"claude-code", "codex", "pi", "opencode"}

    def test_active_defaults_to_claude_code(self, fake_home: Path):
        assert harness.active().id == "claude-code"

    def test_env_selects_the_profile(self, fake_home: Path, monkeypatch):
        monkeypatch.setitem(
            harness.PROFILES, "probe", lambda: _probe_profile(fake_home / "probe")
        )
        monkeypatch.setenv("THINKWEAVE_HARNESS", "probe")
        assert harness.active().id == "probe"

    def test_unknown_profile_fails_loud_and_names_the_valid_ones(
        self, fake_home: Path, monkeypatch, capsys
    ):
        # Fail loud rather than falling back to claude-code — a mis-set harness
        # must not quietly write into the wrong home. But every consumer reads
        # this, so a typo must not surface as a bare traceback.
        monkeypatch.setenv("THINKWEAVE_HARNESS", "claud-code")
        with pytest.raises(SystemExit):
            harness.active()
        err = capsys.readouterr().err
        assert "claud-code" in err and "claude-code" in err


class TestClaudeCodeProfile:
    """Every path is Claude Code's documented location, written out by hand."""

    @pytest.mark.parametrize(
        ("field", "relpath"),
        [
            ("instructions_file", ".claude/CLAUDE.md"),
            ("mcp_config", ".claude.json"),
            ("skills_dir", ".claude/skills"),
            ("plugins_root", ".claude/plugins"),
            ("plugins_cache", ".claude/plugins/cache"),
            ("installed_plugins", ".claude/plugins/installed_plugins.json"),
            ("user_settings", ".claude/settings.json"),
            ("pause_marker", ".claude/thinkweave_paused.json"),
            ("memory_projects_root", ".claude/projects"),
            ("memory_global_dir", ".claude/memory"),
        ],
    )
    def test_home_scoped_paths(self, fake_home: Path, field: str, relpath: str):
        assert getattr(harness.active(), field) == fake_home / relpath

    def test_dev_link_is_the_skills_dir_entry(self, fake_home: Path):
        assert harness.active().dev_link == fake_home / ".claude/skills/thinkweave"

    @pytest.mark.parametrize(
        ("field", "relpath"),
        [
            ("project_settings_relpath", ".claude/settings.local.json"),
            ("project_mcp_config_relpath", ".mcp.json"),
            ("project_plugins_relpath", ".claude/plugins"),
            ("plugin_manifest_relpath", ".claude-plugin/plugin.json"),
        ],
    )
    def test_project_scoped_relpaths(self, fake_home: Path, field: str, relpath: str):
        assert getattr(harness.active(), field) == Path(relpath)

    def test_headless_invocation_shape(self, fake_home: Path):
        p = harness.active()
        assert (p.cli_bin, p.model_flag, p.prompt_flag) == ("claude", "--model", "-p")
        assert p.bypass_permissions_flag == "--dangerously-skip-permissions"

    def test_capability_flags_all_true(self, fake_home: Path):
        p = harness.active()
        assert (p.hooks, p.subagents, p.native_memory, p.headless_slash) == (
            True,
            True,
            True,
            True,
        )


class TestHeadlessArgv:
    """The argv template. Expected lists are the pre-refactor literals from
    ``flows._build_argv`` and ``judge._rejudge_argv``, written out by hand."""

    def test_flow_shape(self, fake_home: Path):
        assert harness.active().headless_argv("/dream", model="sonnet", bypass=True) == [
            "claude",
            "--model",
            "sonnet",
            "-p",
            "/dream",
            "--dangerously-skip-permissions",
        ]

    def test_judge_shape(self, fake_home: Path):
        assert harness.active().headless_argv("/judge-prediction --decision dec-1") == [
            "claude",
            "-p",
            "/judge-prediction --decision dec-1",
        ]

    def test_binary_override(self, fake_home: Path):
        assert harness.active().headless_argv("/x", bin="/opt/claude")[0] == "/opt/claude"


# --------------------------------------------------------------------------- #
# 2. capability flags are CONSULTED, not merely stored
# --------------------------------------------------------------------------- #


def _seed_cc_memory(home: Path) -> None:
    d = home / ".claude" / "projects" / "-home-x-proj" / "memory"
    d.mkdir(parents=True)
    (d / "a-fact.md").write_text(
        "---\nname: a-fact\ndescription: d\nmetadata:\n  type: project\n---\n\nbody\n",
        encoding="utf-8",
    )


class TestSeamGateConsultsNativeMemory:
    def test_walk_runs_when_the_harness_has_native_memory(self, fake_home: Path):
        from thinkweave.synthesis import memory_seam

        _seed_cc_memory(fake_home)
        assert [f["slug"] for f in memory_seam.collect_cc_facts()] == ["a-fact"]

    def test_walk_is_skipped_when_the_harness_has_none(
        self, fake_home: Path, use_profile
    ):
        # dec-0535e46b: "the memory seam becomes a gated capability rather than
        # a port target" — a harness without native memory yields no facts even
        # when the directories happen to exist.
        from thinkweave.synthesis import memory_seam

        _seed_cc_memory(fake_home)
        use_profile(native_memory=False)
        assert memory_seam.collect_cc_facts() == []


class TestCronRendererConsultsCapabilities:
    def test_headless_slash_off_leaves_the_skill_token_bare(
        self, fake_home: Path, use_profile, scheduled_job, plugin_route_active
    ):
        from thinkweave.scheduling import registry

        # Make the plugin route detectable so namespacing WOULD fire. Which of
        # the two install shapes says so is irrelevant here — the marketplace
        # manifest needs no symlink privilege (see conftest).
        plugin_route_active()

        assert "/thinkweave:dream" in registry.resolve_command(
            scheduled_job("claude -p /dream")
        )

        use_profile(headless_slash=False)
        without = registry.resolve_command(scheduled_job("claude -p /dream"))
        assert "/thinkweave:dream" not in without
        assert "-p /dream" in without

    def test_bypass_flag_comes_from_the_profile(
        self, fake_home: Path, use_profile, scheduled_job
    ):
        from thinkweave.scheduling import registry

        use_profile(bypass_permissions_flag="--yolo")
        rendered = registry.resolve_command(scheduled_job("claude -p /dream"))
        assert rendered.endswith("--yolo")

    def test_no_bypass_flag_means_none_is_appended(
        self, fake_home: Path, use_profile, scheduled_job
    ):
        from thinkweave.scheduling import registry

        use_profile(bypass_permissions_flag="")
        assert registry.resolve_command(scheduled_job("claude -p /dream")).endswith(
            "-p /dream"
        )

    def test_absolute_path_head_still_gets_namespacing_and_bypass(
        self, fake_home: Path, scheduled_job, plugin_route_active
    ):
        """A hand-edited job naming the binary by absolute path is rendered
        exactly like the bare-token form.

        `scheduling.yaml` is user-owned and this project has a history of cron
        PATH failures, so pinning an absolute `claude` path there is a plausible
        edit. Pre-refactor such a line got both skill namespacing and
        `--dangerously-skip-permissions`; losing either makes the cron silently
        no-op at its first tool call.
        """
        from thinkweave.scheduling import registry

        plugin_route_active()

        rendered = registry.resolve_command(scheduled_job("/opt/bin/claude -p /dream"))
        assert rendered == (
            "/opt/bin/claude -p /thinkweave:dream --dangerously-skip-permissions"
        )


class TestHooksInstallerConsultsHooksFlag:
    def test_refuses_on_a_harness_without_hooks(self, fake_home: Path, use_profile):
        from thinkweave.surfaces.hooks import install as hooks_install

        use_profile(hooks=False)
        with pytest.raises(SystemExit):
            hooks_install.install_hooks(project_dir=str(fake_home), scope="project")
        assert not (fake_home / ".claude" / "settings.local.json").exists()

    def test_uninstall_is_a_no_op_not_an_exit(self, fake_home: Path, use_profile):
        """Removing what was never installed must not abort the caller.

        ``weave pause`` uninstalls hooks *first*, then removes the MCP entry
        and writes its marker — so an exit here would strand pause half-done on
        a hooks-less harness.
        """
        from thinkweave.surfaces.hooks import install as hooks_install

        use_profile(hooks=False)
        hooks_install.uninstall_hooks(project_dir=str(fake_home), scope="project")

    def test_pause_completes_on_a_hooks_less_harness(
        self, fake_home: Path, use_profile
    ):
        import argparse

        from thinkweave.surfaces.cli import pause as pause_mod

        marker = fake_home / "paused.json"
        use_profile(hooks=False, pause_marker=marker)
        pause_mod.cmd_pause(argparse.Namespace(status=False))
        assert marker.exists()


# --------------------------------------------------------------------------- #
# 3. a new profile needs no consumer edits
# --------------------------------------------------------------------------- #


class TestNewProfileNeedsNoConsumerEdits:
    """Register a profile in the test, then ask each migrated consumer where
    it would read/write. Nothing under ``src/`` mentions ``probe``."""

    @pytest.fixture
    def probe(self, fake_home: Path, monkeypatch) -> harness.HarnessProfile:
        p = _probe_profile(fake_home / "probe")
        monkeypatch.setitem(harness.PROFILES, "probe", lambda: p)
        monkeypatch.setenv("THINKWEAVE_HARNESS", "probe")
        monkeypatch.setattr(harness, "_OVERRIDE", None)
        return p

    def test_mcp_doctor_reads_the_profile_mcp_config(self, probe, tmp_path: Path):
        from thinkweave.surfaces.cli import mcp_doctor as md

        assert md._entry_from_claude_json()[0] == probe.mcp_config
        # …and the project scope, whose file name is equally per-harness.
        assert (
            md._entry_from_project_mcp_json(tmp_path)[0] == tmp_path / "probe.json"
        )

    def test_mcp_doctor_finds_the_profiles_project_plugin_manifests(
        self, probe, tmp_path: Path
    ):
        from thinkweave.surfaces.cli import mcp_doctor as md

        manifest = tmp_path / ".probe/ext" / "some-plugin" / ".probe-ext" / "manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"mcpServers": {"thinkweave": {"command": "x"}}}')
        assert [p for p, _ in md._entries_from_plugin_manifests(tmp_path)] == [manifest]

    def test_hook_settings_paths(self, probe, tmp_path: Path):
        from thinkweave.surfaces.hooks import install as hooks_install

        assert hooks_install._settings_path_for_scope("user") == probe.user_settings
        assert (
            hooks_install._settings_path_for_scope("project", str(tmp_path))
            == tmp_path / ".probe" / "settings.json"
        )

    def test_flow_argv(self, probe, monkeypatch):
        from thinkweave.operations import flows

        monkeypatch.delenv("THINKWEAVE_CLAUDE_BIN", raising=False)
        monkeypatch.delenv("PERSONAL_MEM_CLAUDE_BIN", raising=False)
        # The model is the profile's too — `sonnet` is a Claude Code name, and
        # rendering it for another vendor's binary produces a broken command.
        assert flows._build_argv("/dream") == [
            "probe-cli",
            "-m",
            "probe-model",
            "--exec",
            "/dream",
        ]

    def test_rejudge_argv(self, probe):
        from thinkweave.surfaces.cli import judge

        assert judge._rejudge_argv("dec-1", None) == [
            "probe-cli",
            "--exec",
            "/judge-prediction --decision dec-1",
        ]

    def test_namespace_rule_is_per_profile(self, probe, plugin_route_active):
        from thinkweave.core.plugin_route import plugin_namespace

        # Register the plugin at the *probe's* own location — the point is that
        # the detector reads the paths it is handed, not any Claude Code default.
        plugin_route_active(probe.installed_plugins)
        # The probe harness declares no headless slash commands, so it has no
        # namespace regardless of what is on disk…
        assert harness.active().namespace() is None
        # …while the raw detector still answers for the paths it is handed.
        assert (
            plugin_namespace(manifest=probe.installed_plugins, dev_link=probe.dev_link)
            == "thinkweave"
        )
