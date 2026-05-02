"""Tests for the workflow stager (Workstream E)."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.flows import (
    FlowSpec,
    FlowStage,
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
            "    log: ~/.cache/personal_mem/flow.log\n"
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
        config.mem_dir.mkdir(parents=True, exist_ok=True)
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

    def test_respects_PERSONAL_MEM_CLAUDE_BIN(self, monkeypatch):
        monkeypatch.setenv("PERSONAL_MEM_CLAUDE_BIN", "/custom/claude")
        cmd = _build_command("/x")
        assert cmd.startswith("/custom/claude ")


# ---------------------------------------------------------------------------
# run_flow dry-run
# ---------------------------------------------------------------------------


class TestRunFlowDryRun:
    def test_dry_run_prints_invocations_no_subprocess(self, capsys):
        spec = FlowSpec(
            name="t",
            description="",
            stages=(FlowStage(run="/a"), FlowStage(run="/b", sleep=10)),
        )
        code = run_flow(spec, dry_run=True)
        assert code == 0
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
