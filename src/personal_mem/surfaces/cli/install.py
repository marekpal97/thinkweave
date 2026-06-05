"""``mem install`` — machine-scope setup.

Verifies the personal_mem console scripts are reachable on PATH and
idempotently registers the personal-mem MCP server entry in
``~/.claude.json`` so Claude Code can launch it. Run once per machine
after ``pip install`` / ``pipx install``.

Scope boundary: this command never touches a vault or a project's
``.claude/settings.json``. ``mem init`` owns the vault; ``mem hooks
install`` (invoked by ``/onboard``) owns project-side hook registration.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

CLAUDE_JSON = Path.home() / ".claude.json"
SERVER_NAME = "personal-mem"
REQUIRED_SCRIPTS = ("mem", "mem-hook", "mem-mcp")


def _detect_uv_path() -> str:
    """Resolve uv on PATH. Returns the literal 'uv' if not found — the
    config still works as long as the user's shell can resolve it later."""
    return shutil.which("uv") or "uv"


def _detect_project_root() -> Path:
    """The personal_mem source tree this `mem` invocation came from.

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
    """Construct the canonical ``mcpServers.personal-mem`` block.

    Uses the ``mem-mcp`` console script (stable invocation, layout-
    independent — see ARCHITECTURE.md §Invocation surface).
    """
    args = ["run", "--project", str(project_root), "--extra", "mcp", "mem-mcp"]
    entry: dict[str, Any] = {
        "type": "stdio",
        "command": _detect_uv_path(),
        "args": args,
        "env": {},
    }
    if vault_root:
        entry["env"]["PERSONAL_MEM_VAULT"] = vault_root
    return entry


def _check_scripts() -> list[str]:
    """Return the list of required console scripts missing from PATH."""
    return [s for s in REQUIRED_SCRIPTS if shutil.which(s) is None]


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


def cmd_install(args: argparse.Namespace) -> None:
    missing = _check_scripts()
    if missing:
        print(
            f"error: required console scripts missing from PATH: {', '.join(missing)}",
            file=sys.stderr,
        )
        print(
            "Install personal-mem first (e.g. `pipx install personal-mem` or "
            "`pip install -e .[all]` from a clone), then re-run `mem install`.",
            file=sys.stderr,
        )
        sys.exit(1)

    project_root = _detect_project_root()
    new_entry = _build_server_entry(project_root, vault_root=args.vault)

    if not CLAUDE_JSON.exists():
        if not args.yes:
            print(f"~/.claude.json does not exist. `mem install` will create it.")
            print("Re-run with --yes to proceed.")
            sys.exit(1)
        cfg: dict[str, Any] = {"mcpServers": {SERVER_NAME: new_entry}}
        CLAUDE_JSON.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {CLAUDE_JSON} with personal-mem MCP entry.")
        _print_next_steps()
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
        print(f"Registered personal-mem MCP server in {CLAUDE_JSON}.")
        _print_next_steps()
        return

    if _entries_equal(existing, new_entry):
        print(f"personal-mem MCP server already registered (no change).")
        _print_next_steps()
        return

    print(f"personal-mem MCP server already in {CLAUDE_JSON} but differs:")
    for line in _diff_lines(existing, new_entry):
        print(line)
    print()
    if not args.yes:
        print("Re-run with --yes to overwrite, or edit ~/.claude.json by hand.")
        sys.exit(1)
    servers[SERVER_NAME] = new_entry
    _atomic_write_json(CLAUDE_JSON, cfg)
    print(f"Updated personal-mem MCP entry in {CLAUDE_JSON}.")
    _print_next_steps()


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
    print("Tip: pass `--vault PATH` to `mem install` to bake the vault path into the")
    print("MCP server entry now; otherwise `/onboard` will ask and persist it.")
