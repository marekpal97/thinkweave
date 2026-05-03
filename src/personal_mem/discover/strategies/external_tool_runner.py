"""External-tool-runner strategy — shell out to user-provided scripts.

Lets a project plug an arbitrary script into ``mem discover`` without
modifying the framework. Configuration lives in ``sources.yaml``:

    projects:
      trade_ideas:
        external_tool_runner:
          tools:
            - command: ["./scripts/scrape_signals.py"]
            - command: ["python", "-m", "tools.gh_trending"]

Each tool is invoked with the project name as an extra argument. Its
stdout is read line by line — every non-empty line that parses as JSON
becomes a queue item dict (with ``strategy=external_tool_runner``
stamped onto it for provenance). Lines that fail to parse are skipped
silently; tools are expected to write only JSONL.

Tools run with a configurable timeout (default 60 s). A non-zero exit
status drops the tool's output but doesn't abort the strategy — the
remaining tools still run.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any


class ExternalToolRunnerStrategy:
    name = "external_tool_runner"

    def run(
        self,
        vault: Any,
        project: str | None,
        config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        config = config or {}
        tools = self._tools_for_project(config, project)
        if not tools:
            return []

        timeout = int(
            config.get("projects", {})
            .get(project or "default", {})
            .get(self.name, {})
            .get("timeout", 60)
        )

        out: list[dict[str, Any]] = []
        for tool in tools:
            cmd = self._cmd(tool)
            if not cmd:
                continue
            args = list(cmd)
            if project:
                args.append(project)
            try:
                proc = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                continue
            if proc.returncode != 0:
                continue
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                payload.setdefault("strategy", self.name)
                payload.setdefault("kind", "external")
                out.append(payload)
        return out

    @staticmethod
    def _tools_for_project(
        config: dict[str, Any], project: str | None
    ) -> list[dict[str, Any] | str | list[str]]:
        projects = config.get("projects", {}) or {}
        scope = projects.get(project or "default", {}) or {}
        tools = (scope.get("external_tool_runner") or {}).get("tools", [])
        if not isinstance(tools, list):
            return []
        return list(tools)

    @staticmethod
    def _cmd(tool: Any) -> list[str]:
        if isinstance(tool, list):
            return [str(part) for part in tool]
        if isinstance(tool, dict):
            cmd = tool.get("command", [])
            if isinstance(cmd, str):
                return shlex.split(cmd)
            if isinstance(cmd, list):
                return [str(part) for part in cmd]
            return []
        if isinstance(tool, str):
            return shlex.split(tool)
        return []


STRATEGY = ExternalToolRunnerStrategy()
