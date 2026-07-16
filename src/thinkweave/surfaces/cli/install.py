"""``weave install`` — machine-scope setup.

Verifies the thinkweave console scripts are reachable on PATH and
idempotently registers the thinkweave MCP server entry in
``~/.claude.json`` so Claude Code can launch it. Additionally appends a
small sentinel-wrapped block to ``~/.claude/CLAUDE.md`` (the user-global
instructions file Claude Code loads every turn) — a persistent nudge so
the model reaches for ``weave_*`` tools instead of filesystem search even
in long sessions where the SessionStart context payload has been
compacted away. Pass ``--no-claude-md`` to skip that touch. Run once
per machine after ``pip install`` / ``pipx install``.

Scope boundary: this command never touches a vault or a project's
``.claude/settings.json``. ``weave init`` owns the vault; ``weave hooks
install`` (invoked by ``/onboard``) owns project-side hook registration.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Any, NamedTuple

CLAUDE_JSON = Path.home() / ".claude.json"
CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"
MARKER = Path.home() / ".claude" / "thinkweave_paused.json"
PLUGINS_ROOT = Path.home() / ".claude" / "plugins"
SKILLS_DIR = Path.home() / ".claude" / "skills"
SERVER_NAME = "thinkweave"
DEV_LINK = SKILLS_DIR / SERVER_NAME  # ~/.claude/skills/thinkweave (weave dev-link target)
REQUIRED_SCRIPTS = ("weave", "weave-hook", "weave-mcp")

CLAUDE_MD_BLOCK_START = "<!-- thinkweave:start -->"
CLAUDE_MD_BLOCK_END = "<!-- thinkweave:end -->"
CLAUDE_MD_BLOCK_BODY = (
    "If `weave_*` MCP tools are available, thinkweave (Obsidian-native memory "
    "layer) is your durable memory for this session. Prefer `weave_search` / "
    "`weave_context` / `weave_graph` over filesystem search, and run `/wrap` "
    "before `/clear`."
)


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
    """Construct the canonical ``mcpServers.thinkweave`` block.

    Uses the ``weave-mcp`` console script (stable invocation, layout-
    independent — see ARCHITECTURE.md §Invocation surface).
    """
    args = ["run", "--project", str(project_root), "--extra", "mcp", "weave-mcp"]
    entry: dict[str, Any] = {
        "type": "stdio",
        "command": _detect_uv_path(),
        "args": args,
        "env": {},
    }
    if vault_root:
        entry["env"]["THINKWEAVE_VAULT"] = vault_root
    return entry


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
    """Run ``uv sync --extra mcp`` eagerly so the first Claude Code session
    after install doesn't pay 30s–2min of dependency resolution. Streams
    uv's output so users see progress; a non-zero exit aborts the
    install with a clear pointer to manual retry."""
    print()
    print(f"Syncing thinkweave dependencies at {project_root} …")
    print("(one-time, ~30s–2min depending on cache)")
    try:
        result = subprocess.run(
            ["uv", "sync", "--project", str(project_root), "--extra", "mcp"],
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
            f"  uv sync --project {project_root} --extra mcp",
            file=sys.stderr,
        )
        sys.exit(1)
    print("Dependencies synced.")


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
    if not PLUGINS_ROOT.exists():
        return None
    for manifest in PLUGINS_ROOT.glob("*/.claude-plugin/plugin.json"):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if SERVER_NAME in data.get("mcpServers", {}):
            return manifest
    return None


def _entries_equal(a: dict, b: dict) -> bool:
    """Compare two MCP-server blocks ignoring key order."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _diff_lines(old: dict, new: dict) -> list[str]:
    """Render a minimal human-readable diff between two MCP-server blocks."""
    old_str = json.dumps(old, indent=2, sort_keys=True).splitlines()
    new_str = json.dumps(new, indent=2, sort_keys=True).splitlines()
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
    return f"{CLAUDE_MD_BLOCK_START}\n{CLAUDE_MD_BLOCK_BODY}\n{CLAUDE_MD_BLOCK_END}"


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
    """Replace an existing block in place, or append a new one. Never edits
    bytes outside the sentinels — hand-edits adjacent to the block survive."""
    start = text.find(CLAUDE_MD_BLOCK_START)
    end = text.find(CLAUDE_MD_BLOCK_END, max(start, 0))
    if start == -1 or end == -1:
        # absent or only one sentinel (corrupt) — append a fresh block
        sep = "" if text == "" or text.endswith("\n") else "\n"
        return f"{text}{sep}\n{new_block}\n"
    return text[:start] + new_block + text[end + len(CLAUDE_MD_BLOCK_END) :]


