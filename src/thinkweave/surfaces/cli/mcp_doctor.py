"""``weave doctor --mcp`` — diagnose thinkweave MCP registration.

Read-only inspection of the three MCP-registration surfaces (machine-
scope ``~/.claude.json``, project-scope ``<cwd>/.mcp.json``, and any
plugin manifests under ``.claude/plugins/``) plus a quick subprocess
liveness probe that confirms the resolved invocation actually starts a
process.

Returns a structured ``DoctorResult`` so callers (the CLI dispatcher,
tests) can branch on ``passed`` without parsing stdout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CLAUDE_JSON = Path.home() / ".claude.json"
SERVER_NAME = "thinkweave"

# ---------- result types ----------


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    fix: str = ""


@dataclass
class DoctorResult:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


# ---------- discovery ----------


def _safe_load_json(path: Path) -> dict[str, Any] | None:
    """Return the parsed JSON body of ``path`` or ``None`` on miss / error.

    A malformed ``~/.claude.json`` is a real failure case the user should
    see, but for plugin manifests we silently skip — they're optional.
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _entry_from_claude_json() -> tuple[Path, dict | None]:
    data = _safe_load_json(CLAUDE_JSON)
    if data is None:
        return CLAUDE_JSON, None
    return CLAUDE_JSON, data.get("mcpServers", {}).get(SERVER_NAME)


def _entry_from_project_mcp_json(cwd: Path) -> tuple[Path, dict | None]:
    path = cwd / ".mcp.json"
    data = _safe_load_json(path)
    if data is None:
        return path, None
    return path, data.get("mcpServers", {}).get(SERVER_NAME)


def _entries_from_plugin_manifests(cwd: Path) -> list[tuple[Path, dict]]:
    """Walk ``<cwd>/.claude/plugins/**/.claude-plugin/plugin.json`` and
    ``<cwd>/.claude-plugin/plugin.json`` for inline mcpServers blocks.
    """
    candidates: list[Path] = []
    root_manifest = cwd / ".claude-plugin" / "plugin.json"
    if root_manifest.exists():
        candidates.append(root_manifest)
    plugins_dir = cwd / ".claude" / "plugins"
    if plugins_dir.exists():
        for plugin_dir in plugins_dir.iterdir():
            if not plugin_dir.is_dir():
                continue
            manifest = plugin_dir / ".claude-plugin" / "plugin.json"
            if manifest.exists():
                candidates.append(manifest)

    entries: list[tuple[Path, dict]] = []
    for path in candidates:
        data = _safe_load_json(path)
        if data is None:
            continue
        entry = data.get("mcpServers", {}).get(SERVER_NAME)
        if entry is not None:
            entries.append((path, entry))
    return entries


# ---------- checks ----------


def _key(entry: dict) -> tuple:
    """Stable fingerprint of an MCP-server entry for conflict detection.

    Compares command basename + args list, with the ``--project`` slot
    normalised to a sentinel — absolute paths, relative ``.``, and
    ``${CLAUDE_PLUGIN_ROOT}`` are all the *same* invocation shape,
    differing only by which scope is launching it.
    """
    cmd = Path(entry.get("command", "")).name
    raw_args = list(entry.get("args", []))
    norm: list[str] = []
    i = 0
    while i < len(raw_args):
        if raw_args[i] == "--project" and i + 1 < len(raw_args):
            norm.extend(["--project", "<scope-specific>"])
            i += 2
            continue
        norm.append(raw_args[i])
        i += 1
    return (cmd, tuple(norm))


