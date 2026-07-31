"""Install/uninstall thinkweave hooks into Claude Code settings.

Merge pattern — reads existing settings, appends hooks,
preserves permissions. Non-destructive.

Hook definitions (events, matchers, commands, timeouts) are DERIVED from
the canonical ``hooks/hooks.json`` at the repo root — the same file the
plugin route loads. This module carries no duplicate of the definitions
(#50); the only per-route transformation is substituting
``${CLAUDE_PLUGIN_ROOT}`` with the absolute repo root.
"""

from __future__ import annotations

import copy
import difflib
import json
from pathlib import Path

# Substrings that identify a thinkweave hook command in settings,
# across every historical form this project has written. On reinstall,
# any stored command matching one of these gets rewritten in place to
# the current absolute-path form — so upgrading is always a single
# `weave hooks install` away regardless of which variant is stuck in the
# file.
#
#   - `weave-hook`: console-script name, current form (and the brief
#     bare-name form shipped before absolute-path resolution).
#   - `run_hook.sh`: the original bash wrapper, deleted in favor of the
#     entry point.
#   - `thinkweave.surfaces.hooks.handler`: `python -m thinkweave.surfaces.hooks.handler`
#     entries from even earlier installs.
HOOK_MARKERS = (
    "weave-hook",
    "run_hook.sh",
    "thinkweave.surfaces.hooks.handler",
    "thinkweave.hooks.handler",  # legacy (pre-restructure)
)


