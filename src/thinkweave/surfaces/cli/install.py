"""``weave install`` — machine-scope setup.

Verifies the thinkweave console scripts are reachable on PATH and
idempotently registers the thinkweave MCP server in the active harness's
own config (``~/.claude.json`` for Claude Code, ``$CODEX_HOME/config.toml``
for Codex — ``--harness`` picks). Additionally appends a small
sentinel-wrapped block to that harness's always-loaded instructions file
(``~/.claude/CLAUDE.md``, ``$CODEX_HOME/AGENTS.md``) — a persistent nudge so
the model reaches for ``weave_*`` tools instead of filesystem search even
in long sessions where the SessionStart context payload has been
compacted away. Pass ``--no-claude-md`` to skip that touch. Run once
per machine per harness after ``pip install`` / ``pipx install``.

Every path comes from the :class:`~thinkweave.core.harness.HarnessProfile`
and every read/write of the MCP config goes through
:mod:`thinkweave.core.mcp_config`, which knows that harness's file format.

Scope boundary: this command never touches a vault or a project's
settings file. ``weave init`` owns the vault; ``weave hooks install``
(invoked by ``/onboard``) owns project-side hook registration.

``weave install`` owns the MCP registration, the instructions block, and
(once #107 lands) hooks. It finds an existing entry by key rather than by any
marker of ours, so one written by a hand-edit or by ChatGPT desktop's
Settings→Import is adopted and converged rather than duplicated — see
README.md "Other harnesses" for that interplay.
"""

from __future__ import annotations

import argparse
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Any, NamedTuple

from thinkweave.core import fswrite, harness_docs, mcp_config
from thinkweave.core.harness import active as _profile

SERVER_NAME = "thinkweave"
REQUIRED_SCRIPTS = ("weave", "weave-hook", "weave-mcp")

#: The stdio MCP server, addressed as a module. Every launch surface uses this
#: rather than the ``weave-mcp`` console script — see ``_build_server_entry``.
MCP_MODULE = "thinkweave.surfaces.mcp.server"

# Every harness touchpoint this module reads or writes — the MCP config, the
# instructions file, the plugins root, the dev-link target, the pause marker —
# comes from the active profile. `_profile()` is the single read point, so a
# `--harness` flag (#106) has exactly one place to interpose; the accessors
# below just spell the individual fields at their call sites.


def _is_windows() -> bool:
    """The Windows gate, behind a function so tests can flip it.

    Patching ``os.name`` instead would flip it for the whole process —
    ``pathlib`` then hands out ``WindowsPath`` and every later ``Path(...)``
    on a POSIX host raises.
    """
    return os.name == "nt"


def _mcp_config() -> Path:
    """Where the harness reads its MCP server registrations from."""
    return _profile().mcp_config


def _instructions() -> Path:
    """The always-loaded user-global instructions file we splice a block into."""
    return _profile().instructions_file


def _marker() -> Path:
    """The `weave pause` marker."""
    return _profile().pause_marker


def _plugins_root() -> Path:
    return _profile().plugins_root


def _skills_dir() -> Path:
    return _profile().skills_dir


def _dev_link() -> Path:
    """The `weave dev-link` symlink target inside the harness's skills dir."""
    return _profile().dev_link


# The sentinels are harness-independent — an HTML comment is inert in every
# markdown instructions file — so only the body between them comes from the
# profile.
CLAUDE_MD_BLOCK_START = "<!-- thinkweave:start -->"
CLAUDE_MD_BLOCK_END = "<!-- thinkweave:end -->"


def _detect_uv_path() -> str:
    """Resolve uv on PATH. Returns the literal 'uv' if not found — the
    config still works as long as the user's shell can resolve it later."""
    return shutil.which("uv") or "uv"


