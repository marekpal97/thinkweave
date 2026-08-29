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
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from thinkweave.core import mcp_config
from thinkweave.core.harness import active as _profile

# Borrowed from the installer so the doctor's Windows gate and the remedy it
# prints are the same ones the installer acts on.
from thinkweave.surfaces.cli.install import _detect_project_root, _is_windows

SERVER_NAME = "thinkweave"

# Extensions a direct CreateProcess can launch on Windows. Deliberately a
# closed set rather than a read of %PATHEXT%: this decides whether a *shell-less*
# MCP spawn can run the file, and the entries a user has added to PATHEXT (.py,
# .ps1) are resolved by the shell, not by CreateProcess.
_WIN_EXEC_SUFFIXES = frozenset({".cmd", ".bat", ".exe", ".com"})

# The three harness-scoped locations the doctor inspects, all read from the
# active profile: the machine-scope MCP config, plus the two HOME-scoped plugin
# install locations. A marketplace install copies the plugin to
# ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/; `weave dev-link`
# symlinks the checkout to ~/.claude/skills/<name>/. The plugin manifest there
# declares the MCP server, so the doctor must scan these to recognise a clean
# plugin-only install (no raw entry in the harness's own MCP config).

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

    A malformed harness MCP config is a real failure case the user should
    see, but for plugin manifests we silently skip — they're optional.
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _mcp_servers(data: dict[str, Any]) -> dict[str, Any]:
    """Return the ``mcpServers`` block if it's an inline dict, else ``{}``.

    The plugin manifest schema also allows ``mcpServers`` to be a *string*
    path to an external file (e.g. Atlassian's ``"./.mcp.json"``) — the
    doctor only inspects inline blocks, so an external-file reference is
    treated as declaring nothing here rather than crashing.
    """
    servers = data.get("mcpServers", {})
    return servers if isinstance(servers, dict) else {}


def _safe_read_entry(path: Path) -> dict | None:
    """The thinkweave block from a harness MCP config, in whatever format that
    harness uses (JSON for Claude Code, TOML for Codex) and under whatever
    servers key the profile declares (``mcp`` on OpenCode). A malformed file
    reads as "absent" — the doctor reports the missing registration rather
    than aborting on someone else's syntax error. Only the profile's own
    machine/project files come through here; plugin manifests are read
    elsewhere and always use Claude Code's ``mcpServers`` shape."""
    try:
        return mcp_config.read_entry(
            path, SERVER_NAME, servers_key=_profile().mcp_servers_key
        )
    except mcp_config.MalformedConfig:
        return None


def _entry_from_claude_json() -> tuple[Path, dict | None]:
    """The machine-scope MCP registration, read from the harness's own config."""
    path = _profile().mcp_config
    return path, _safe_read_entry(path)


def _entry_from_project_mcp_json(cwd: Path) -> tuple[Path, dict | None]:
    path = cwd / _profile().project_mcp_config_relpath
    return path, _safe_read_entry(path)


