"""Detect the Claude Code plugin install route and namespace skill tokens.

Claude Code registers plugin-shipped slash commands and agents under the
plugin's namespace (``/personal-mem:dream``, ``personal-mem:dream-wrap-worker``)
with **no bare-name aliasing** — verified empirically 2026-06-12 against a
probe plugin (bare invocations fail with "Unknown command" / "Agent type not
found"). Project-scope installs (a repo clone with ``.claude/commands`` +
``.claude/agents``) resolve bare names only.

Anything that *renders* a skill invocation into a deterministic surface — the
cron block (``scheduling/registry.resolve_command``) and flow stages
(``flows._build_argv``) — must therefore pick the name by route at render
time. LLM-read surfaces (the skill markdown files) instead carry a prose
fallback rule ("if the bare name doesn't resolve, retry with the
``personal-mem:`` prefix").

Detection reads the plugin manager's ``installed_plugins.json`` — the same
registry Claude Code consults at session start. Missing/corrupt file means
"not the plugin route" (legacy/clone installs don't have one).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PLUGIN_NAME = "personal-mem"

_INSTALLED_PLUGINS = Path.home() / ".claude" / "plugins" / "installed_plugins.json"

# A bare skill token: leading slash + kebab name. Already-namespaced tokens
# (`/personal-mem:dream`) and filesystem paths (`/home/...`) don't match.
_SKILL_TOKEN = re.compile(r"^/(?P<name>[a-z][a-z0-9-]*)$")


def plugin_namespace(*, manifest: Path | None = None) -> str | None:
    """Return ``'personal-mem'`` when the plugin route is active, else None.

    ``manifest`` overrides the default ``~/.claude/plugins/installed_plugins.json``
    location (tests).
    """
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

    ``namespace_prompt("/dream --essence-cap 0", "personal-mem")`` →
    ``"/personal-mem:dream --essence-cap 0"``. Non-skill prompts, paths,
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