def _canonical_hooks_path() -> Path:
    """Locate the canonical ``hooks/hooks.json`` shipped at the repo root.

    Walks up from this module's file — the package is installed editable
    into the repo venv by ``uv run --project`` on every supported route
    (there is no PyPI wheel), so the repo layout is always present. Fails
    loud otherwise: an installer that cannot see its source of truth must
    not fall back to a guess.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "hooks" / "hooks.json"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "hooks/hooks.json not found above "
        f"{Path(__file__).resolve()} — `weave hooks install` derives the "
        "hook definitions from the canonical file and must run from a "
        "thinkweave repo checkout (plugin or dev clone)."
    )


def _load_canonical_hooks(hooks_json: str | Path = "") -> dict:
    """Parse the canonical hook definitions: ``{event: [entries…]}``.

    ``hooks_json`` overrides the content source (contract tests drive the
    installer with a mutated temp copy); the default is the repo's own
    ``hooks/hooks.json``.
    """
    path = Path(hooks_json) if hooks_json else _canonical_hooks_path()
    return json.loads(path.read_text(encoding="utf-8"))["hooks"]


def _repo_root() -> Path:
    """The absolute repo root — what ``${CLAUDE_PLUGIN_ROOT}`` means on the
    machine route. Always derived from the real checkout (parent of the
    canonical ``hooks/`` dir), never from the content-source override."""
    return _canonical_hooks_path().parent.parent


def _localize_command(command: str, root: Path) -> str:
    """Per-route transformation, machine route: substitute the plugin-route
    ``${CLAUDE_PLUGIN_ROOT}`` placeholder with the absolute repo root.

    This is the ONLY divergence allowed between what the plugin route loads
    and what ``weave hooks install`` writes. The command still resolves
    ``weave-hook`` at fire time — it invokes the committed
    ``bin/weave-hook-launch`` shim, which runs the same 3-tier uv ladder as
    ``bin/weave-mcp-launch`` (the #47/#52 launcher story) so hooks survive
    the stripped, non-login harness PATH. No interpreter or venv path is
    snapshotted at install time; only the launcher's own committed path is
    made absolute, so a moved/stale venv can no longer break installed hooks.
    """
    return command.replace("${CLAUDE_PLUGIN_ROOT}", str(root))


def _settings_path(project_dir: str = "") -> Path:
    """Find the settings.local.json path."""
    if project_dir:
        return Path(project_dir) / ".claude" / "settings.local.json"
    # Default: current working directory
    return Path.cwd() / ".claude" / "settings.local.json"


def _settings_path_for_scope(scope: str, project_dir: str = "") -> Path:
    """Dispatch the settings.json target by install scope.

    ``project`` — the legacy default. Writes to ``<cwd or project_dir>/.claude/
    settings.local.json``. Fires only inside that project tree.

    ``user`` — machine-scope. Writes to ``~/.claude/settings.json`` (note:
    non-local, the per-user file). Fires in every Claude Code session on
    this machine. Used by the legacy `/onboard` install path to mirror
    what the plugin manifest provides for free.
    """
    if scope == "project":
        return _settings_path(project_dir)
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    raise ValueError(
        f"unknown scope {scope!r}; expected one of: 'project', 'user'"
    )


def _build_installed_settings(
    existing: dict, hooks_json: str | Path = ""
) -> dict:
    """Return a settings dict with thinkweave hooks merged in.

    Pure function — operates on a deep copy of ``existing``, never mutates
    the input. The hook definitions are DERIVED from the canonical
    ``hooks/hooks.json`` (single authoring place for events, matchers,
    commands, and timeouts — #50); the only transformation applied is
    ``_localize_command``. Merge semantics:

    - every canonical event/matcher entry is ensured (rewrite-in-place of
      any historical thinkweave form via HOOK_MARKERS, else append);
    - foreign hooks are preserved untouched;
    - thinkweave entries under events NO LONGER in the canonical file
      (e.g. the retired PreToolUse phase) are stripped, so reinstalling
      converges stale settings without disturbing anything else.
    """
    settings = copy.deepcopy(existing)
    hooks = settings.setdefault("hooks", {})
    canonical = _load_canonical_hooks(hooks_json)
    root = _repo_root()

    for event, canonical_entries in canonical.items():
        entries = hooks.setdefault(event, [])
        # Slot disambiguation is only needed when one event owns several
        # thinkweave entries (PostToolUse: action gate + MCP gate) — the
        # entry's matcher is the slot key.
        multi = len(canonical_entries) > 1
        for c_entry in canonical_entries:
            matcher = c_entry.get("matcher", "")
            c_hook = c_entry["hooks"][0]
            _ensure_hook(
                entries,
                matcher,
                _localize_command(c_hook["command"], root),
                slot=matcher if multi else None,
                timeout=c_hook.get("timeout"),
            )

    # Retired phases: strip thinkweave entries from any event the
    # canonical file no longer authors (foreign hooks stay).
    for event in list(hooks):
        if event not in canonical:
            _strip_thinkweave_hooks(hooks, event)

    return settings


def _build_uninstalled_settings(existing: dict) -> dict:
    """Return a settings dict with thinkweave hooks removed.

    Pure function — mirrors ``_build_installed_settings`` so uninstall and
    its dry-run share one implementation.
    """
    settings = copy.deepcopy(existing)
    hooks = settings.get("hooks", {})

    # Iterate every event present in the file (not a hardcoded list) so
    # uninstall round-trips any event the canonical hooks.json authors —
    # including ones added after this code shipped.
    for hook_type in list(hooks):
        entries = hooks.get(hook_type, [])
        hooks[hook_type] = [
            entry
            for entry in entries
            if not any(
                _is_thinkweave_hook(h.get("command", ""))
                for h in entry.get("hooks", [])
            )
        ]
        # Clean up empty arrays
        if not hooks[hook_type]:
            del hooks[hook_type]

    if "hooks" in settings and not hooks:
        del settings["hooks"]

    return settings


def _settings_diff(before: dict, after: dict, target: Path) -> str:
    """Unified diff over pretty-printed JSON, suitable for printing."""
    before_text = json.dumps(before, indent=2, sort_keys=False) + "\n"
    after_text = json.dumps(after, indent=2, sort_keys=False) + "\n"
    diff = difflib.unified_diff(
        before_text.splitlines(keepends=True),
        after_text.splitlines(keepends=True),
        fromfile=f"a/{target}",
        tofile=f"b/{target}",
        n=3,
    )
    return "".join(diff)


def install_hooks(
    project_dir: str = "",
    scope: str = "project",
    dry_run: bool = False,
    hooks_json: str = "",
) -> None:
    """Install thinkweave hooks into the chosen Claude Code settings file.

    ``scope`` selects the settings file: ``"project"`` (default — preserves
    the legacy behaviour) writes to ``<project_dir or cwd>/.claude/
    settings.local.json``; ``"user"`` writes to ``~/.claude/settings.json``
    so hooks fire in every Claude Code session on this machine. The
    plugin install path gets global hooks via the plugin manifest; this
    flag is the legacy install path's equivalent.

    ``dry_run=True`` prints the planned unified diff against the current
    settings file and returns without writing.

    ``hooks_json`` overrides the canonical-definitions source (contract
    tests only); defaults to the repo's ``hooks/hooks.json``.
    """
    settings_path = _settings_path_for_scope(scope, project_dir)

    # Read existing settings (no parent mkdir yet — dry-run must not
    # create the .claude directory either).
    existing: dict = {}
    if settings_path.exists():
        existing = json.loads(settings_path.read_text(encoding="utf-8"))

    planned = _build_installed_settings(existing, hooks_json=hooks_json)

    if dry_run:
        print(f"Would write: {settings_path}")
        diff = _settings_diff(existing, planned, settings_path)
        if diff:
            print(diff, end="")
        else:
            print("(no changes)")
        return

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(planned, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Hooks installed at {settings_path} (scope={scope})\n"
        "  SessionStart hook will inject ~7–10k tokens of project context "
        "on the next Claude Code session."
    )


def uninstall_hooks(
    project_dir: str = "",
    scope: str = "project",
    dry_run: bool = False,
) -> None:
    """Remove thinkweave hooks from the scoped Claude Code settings file."""
    settings_path = _settings_path_for_scope(scope, project_dir)
    if not settings_path.exists():
        print("No settings file found.")
        return

    existing = json.loads(settings_path.read_text(encoding="utf-8"))
    planned = _build_uninstalled_settings(existing)

    if dry_run:
        print(f"Would write: {settings_path}")
        diff = _settings_diff(existing, planned, settings_path)
        if diff:
            print(diff, end="")
        else:
            print("(no changes)")
        return

    settings_path.write_text(
        json.dumps(planned, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Hooks removed from {settings_path} (scope={scope})")


def _is_thinkweave_hook(command: str) -> bool:
    """True if `command` is a thinkweave hook in any historical form."""
    if not command:
        return False
    return any(marker in command for marker in HOOK_MARKERS)


def _strip_thinkweave_hooks(hooks: dict, hook_type: str) -> None:
    """Remove every thinkweave entry under `hook_type`, leaving foreign
    hooks untouched. Used to retire a hook phase we no longer install
    (currently PreToolUse) — re-running `weave hooks install` cleans the
    stale entry out of existing settings without disturbing anything else.
    """
    entries = hooks.get(hook_type, [])
    pruned = [
        entry
        for entry in entries
        if not any(
            _is_thinkweave_hook(h.get("command", ""))
            for h in entry.get("hooks", [])
        )
    ]
    if pruned:
        hooks[hook_type] = pruned
    elif hook_type in hooks:
        del hooks[hook_type]


def _ensure_hook(
    entries: list,
    matcher: str,
    command: str,
    *,
    slot: str | None = None,
    timeout: int | None = None,
) -> None:
    """Add a hook entry, or rewrite any existing thinkweave hook in place.

    Matches any form this project has ever written (see HOOK_MARKERS),
    so reinstalling always converges to the current canonical form
    regardless of which historical variant is stored in the file.

    ``slot``: when a single hook phase has more than one thinkweave
    entry (PostToolUse owns two — the ``Write|Edit|Bash`` action gate
    and the ``mcp__thinkweave__.*`` MCP gate), we need to disambiguate
    which existing entry to rewrite. The slot is matched by the entry's
    ``matcher`` string, so legacy single-entry installs migrate cleanly:
    the first thinkweave entry that matches the requested slot's
    matcher is rewritten in place; any unmatched-slot install appends a
    fresh entry. With no slot, the first thinkweave entry found is
    rewritten — preserving the original single-matcher contract for
    SessionStart / UserPromptSubmit / Stop, which only ever own one
    entry per phase.
    """
    for entry in entries:
        entry_matcher = entry.get("matcher", "")
        # Slot-aware: only rewrite an entry that already targets this
        # slot. Lets a single phase own multiple thinkweave entries
        # (PostToolUse: action gate + MCP gate) without each install
        # call clobbering the other.
        if slot is not None and entry_matcher != matcher:
            continue
        for hook in entry.get("hooks", []):
            if _is_thinkweave_hook(hook.get("command", "")):
                hook["command"] = command
                # Snap matcher + timeout to the canonical values too, in case a
                # legacy install wrote a stale matcher or timeout. A canonical
                # entry without a timeout means "inherit the harness default" —
                # drop any stale explicit value.
                if timeout is None:
                    hook.pop("timeout", None)
                else:
                    hook["timeout"] = timeout
                entry["matcher"] = matcher
                return

    fresh: dict = {"type": "command", "command": command}
    if timeout is not None:
        fresh["timeout"] = timeout
    entries.append({"matcher": matcher, "hooks": [fresh]})