def _entries_from_plugin_manifests(cwd: Path) -> list[tuple[Path, dict]]:
    """Collect thinkweave mcpServers blocks from every plugin manifest that
    could be active: the cwd-relative project plugin (``<cwd>/.claude-plugin``,
    ``<cwd>/.claude/plugins``) AND the HOME-scoped install locations — the
    marketplace cache (``~/.claude/plugins/cache/<mkt>/<plugin>/<ver>``) and the
    dev-link skills dir (``~/.claude/skills/<name>``).

    Scanning HOME is what lets the doctor recognise a clean plugin-only install
    (no raw ``~/.claude.json`` entry) — without it, a plugin-route user running
    from an arbitrary cwd sees a false "not registered" FAIL.
    """
    profile = _profile()
    manifest_rel = profile.plugin_manifest_relpath

    candidates: list[Path] = []
    root_manifest = cwd / manifest_rel
    if root_manifest.exists():
        candidates.append(root_manifest)
    plugins_dir = cwd / profile.project_plugins_relpath
    if plugins_dir.exists():
        for plugin_dir in plugins_dir.iterdir():
            if not plugin_dir.is_dir():
                continue
            manifest = plugin_dir / manifest_rel
            if manifest.exists():
                candidates.append(manifest)

    # HOME-scoped installs — where plugins actually live for real users. The
    # leading wildcards are this harness's nesting depth for each location:
    # cache/<marketplace>/<plugin>/<version>/… and skills/<name>/….
    if profile.plugins_cache.exists():
        candidates.extend(profile.plugins_cache.glob(f"*/*/*/{manifest_rel.as_posix()}"))
    if profile.skills_dir.exists():
        candidates.extend(profile.skills_dir.glob(f"*/{manifest_rel.as_posix()}"))

    entries: list[tuple[Path, dict]] = []
    seen: set[Path] = set()
    for path in candidates:
        # Dedup by physical path — a dev-link symlink can resolve to the same
        # checkout as the cwd manifest; counting it twice would be a phantom
        # "2 scopes" conflict.
        try:
            rp = path.resolve()
        except OSError:
            rp = path
        if rp in seen:
            continue
        seen.add(rp)
        data = _safe_load_json(path)
        if data is None:
            continue
        entry = _mcp_servers(data).get(SERVER_NAME)
        if entry is not None:
            entries.append((path, entry))
    return entries


def _command_stem(command: str) -> str:
    """A command's basename, with a Windows executable suffix normalised away.

    ``_detect_uv_path`` stores ``shutil.which("uv")``, which on Windows is
    ``C:\\…\\uv.EXE`` — uppercase suffix included. A plain basename therefore
    fingerprinted the machine entry as ``uv.EXE`` while the launcher branch
    below produced ``uv``, so any Windows install carrying BOTH a machine entry
    and the committed ``.mcp.json`` was reported as a cross-scope conflict that
    did not exist.

    Stripping ``.cmd`` is the same normalisation seen from the other side: the
    ``.cmd`` launcher and its extensionless POSIX sibling are two
    implementations of one command and must fingerprint alike.
    """
    name = Path(command).name
    suffix = Path(name).suffix.lower()
    if suffix in _WIN_EXEC_SUFFIXES:
        name = name[: -len(suffix)]
    # Windows paths are case-insensitive, so `UV.EXE` and `uv` are one command
    # there. On POSIX they are genuinely two, and case is left alone.
    return name.casefold() if sys.platform == "win32" else name


def _key(entry: dict) -> tuple:
    """Stable fingerprint of an MCP-server entry for conflict detection.

    Compares command basename + args list, with the ``--project`` slot
    normalised to a sentinel — absolute paths, relative ``.``, and
    ``${CLAUDE_PLUGIN_ROOT}`` are all the *same* invocation shape,
    differing only by which scope is launching it.

    ``--no-sync`` is dropped for the same reason: it changes how uv *prepares*
    the environment, not what gets launched into it. The machine-scope entry
    passes it (``weave install`` has already synced) and the portable launchers
    do not (they bootstrap the plugin route), so without this the two would
    report a phantom cross-scope conflict.
    """
    cmd = _command_stem(entry.get("command", ""))
    raw_args = list(entry.get("args", []))
    norm: list[str] = []
    i = 0
    while i < len(raw_args):
        if raw_args[i] == "--project" and i + 1 < len(raw_args):
            norm.extend(["--project", "<scope-specific>"])
            i += 2
            continue
        if raw_args[i] == "--no-sync":
            i += 1
            continue
        norm.append(raw_args[i])
        i += 1
    # `_command_stem` has already folded the native-Windows `.cmd` launcher onto
    # its POSIX sibling's name — they are one command, two implementations.
    if cmd == "weave-mcp-launch":
        # The portable launcher (#52) IS the canonical uv-run invocation —
        # it resolves uv and execs `uv run --project <root> --extra mcp
        # weave-mcp` — so it fingerprints identically to a direct uv entry
        # (e.g. the machine scope written by `weave install`).
        return (
            "uv",
            (
                "run", "--project", "<scope-specific>",
                "--extra", "mcp", "python", "-m",
                "thinkweave.surfaces.mcp.server",
                *norm,
            ),
        )
    return (cmd, tuple(norm))