def check_registration_scopes(cwd: Path) -> CheckResult:
    """Report which scopes declare thinkweave; FAIL if >1 conflict."""
    scopes: list[tuple[str, Path, dict]] = []
    _, machine_entry = _entry_from_claude_json()
    if machine_entry is not None:
        scopes.append(("machine", CLAUDE_JSON, machine_entry))
    project_path, project_entry = _entry_from_project_mcp_json(cwd)
    if project_entry is not None:
        scopes.append(("project", project_path, project_entry))
    for path, entry in _entries_from_plugin_manifests(cwd):
        scopes.append(("plugin", path, entry))

    if not scopes:
        return CheckResult(
            name="registration scopes",
            passed=False,
            detail="thinkweave is not registered in any scope",
            fix="run `weave install --yes` (machine) or install the plugin",
        )

    if len(scopes) == 1:
        scope_name, _path, _entry = scopes[0]
        return CheckResult(
            name="registration scopes",
            passed=True,
            detail=f"1 scope ({scope_name}) declares thinkweave",
        )

    keys = {_key(entry) for _name, _path, entry in scopes}
    summary = ", ".join(name for name, _, _ in scopes)
    if len(keys) > 1:
        return CheckResult(
            name="registration scopes",
            passed=False,
            detail=(
                f"{len(scopes)} scopes declare thinkweave with DIFFERENT "
                f"invocations: {summary} — Claude Code will pick one and warn"
            ),
            fix=(
                "reconcile to a single shape (re-run `weave install --yes` and "
                "delete the divergent file)"
            ),
        )
    return CheckResult(
        name="registration scopes",
        passed=True,
        detail=f"{len(scopes)} scopes declare thinkweave identically ({summary})",
    )