def _detect_project_root() -> Path:
    """The thinkweave source tree this `weave` invocation came from.

    Resolved from the package install location, walking up to the dir
    that contains ``pyproject.toml``. Falls back to the cwd if the
    package was installed from a wheel rather than ``-e``.
    """
    pkg_init = Path(__file__).resolve()
    for parent in pkg_init.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def _build_server_entry(project_root: Path, vault_root: str | None) -> dict[str, Any]:
    """Construct the canonical thinkweave server block.

    Runs the server as a **module**, not via the ``weave-mcp`` console script.
    The script is an ``.exe`` shim on Windows, and a ``uv`` sync interrupted by a
    file lock deletes the shims before it fails — leaving a venv that imports
    perfectly but has no ``weave-mcp.exe``, at which point a console-script entry
    is permanently broken while ``python -m`` still works. Measured on
    2026-08-03: editing ``pyproject.toml`` with a session running did exactly
    that. Module execution depends only on the package being importable, which is
    what ``uv run`` already guarantees. The result is
    normalised to whatever the harness's config *format* stores — Codex's TOML
    carries no ``type`` key — so that a re-read compares equal and a repeat
    install is a genuine no-op.

    ``--no-sync`` is safe *here specifically* because ``cmd_install`` has
    already run :func:`_uv_sync` eagerly, so the environment this entry launches
    into is known-good at the moment it is written. Re-resolving at every
    session start buys nothing and costs two ways: latency on a cold cache, and
    on Windows a genuine hazard — ``uv run`` wants to rewrite
    ``.venv\\Scripts\\weave-mcp.exe``, which fails with a sharing violation
    while a previously-spawned server still holds that image open.

    The portable launchers deliberately do NOT pass it. On the plugin route
    nothing ever runs ``weave install``, so the launcher's implicit sync is that
    route's only dependency bootstrap; ``mcp_doctor._key`` normalises the flag
    away so the two shapes still fingerprint as one invocation.
    """
    args = [
        "run",
        "--no-sync",
        "--project",
        str(project_root),
        "--extra",
        "mcp",
        "python",
        "-m",
        MCP_MODULE,
    ]
    entry: dict[str, Any] = {
        "type": "stdio",
        "command": _detect_uv_path(),
        "args": args,
        "env": {},
    }
    if vault_root:
        entry["env"]["THINKWEAVE_VAULT"] = vault_root
    return mcp_config.canonical(_mcp_config(), entry)


class ScriptsCheck(NamedTuple):
    """Result of the console-script reachability probe (see #47)."""

    state: str
    """``ok`` — all scripts resolve on PATH. ``venv_off_path`` — the scripts
    exist in the running interpreter's Scripts/bin dir, but that dir is not
    on PATH (remediation is PATH, not pip). ``absent`` — the scripts aren't
    even in the venv (remediation is ``pip install``)."""

    missing: list[str]
    """The REQUIRED_SCRIPTS that did not resolve on PATH."""

    scripts_dir: Path
    """The venv scripts dir that was probed — named verbatim in the
    ``venv_off_path`` remediation message."""


def _venv_scripts_dir() -> Path:
    """The scripts directory of the running interpreter's environment
    (``.venv/bin`` on POSIX, ``.venv\\Scripts`` on Windows). ``weave
    install`` itself runs from that environment, so this is where the
    console scripts physically live when they exist at all."""
    return Path(sysconfig.get_path("scripts"))


def _check_scripts(
    scripts_dir: Path | None = None, path_env: str | None = None
) -> ScriptsCheck:
    """Classify console-script reachability.

    The historical failure mode (#47): the ``weave``/``weave-hook``/
    ``weave-mcp`` console scripts install only into the repo venv, and
    nothing puts that dir on PATH — so the MCP half (launched via an
    absolute ``uv run`` command) works while every bare ``weave …`` shell
    call fails. Distinguishing *present-but-off-PATH* from *genuinely
    missing* lets ``cmd_install`` give a PATH-specific remediation instead
    of useless ``pip install`` advice.

    ``scripts_dir`` / ``path_env`` exist for tests (controlled fake venv
    layout + controlled PATH); production callers pass neither.
    """
    if scripts_dir is None:
        scripts_dir = _venv_scripts_dir()
    missing = [
        s for s in REQUIRED_SCRIPTS if shutil.which(s, path=path_env) is None
    ]
    if not missing:
        return ScriptsCheck("ok", [], scripts_dir)
    in_venv = all(
        (scripts_dir / s).exists() or (scripts_dir / f"{s}.exe").exists()
        for s in REQUIRED_SCRIPTS
    )
    return ScriptsCheck("venv_off_path" if in_venv else "absent", missing, scripts_dir)


def _advise_scripts_path(check: ScriptsCheck, yes: bool) -> None:
    """PATH-specific remediation for the ``venv_off_path`` state.

    Same consent posture as the CLAUDE.md splice: preview the situation,
    require ``--yes`` to proceed. Persisting PATH itself is deliberately
    NOT attempted — shells and platforms disagree on where PATH lives
    (profile files, the Windows registry), so we name the exact dir and
    the exact line to add instead, and let ``--yes`` continue the install
    (which is otherwise fully functional: the MCP entry launches via uv,
    never via PATH)."""
    d = check.scripts_dir
    print(
        f"warning: the weave console scripts ({', '.join(check.missing)}) are\n"
        f"installed in this environment, but its scripts directory is not on PATH:\n"
        f"  {d}\n"
        f"The MCP server is unaffected (it launches via uv, not PATH), but bare\n"
        f"`weave ...` calls — your shell, cron entries, /wrap's finalize step —\n"
        f"won't resolve in a fresh shell until you add that directory to PATH:\n"
        f'  export PATH="{d}:$PATH"      # POSIX shells; add to your profile\n'
        f'  $env:Path = "{d};$env:Path"  # Windows PowerShell; persist via System Settings\n',
        file=sys.stderr,
    )
    if not yes:
        print(
            "Re-run with --yes to continue the install without the PATH fix.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(
        "Continuing (--yes) — remember to add the directory to PATH.",
        file=sys.stderr,
    )


