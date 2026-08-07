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
  confirmed Codex accepts the equivalent inline ``env = { … }`` table. That run
  was on POSIX; the expected *shape* is the invariant, so the paths inside it
  are substituted per platform (see ``EXPECTED_CONFIG_TOML``) — on Windows they
  carry the ``\\\\`` escaping a TOML basic string requires for a backslash.

  One deliberate divergence from that transcript: thinkweave writes
  ``--no-sync`` into ``args``, which ``codex mcp add`` had no reason to. It is
  safe because ``weave install`` has already run ``uv sync`` eagerly, and it
  matters most on Windows, where a re-sync at spawn time can hit a sharing
  violation trying to rewrite ``weave-mcp.exe`` under a live server. See
  ``_build_server_entry``.
* ``codex exec --help`` (0.146.0) for the invocation shape: prompt is
  positional, ``--model``/``-m``, ``--dangerously-bypass-approvals-and-sandbox``.
* https://learn.chatgpt.com/docs/extend/mcp for ``mcp_servers`` and the
  "trusted projects only" caveat on project-scope ``.codex/config.toml``.

Nothing here touches a real ``~/.codex``: every test aims the profile at a tmp
dir, and the suite-wide ``_sandbox_harness_home`` fixture is the backstop.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from thinkweave.core import harness
from thinkweave.surfaces.cli import install as install_mod

_WINDOWS = os.name == "nt"

# The two machine-dependent values the `installable` fixture pins, and the vault
# the install bakes in. Platform-shaped: `_detect_project_root` returns a
# `Path`, which the writer renders via `str()`, so a POSIX literal would come
# back out of a `WindowsPath` spelt with backslashes. Using a native path per
# platform keeps the fixture honest about what the product is actually handed.
UV_PATH = r"C:\tools\uv.exe" if _WINDOWS else "/usr/bin/uv"
PROJECT_ROOT = Path(r"C:\srv\thinkweave") if _WINDOWS else Path("/srv/thinkweave")
VAULT_PATH = r"C:\srv\vault" if _WINDOWS else "/srv/vault"

# …and the TOML those spell, written out by hand rather than derived from the
# writer. Per the TOML spec a literal backslash inside a basic string is `\\`,
# so the Windows column doubles as the regression pin for path escaping — the
# thing that would otherwise hand `codex exec --strict-config` an invalid file.
_UV_TOML = r'"C:\\tools\\uv.exe"' if _WINDOWS else '"/usr/bin/uv"'
_ROOT_TOML = r'"C:\\srv\\thinkweave"' if _WINDOWS else '"/srv/thinkweave"'
_VAULT_TOML = r'"C:\\srv\\vault"' if _WINDOWS else '"/srv/vault"'

# The exact table `codex mcp add` emitted, transcribed by hand from that run —
# `env` inlined, which the same CLI accepted on read-back. No `type` key.
EXPECTED_CONFIG_TOML = f"""\
[mcp_servers.thinkweave]
command = {_UV_TOML}
args = ["run", "--no-sync", "--project", {_ROOT_TOML}, "--extra", "mcp", "python", "-m", "thinkweave.surfaces.mcp.server"]
"""

EXPECTED_CONFIG_TOML_WITH_VAULT = f"""\
[mcp_servers.thinkweave]
command = {_UV_TOML}
args = ["run", "--no-sync", "--project", {_ROOT_TOML}, "--extra", "mcp", "python", "-m", "thinkweave.surfaces.mcp.server"]
env = {{ "THINKWEAVE_VAULT" = {_VAULT_TOML} }}
"""

CODEX_BIN = shutil.which("codex")
needs_codex = pytest.mark.skipif(
    CODEX_BIN is None, reason="Codex CLI not installed on this machine"
)


@pytest.fixture
def installable(monkeypatch: pytest.MonkeyPatch, stub_install_validators) -> None:
    """Neutralise the environment-probing half of ``cmd_install`` and pin the
    two machine-dependent values, so the written bytes are fully determined.
    The three validators come from the shared ``stub_install_validators``."""
    monkeypatch.setattr(
        install_mod,
        "_check_scripts",
        lambda: install_mod.ScriptsCheck("ok", [], Path("/unused")),
    )
    monkeypatch.setattr(install_mod, "_detect_uv_path", lambda: UV_PATH)
    monkeypatch.setattr(install_mod, "_detect_project_root", lambda: PROJECT_ROOT)
    # User-PATH persistence is integration-tested by #164's bootstrap suite;
    # config-writer tests must never touch the developer's registry.
    monkeypatch.setattr(install_mod, "_prepend_windows_user_path", lambda _path: None)


