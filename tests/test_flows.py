"""Tests for the workflow stager (Workstream E)."""

from __future__ import annotations

from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.operations.flows import (
    FlowSpec,
    FlowStage,
    _build_argv,
    _build_command,
    _parse_flows_yaml,
    flows_path,
    load_flows,
    run_flow,
)


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def config(vault_dir: Path) -> Config:
    return Config(vault_root=vault_dir)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class TestFlowsYamlParser:
    def test_empty_file_yields_empty_dict(self):
        assert _parse_flows_yaml("") == {}

    def test_no_flows_block_yields_empty_dict(self):
        assert _parse_flows_yaml("# nothing here\n") == {}

    def test_minimal_flow(self):
        text = (
            "flows:\n"
            "  daily-research:\n"
            "    description: \"Drain the queue\"\n"
            "    stages:\n"
            "      - run: \"/discover\"\n"
            "      - run: \"/research --queue --batch 5\"\n"
        )
        flows = _parse_flows_yaml(text)
        assert "daily-research" in flows
        spec = flows["daily-research"]
        assert spec.description == "Drain the queue"
        assert len(spec.stages) == 2
        assert spec.stages[0].run == "/discover"
        assert spec.stages[1].run == "/research --queue --batch 5"

    def test_stage_sleep_parsed(self):
        text = (
            "flows:\n"
            "  x:\n"
            "    stages:\n"
            "      - run: \"/a\"\n"
            "        sleep: 1800\n"
            "      - run: \"/b\"\n"
        )
        flows = _parse_flows_yaml(text)
        spec = flows["x"]
        assert spec.stages[0].sleep == 1800
        assert spec.stages[1].sleep == 0

    def test_on_error_abort(self):
        text = (
            "flows:\n"
            "  x:\n"
            "    on_error: abort\n"
            "    stages:\n"
            "      - run: \"/a\"\n"
        )
        flows = _parse_flows_yaml(text)
        assert flows["x"].on_error == "abort"

    def test_log_path_expanded(self):
        text = (
            "flows:\n"
            "  x:\n"
            "    log: ~/.cache/thinkweave/flow.log\n"
            "    stages:\n"
            "      - run: \"/a\"\n"
        )
        flows = _parse_flows_yaml(text)
        # ~ should be expanded.
        assert "~" not in str(flows["x"].log)

    def test_two_flows(self):
        text = (
            "flows:\n"
            "  alpha:\n"
            "    stages:\n"
            "      - run: \"/a\"\n"
            "  beta:\n"
            "    stages:\n"
            "      - run: \"/b\"\n"
        )
        flows = _parse_flows_yaml(text)
        assert set(flows) == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# load_flows
# ---------------------------------------------------------------------------


class TestLoadFlows:
    def test_missing_file_returns_empty(self, config: Config):
        assert load_flows(config) == {}

    def test_loads_from_disk(self, config: Config):
        config.config_dir.mkdir(parents=True, exist_ok=True)
        flows_path(config).write_text(
            "flows:\n"
            "  smoke:\n"
            "    description: \"smoke test\"\n"
            "    stages:\n"
            "      - run: \"/smoke\"\n",
            encoding="utf-8",
        )
        flows = load_flows(config)
        assert "smoke" in flows


# ---------------------------------------------------------------------------
# Command building
# ---------------------------------------------------------------------------


class TestBuildCommand:
    def test_basic_invocation(self):
        cmd = _build_command("/discover")
        assert "claude" in cmd
        assert "--model sonnet" in cmd
        assert "-p /discover" in cmd
        assert "--dangerously-skip-permissions" in cmd

    def test_quoted_arg_with_spaces(self):
        cmd = _build_command("/discover (gap cap of 10)")
        # shlex.quote wraps the whole thing in single quotes.
        assert "'/discover (gap cap of 10)'" in cmd

    def test_respects_THINKWEAVE_CLAUDE_BIN(self, monkeypatch):
        monkeypatch.setenv("THINKWEAVE_CLAUDE_BIN", "/custom/claude")
        cmd = _build_command("/x")
        assert cmd.startswith("/custom/claude ")

    def test_namespaces_skill_under_plugin_route(self, monkeypatch, tmp_path):
        # Plugin route active → stage skill tokens render namespaced
        # (plugin commands have no bare-name aliasing).
        import json

        from thinkweave.core import plugin_route

        manifest = tmp_path / "installed_plugins.json"
        manifest.write_text(
            json.dumps({"version": 2, "plugins": {"thinkweave@mp": []}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(plugin_route, "_INSTALLED_PLUGINS", manifest)
        argv = _build_argv("/discover --strategy rss_poll")
        assert "/thinkweave:discover --strategy rss_poll" in argv


class TestBuildArgv:
    """The execution path is an argv list — no shell parsing, so a prompt
    with spaces/quotes/backslashes survives intact on every OS."""

    def test_argv_keeps_prompt_as_single_token(self):
        argv = _build_argv("/discover (gap cap of 10)")
        assert argv == [
            "claude",
            "--model",
            "sonnet",
            "-p",
            "/discover (gap cap of 10)",
            "--dangerously-skip-permissions",
        ]

    def test_argv_does_not_split_windows_path_in_prompt(self):
        # The whole prompt is a single -p token; a backslash path inside it
        # must survive intact (shlex.split would have mangled it).
        argv = _build_argv('/x C:\\Users\\me\\f.md')
        assert argv[4] == "/x C:\\Users\\me\\f.md"

    def test_argv_respects_bin_override(self, monkeypatch):
        monkeypatch.setenv("THINKWEAVE_CLAUDE_BIN", "/custom/claude")
        assert _build_argv("/x")[0] == "/custom/claude"


# ---------------------------------------------------------------------------
# run_flow dry-run
# ---------------------------------------------------------------------------


class TestRunFlowDryRun:
    def test_dry_run_records_invocations_no_subprocess(self):
        spec = FlowSpec(
            name="t",
            description="",
            stages=(FlowStage(run="/a"), FlowStage(run="/b", sleep=10)),
        )
        result = run_flow(spec, dry_run=True)
        # Operation returns data — no stdout, no subprocess.
        assert result.dry_run is True
        assert result.last_code == 0
        cmds = [s.cmd for s in result.stages]
        assert any("/a" in c for c in cmds)
        assert any("/b" in c for c in cmds)
        assert result.stages[1].sleep == 10
        assert all(not s.ran for s in result.stages)

    def test_surface_prints_dry_run_plan(self, capsys):
        from thinkweave.surfaces.cli.flows import _print_flow_result

        spec = FlowSpec(
            name="t",
            description="",
            stages=(FlowStage(run="/a"), FlowStage(run="/b", sleep=10)),
        )
        _print_flow_result(run_flow(spec, dry_run=True))
        out = capsys.readouterr().out
        assert "/a" in out
        assert "/b" in out
        assert "sleep 10s" in out

    def test_dry_run_does_not_create_log(self, tmp_path):
        log_path = tmp_path / "flow.log"
        spec = FlowSpec(
            name="t",
            description="",
            stages=(FlowStage(run="/a"),),
            log=log_path,
        )
        run_flow(spec, dry_run=True)
        assert not log_path.exists()