def _check_uv_available() -> None:
    """Validate that ``uv`` is on PATH. The MCP server entry invokes
    ``uv run --project ... weave-mcp``, so a missing uv means the server
    silently fails to spawn at every Claude Code session start. Fail
    fast at install instead."""
    if shutil.which("uv") is None:
        print(
            "error: `uv` not found on PATH. thinkweave's MCP server uses uv\n"
            "to resolve its dependencies on demand. Install uv first:\n"
            "  https://docs.astral.sh/uv/getting-started/installation/\n"
            "then re-run `weave install`.",
            file=sys.stderr,
        )
        sys.exit(1)


def _check_pyproject_reachable(project_root: Path) -> None:
    """Refuse to write a broken MCP entry. The MCP command is
    ``uv run --project <project_root> weave-mcp`` — if there's no
    ``pyproject.toml`` at that root, uv has nothing to resolve from. This
    happens on ``pipx install`` (package files live in pipx's isolated
    ``site-packages/`` with no upstream ``pyproject.toml``);
    ``_detect_project_root`` falls back to cwd and the install silently
    bakes the user's terminal directory into the MCP entry."""
    if not (project_root / "pyproject.toml").exists():
        print(
            f"error: could not find thinkweave's pyproject.toml.\n"
            f"  detected project_root={project_root}\n"
            f"This usually means thinkweave was installed via `pipx`, which\n"
            f"is not supported (the MCP entry needs a resolvable source tree).\n"
            f"Use the plugin route, or `pip install -e .[all]` from a git clone.",
            file=sys.stderr,
        )
        sys.exit(1)


def _uv_sync(project_root: Path) -> None:
    """Run ``uv sync --extra all`` eagerly so the first Claude Code session
    after install doesn't pay 30s–2min of dependency resolution. Streams
    uv's output so users see progress; a non-zero exit aborts the
    install with a clear pointer to manual retry.

    ``--extra all``, never a single extra: ``uv sync`` prunes every extra
    it is not told to keep, so ``--extra mcp`` here would uninstall the
    news/embeddings/gemini/youtube deps from an already-synced venv —
    the same silent pruning the ``--no-sync`` launchers exist to prevent
    (killed the news pull 2026-08-11→22; ``weave doctor --mcp`` "venv
    extras" catches the aftermath)."""
    print()
    print(f"Syncing thinkweave dependencies at {project_root} …")
    print("(one-time, ~30s–2min depending on cache)")
    try:
        result = subprocess.run(
            ["uv", "sync", "--project", str(project_root), "--extra", "all"],
            check=False,
        )
    except FileNotFoundError:
        # _check_uv_available already validated; defend anyway
        print("error: `uv` not found at sync time.", file=sys.stderr)
        sys.exit(1)
    if result.returncode != 0:
        print(
            f"\nerror: `uv sync` exited {result.returncode}. The MCP server\n"
            f"likely won't start. Retry manually after fixing the error:\n"
            f"  uv sync --project {project_root} --extra all",
            file=sys.stderr,
        )
        sys.exit(1)
    print("Dependencies synced.")


def _venv_purelib() -> Path:
    """Site-packages belonging to the environment running the installer."""
    return Path(sysconfig.get_path("purelib"))


def _base_python() -> Path:
    """The interpreter the launcher runs. ``sys._base_executable`` names the
    real interpreter behind a venv (what we want, since the launcher supplies
    its own ``PYTHONPATH`` rather than going through the venv), but it is
    private and absent on some builds — fall back to the running one."""
    return Path(getattr(sys, "_base_executable", None) or sys.executable)


def _render_codex_windows_cli_launcher(
    *, project_root: Path, base_python: Path, purelib: Path
) -> str:
    """Render a CLI launcher that does not process the editable ``.pth``."""
    pythonpath = f"{project_root / 'src'};{purelib}"
    return (
        "@echo off\n"
        "setlocal EnableExtensions\n"
        'set "PYTHONDONTWRITEBYTECODE=1"\n'
        # An `if defined` guard rather than a bare `;%PYTHONPATH%` — an unset
        # variable expands to nothing in a batch file, and the trailing `;`
        # that leaves behind is an empty entry, which Python reads as the cwd.
        f'if defined PYTHONPATH (set "PYTHONPATH={pythonpath};%PYTHONPATH%")'
        f' else (set "PYTHONPATH={pythonpath}")\n'
        f'"{base_python}" -S -m thinkweave %*\n'
        "exit /b %ERRORLEVEL%\n"
    )


