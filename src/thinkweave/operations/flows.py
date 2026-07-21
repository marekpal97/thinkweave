"""Workflow stager — named, reusable pipelines of `claude -p` skill calls.

Mirrors the source-registry pattern: a tiny declarative spec drives behavior.
Each flow is a sequence of stages, each stage a literal `claude -p`
invocation. Skills are already the unit of composition; flows just
sequence them and remove the "remember the order and copy the flags"
problem.

The flow definitions live at ``vault/.weave/flows.yaml`` (alongside other
vault-local config like ``concept_aliases.yaml``). When the file is
missing, ``load_flows`` returns an empty dict — no flows installed.

Design constraints — kept narrow on purpose:

- No templating, no conditionals, no parallel branches.
- ``stage.run`` is a literal argument string passed to ``claude -p``.
- ``sleep`` between stages is an integer of seconds.
- ``on_error`` is ``continue`` (default) or ``abort``.
- The harness model is hardcoded to ``sonnet`` and the flag set matches
  the existing cron entries (``--dangerously-skip-permissions``).

When this surface needs to grow (parallel branches, conditionals on
return code, templating), prefer adding a separate primitive over
expanding ``FlowSpec`` — keep the small spec small.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from thinkweave.core.config import Config
from thinkweave.core.plugin_route import namespace_prompt, plugin_namespace


OnError = Literal["continue", "abort"]


@dataclass(frozen=True)
class FlowStage:
    run: str               # literal `claude -p` argument
    sleep: int = 0         # seconds to sleep AFTER this stage runs


@dataclass(frozen=True)
class FlowSpec:
    name: str
    description: str
    stages: tuple[FlowStage, ...]
    on_error: OnError = "continue"
    log: Path | None = None  # absolute path; None = stdout/stderr only

    def render_invocation(self, stage: FlowStage) -> str:
        """The literal command we'd execute for one stage. Used by --dry-run."""
        return _build_command(stage.run)


@dataclass
class FlowStageResult:
    """Outcome of one stage in a :func:`run_flow` pass."""

    index: int          # 1-based position
    total: int          # number of stages in the flow
    cmd: str            # the display command (from ``_build_command``)
    sleep: int = 0      # seconds slept after this stage (0 = none)
    returncode: int | None = None  # None in dry-run or if never reached
    aborted: bool = False          # this stage triggered on_error=abort
    ran: bool = False              # the subprocess actually executed


@dataclass
class FlowRunResult:
    """Structured outcome of :func:`run_flow`.

    The operation returns data and does its own file logging; the CLI surface
    formats stdout (dry-run plan or, for logless real runs, the per-stage
    banners) and picks the exit code. Mirrors the ``wrap.py`` pattern.
    """

    name: str
    dry_run: bool
    last_code: int = 0
    logged_to_file: bool = False
    stages: list[FlowStageResult] = field(default_factory=list)


def flows_path(config: Config) -> Path:
    """Vault-local config file. Same dir as concept_aliases.yaml etc.

    Resolved under ``vault/config/flows.yaml`` (canonical). A file still
    at ``vault/.weave/flows.yaml`` raises :class:`LegacyConfigLocationError`.
    """
    from thinkweave.core.config import resolve_config_file

    return resolve_config_file(config.vault_root, "flows.yaml")


def load_flows(config: Config, *, path: Path | None = None) -> dict[str, FlowSpec]:
    """Load named flows from ``vault/.weave/flows.yaml``.

    Returns an empty dict if the file is absent. Parsing is intentionally
    minimal — we accept the small dialect documented in
    ``scripts/example-crontab`` and the shipped template.
    """
    p = path or flows_path(config)
    if not p.exists():
        return {}

    raw = p.read_text(encoding="utf-8")
    return _parse_flows_yaml(raw)


def run_flow(spec: FlowSpec, *, dry_run: bool = False) -> FlowRunResult:
    """Execute a flow's stages in order, returning a :class:`FlowRunResult`.

    Pure of stdout: when ``spec.log`` is set the per-stage banners + captured
    subprocess output go to that log file; otherwise the surface renders the
    banners from the result. ``dry_run`` records the resolved invocations
    without executing them (no side effects). The CLI surface picks the exit
    code from ``result.last_code``.
    """
    result = FlowRunResult(
        name=spec.name,
        dry_run=dry_run,
        logged_to_file=bool(spec.log and not dry_run),
    )

    log_handle = None
    if spec.log and not dry_run:
        spec.log.parent.mkdir(parents=True, exist_ok=True)
        log_handle = spec.log.open("a", encoding="utf-8")

    try:
        for i, stage in enumerate(spec.stages):
            cmd = _build_command(stage.run)
            sr = FlowStageResult(
                index=i + 1, total=len(spec.stages), cmd=cmd, sleep=stage.sleep
            )
            result.stages.append(sr)
            if dry_run:
                continue

            if log_handle:
                _log(log_handle, f"\n=== flow {spec.name} stage {i + 1}/{len(spec.stages)} ===")
                _log(log_handle, f"$ {cmd}")
            proc = subprocess.run(
                _build_argv(stage.run),
                stdout=log_handle if log_handle else None,
                stderr=subprocess.STDOUT if log_handle else None,
            )
            sr.returncode = proc.returncode
            sr.ran = True
            result.last_code = proc.returncode
            if log_handle:
                _log(log_handle, f"=== exit {proc.returncode} ===")

            if proc.returncode != 0 and spec.on_error == "abort":
                sr.aborted = True
                if log_handle:
                    _log(
                        log_handle,
                        f"[{spec.name}] aborting on stage {i + 1} (on_error=abort)",
                    )
                break

            if stage.sleep and i < len(spec.stages) - 1:
                time.sleep(stage.sleep)
    finally:
        if log_handle:
            log_handle.close()
    return result


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_argv(run_arg: str) -> list[str]:
    """The argv list executed for one stage.

    Building the argv directly (rather than a shell string parsed by
    ``shlex.split``) keeps execution correct on every OS — the prompt may
    contain spaces, quotes, or backslashes (Windows paths) that POSIX
    shell-splitting would mangle. Hardcodes the Claude Code flags the cron
    entries use.
    """
    # PERSONAL_MEM_CLAUDE_BIN: pre-rename migration fallback (→ thinkweave 2026-06-13).
    bin_path = (
        os.environ.get("THINKWEAVE_CLAUDE_BIN")
        or os.environ.get("PERSONAL_MEM_CLAUDE_BIN")
        or "claude"
    )
    # Plugin-route installs register skills namespaced (`/thinkweave:dream`),
    # with no bare-name aliasing — rewrite the stage's skill token to match.
    run_arg = namespace_prompt(run_arg, plugin_namespace())
    return [bin_path, "--model", "sonnet", "-p", run_arg, "--dangerously-skip-permissions"]


