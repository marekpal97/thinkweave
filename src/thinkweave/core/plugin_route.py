"""Detect the Claude Code plugin install route and namespace skill tokens.

Claude Code registers plugin-shipped slash commands and agents under the
plugin's namespace (``/thinkweave:dream``, ``thinkweave:dream-wrap-worker``)
with **no bare-name aliasing** — verified empirically 2026-06-12 against a
probe plugin (bare invocations fail with "Unknown command" / "Agent type not
found"). Project-scope installs (a repo clone with ``.claude/commands`` +
``.claude/agents``) resolve bare names only.

Anything that *renders* a skill invocation into a deterministic surface — the
cron block (``scheduling/registry.resolve_command``), flow stages
(``flows._build_argv``), and the manual-rejudge shell-out
(``surfaces/cli/judge._rejudge_argv``) — must therefore pick the name by route
at render time. Keep that list in sync: a new hand-built ``claude -p
"/skill"`` that skips ``namespace_prompt`` is broken on the plugin route.
LLM-read surfaces (the skill markdown files) instead carry a prose fallback
rule ("if the bare name doesn't resolve, retry with the ``thinkweave:``
prefix").

The plugin route has **two install shapes**, both namespaced:

* **marketplace** — recorded in the plugin manager's ``installed_plugins.json``
  (the registry Claude Code consults at session start).
* **dev-link** (``weave dev-link``) — a ``~/.claude/skills/thinkweave`` symlink
  that Claude Code auto-loads as the ``thinkweave@skills-dir`` plugin. It is
  **not** written into ``installed_plugins.json``, so it is detected by the
  symlink itself. Missing both ⇒ a project-scope clone (bare names only).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PLUGIN_NAME = "thinkweave"

_INSTALLED_PLUGINS = Path.home() / ".claude" / "plugins" / "installed_plugins.json"

# The `weave dev-link` symlink. Its mere existence (Claude Code auto-loads the
# checkout as the `thinkweave@skills-dir` plugin) means skills are namespaced.
_DEV_LINK = Path.home() / ".claude" / "skills" / PLUGIN_NAME

# A bare skill token: leading slash + kebab name. Already-namespaced tokens
# (`/thinkweave:dream`) and filesystem paths (`/home/...`) don't match.
_SKILL_TOKEN = re.compile(r"^/(?P<name>[a-z][a-z0-9-]*)$")


def plugin_namespace(
    *, manifest: Path | None = None, dev_link: Path | None = None
) -> str | None:
    """Return ``'thinkweave'`` when the plugin route is active, else None.

    Active = either install shape that registers skills namespaced (no bare
    alias): the ``weave dev-link`` symlink, or a marketplace entry in
    ``installed_plugins.json``. ``manifest`` / ``dev_link`` override the
    default probe locations (tests).
    """
    # dev-link route: the skills-dir symlink auto-loads as a namespaced plugin
    # without ever touching installed_plugins.json — check the symlink itself.
    link = dev_link or _DEV_LINK
    try:
        if link.is_symlink():
            return PLUGIN_NAME
    except OSError:
        pass

    path = manifest or _INSTALLED_PLUGINS
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return None
    for key in plugins:
        # Keys are "<name>@<marketplace>" in the v2 schema; bare "<name>"
        # in older files. Either way the name is the part before '@'.
        if str(key).split("@", 1)[0] == PLUGIN_NAME:
            return PLUGIN_NAME
    return None


def namespace_prompt(arg: str, ns: str | None) -> str:
    """Prefix the leading bare skill token of a ``claude -p`` argument.

    ``namespace_prompt("/dream --essence-cap 0", "thinkweave")`` →
    ``"/thinkweave:dream --essence-cap 0"``. Non-skill prompts, paths,
    and already-namespaced tokens pass through unchanged. ``ns=None`` is a
    no-op so callers can pass ``plugin_namespace()`` straight in.
    """
    if not ns:
        return arg
    head, sep, rest = arg.partition(" ")
    m = _SKILL_TOKEN.match(head)
    if not m:
        return arg
    return f"/{ns}:{m.group('name')}{sep}{rest}"