def _normalize_path_entry(entry: str) -> str:
    """Comparable form of one Windows PATH entry: ``%VARS%`` expanded, case
    folded, separators unified, trailing separator dropped — so ``C:\\x\\bin\\``
    and ``c:/x/bin`` are recognised as the same directory. Windows rules come
    from ``ntpath`` explicitly, which keeps the comparison testable off
    Windows."""
    return ntpath.normcase(ntpath.expandvars(entry)).rstrip("\\")


def _prepend_path_value(current: str, wanted: str) -> str:
    """``current`` with ``wanted`` in front, unchanged if it is already there."""
    parts = [part for part in current.split(";") if part]
    target = _normalize_path_entry(wanted)
    if any(_normalize_path_entry(part) == target for part in parts):
        return current
    return ";".join([wanted, *parts])


def _remove_path_value(current: str, unwanted: str) -> str:
    """``current`` with every entry naming ``unwanted`` dropped."""
    target = _normalize_path_entry(unwanted)
    return ";".join(
        part
        for part in current.split(";")
        if part and _normalize_path_entry(part) != target
    )


def _broadcast_environment_change() -> None:
    """Tell running processes to re-read the environment.

    A registry write alone reaches only processes started after the next
    sign-in: Explorer caches the environment it hands to everything it
    launches, a restarted Codex included.
    """
    import ctypes
    from ctypes import wintypes

    try:
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF,  # HWND_BROADCAST
            0x1A,  # WM_SETTINGCHANGE
            0,
            ctypes.c_wchar_p("Environment"),
            0x2,  # SMTO_ABORTIFHUNG
            5000,
            ctypes.byref(wintypes.DWORD()),
        )
    except (AttributeError, OSError) as exc:
        print(
            f"note: could not broadcast the PATH change ({exc}). Already-running\n"
            f"programs keep the old PATH — sign out of Windows and back in once.",
            file=sys.stderr,
        )


def _edit_windows_user_path(directory: Path, *, remove: bool) -> bool:
    """Add or drop ``directory`` in the current process's PATH and in the
    persisted user PATH (``HKCU\\Environment``). Returns whether the persisted
    value changed."""
    import winreg

    wanted = str(directory)
    edit = _remove_path_value if remove else _prepend_path_value
    os.environ["PATH"] = edit(os.environ.get("PATH", ""), wanted)
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        "Environment",
        0,
        winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
    ) as key:
        try:
            current, value_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, value_type = "", winreg.REG_EXPAND_SZ
        updated = edit(current, wanted)
        if updated == current:
            return False
        winreg.SetValueEx(key, "Path", 0, value_type, updated)
    _broadcast_environment_change()
    return True


def _prepend_windows_user_path(directory: Path) -> None:
    """Prepend ``directory`` to the current and future Windows user PATH."""
    _edit_windows_user_path(directory, remove=False)


def _remove_windows_user_path(directory: Path) -> bool:
    """Drop ``directory`` from the current and future Windows user PATH."""
    return _edit_windows_user_path(directory, remove=True)


def _codex_windows_launcher() -> Path:
    """Where the sandbox-safe bare ``weave`` command is installed."""
    return _profile().mcp_config.parent / "bin" / "weave.cmd"


def _install_codex_windows_cli(project_root: Path) -> Path | None:
    """Install Codex's sandbox-safe bare ``weave`` command on Windows."""
    if not _is_windows() or not _profile().windows_cli_shim:
        return None
    target = _codex_windows_launcher()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _render_codex_windows_cli_launcher(
            project_root=project_root,
            base_python=_base_python(),
            purelib=_venv_purelib(),
        ),
        encoding="utf-8",
        newline="\r\n",
    )
    _prepend_windows_user_path(target.parent)
    print(f"Installed sandbox-safe Codex CLI launcher at {target}.")
    return target


def _plugin_provides_mcp() -> Path | None:
    """Return the path to a plugin manifest that already declares the
    thinkweave MCP server, or None if no installed plugin claims it.

    Plugin-route users have the MCP entry sourced from
    ``~/.claude/plugins/<name>/.claude-plugin/plugin.json`` (the plugin
    manager owns ``~/.claude.json`` for plugin-managed servers). When this
    helper returns a path, ``cmd_install`` skips its ``~/.claude.json``
    write — duplicate registrations would cause Claude Code to try to
    spawn the server twice — and only adds the CLAUDE.md nudge.

    Corrupt or unreadable manifests are silently skipped rather than
    aborting the install; a broken plugin shouldn't block ``weave install``.
    """
    if not _plugins_root().exists():
        return None
    manifest_rel = _profile().plugin_manifest_relpath.as_posix()
    for manifest in _plugins_root().glob(f"*/{manifest_rel}"):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if SERVER_NAME in data.get("mcpServers", {}):
            return manifest
    return None