def check_registration_scopes(cwd: Path) -> CheckResult:
    """Report which scopes declare thinkweave; FAIL if >1 conflict."""
    scopes: list[tuple[str, Path, dict]] = []
    _, machine_entry = _entry_from_claude_json()
    if machine_entry is not None:
        scopes.append(("machine", _profile().mcp_config, machine_entry))
    project_path, project_entry = _entry_from_project_mcp_json(cwd)
    if project_entry is not None:
        # Some harnesses only honour a project-scope registration under a
        # condition of their own (Codex: the project must be trusted). Saying
        # "registered" without that would read as the doctor lying when the
        # tools don't show up.
        caveat = _profile().project_mcp_caveat
        scopes.append((f"project ({caveat})" if caveat else "project",
                       project_path, project_entry))
    for path, entry in _entries_from_plugin_manifests(cwd):
        scopes.append(("plugin", path, entry))

    if not scopes:
        # The fix has to name the harness it applies to: `weave install`
        # defaults to Claude Code, so handing a Codex user the bare command
        # would write the registration into the wrong home.
        harness_flag = (
            f" {_profile().harness_flag}" if _profile().harness_flag else ""
        )
        return CheckResult(
            name="registration scopes",
            passed=False,
            detail="thinkweave is not registered in any scope",
            fix=(
                f"run `weave install --yes{harness_flag}` (machine) "
                "or install the plugin"
            ),
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
                f"invocations: {summary} — {_profile().display_name} will pick "
                "one and warn"
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


def _repo_local_hook_files(cwd: Path) -> list[Path]:
    """Repo-local files that declare hooks, for a harness that never fires
    them from there.

    Codex accepts hooks in TWO representations per config layer — a
    ``hooks.json`` and an inline ``[hooks]`` table in the sibling
    ``config.toml`` — so checking only one leaves half the failure
    undiagnosed. The ``config.toml`` in that directory is also where #106
    writes ``[mcp_servers]``, which is a different concern; only a ``hooks``
    key counts.
    """
    hooks_rel = _profile().project_settings_relpath
    found: list[Path] = []

    hooks_file = cwd / hooks_rel
    data = _safe_load_json(hooks_file)
    if data and data.get("hooks"):
        found.append(hooks_file)

    toml_file = cwd / hooks_rel.parent / "config.toml"
    if toml_file.exists():
        try:
            if tomllib.loads(toml_file.read_text(encoding="utf-8")).get("hooks"):
                found.append(toml_file)
        except (OSError, ValueError):
            # Someone else's syntax error is not this check's business.
            pass
    return found


def check_hook_scope(cwd: Path) -> CheckResult:
    """FAIL when hooks are declared where this harness will never run them.

    Only meaningful for a ``hooks_global_only`` harness. On Codex a repo-local
    entry parses cleanly and then simply never fires (openai/codex#17532; the
    manual additionally gates project ``.codex/`` layers on the project being
    trusted) — silently-inert config, which is the failure class this project
    keeps getting bitten by, so it gets a named check rather than a footnote.
    """
    stray = _repo_local_hook_files(cwd)
    if not stray:
        return CheckResult(
            name="hook scope",
            passed=True,
            detail="no repo-local hook declarations",
        )
    listed = ", ".join(str(p) for p in stray)
    return CheckResult(
        name="hook scope",
        passed=False,
        detail=(
            f"hooks declared repo-local in {listed} — {_profile().id} accepts "
            "these and then never fires them"
        ),
        fix=(
            "move them to the machine scope: "
            f"`weave hooks install --scope user --harness {_profile().id}` "
            f"(writes {_profile().user_settings}), then delete the repo-local "
            "hook entries"
        ),
    )


# Slash-command symlinks are machine-local by convention (never committed), so
# the only durable remedy the doctor can offer for a known one is the command
# that recreates it. `/issue-loop` points at the funloops sibling checkout —
# the target CLAUDE.md documents.
_COMMAND_LINK_REPOINT = {
    "issue-loop.md": (
        "ln -sfn ../../../funloops/packages/devloop/docs/agents/"
        "issue-loop.command.md .claude/commands/issue-loop.md"
    ),
}


def check_command_symlinks(cwd: Path) -> CheckResult:
    """FAIL when a machine-local slash-command symlink no longer resolves —
    the harness silently drops the command from its menu (#172).

    Report-only: the repair is machine-local, so the doctor names the
    command rather than running it. A checkout without symlink support
    simply has none to find; an absent or unreadable directory reads as
    "nothing to report" rather than raising.
    """
    try:
        entries = sorted((cwd / ".claude" / "commands").iterdir())
    except OSError:
        entries = []
    dangling = [
        (e.name, os.readlink(e))
        for e in entries
        if e.is_symlink() and not e.exists()
    ]
    if not dangling:
        return CheckResult(
            name="command symlinks",
            passed=True,
            detail="no dangling links under .claude/commands/",
        )
    return CheckResult(
        name="command symlinks",
        passed=False,
        detail=(
            "dangling: "
            + ", ".join(f"{name} → {target}" for name, target in dangling)
        ),
        fix="; ".join(
            _COMMAND_LINK_REPOINT.get(
                name, f"re-point or delete .claude/commands/{name}"
            )
            for name, _ in dangling
        ),
    )


def _git_bash_path(path: Path) -> str:
    """Translate an absolute Windows path for Git Bash."""
    value = path.resolve().as_posix()
    return f"/{value[0].lower()}{value[2:]}"


def _launcher_probe_argv(resolved: str, args: list[str]) -> list[str]:
    """Run POSIX launcher shims through Git Bash on Windows."""
    argv = [resolved, *args]
    if os.name != "nt":
        return argv

    path = Path(resolved)
    try:
        is_shell_script = path.is_file() and path.read_bytes()[:2] == b"#!"
    except OSError:
        is_shell_script = False
    if not is_shell_script:
        return argv

    git = shutil.which("git")
    if not git:
        return argv
    bash = Path(git).resolve().parents[1] / "bin" / "bash.exe"
    if not bash.is_file():
        return argv
    return [str(bash), _git_bash_path(path), *args]

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
    plugin_root: Path | None = None
    if machine_entry is not None:
        entry, source = machine_entry, str(_profile().mcp_config)
    elif project_entry is not None:
        entry, source = project_entry, str(project_path)
    elif plugin_entries:
        manifest_path, entry = plugin_entries[0]
        source = str(manifest_path)
        # <plugin-root>/.claude-plugin/plugin.json — what Claude Code
        # substitutes for ${CLAUDE_PLUGIN_ROOT} when launching.
        plugin_root = manifest_path.parent.parent
    else:
        return CheckResult(
            name="launcher resolves",
            passed=False,
            detail="no MCP entry to probe",
            fix="register thinkweave first (see scope check above)",
        )

    raw_cmd = entry.get("command", "")
    args = list(entry.get("args", []))

    # Expand env vars in the command AND args (notably ${CLAUDE_PLUGIN_ROOT}
    # for plugins — since #52 the plugin command is
    # `${CLAUDE_PLUGIN_ROOT}/bin/weave-mcp-launch`). For a plugin entry we
    # substitute its own plugin root; otherwise fall back to the cwd so the
    # invocation *shape* is still validated when the plugin isn't installed.
    env_subs = {"CLAUDE_PLUGIN_ROOT": str(plugin_root or cwd)}
    cmd = _expand_env(raw_cmd, env_subs)
    expanded = [
        a if not isinstance(a, str) else _expand_env(a, env_subs) for a in args
    ]

    if "/" in cmd or os.sep in cmd:
        # Path-shaped command (e.g. the portable launcher). A relative path
        # is resolved against the launching scope's root — the project dir
        # for .mcp.json — mirroring Claude Code's spawn cwd, NOT against
        # wherever the doctor process happens to run.
        cmd_path = Path(cmd)
        if not cmd_path.is_absolute():
            cmd_path = (plugin_root or cwd) / cmd_path
        if not (cmd_path.exists() and os.access(cmd_path, os.X_OK)):
            return CheckResult(
                name="launcher resolves",
                passed=False,
                detail=(
                    f"command `{cmd}` (from {source}) resolves to "
                    f"`{cmd_path}` which does not exist or is not executable"
                ),
                fix=(
                    "re-run `weave install --yes` (machine scope) or "
                    "re-clone so bin/weave-mcp-launch exists and carries "
                    "the exec bit"
                ),
            )
        # NB: do NOT reject an extensionless command on Windows. It is tempting
        # — a raw CreateProcess cannot spawn a `#!/bin/sh` file, and
        # `os.access(X_OK)` says yes to any existing file there, so this looks
        # like a false green. It is not: Claude Code resolves an MCP `command`
        # through a shell (Git Bash, per CLAUDE_CODE_GIT_BASH_PATH), and
        # `claude mcp list` reports the committed `bin/weave-mcp-launch` as
        # Connected on native Windows. A gate here turns a working install into
        # a red doctor, which is strictly worse than the imagined false green.
        resolved = str(cmd_path)
    else:
        resolved = shutil.which(cmd) or cmd
        if shutil.which(cmd) is None:
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
        probe_argv = _launcher_probe_argv(resolved, expanded)
        proc = subprocess.run(
            probe_argv,
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
    except OSError as exc:
        return CheckResult(
            name="launcher resolves",
            passed=False,
            detail=f"could not exec `{resolved}` (from {source}): {exc}",
            fix=(
                "install Git for Windows when the launcher is a POSIX shell "
                "script, or re-run `weave install --yes`"
            ),
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


#: How long a `weave --help` probe may take. Generous because the plugin
#: route's shim is a bare `uv run`, which cold-syncs dependencies on first call.
_WEAVE_CLI_PROBE_TIMEOUT = 60


def _weave_cli_fix() -> str:
    """The one remedy that does not itself need ``weave`` on PATH."""
    return (
        f"run `uv run --project {_detect_project_root()} weave install "
        f"--yes --harness codex`"
    )


def check_weave_cli() -> CheckResult:
    """Verify that bare ``weave`` resolves and imports in this sandbox.

    Hard-failing only on Windows, where the installer has a real remedy for a
    missing ``weave`` (it writes the launcher and edits the user PATH in the
    registry). On POSIX it has neither — ``_advise_scripts_path`` deliberately
    declines to persist PATH there — so a failure is reported informationally
    rather than reddening the whole report over something the fix can't fix.
    """
    result = _probe_weave_cli()
    if result.passed or _is_windows():
        return result
    return replace(
        result,
        passed=True,
        detail=f"{result.detail} (informational on this OS)",
        fix="",
    )


def _probe_weave_cli() -> CheckResult:
    """Resolve and run bare ``weave``, ignoring how bad a failure is."""
    found = shutil.which("weave")
    if not found:
        return CheckResult(
            name="weave CLI",
            passed=False,
            detail="`weave` is not on PATH",
            fix=_weave_cli_fix(),
        )
    try:
        proc = subprocess.run(
            [found, "--help"],
            timeout=_WEAVE_CLI_PROBE_TIMEOUT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # Slow is not broken: the shim may be resolving dependencies.
        return CheckResult(
            name="weave CLI",
            passed=True,
            detail=(
                f"resolves at {found} but `--help` did not finish in "
                f"{_WEAVE_CLI_PROBE_TIMEOUT}s (may still be syncing dependencies)"
            ),
        )
    except OSError as exc:
        return CheckResult(
            name="weave CLI",
            passed=False,
            detail=f"`weave` resolves to {found} but cannot execute: {exc}",
            fix=_weave_cli_fix(),
        )
    if proc.returncode == 0:
        return CheckResult(
            name="weave CLI", passed=True, detail=f"resolves and imports at {found}"
        )
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    import_failure = any(
        marker in stderr.casefold() for marker in ("modulenotfounderror", "importerror")
    )
    detail = (
        f"`weave` resolves to {found} but cannot import under the current sandbox"
        if import_failure
        else (
            f"`weave` resolves to {found} but exits {proc.returncode}: {stderr[-200:] or '<empty>'}"
        )
    )
    return CheckResult(
        name="weave CLI",
        passed=False,
        detail=detail,
        fix=_weave_cli_fix(),
    )


#: Optional-extra modules the scheduled lanes import. The venv is synced to a
#: fixed extra set; a `uv sync --extra <one>` or a pre-#164 `bin/weave` call
#: silently uninstalls the rest, and the lanes then die invisibly in cron
#: (news pull 2026-08-11→22: `feedparser_missing`; embed/dream narrowly
#: escaped the same on 2026-08-22). Map: module → (extra, lanes it carries).
_EXTRA_MODULES: tuple[tuple[str, str, str], ...] = (
    ("mcp", "mcp", "MCP server"),
    ("numpy", "embeddings", "embed-warm, similarity search, drift"),
    ("openai", "embeddings", "embeddings, hubs batch, LLM wrapper"),
    ("feedparser", "news", "news / podcast / youtube rss_poll"),
    ("readability", "news", "news article extraction"),
    ("google.genai", "gemini", "podcast transcription"),
    ("youtube_transcript_api", "youtube", "youtube captions"),
)


def _extra_importable(mod: str) -> bool:
    """``find_spec`` on a dotted name RAISES ModuleNotFoundError when the
    parent package is absent (``google.genai`` without ``google``); it only
    returns None when the parent exists. It also *imports* the parent, whose
    ``__init__`` may raise anything, and raises ValueError for a sys.modules
    entry with ``__spec__ = None``. ``except Exception`` is deliberate: this
    check's one job is diagnosing a damaged venv, so any failure to resolve
    the module IS the finding — report it missing, never crash the doctor."""
    import importlib.util

    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def check_venv_extras() -> CheckResult:
    """Every optional extra the acquisition + dream crons import must be
    importable from *this* interpreter. Harness-independent."""
    missing = [
        (mod, extra, lanes)
        for mod, extra, lanes in _EXTRA_MODULES
        if not _extra_importable(mod)
    ]
    if not missing:
        return CheckResult(
            name="venv extras",
            passed=True,
            detail=f"all {len(_EXTRA_MODULES)} optional-extra modules importable",
        )
    listing = "; ".join(f"{m} [{e}] → {l}" for m, e, l in missing)
    return CheckResult(
        name="venv extras",
        passed=False,
        detail=f"missing: {listing}",
        fix=(
            f"`uv sync --project {_detect_project_root()} --extra all` — never "
            "`--extra <one>`: uv sync prunes every extra it is not told to keep"
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
    # Only harnesses that ignore repo-local hooks get this row, so a Claude
    # Code report is unchanged.
    if _profile().hooks_global_only:
        result.checks.append(check_hook_scope(cwd))
        result.checks.append(check_weave_cli())
    # Harness-independent: the dangling link is a fact about the checkout, not
    # about which harness is reading it.
    result.checks.append(check_command_symlinks(cwd))
    result.checks.append(check_vault_env())
    result.checks.append(check_venv_extras())
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