def _install_claude_md_block(yes: bool) -> None:
    """Idempotently splice the thinkweave block into ``~/.claude/CLAUDE.md``.

    Mirrors the consent posture of the ``~/.claude.json`` write: announce
    what would change, require ``--yes`` to actually write. Skip path is
    ``--no-claude-md`` on the parser (see ``cmd_install``).
    """
    import os

    new_block = _render_claude_md_block()

    if not CLAUDE_MD.exists():
        print()
        print(f"~/.claude/CLAUDE.md does not exist. `weave install` will create it")
        print("with a small thinkweave block (loaded into every Claude Code session):")
        print()
        for line in new_block.splitlines():
            print(f"  {line}")
        if not yes:
            print()
            print("Re-run with --yes to write, or --no-claude-md to skip.")
            sys.exit(1)
        CLAUDE_MD.parent.mkdir(parents=True, exist_ok=True)
        CLAUDE_MD.write_text(new_block + "\n", encoding="utf-8")
        print()
        print(f"Wrote {CLAUDE_MD} with thinkweave block.")
        return

    text = CLAUDE_MD.read_text(encoding="utf-8")
    existing = _extract_claude_md_block(text)
    if existing == new_block:
        print(f"thinkweave block already present in {CLAUDE_MD} (no change).")
        return

    action = "Update" if existing is not None else "Append to"
    print()
    print(f"{action} {CLAUDE_MD} (user-global, loaded into every Claude Code session) with:")
    print()
    for line in new_block.splitlines():
        print(f"  {line}")
    if not yes:
        print()
        print("Re-run with --yes to apply, or --no-claude-md to skip.")
        sys.exit(1)

    new_text = _splice_claude_md_block(text, new_block)
    tmp = CLAUDE_MD.with_suffix(CLAUDE_MD.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, CLAUDE_MD)
    print()
    verb = "Updated" if existing is not None else "Appended"
    print(f"{verb} thinkweave block in {CLAUDE_MD}.")


def _remove_mcp_entry() -> bool:
    """Remove the thinkweave MCP entry from ``~/.claude.json``. Other
    servers and top-level keys survive. Returns False if nothing to do."""
    if not CLAUDE_JSON.exists():
        return False
    cfg = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    servers = cfg.get("mcpServers", {})
    if SERVER_NAME not in servers:
        return False
    servers.pop(SERVER_NAME)
    _atomic_write_json(CLAUDE_JSON, cfg)
    return True


def _restore_mcp_entry() -> None:
    """Re-register the thinkweave MCP entry. Used by ``weave resume``."""
    entry = _build_server_entry(_detect_project_root(), vault_root=None)
    cfg: dict = {}
    if CLAUDE_JSON.exists():
        cfg = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    cfg.setdefault("mcpServers", {})[SERVER_NAME] = entry
    _atomic_write_json(CLAUDE_JSON, cfg)