def _diff_lines(old: dict, new: dict) -> list[str]:
    """Render a minimal human-readable diff between two MCP-server blocks.

    Display only, so `default=str` is the right answer for a TOML-native value
    (a date) that JSON cannot encode.
    """
    old_str = json.dumps(old, indent=2, sort_keys=True, default=str).splitlines()
    new_str = json.dumps(new, indent=2, sort_keys=True, default=str).splitlines()
    out: list[str] = []
    for line in old_str:
        if line not in new_str:
            out.append(f"  - {line}")
    for line in new_str:
        if line not in old_str:
            out.append(f"  + {line}")
    return out


def _render_claude_md_block() -> str:
    """The exact bytes we want between the sentinels (no trailing newline)."""
    body = _profile().instructions_block_body
    return f"{CLAUDE_MD_BLOCK_START}\n{body}\n{CLAUDE_MD_BLOCK_END}"


def _extract_claude_md_block(text: str) -> str | None:
    """Return the existing sentinel-wrapped block, or None if absent/corrupt."""
    start = text.find(CLAUDE_MD_BLOCK_START)
    if start == -1:
        return None
    end = text.find(CLAUDE_MD_BLOCK_END, start)
    if end == -1:
        return None
    return text[start : end + len(CLAUDE_MD_BLOCK_END)]


def _splice_claude_md_block(text: str, new_block: str) -> str:
    """Replace an existing block in place, or append a new one (absent or
    single-sentinel-corrupt file). Never edits bytes outside the sentinels —
    hand-edits adjacent to the block survive."""
    return fswrite.replace_between(
        text,
        CLAUDE_MD_BLOCK_START,
        CLAUDE_MD_BLOCK_END,
        new_block,
        on_missing="append",
    )


def _install_claude_md_block(yes: bool) -> None:
    """Idempotently splice the thinkweave block into ``~/.claude/CLAUDE.md``.

    Mirrors the consent posture of the ``~/.claude.json`` write: announce
    what would change, require ``--yes`` to actually write. Skip path is
    ``--no-claude-md`` on the parser (see ``cmd_install``).
    """
    new_block = _render_claude_md_block()

    if not _instructions().exists():
        print()
        print(f"{_instructions()} does not exist. `weave install` will create it")
        print("with a small thinkweave block (loaded into every session):")
        print()
        for line in new_block.splitlines():
            print(f"  {line}")
        if not yes:
            print()
            print("Re-run with --yes to write, or --no-claude-md to skip.")
            sys.exit(1)
        _instructions().parent.mkdir(parents=True, exist_ok=True)
        _instructions().write_text(new_block + "\n", encoding="utf-8")
        print()
        print(f"Wrote {_instructions()} with thinkweave block.")
        return

    text = _instructions().read_text(encoding="utf-8")
    existing = _extract_claude_md_block(text)
    if existing == new_block:
        print(f"thinkweave block already present in {_instructions()} (no change).")
        return

    action = "Update" if existing is not None else "Append to"
    print()
    print(f"{action} {_instructions()} (user-global, loaded into every session) with:")
    print()
    for line in new_block.splitlines():
        print(f"  {line}")
    if not yes:
        print()
        print("Re-run with --yes to apply, or --no-claude-md to skip.")
        sys.exit(1)

    fswrite.atomic_write_text(_instructions(), _splice_claude_md_block(text, new_block))
    print()
    verb = "Updated" if existing is not None else "Appended"
    print(f"{verb} thinkweave block in {_instructions()}.")


def _servers_key() -> str:
    """The active harness's declared MCP servers key. Passed to every
    ``mcp_config`` call in this module: the suffix-derived default agrees for
    most harnesses, but OpenCode is a JSON file whose key is ``mcp`` — writing
    the default there registers a server the harness never reads (r1)."""
    return _profile().mcp_servers_key


def _remove_mcp_entry() -> bool:
    """Remove the thinkweave MCP entry from the harness's config. Other
    servers and top-level keys survive. Returns False if nothing to do."""
    return mcp_config.remove_entry(
        _mcp_config(), SERVER_NAME, servers_key=_servers_key()
    )


def _restore_mcp_entry() -> None:
    """Re-register the thinkweave MCP entry. Used by ``weave resume``."""
    entry = _build_server_entry(_detect_project_root(), vault_root=None)
    mcp_config.write_entry(
        _mcp_config(), SERVER_NAME, entry, servers_key=_servers_key()
    )


