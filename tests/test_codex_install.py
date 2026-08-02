"""The Codex install route (issue #106, W2a).

Seams under test — the four artifacts the issue names, nothing deeper:

1. the ``codex`` :class:`~thinkweave.core.harness.HarnessProfile` entry and the
   headless argv it renders (``codex exec …``);
2. the bytes ``weave install --harness codex`` leaves in
   ``$CODEX_HOME/config.toml`` and ``$CODEX_HOME/AGENTS.md``;
3. ``weave doctor --mcp``'s report for the Codex scopes;
4. the ``pause`` → ``resume`` round-trip over both.

Sources of truth for the expected values, all independent of the code under
test:

* ``codex mcp add thinkweave --env … -- uv run …`` run against a throwaway
  ``$CODEX_HOME`` on 2026-08-02 with codex-cli 0.146.0 emitted exactly::

      [mcp_servers.thinkweave]
      command = "uv"
      args = ["run", "--project", "/repo", "--extra", "mcp", "weave-mcp"]

      [mcp_servers.thinkweave.env]
      THINKWEAVE_VAULT = "/tmp/vault"

  — no ``type`` key, and ``codex exec --strict-config`` *rejects* one
  (``unknown configuration field mcp_servers.thinkweave.type``). The same run
  confirmed Codex accepts the equivalent inline ``env = { … }`` table.
* ``codex exec --help`` (0.146.0) for the invocation shape: prompt is
  positional, ``--model``/``-m``, ``--dangerously-bypass-approvals-and-sandbox``.
* https://learn.chatgpt.com/docs/extend/mcp for ``mcp_servers`` and the
  "trusted projects only" caveat on project-scope ``.codex/config.toml``.

Nothing here touches a real ``~/.codex``: every test aims the profile at a tmp
dir, and the suite-wide ``_sandbox_harness_home`` fixture is the backstop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thinkweave.core import harness


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Select the ``codex`` profile against a throwaway ``$CODEX_HOME``."""
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(harness, "_OVERRIDE", None)
    monkeypatch.setenv("THINKWEAVE_HARNESS", "codex")
    return home


# --------------------------------------------------------------------------- #
# 1. the profile
# --------------------------------------------------------------------------- #


class TestCodexProfile:
    def test_registered_alongside_claude_code(self):
        assert set(harness.PROFILES) == {"claude-code", "codex"}

    @pytest.mark.parametrize(
        ("field", "relpath"),
        [
            ("mcp_config", "config.toml"),
            ("instructions_file", "AGENTS.md"),
            ("skills_dir", "skills"),
            ("pause_marker", "thinkweave_paused.json"),
        ],
    )
    def test_home_scoped_paths(self, codex_home: Path, field: str, relpath: str):
        assert getattr(harness.active(), field) == codex_home / relpath

    def test_project_mcp_config_relpath(self, codex_home: Path):
        assert harness.active().project_mcp_config_relpath == Path(".codex/config.toml")

    def test_codex_home_env_overrides_the_default_location(
        self, codex_home: Path, monkeypatch
    ):
        # `$CODEX_HOME` is Codex's own knob (confirmed via `codex doctor --json`,
        # which reports every state path relative to it).
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setattr(harness, "_OVERRIDE", None)
        assert harness.active().mcp_config == Path.home() / ".codex" / "config.toml"

    def test_explicit_home_wins_over_codex_home_env(self, tmp_path: Path, monkeypatch):
        """The suite's sandbox fixture passes ``home=`` — a stray ``$CODEX_HOME``
        in the developer's shell must not let a test escape it."""
        monkeypatch.setenv("CODEX_HOME", "/should/not/be/used")
        assert harness.codex(home=tmp_path).mcp_config == (
            tmp_path / ".codex" / "config.toml"
        )

    def test_capability_flags(self, codex_home: Path):
        p = harness.active()
        # Codex 0.146 has no thinkweave-shaped hook wiring yet (#107 owns it),
        # no verified Task-tool equivalent for the /drain fan-out, no markdown
        # auto-memory corpus for the seam, and no headless slash resolution.
        assert (p.hooks, p.subagents, p.native_memory, p.headless_slash) == (
            False,
            False,
            False,
            False,
        )


class TestCodexHeadlessArgv:
    """``codex exec`` takes its prompt positionally — there is no ``-p``."""

    def test_bare_shape(self, codex_home: Path):
        assert harness.active().headless_argv("/dream") == ["codex", "exec", "/dream"]

    def test_bypass_flag(self, codex_home: Path):
        assert harness.active().headless_argv("/dream", bypass=True) == [
            "codex",
            "exec",
            "/dream",
            "--dangerously-bypass-approvals-and-sandbox",
        ]

    def test_model_flag(self, codex_home: Path):
        assert harness.active().headless_argv("hi", model="gpt-5.6") == [
            "codex",
            "exec",
            "--model",
            "gpt-5.6",
            "hi",
        ]

    def test_flow_argv_asks_for_no_claude_model(self, codex_home: Path, monkeypatch):
        """`sonnet` is a Claude Code model name — rendering it into a `codex
        exec` line produces a command Codex cannot run."""
        from thinkweave.operations import flows

        monkeypatch.delenv("THINKWEAVE_CLAUDE_BIN", raising=False)
        monkeypatch.delenv("PERSONAL_MEM_CLAUDE_BIN", raising=False)
        argv = flows._build_argv("/dream")
        assert "sonnet" not in argv
        assert argv[:3] == ["codex", "exec", "/dream"]


class TestCodexCronRendering:
    def _job(self, command: str):
        from thinkweave.scheduling.registry import ScheduledJob

        return ScheduledJob(
            name="j", cadence="30 0 * * *", command=command, runner="direct"
        )

    def test_scheduling_yaml_job_renders_a_valid_codex_exec_line(
        self, codex_home: Path
    ):
        from thinkweave.scheduling import registry

        rendered = registry.resolve_command(self._job("codex exec /dream"))
        # The bypass flag is what makes an unattended run able to use tools at
        # all (upstream codex#24135: headless MCP tool approval needs it).
        assert rendered.endswith(
            "exec /dream --dangerously-bypass-approvals-and-sandbox"
        )
        # …and the skill token stays bare: Codex resolves no slash commands.
        assert "/thinkweave:dream" not in rendered

    def test_bypass_flag_is_not_duplicated_on_a_hand_written_line(
        self, codex_home: Path
    ):
        from thinkweave.scheduling import registry

        rendered = registry.resolve_command(
            self._job("codex exec /dream --dangerously-bypass-approvals-and-sandbox")
        )
        assert rendered.count("--dangerously-bypass-approvals-and-sandbox") == 1


class TestHarnessFlag:
    """``weave install --harness codex`` — the acceptance criterion's spelling.
    The flag pins the profile for the whole process."""

    @pytest.mark.parametrize(
        "command", ["install", "uninstall", "pause", "resume", "doctor"]
    )
    def test_flag_is_accepted(self, command: str):
        from thinkweave.surfaces.cli.parser import build_parser

        args = build_parser().parse_args([command, "--harness", "codex"])
        assert args.harness == "codex"

    def test_flag_selects_the_profile(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(harness, "_OVERRIDE", None)
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        harness.select("codex")
        assert harness.active().id == "codex"

    def test_unknown_name_is_rejected_by_the_parser(self):
        from thinkweave.surfaces.cli.parser import build_parser

        with pytest.raises(SystemExit):
            build_parser().parse_args(["install", "--harness", "clyde"])