def _remove_claude_md_block() -> bool:
    """Strip the sentinel-wrapped block (plus surrounding blank lines)
    from ``~/.claude/CLAUDE.md``. Returns False if no block present."""
    if not CLAUDE_MD.exists():
        return False
    text = CLAUDE_MD.read_text(encoding="utf-8")
    pattern = re.compile(
        r"\n*" + re.escape(CLAUDE_MD_BLOCK_START)
        + r".*?" + re.escape(CLAUDE_MD_BLOCK_END) + r"\n*",
        re.DOTALL,
    )
    new_text, n = pattern.subn("\n", text, count=1)
    if n == 0:
        return False
    CLAUDE_MD.write_text(new_text.lstrip("\n"), encoding="utf-8")
    return True


def _write_mcp_entry(args: argparse.Namespace, new_entry: dict) -> None:
    """Ensure the thinkweave MCP entry exists in ``~/.claude.json``.

    Four states: file missing, entry missing, entry matches, entry differs.
    The first and last require ``--yes`` (creating a new file or overwriting
    a divergent entry); the middle two are idempotent / write-through. Exits
    the process when consent is needed but not granted.
    """
    if not CLAUDE_JSON.exists():
        if not args.yes:
            print("~/.claude.json does not exist. `weave install` will create it.")
            print("Re-run with --yes to proceed.")
            sys.exit(1)
        cfg: dict[str, Any] = {"mcpServers": {SERVER_NAME: new_entry}}
        CLAUDE_JSON.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {CLAUDE_JSON} with thinkweave MCP entry.")
        return

    try:
        cfg = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"error: {CLAUDE_JSON} is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    servers = cfg.setdefault("mcpServers", {})
    existing = servers.get(SERVER_NAME)

    if existing is None:
        servers[SERVER_NAME] = new_entry
        _atomic_write_json(CLAUDE_JSON, cfg)
        print(f"Registered thinkweave MCP server in {CLAUDE_JSON}.")
        return

    if _entries_equal(existing, new_entry):
        print("thinkweave MCP server already registered (no change).")
        return

    print(f"thinkweave MCP server already in {CLAUDE_JSON} but differs:")
    for line in _diff_lines(existing, new_entry):
        print(line)
    print()
    if not args.yes:
        print("Re-run with --yes to overwrite, or edit ~/.claude.json by hand.")
        sys.exit(1)
    servers[SERVER_NAME] = new_entry
    _atomic_write_json(CLAUDE_JSON, cfg)
    print(f"Updated thinkweave MCP entry in {CLAUDE_JSON}.")


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
            f"Skipping ~/.claude.json write (plugin manager owns that registration)."
        )
        if args.vault:
            print(
                "Note: --vault is a no-op on the plugin route — set "
                "THINKWEAVE_VAULT in your shell env instead."
            )
        # Eager `uv sync` is skipped on the plugin route — the plugin's
        # source path (`${CLAUDE_PLUGIN_ROOT}`) is resolved by the plugin
        # runtime, not by `weave`. First MCP launch syncs lazily.
        if not getattr(args, "no_claude_md", False):
            _install_claude_md_block(args.yes)
        _print_next_steps()
        return

    project_root = _detect_project_root()
    _check_pyproject_reachable(project_root)
    new_entry = _build_server_entry(project_root, vault_root=args.vault)

    _write_mcp_entry(args, new_entry)
    _uv_sync(project_root)
    if not getattr(args, "no_claude_md", False):
        _install_claude_md_block(args.yes)
    _print_next_steps()