def _remove_claude_md_block() -> bool:
    """Strip the sentinel-wrapped block (plus surrounding blank lines)
    from ``~/.claude/CLAUDE.md``. Returns False if no block present."""
    if not _instructions().exists():
        return False
    text = _instructions().read_text(encoding="utf-8")
    pattern = re.compile(
        r"\n*" + re.escape(CLAUDE_MD_BLOCK_START)
        + r".*?" + re.escape(CLAUDE_MD_BLOCK_END) + r"\n*",
        re.DOTALL,
    )
    new_text, n = pattern.subn("\n", text, count=1)
    if n == 0:
        return False
    _instructions().write_text(new_text.lstrip("\n"), encoding="utf-8")
    return True


def _write_mcp_entry(args: argparse.Namespace, new_entry: dict) -> None:
    """Ensure the thinkweave MCP entry exists in the harness's MCP config.

    Four states: file missing, entry missing, entry matches, entry differs.
    The first and last require ``--yes`` (creating a new file or overwriting
    a divergent entry); the middle two are idempotent / write-through. Exits
    the process when consent is needed but not granted.

    Detection is purely by key, so an entry written by someone else — a
    hand-edit, or ChatGPT desktop's Settings→Import — lands in the "differs"
    branch and is adopted, never duplicated (#106).
    """
    path = _mcp_config()

    # Degrade out loud (dec-5a076384, r2): on a row whose MCP registration is
    # itself a documented degradation — the entry body is Claude Code's shape,
    # unverified against this harness's schema — the success line carries the
    # profile's provenance instead of an unqualified "Registered". Measured
    # rows (CC, Codex) keep their exact pre-#191 lines.
    qualifier = ""
    if any(
        "mcp registration" in d.capability.lower()
        for d in _profile().degradations
    ):
        qualifier = f" [{_profile().evidence} — see the degradations listed below]"

    if not path.exists():
        if not args.yes:
            print(f"{path} does not exist. `weave install` will create it.")
            print("Re-run with --yes to proceed.")
            sys.exit(1)
        mcp_config.write_entry(path, SERVER_NAME, new_entry, servers_key=_servers_key())
        print(f"Wrote {path} with thinkweave MCP entry.{qualifier}")
        return

    # A MalformedConfig from here (or from either write below) carries its own
    # remedy and is turned into a clean exit by the CLI's error boundary.
    existing = mcp_config.read_entry(path, SERVER_NAME, servers_key=_servers_key())

    if existing is None:
        mcp_config.write_entry(path, SERVER_NAME, new_entry, servers_key=_servers_key())
        print(f"Registered thinkweave MCP server in {path}.{qualifier}")
        return

    if existing == new_entry:
        print("thinkweave MCP server already registered (no change).")
        return

    print(f"thinkweave MCP server already in {path} but differs:")
    for line in _diff_lines(existing, new_entry):
        print(line)
    print()
    if not args.yes:
        print(f"Re-run with --yes to overwrite, or edit {path} by hand.")
        sys.exit(1)
    mcp_config.write_entry(path, SERVER_NAME, new_entry, servers_key=_servers_key())
    print(f"Updated thinkweave MCP entry in {path}.{qualifier}")


def cmd_install(args: argparse.Namespace) -> None:
    check = _check_scripts()
    if check.state == "absent":
        print(
            f"error: required console scripts missing from PATH: {', '.join(check.missing)}",
            file=sys.stderr,
        )
        print(
            "Install thinkweave first (e.g. `pip install -e .[all]` from a "
            "clone), then re-run `weave install`.",
            file=sys.stderr,
        )
        sys.exit(1)
    if check.state == "venv_off_path":
        _advise_scripts_path(check, yes=args.yes)

    _check_uv_available()

    plugin_manifest = _plugin_provides_mcp()
    if plugin_manifest is not None:
        print(
            f"thinkweave MCP entry is provided by plugin manifest:\n"
            f"  {plugin_manifest}\n"
            f"Skipping {_mcp_config()} write (plugin manager owns that registration)."
        )
        if args.vault:
            print(
                "Note: --vault is a no-op on the plugin route — set "
                "THINKWEAVE_VAULT in your shell env instead."
            )
        # Eager `uv sync` is skipped on the plugin route — the plugin's
        # source path (`${CLAUDE_PLUGIN_ROOT}`) is resolved by the plugin
        # runtime, not by `weave`. NOTE: nothing syncs lazily either — all
        # launchers run `uv run --no-sync` (#156/#164), so a fresh plugin
        # checkout needs a manual `uv sync --extra all` before the MCP
        # server can start. First-sync ownership is tracked in #198.
        if not getattr(args, "no_claude_md", False):
            _install_claude_md_block(args.yes)
        _print_next_steps()
        return

    project_root = _detect_project_root()
    _check_pyproject_reachable(project_root)
    new_entry = _build_server_entry(project_root, vault_root=args.vault)

    _write_mcp_entry(args, new_entry)
    _uv_sync(project_root)
    _install_codex_windows_cli(project_root)
    if not getattr(args, "no_claude_md", False):
        _install_claude_md_block(args.yes)
    _print_next_steps()