def _build_command(run_arg: str) -> str:
    """Human-readable display string for one stage (dry-run + logs).

    Execution goes through :func:`_build_argv`; this is only for showing the
    user what will run. Quoting is OS-correct: ``shlex.join`` on POSIX,
    ``subprocess.list2cmdline`` on Windows.
    """
    argv = _build_argv(run_arg)
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def _log(handle, text: str) -> None:
    """Write one banner line to the flow's log file. ``run_flow`` only calls
    this with a real handle (logless runs render banners at the surface), so
    there's no stdout branch — keeping this module free of ``print``.
    """
    handle.write(text + "\n")
    handle.flush()


def _parse_flows_yaml(text: str) -> dict[str, FlowSpec]:
    """Minimal flows.yaml parser.

    Grammar (intentionally narrow):

        flows:
          <name>:
            description: ...
            log: ...                  # optional path
            on_error: continue|abort  # optional, default continue
            stages:
              - run: "..."            # required
                sleep: 1800           # optional
              - run: "..."

    String values may be unquoted, single-quoted, or double-quoted.
    Anything we don't understand is ignored — parsing is best-effort
    so a broken file silently disables flows rather than crashing the
    CLI.
    """
    lines = text.splitlines()
    out: dict[str, FlowSpec] = {}

    # Walk to "flows:" header.
    i = 0
    while i < len(lines) and lines[i].strip() != "flows:":
        i += 1
    if i >= len(lines):
        return {}
    i += 1

    # Each top-level child of `flows:` is a flow entry.
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        # End of flows: block when indentation drops back to column 0.
        if line[:1] not in (" ", "\t"):
            break
        # A flow header looks like `  <name>:` at exactly 2-space indent.
        stripped = line.strip()
        if stripped.endswith(":") and not stripped.startswith("- "):
            name = stripped[:-1]
            i, spec = _parse_one_flow(name, lines, i + 1)
            out[name] = spec
            continue
        i += 1

    return out


def _parse_one_flow(name: str, lines: list[str], start: int) -> tuple[int, FlowSpec]:
    description = ""
    on_error: OnError = "continue"
    log_path: Path | None = None
    stages: list[FlowStage] = []

    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        # Detect the next flow (4-or-fewer-space indent ending in ':')
        # or end of flows block (indent dropping to 0).
        if line[:1] not in (" ", "\t"):
            break
        indent = len(line) - len(line.lstrip(" "))
        if indent <= 2:
            # Sibling flow or higher-level key.
            break
        stripped = line.strip()

        if stripped.startswith("description:"):
            description = _strip_value(stripped.split(":", 1)[1])
            i += 1
            continue
        if stripped.startswith("log:"):
            raw = _strip_value(stripped.split(":", 1)[1])
            log_path = Path(os.path.expanduser(raw))
            i += 1
            continue
        if stripped.startswith("on_error:"):
            val = _strip_value(stripped.split(":", 1)[1])
            if val in ("continue", "abort"):
                on_error = val  # type: ignore[assignment]
            i += 1
            continue
        if stripped == "stages:":
            i, stages = _parse_stages(lines, i + 1, parent_indent=indent)
            continue
        i += 1

    return i, FlowSpec(
        name=name,
        description=description,
        stages=tuple(stages),
        on_error=on_error,
        log=log_path,
    )


def _parse_stages(
    lines: list[str], start: int, parent_indent: int
) -> tuple[int, list[FlowStage]]:
    stages: list[FlowStage] = []
    current_run: str | None = None
    current_sleep: int = 0

    def _flush() -> None:
        nonlocal current_run, current_sleep
        if current_run is not None:
            stages.append(FlowStage(run=current_run, sleep=current_sleep))
        current_run, current_sleep = None, 0

    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= parent_indent:
            # Out of the stages block.
            break

        stripped = line.strip()
        if stripped.startswith("- run:"):
            _flush()
            current_run = _strip_value(stripped[len("- run:"):])
        elif stripped.startswith("run:") and current_run is None:
            # Tolerate `- run:` followed by `run:` on a continuation line.
            current_run = _strip_value(stripped.split(":", 1)[1])
        elif stripped.startswith("sleep:"):
            try:
                current_sleep = int(_strip_value(stripped.split(":", 1)[1]))
            except ValueError:
                current_sleep = 0
        # Other keys ignored intentionally.
        i += 1

    _flush()
    return i, stages


def _strip_value(raw: str) -> str:
    """Strip whitespace + matching outer quotes from a YAML scalar."""
    v = raw.strip()
    if (v.startswith('"') and v.endswith('"')) or (
        v.startswith("'") and v.endswith("'")
    ):
        v = v[1:-1]
    return v