def _install(**kw) -> None:
    install_mod.cmd_install(
        argparse.Namespace(
            **{"yes": True, "vault": None, "no_claude_md": True, **kw}
        )
    )


# --------------------------------------------------------------------------- #
# 1. the profile
# --------------------------------------------------------------------------- #


class TestCodexProfile:
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
        # Hooks landed in #107 (see tests/test_codex_hooks.py). Interactive
        # worker skills now project onto native Codex subagents; Codex still
        # has no markdown auto-memory corpus or headless slash resolution.
        assert (p.hooks, p.subagents, p.native_memory, p.headless_slash) == (
            True,
            True,
            False,
            False,
        )


class TestNextSteps:
    """The post-install screen must only name things the active harness can do.

    It ended with "3. /onboard" for every harness. thinkweave ships no Codex
    skill bundle, so on Codex that command does not exist — the one screen whose
    entire job is telling the user what to do next was naming something
    unrunnable.
    """

    def test_codex_does_not_advertise_the_onboard_skill(
        self, codex_home: Path, capsys
    ):
        install_mod._print_next_steps()
        out = capsys.readouterr().out
        assert "/onboard" not in out
        # …and says what to do instead, via the CLI that does exist.
        assert "weave init" in out
        assert "weave hooks install --scope user --harness codex" in out
        assert "restart codex" in out.lower()

    def test_claude_code_output_is_unchanged(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(harness, "_OVERRIDE", harness.claude_code(home=tmp_path))
        install_mod._print_next_steps()
        out = capsys.readouterr().out
        assert "/onboard" in out
        assert "weave hooks install" not in out

    def test_the_fallback_names_only_real_subcommands(
        self, codex_home: Path, capsys
    ):
        """A next-step naming a command that does not parse would repeat the
        original bug in a new spelling, so the advice is checked against the
        actual CLI parser."""
        from thinkweave.surfaces.cli import build_parser

        install_mod._print_next_steps()
        out = capsys.readouterr().out

        parser = build_parser()
        known: set[str] = set()
        for action in parser._actions:
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict):
                known.update(choices)
        assert known, "could not introspect the CLI subcommands"
        for line in out.splitlines():
            stripped = line.strip().lstrip("0123456789. ")
            if stripped.startswith("weave "):
                verb = stripped.split()[1]
                assert verb in known, f"next-step names unknown subcommand: {verb}"