def cmd_uninstall(args: argparse.Namespace) -> None:
    """Reverse `weave install` — remove the MCP entry, the CLAUDE.md block,
    and any leftover pause marker. Vault, hooks, plugin manifest, and cron
    jobs are out of scope (they have their own owners)."""
    md_block_present = (
        _instructions().exists()
        and CLAUDE_MD_BLOCK_START in _instructions().read_text(encoding="utf-8")
    )
    mcp_present = _raw_mcp_entry_present()
    launcher = _codex_windows_launcher() if _profile().windows_cli_shim else None

    to_remove: list[str] = []
    if mcp_present:
        to_remove.append(f"thinkweave MCP entry in {_mcp_config()}")
    if md_block_present:
        to_remove.append(f"thinkweave block in {_instructions()}")
    if _marker().exists():
        to_remove.append(f"pause marker {_marker()}")
    if launcher is not None and launcher.exists():
        to_remove.append(f"Codex CLI launcher {launcher} (and its user PATH entry)")

    if not to_remove:
        print("Nothing to remove — `weave install` has not touched this machine.")
        return

    print("`weave uninstall` will remove:")
    for item in to_remove:
        print(f"  - {item}")
    print()
    print("Untouched: vault, hooks (run `weave hooks uninstall --scope user` separately),")
    print("           plugin manifest (use your plugin manager), cron jobs.")

    if not args.yes:
        print()
        print("Re-run with --yes to proceed.")
        sys.exit(1)

    if _remove_mcp_entry():
        print(f"Removed MCP entry from {_mcp_config()}.")
    if _remove_claude_md_block():
        print(f"Removed thinkweave block from {_instructions()}.")
    if _marker().exists():
        _marker().unlink()
        print(f"Removed pause marker {_marker()}.")
    if launcher is not None and launcher.exists():
        launcher.unlink()
        print(f"Removed Codex CLI launcher {launcher}.")
        if _is_windows() and _remove_windows_user_path(launcher.parent):
            print(f"Removed {launcher.parent} from the user PATH.")
    print()
    print(f"Done. Restart {_profile().cli_bin} so the MCP server is no longer launched.")


def _print_next_steps() -> None:
    """Post-install instructions the *active* harness can actually follow.

    This used to end with "3. /onboard" unconditionally. On Codex that is a dead
    end: the repository's minimal Codex skill bundle is not exported by the
    installer and still has no onboard skill. Everything ``/onboard``
    orchestrates is reachable from the CLI, so the fallback lists those steps
    rather than naming something unrunnable.
    """
    profile = _profile()
    cli = profile.cli_bin
    print()
    print("Next:")
    print(f"  1. Restart {cli}            # MCP server only spawns on session start")
    print(f"  2. cd <repo> && {cli}       # open a project")
    if profile.ships_skills:
        print("  3. /onboard                 # vault wiring, hooks, CC backfill, ontology, sources, smoke test")
        print()
        print("Tip: pass `--vault PATH` to `weave install` to bake the vault path into the")
        print("MCP server entry now; otherwise `/onboard` will ask and persist it.")
        return

    # No skills on this harness — spell out the same steps as CLI commands,
    # without naming the skill: a user reading this cannot run it, so mentioning
    # it only invites them to try. The SAME rule gates each step on the
    # profile: `weave hooks install` exits 1 on a hooks-less harness and
    # `weave import <id>` needs an importer, so naming either to an E0 user
    # is the rot this screen was rewritten to remove (r1).
    name = profile.display_name or profile.id
    step = 3
    print(f"  {step}. weave init               # vault wiring ({name} has no thinkweave skills)")
    if profile.hooks:
        step += 1
        print(f"  {step}. weave hooks install --scope user {profile.harness_flag}".rstrip())
        if profile.hooks_install_caveat:
            print("     (then trust the hooks — see the note printed by that command)")
    if profile.load_transcript_importer() is not None:
        step += 1
        print(f"  {step}. weave import {profile.id} --enrich   # backfill prior sessions")
    # Degrade OUT LOUD at the point of action (dec-5a076384): what this
    # harness does not deliver is stated here, not discovered later.
    degraded = harness_docs.render_degradations(profile)
    if degraded:
        print()
        print(f"{name} degradations — documented, not silently faked:")
        print(degraded)
    print()
    print("Tip: pass `--vault PATH` to `weave install` to bake the vault path into the")
    print("MCP server entry now; `weave init` will otherwise ask and persist it.")