def cmd_uninstall(args: argparse.Namespace) -> None:
    """Reverse `weave install` — remove the MCP entry, the CLAUDE.md block,
    and any leftover pause marker. Vault, hooks, plugin manifest, and cron
    jobs are out of scope (they have their own owners)."""
    md_block_present = (
        CLAUDE_MD.exists()
        and CLAUDE_MD_BLOCK_START in CLAUDE_MD.read_text(encoding="utf-8")
    )
    mcp_present = False
    if CLAUDE_JSON.exists():
        try:
            cfg = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
            mcp_present = SERVER_NAME in cfg.get("mcpServers", {})
        except json.JSONDecodeError:
            pass

    to_remove: list[str] = []
    if mcp_present:
        to_remove.append(f"thinkweave MCP entry in {CLAUDE_JSON}")
    if md_block_present:
        to_remove.append(f"thinkweave block in {CLAUDE_MD}")
    if MARKER.exists():
        to_remove.append(f"pause marker {MARKER}")

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
        print(f"Removed MCP entry from {CLAUDE_JSON}.")
    if _remove_claude_md_block():
        print(f"Removed thinkweave block from {CLAUDE_MD}.")
    if MARKER.exists():
        MARKER.unlink()
        print(f"Removed pause marker {MARKER}.")
    print()
    print("Done. Restart Claude Code so the MCP server is no longer launched.")


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON via tempfile + os.replace to avoid corrupting ~/.claude.json
    if the process is interrupted mid-write."""
    import os

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _print_next_steps() -> None:
    print()
    print("Next:")
    print("  1. Restart Claude Code      # MCP server only spawns on session start")
    print("  2. cd <repo> && claude      # open a project")
    print("  3. /onboard                 # vault wiring, hooks, CC backfill, ontology, sources, smoke test")
    print()
    print("Tip: pass `--vault PATH` to `weave install` to bake the vault path into the")
    print("MCP server entry now; otherwise `/onboard` will ask and persist it.")


def _raw_mcp_entry_present() -> bool:
    """True if ``~/.claude.json`` carries a hand-written thinkweave MCP entry
    (the ``weave install`` escape hatch). dev-link uses this to warn: the
    plugin manifest already declares the server, so a leftover raw entry
    would make Claude Code spawn ``thinkweave`` twice."""
    if not CLAUDE_JSON.exists():
        return False
    try:
        cfg = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return SERVER_NAME in cfg.get("mcpServers", {})


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
    if not (repo / ".claude-plugin" / "plugin.json").exists():
        print(
            f"error: no .claude-plugin/plugin.json under {repo}.\n"
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
            "warning: a raw thinkweave MCP entry is still in ~/.claude.json.\n"
            "  Combined with the dev-linked plugin, Claude Code registers the\n"
            "  `thinkweave` server twice. Run `weave uninstall` to drop the raw\n"
            "  entry (the plugin manifest provides the server).",
            file=sys.stderr,
        )

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    if DEV_LINK.is_symlink():
        if DEV_LINK.resolve() == repo.resolve():
            print(f"Already dev-linked: {DEV_LINK} → {repo}")
            _print_dev_link_next_steps()
            return
        if not getattr(args, "force", False):
            print(
                f"error: {DEV_LINK} already points at {DEV_LINK.resolve()}.\n"
                f"Re-run with --force to repoint it at {repo}.",
                file=sys.stderr,
            )
            sys.exit(1)
        DEV_LINK.unlink()
    elif DEV_LINK.exists():
        print(
            f"error: {DEV_LINK} exists and is not a symlink (a real plugin\n"
            "dir?). Remove it by hand if you mean to dev-link this checkout.",
            file=sys.stderr,
        )
        sys.exit(1)

    DEV_LINK.symlink_to(repo)
    print(f"Dev-linked {DEV_LINK} → {repo}")
    _print_dev_link_next_steps()


def cmd_dev_unlink(args: argparse.Namespace) -> None:
    """Reverse ``weave dev-link`` — remove the ``~/.claude/skills/thinkweave``
    symlink. A real (non-symlink) install at that path is left untouched."""
    if DEV_LINK.is_symlink():
        target = DEV_LINK.resolve()
        DEV_LINK.unlink()
        print(f"Removed dev-link {DEV_LINK} (was → {target}).")
        print("Restart Claude Code to drop the /thinkweave:* commands.")
        return
    if DEV_LINK.exists():
        print(
            f"note: {DEV_LINK} is not a symlink (real install?) — left untouched.",
            file=sys.stderr,
        )
        return
    print(f"No dev-link at {DEV_LINK} (nothing to remove).")