class TestCodexHeadlessArgv:
    """``codex exec`` takes its prompt positionally — there is no ``-p``."""

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({}, ["codex", "exec", "/dream"]),
            (
                # Two independent gates on an unattended run: tool approval,
                # and (since #107 turned hooks on) hook trust — Codex runs
                # zero hooks until each definition is trusted via `/hooks`,
                # which a cron cannot answer.
                {"bypass": True},
                [
                    "codex", "exec", "/dream",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--dangerously-bypass-hook-trust",
                ],
            ),
            (
                {"model": "gpt-5.6"},
                ["codex", "exec", "--model", "gpt-5.6", "/dream"],
            ),
        ],
        ids=["bare", "bypass", "model"],
    )
    def test_argv_shape(self, codex_home: Path, kwargs: dict, expected: list[str]):
        assert harness.active().headless_argv("/dream", **kwargs) == expected

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
    def test_scheduling_yaml_job_renders_a_valid_codex_exec_line(
        self, codex_home: Path, scheduled_job
    ):
        from thinkweave.scheduling import registry

        rendered = registry.resolve_command(scheduled_job("codex exec /dream"))
        # The bypass flag is what makes an unattended run able to use tools at
        # all (upstream codex#24135: headless MCP tool approval needs it).
        assert rendered.endswith(
            "exec /dream --dangerously-bypass-approvals-and-sandbox"
        )
        # …and the skill token stays bare: Codex resolves no slash commands.
        assert "/thinkweave:dream" not in rendered

    def test_bypass_flag_is_not_duplicated_on_a_hand_written_line(
        self, codex_home: Path, scheduled_job
    ):
        from thinkweave.scheduling import registry

        rendered = registry.resolve_command(
            scheduled_job(
                "codex exec /dream --dangerously-bypass-approvals-and-sandbox"
            )
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


# --------------------------------------------------------------------------- #
# 2. the config.toml the install writes
# --------------------------------------------------------------------------- #


class TestConfigTomlWriter:
    def test_fresh_install_writes_the_documented_table(
        self, codex_home: Path, installable
    ):
        """Byte-for-byte against the shape `codex mcp add` emits — which also
        pins the absence of a `type` key, the one `codex exec --strict-config`
        rejects as `unknown configuration field mcp_servers.thinkweave.type`.
        """
        _install()
        assert (codex_home / "config.toml").read_text(encoding="utf-8") == (
            EXPECTED_CONFIG_TOML
        )

    def test_vault_lands_in_an_env_table(self, codex_home: Path, installable):
        _install(vault=VAULT_PATH)
        assert (codex_home / "config.toml").read_text(encoding="utf-8") == (
            EXPECTED_CONFIG_TOML_WITH_VAULT
        )

    def test_existing_user_config_survives_byte_for_byte(
        self, codex_home: Path, installable
    ):
        prior = (
            "# my codex config\n"
            'model = "gpt-5.6"\n'
            "\n"
            "[mcp_servers.other]\n"
            'command = "npx"\n'
            'args = ["-y", "some-mcp"]\n'
        )
        (codex_home / "config.toml").write_text(prior, encoding="utf-8")

        _install()

        text = (codex_home / "config.toml").read_text(encoding="utf-8")
        assert text.startswith(prior)
        assert text.endswith(EXPECTED_CONFIG_TOML)

    def test_a_foreign_entry_is_adopted_not_duplicated(
        self, codex_home: Path, installable, capsys
    ):
        """ChatGPT desktop's Settings→Import can pre-create a `thinkweave`
        entry carrying no sentinel of ours. Detection is key-scoped, so we
        adopt and converge it rather than appending a second table — and say
        what changed before doing so."""
        (codex_home / "config.toml").write_text(
            "[mcp_servers.thinkweave]\n"
            'command = "uvx"\n'
            'args = ["thinkweave-mcp"]\n',
            encoding="utf-8",
        )

        _install()

        text = (codex_home / "config.toml").read_text(encoding="utf-8")
        assert text.count("[mcp_servers.thinkweave]") == 1
        assert "uvx" not in text
        assert tomllib.loads(text)["mcp_servers"]["thinkweave"] == {
            "command": UV_PATH,
            "args": ["run", "--no-sync", "--project", str(PROJECT_ROOT),
                     "--extra", "mcp", "python", "-m",
                     "thinkweave.surfaces.mcp.server"],
        }

        out = capsys.readouterr().out
        assert "differs" in out
        assert "uvx" in out  # the old shape…
        # …and the new one. The drift report renders the entry as JSON, so the
        # path appears in its JSON spelling — on Windows, backslash-escaped.
        assert json.dumps(UV_PATH) in out

    def test_drifted_install_needs_consent(self, codex_home: Path, installable):
        (codex_home / "config.toml").write_text(
            '[mcp_servers.thinkweave]\ncommand = "uvx"\nargs = []\n', encoding="utf-8"
        )
        with pytest.raises(SystemExit):
            _install(yes=False)
        # …and nothing was written behind the refusal.
        assert "uvx" in (codex_home / "config.toml").read_text(encoding="utf-8")

    def test_second_install_is_a_no_op(self, codex_home: Path, installable, capsys):
        _install()
        first = (codex_home / "config.toml").read_text(encoding="utf-8")
        _install()
        assert (codex_home / "config.toml").read_text(encoding="utf-8") == first
        assert "already registered" in capsys.readouterr().out

    def test_a_sibling_env_subtable_is_replaced_wholesale(
        self, codex_home: Path, installable
    ):
        """`codex mcp add` writes env as a *sub*-table. Adopting an entry in
        that form must not strand `[mcp_servers.thinkweave.env]` behind."""
        (codex_home / "config.toml").write_text(
            "[mcp_servers.thinkweave]\n"
            'command = "uv"\n'
            "args = []\n"
            "\n"
            "[mcp_servers.thinkweave.env]\n"
            'THINKWEAVE_VAULT = "/old/vault"\n'
            "\n"
            "[mcp_servers.other]\n"
            'command = "npx"\n',
            encoding="utf-8",
        )

        _install()

        doc = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
        assert "env" not in doc["mcp_servers"]["thinkweave"]
        assert doc["mcp_servers"]["other"] == {"command": "npx"}

    def test_malformed_config_is_refused_not_clobbered(
        self, codex_home: Path, installable, capsys
    ):
        from thinkweave.surfaces.cli import main as cli_main

        (codex_home / "config.toml").write_text("this is [ not toml\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            cli_main(["install", "--harness", "codex", "--yes", "--no-claude-md"])
        assert exc.value.code != 0
        assert "not valid toml" in capsys.readouterr().err.lower()
        assert (codex_home / "config.toml").read_text(encoding="utf-8") == (
            "this is [ not toml\n"
        )

    def test_a_comment_documenting_the_next_server_survives(
        self, codex_home: Path, installable
    ):
        """A comment sitting directly above a header documents *that* header.
        Ours is the table above it, so replacing ours must hand it back rather
        than swallow it — the parse-based verify cannot see this, because
        comments do not survive `tomllib.loads` on either side.
        """
        (codex_home / "config.toml").write_text(
            "[mcp_servers.thinkweave]\n"
            'command = "old"\n'
            "\n"
            "# my notes about the next server\n"
            "[mcp_servers.other]\n"
            'command = "x"\n',
            encoding="utf-8",
        )

        _install()

        text = (codex_home / "config.toml").read_text(encoding="utf-8")
        assert "# my notes about the next server\n[mcp_servers.other]\n" in text
        # …and the blank line separating our table from that comment.
        assert "\n\n# my notes" in text

    def test_a_comment_inside_our_table_goes_with_it(
        self, codex_home: Path, installable
    ):
        """The mirror case: a comment followed by one of our own keys documents
        *our* table, so it leaves with the table it belonged to."""
        (codex_home / "config.toml").write_text(
            "[mcp_servers.thinkweave]\n"
            "# this documents the command below\n"
            'command = "old"\n',
            encoding="utf-8",
        )
        _install()
        assert "documents the command below" not in (
            codex_home / "config.toml"
        ).read_text(encoding="utf-8")

    def test_removal_hands_back_the_next_servers_comment_too(
        self, codex_home: Path, installable
    ):
        (codex_home / "config.toml").write_text(
            "[mcp_servers.thinkweave]\n"
            'command = "old"\n'
            "\n"
            "# keep me\n"
            "[mcp_servers.other]\n"
            'command = "x"\n',
            encoding="utf-8",
        )
        assert install_mod._remove_mcp_entry()
        assert (codex_home / "config.toml").read_text(encoding="utf-8") == (
            "# keep me\n[mcp_servers.other]\ncommand = \"x\"\n"
        )

    def test_an_unspliceable_config_exits_cleanly_with_the_remedy(
        self, codex_home: Path, installable, capsys
    ):
        """The inline-table spelling — a plausible hand-written or Import shape
        — cannot be replaced by a line-oriented writer. Refusing is the
        documented ceiling; dying on a traceback is not.
        """
        from thinkweave.surfaces.cli import main as cli_main

        prior = (
            "[mcp_servers]\n"
            'thinkweave = { command = "uvx", args = ["x"] }\n'
        )
        (codex_home / "config.toml").write_text(prior, encoding="utf-8")

        with pytest.raises(SystemExit) as exc:
            cli_main(["install", "--harness", "codex", "--yes", "--no-claude-md"])
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "edit the [mcp_servers.thinkweave] table by hand" in err
        # …and the file it could not edit is exactly as it was.
        assert (codex_home / "config.toml").read_text(encoding="utf-8") == prior

    @pytest.mark.skipif(
        os.name == "nt",
        reason=(
            "Windows has no POSIX mode bits: os.chmod honours only the "
            "read-only flag, so `chmod(0o600)` reads back as 0o666 and the "
            "0600 guarantee is not expressible. The product still performs the "
            "chmod (harmless on Windows, load-bearing on POSIX)."
        ),
    )
    def test_the_file_mode_is_preserved(self, codex_home: Path, installable):
        """Codex creates config.toml 0600 and it can carry env secrets —
        rewriting it must not widen that to the umask default."""
        cfg = codex_home / "config.toml"
        cfg.write_text('[mcp_servers.other]\ncommand = "npx"\n', encoding="utf-8")
        cfg.chmod(0o600)
        _install()
        assert cfg.stat().st_mode & 0o777 == 0o600

    def test_a_non_bmp_vault_path_round_trips(self, codex_home: Path, installable):
        """TOML forbids the surrogate-pair escapes `json.dumps` emits by
        default, so an emoji in a path would produce a file Codex cannot read."""
        _install(vault="/srv/\N{ROCKET}/vault")
        doc = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
        assert doc["mcp_servers"]["thinkweave"]["env"] == {
            "THINKWEAVE_VAULT": "/srv/\N{ROCKET}/vault"
        }

    @needs_codex
    def test_codex_itself_reads_back_what_we_wrote(
        self, codex_home: Path, installable, monkeypatch
    ):
        """The strongest available check that criterion 1 holds: hand the file
        to the real Codex CLI and ask it to resolve the server."""
        _install(vault=VAULT_PATH)
        proc = subprocess.run(
            [CODEX_BIN, "mcp", "get", "thinkweave"],
            env={**os.environ, "CODEX_HOME": str(codex_home)},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert "transport: stdio" in proc.stdout
        assert f"command: {UV_PATH}" in proc.stdout


class TestUninstallAndPauseRoundTrip:
    def test_uninstall_removes_only_our_table(self, codex_home: Path, installable):
        (codex_home / "config.toml").write_text(
            '[mcp_servers.other]\ncommand = "npx"\n', encoding="utf-8"
        )
        _install()
        install_mod.cmd_uninstall(argparse.Namespace(yes=True))

        text = (codex_home / "config.toml").read_text(encoding="utf-8")
        assert "thinkweave" not in text
        assert tomllib.loads(text)["mcp_servers"] == {"other": {"command": "npx"}}

    def test_pause_resume_round_trips(self, codex_home: Path, installable):
        """Byte-identical here because a fresh install leaves our table last.
        Position is not preserved in general — see the test below."""
        from thinkweave.surfaces.cli import pause as pause_mod

        _install()
        before = (codex_home / "config.toml").read_text(encoding="utf-8")

        pause_mod.cmd_pause(argparse.Namespace(status=False))
        assert "thinkweave" not in (codex_home / "config.toml").read_text(
            encoding="utf-8"
        )
        assert (codex_home / "thinkweave_paused.json").exists()

        pause_mod.cmd_resume(argparse.Namespace())
        assert (codex_home / "config.toml").read_text(encoding="utf-8") == before
        assert not (codex_home / "thinkweave_paused.json").exists()

    def test_resume_restores_the_entry_but_not_its_position(
        self, codex_home: Path, installable
    ):
        """`weave resume` re-runs the idempotent installer rather than
        restoring saved bytes (so an upgrade mid-pause doesn't strand stale
        config), which means our table comes back at the end of the file. The
        config is equivalent — TOML tables are unordered — and the user's own
        content is untouched, but the bytes are not identical.
        """
        from thinkweave.surfaces.cli import pause as pause_mod

        _install()
        (codex_home / "config.toml").write_text(
            (codex_home / "config.toml").read_text(encoding="utf-8")
            + "\n# a server I added later\n[mcp_servers.other]\ncommand = \"npx\"\n",
            encoding="utf-8",
        )
        before = tomllib.loads(
            (codex_home / "config.toml").read_text(encoding="utf-8")
        )

        pause_mod.cmd_pause(argparse.Namespace(status=False))
        # The removal leaves no blank residue at the top of the file.
        assert (codex_home / "config.toml").read_text(encoding="utf-8") == (
            '# a server I added later\n[mcp_servers.other]\ncommand = "npx"\n'
        )

        pause_mod.cmd_resume(argparse.Namespace())
        text = (codex_home / "config.toml").read_text(encoding="utf-8")
        assert tomllib.loads(text) == before
        assert "# a server I added later" in text

    def test_pause_names_the_file_it_actually_edited(
        self, codex_home: Path, installable, capsys
    ):
        from thinkweave.surfaces.cli import pause as pause_mod

        _install(no_claude_md=False)
        pause_mod.cmd_pause(argparse.Namespace(status=False))
        assert "CLAUDE.md" not in capsys.readouterr().out

    def test_resume_honours_a_marker_written_before_the_rename(
        self, codex_home: Path, installable
    ):
        """Machines paused by an older version have `CLAUDE.md block` sitting
        in their marker — resuming must still restore the block."""
        import json

        from thinkweave.surfaces.cli import pause as pause_mod

        (codex_home / "thinkweave_paused.json").write_text(
            json.dumps({"paused_at": "2026-01-01", "removed": ["CLAUDE.md block"]}),
            encoding="utf-8",
        )
        pause_mod.cmd_resume(argparse.Namespace())
        assert install_mod.CLAUDE_MD_BLOCK_START in (
            codex_home / "AGENTS.md"
        ).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 3. mcp-doctor
# --------------------------------------------------------------------------- #


class TestMcpDoctorCodexScopes:
    def test_absent_server_is_reported(self, codex_home: Path):
        from thinkweave.surfaces.cli.mcp_doctor import check_registration_scopes

        check = check_registration_scopes(codex_home)
        assert not check.passed
        assert "not registered in any scope" in check.detail

    def test_machine_scope_is_found_in_config_toml(
        self, codex_home: Path, installable
    ):
        from thinkweave.surfaces.cli.mcp_doctor import check_registration_scopes

        _install()
        check = check_registration_scopes(codex_home)
        assert check.passed
        assert "1 scope (machine)" in check.detail

    def test_project_scope_is_the_codex_relpath(self, codex_home: Path, tmp_path: Path):
        from thinkweave.surfaces.cli import mcp_doctor as md

        assert md._entry_from_project_mcp_json(tmp_path)[0] == (
            tmp_path / ".codex" / "config.toml"
        )

    def test_project_scope_carries_the_trust_caveat(
        self, codex_home: Path, tmp_path: Path
    ):
        """Codex reads a project `.codex/config.toml` for *trusted projects
        only* — a registration sitting in an untrusted project is invisible,
        which otherwise reads as "the doctor lied"."""
        from thinkweave.surfaces.cli.mcp_doctor import check_registration_scopes

        proj = tmp_path / "proj" / ".codex"
        proj.mkdir(parents=True)
        (proj / "config.toml").write_text(
            '[mcp_servers.thinkweave]\ncommand = "uv"\nargs = []\n', encoding="utf-8"
        )
        check = check_registration_scopes(tmp_path / "proj")
        assert "trusted" in check.detail.lower()

    def test_malformed_config_does_not_crash_the_doctor(self, codex_home: Path):
        from thinkweave.surfaces.cli.mcp_doctor import check_registration_scopes

        (codex_home / "config.toml").write_text("[[[nope\n", encoding="utf-8")
        assert not check_registration_scopes(codex_home).passed


# --------------------------------------------------------------------------- #
# 4. the AGENTS.md instructions block
# --------------------------------------------------------------------------- #


class TestAgentsMdBlock:
    """Codex's always-loaded user-global instructions file is
    ``$CODEX_HOME/AGENTS.md``. Same sentinel-wrapped splice as the CLAUDE.md
    block; the content is what has to change."""

    def test_block_lands_in_agents_md(self, codex_home: Path, installable):
        _install(no_claude_md=False)
        text = (codex_home / "AGENTS.md").read_text(encoding="utf-8")
        assert install_mod.CLAUDE_MD_BLOCK_START in text
        assert install_mod.CLAUDE_MD_BLOCK_END in text
        assert "weave_search" in text

    @pytest.mark.parametrize("token", ["/wrap", "/clear", "Claude"])
    def test_body_names_nothing_claude_code_specific(
        self, codex_home: Path, token: str
    ):
        # Codex resolves no slash commands and is not Claude Code — a nudge
        # naming either is an instruction the model cannot act on.
        assert token not in harness.active().instructions_block_body

    def test_body_spells_out_the_missing_session_end_hook(self, codex_home: Path):
        """The epic's anti-goal is a silently faked capability. Codex has no
        Stop hook wired (#107), so the block has to name the explicit call that
        replaces it rather than promising automatic extraction."""
        assert "weave_extract" in harness.active().instructions_block_body

    def test_splice_preserves_the_users_own_agents_md(
        self, codex_home: Path, installable
    ):
        (codex_home / "AGENTS.md").write_text(
            "# My global instructions\n\nAlways use tabs.\n", encoding="utf-8"
        )
        _install(no_claude_md=False)
        text = (codex_home / "AGENTS.md").read_text(encoding="utf-8")
        assert "Always use tabs.\n" in text
        assert text.count(install_mod.CLAUDE_MD_BLOCK_START) == 1

    def test_uninstall_strips_it_again(self, codex_home: Path, installable):
        _install(no_claude_md=False)
        install_mod.cmd_uninstall(argparse.Namespace(yes=True))
        assert install_mod.CLAUDE_MD_BLOCK_START not in (
            codex_home / "AGENTS.md"
        ).read_text(encoding="utf-8")