def _raw_mcp_entry_present() -> bool:
    """True if the harness's own MCP config carries a thinkweave entry (the
    ``weave install`` escape hatch, or a hand-edit). dev-link uses this to
    warn: the plugin manifest already declares the server, so a leftover raw
    entry would make the harness spawn ``thinkweave`` twice."""
    try:
        return (
            mcp_config.read_entry(
                _mcp_config(), SERVER_NAME, servers_key=_servers_key()
            )
            is not None
        )
    except mcp_config.MalformedConfig:
        return False


def _print_dev_link_next_steps() -> None:
    print()
    print("Restart Claude Code, then everything is namespaced under the plugin:")
    print("  /thinkweave:onboard                         # first run")
    print("  /thinkweave:wrap, :tighten, :dream …  # the rest")
    print()
    print("The MCP server + hooks load from the plugin manifest — no `weave install`")
    print("needed. After editing hooks/agents/mcpServers, run `/reload-plugins`.")
    print("Undo with `weave dev-unlink`.")


def cmd_dev_link(args: argparse.Namespace) -> None:
    """Dev/clone setup — symlink this checkout into ``~/.claude/skills/`` so
    Claude Code auto-loads it as a plugin every session (flagless, namespaced
    ``/thinkweave:*``, live edits against the working tree).

    The clone counterpart to the marketplace plugin install. It writes no
    ``~/.claude.json`` MCP entry: the plugin manifest at the checkout root
    declares the server, so the plugin runtime owns registration. Refuses to
    shadow a real marketplace plugin install of the same name, and warns when
    a leftover raw ``~/.claude.json`` entry would double-register the server.
    """
    repo = _detect_project_root()
    manifest_rel = _profile().plugin_manifest_relpath
    if not (repo / manifest_rel).exists():
        print(
            f"error: no {manifest_rel.as_posix()} under {repo}.\n"
            "Run `weave dev-link` from a thinkweave checkout.",
            file=sys.stderr,
        )
        sys.exit(1)

    claimed_by = _plugin_provides_mcp()
    if claimed_by is not None:
        print(
            "note: a marketplace plugin already provides thinkweave\n"
            f"  ({claimed_by}).\n"
            "You don't need dev-link. Uninstall that plugin first if you want\n"
            "to develop against this checkout instead.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Guard: a leftover raw `weave install` entry double-registers the server
    # (the skills-dir plugin also declares it). Warn loudly; non-fatal because
    # the user may have wired the raw entry into another host on purpose.
    if _raw_mcp_entry_present():
        print(
            f"warning: a raw thinkweave MCP entry is still in {_mcp_config()}.\n"
            "  Combined with the dev-linked plugin, Claude Code registers the\n"
            "  `thinkweave` server twice. Run `weave uninstall` to drop the raw\n"
            "  entry (the plugin manifest provides the server).",
            file=sys.stderr,
        )

    _skills_dir().mkdir(parents=True, exist_ok=True)

    if _dev_link().is_symlink():
        if _dev_link().resolve() == repo.resolve():
            print(f"Already dev-linked: {_dev_link()} → {repo}")
            _print_dev_link_next_steps()
            return
        if not getattr(args, "force", False):
            print(
                f"error: {_dev_link()} already points at {_dev_link().resolve()}.\n"
                f"Re-run with --force to repoint it at {repo}.",
                file=sys.stderr,
            )
            sys.exit(1)
        _dev_link().unlink()
    elif _dev_link().exists():
        print(
            f"error: {_dev_link()} exists and is not a symlink (a real plugin\n"
            "dir?). Remove it by hand if you mean to dev-link this checkout.",
            file=sys.stderr,
        )
        sys.exit(1)

    _dev_link().symlink_to(repo)
    print(f"Dev-linked {_dev_link()} → {repo}")
    _print_dev_link_next_steps()


def cmd_dev_unlink(args: argparse.Namespace) -> None:
    """Reverse ``weave dev-link`` — remove the ``~/.claude/skills/thinkweave``
    symlink. A real (non-symlink) install at that path is left untouched."""
    if _dev_link().is_symlink():
        target = _dev_link().resolve()
        _dev_link().unlink()
        print(f"Removed dev-link {_dev_link()} (was → {target}).")
        print("Restart Claude Code to drop the /thinkweave:* commands.")
        return
    if _dev_link().exists():
        print(
            f"note: {_dev_link()} is not a symlink (real install?) — left untouched.",
            file=sys.stderr,
        )
        return
    print(f"No dev-link at {_dev_link()} (nothing to remove).")