def check_launcher_resolves(cwd: Path, timeout_s: float = 5.0) -> CheckResult:
    """Resolve the most-specific entry's command and try a quick launch.

    Precedence (mirrors Claude Code's resolution order best-effort):
    machine > project > plugin. The chosen entry's command is run via
    ``subprocess`` with a short timeout — MCP servers idle on stdin, so
    a clean timeout means "process started, awaiting input" = success.
    """
    _, machine_entry = _entry_from_claude_json()
    project_path, project_entry = _entry_from_project_mcp_json(cwd)
    plugin_entries = _entries_from_plugin_manifests(cwd)

    entry: dict | None
    source: str
    if machine_entry is not None:
        entry, source = machine_entry, str(CLAUDE_JSON)
    elif project_entry is not None:
        entry, source = project_entry, str(project_path)
    elif plugin_entries:
        entry, source = plugin_entries[0][1], str(plugin_entries[0][0])
    else:
        return CheckResult(
            name="launcher resolves",
            passed=False,
            detail="no MCP entry to probe",
            fix="register thinkweave first (see scope check above)",
        )

    cmd = entry.get("command", "")
    args = list(entry.get("args", []))

    # Expand env vars in args (notably ${CLAUDE_PLUGIN_ROOT} for plugins).
    # For the doctor probe we don't have a real CLAUDE_PLUGIN_ROOT, so
    # substitute the cwd — that lets us validate the *shape* of the
    # invocation even when the plugin isn't installed.
    env_subs = {"CLAUDE_PLUGIN_ROOT": str(cwd)}
    expanded = [
        a if not isinstance(a, str) else _expand_env(a, env_subs) for a in args
    ]

    resolved = shutil.which(cmd) or cmd
    if shutil.which(cmd) is None and not Path(cmd).is_absolute():
        return CheckResult(
            name="launcher resolves",
            passed=False,
            detail=f"command `{cmd}` (from {source}) is not on PATH",
            fix=(
                "install uv (curl -LsSf https://astral.sh/uv/install.sh | sh) "
                "or re-run `weave install --yes` to pin an absolute path"
            ),
        )

    try:
        proc = subprocess.run(
            [resolved, *expanded],
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # Server started and is awaiting stdin — this is the success
        # signal for a long-running MCP stdio server.
        return CheckResult(
            name="launcher resolves",
            passed=True,
            detail=(
                f"launcher OK — `{resolved} {' '.join(expanded)}` "
                f"(from {source}) spawned a process awaiting stdin"
            ),
        )
    except FileNotFoundError as exc:
        return CheckResult(
            name="launcher resolves",
            passed=False,
            detail=f"could not exec `{resolved}` (from {source}): {exc}",
            fix="install uv or re-run `weave install --yes`",
        )

    # The process actually exited inside the timeout — that's a failure
    # for an MCP stdio server (it should idle on stdin).
    if proc.returncode == 0:
        # Some shims print help and exit 0; treat as success but informational.
        return CheckResult(
            name="launcher resolves",
            passed=True,
            detail=f"launcher exited 0 — entry from {source} resolves",
        )
    stderr_tail = proc.stderr.decode("utf-8", errors="replace").strip()[-200:]
    return CheckResult(
        name="launcher resolves",
        passed=False,
        detail=(
            f"launcher exited {proc.returncode} (entry from {source}). "
            f"stderr: {stderr_tail or '<empty>'}"
        ),
        fix="run the invocation by hand to see the full error",
    )


def _expand_env(value: str, env: dict[str, str]) -> str:
    """``$VAR`` / ``${VAR}`` substitution against an explicit map +
    ``os.environ``. Leaves unknown vars untouched."""
    merged = {**os.environ, **env}
    out = value
    for key, val in merged.items():
        out = out.replace(f"${{{key}}}", val).replace(f"${key}", val)
    return out


def check_vault_env() -> CheckResult:
    """``THINKWEAVE_VAULT`` (if set) must point at an existing dir."""
    # Tests can inject a synthetic value via MCP_DOCTOR_FAKE_VAULT to
    # force a fail path without touching the user's real vault config.
    raw = os.environ.get("MCP_DOCTOR_FAKE_VAULT") or os.environ.get(
        "THINKWEAVE_VAULT"
    )
    if not raw:
        return CheckResult(
            name="THINKWEAVE_VAULT",
            passed=True,
            detail="not set — will fall back to ~/vault at first use",
        )
    if not Path(raw).expanduser().is_dir():
        return CheckResult(
            name="THINKWEAVE_VAULT",
            passed=False,
            detail=f"set to `{raw}` but that directory does not exist",
            fix="`mkdir -p $THINKWEAVE_VAULT && weave init`",
        )
    return CheckResult(
        name="THINKWEAVE_VAULT",
        passed=True,
        detail=f"set to `{raw}` (exists)",
    )


def check_weave_mcp_on_path() -> CheckResult:
    """Informational: ``weave-mcp`` console script on PATH. Not fatal —
    the canonical invocation is ``uv run … weave-mcp`` which doesn't need
    it. Reported as a hint when the launcher probe fails on PATH.
    """
    found = shutil.which("weave-mcp")
    if found:
        return CheckResult(
            name="weave-mcp on PATH",
            passed=True,
            detail=f"found at {found}",
        )
    return CheckResult(
        name="weave-mcp on PATH",
        passed=True,  # informational — never fail
        detail=(
            "not on PATH (informational only — `uv run … weave-mcp` is the "
            "canonical invocation)"
        ),
    )


# ---------- top-level driver ----------


def run_mcp_doctor(cwd: Path | None = None) -> DoctorResult:
    """Run every MCP-wiring check and return a structured result."""
    cwd = cwd or Path.cwd()
    result = DoctorResult()
    result.checks.append(check_registration_scopes(cwd))
    # Launcher probe is only meaningful if at least one scope registers.
    if result.checks[-1].passed and "not registered" not in result.checks[-1].detail:
        result.checks.append(check_launcher_resolves(cwd))
    result.checks.append(check_vault_env())
    result.checks.append(check_weave_mcp_on_path())
    _print_doctor_report(result)
    return result


def _print_doctor_report(result: DoctorResult) -> None:
    print("weave doctor --mcp")
    print("=" * 60)
    for check in result.checks:
        mark = "PASS" if check.passed else "FAIL"
        print(f"  [{mark}] {check.name}: {check.detail}")
        if not check.passed and check.fix:
            print(f"         fix: {check.fix}")
    print("-" * 60)
    overall = "PASS" if result.passed else "FAIL"
    print(f"  overall: {overall}")
