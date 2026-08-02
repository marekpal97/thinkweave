"""``HarnessProfile`` — the one seam that owns per-harness facts.

Thinkweave's value layers (core / operations / acquisition, the CLI, the stdio
MCP server) are already harness-agnostic; what was not, until this module, is
*install topology* and *invocation shape*. Roughly eight files each carried
their own copy of "Claude Code keeps its MCP config at ``~/.claude.json`` and
its instructions at ``~/.claude/CLAUDE.md`` and is invoked as ``claude -p``".
A profile collects those facts in one place so a second harness is a data
entry here, not a fork of every consumer (epic #103, dec-0535e46b).

Selection is ``$THINKWEAVE_HARNESS`` (default ``claude-code``). Profiles are
built per call rather than cached at import so a changed ``$HOME`` — the only
input — always applies; ``_OVERRIDE`` is the in-process escape hatch tests and
a future ``--harness`` flag use.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from thinkweave.core.plugin_route import PLUGIN_NAME, namespace_prompt, plugin_namespace


@dataclass(frozen=True)
class HarnessProfile:
    id: str

    # --- capability flags -------------------------------------------------
    hooks: bool
    """The harness can run our lifecycle hooks (session start, prompt submit,
    post-tool-use, stop). False ⇒ ``weave hooks install`` refuses and the user
    is pointed at explicit ``/wrap``-style invocation instead."""

    subagents: bool
    """The harness can fan work out to parallel sub-agents (the ``/drain`` and
    ``/dream`` worker topology). Recorded here for the W2 Codex adapter; no
    consumer in this wave."""

    native_memory: bool
    """The harness keeps its own durable memory corpus on disk, which the
    memory seam reconciles against the vault. False ⇒ the seam walk yields
    nothing rather than inventing a corpus (dec-0535e46b)."""

    headless_slash: bool
    """The harness resolves slash-command skills in a headless
    (``-p``/one-shot) invocation. False ⇒ deterministic renderers leave the
    prompt alone instead of rewriting a skill token the harness cannot run."""

    # --- paths ------------------------------------------------------------
    instructions_file: Path
    """The always-loaded user-global instructions file we splice a
    sentinel-wrapped block into (``~/.claude/CLAUDE.md``)."""

    mcp_config: Path
    """Where an MCP server registration is read from / written to. The *format*
    of that file is the writer's business, not the profile's (#106)."""

    skills_dir: Path
    plugins_root: Path
    plugins_cache: Path
    installed_plugins: Path

    user_settings: Path
    """Machine-scope hook settings file."""

    project_settings_relpath: Path
    """Project-scope hook settings, relative to the repo root."""

    project_mcp_config_relpath: Path
    """Project-scope MCP registration file, relative to the repo root
    (``.mcp.json``)."""

    project_plugins_relpath: Path
    """Where project-local plugins live, relative to the repo root. Each entry
    inside is a plugin dir carrying a :attr:`plugin_manifest_relpath`."""

    plugin_manifest_relpath: Path
    """A plugin's manifest, relative to the plugin's own root."""

    pause_marker: Path
    memory_projects_root: Path
    memory_global_dir: Path

    # --- headless invocation ----------------------------------------------
    cli_bin: str
    model_flag: str
    prompt_flag: str
    bypass_permissions_flag: str
    """Flag granting unattended tool use. Empty when the harness has none —
    the renderers then append nothing."""

    @property
    def dev_link(self) -> Path:
        """Where ``weave dev-link`` symlinks a checkout so the harness loads it
        as a plugin."""
        return self.skills_dir / PLUGIN_NAME

    def namespace(self) -> str | None:
        """The plugin namespace skill tokens must carry, or None for bare names.

        Probes this profile's own plugin locations. A harness that cannot run
        slash commands headlessly has no namespace to apply at all.
        """
        if not self.headless_slash:
            return None
        return plugin_namespace(
            manifest=self.installed_plugins, dev_link=self.dev_link
        )

    def namespaced(self, prompt: str) -> str:
        """Apply this harness's namespace rule to a headless prompt."""
        return namespace_prompt(prompt, self.namespace())

    def headless_argv(
        self,
        prompt: str,
        *,
        model: str | None = None,
        bypass: bool = False,
        bin: str | None = None,
    ) -> list[str]:
        """Argv for a one-shot headless invocation.

        Deliberately dumb about the prompt — callers that want the skill token
        namespaced pass it through :meth:`namespaced` first, so what goes in is
        what comes out. ``bin`` overrides the binary (the
        ``THINKWEAVE_CLAUDE_BIN`` escape hatch).
        """
        argv = [bin or self.cli_bin]
        if model:
            argv += [self.model_flag, model]
        argv += [self.prompt_flag, prompt]
        if bypass and self.bypass_permissions_flag:
            argv.append(self.bypass_permissions_flag)
        return argv


def claude_code(home: Path | None = None) -> HarnessProfile:
    """Claude Code's install topology and invocation shape.

    Note ``mcp_config`` sits at ``~/.claude.json`` — *beside* the ``~/.claude``
    directory, not inside it. That asymmetry is exactly the kind of fact a
    profile exists to hold.
    """
    h = home or Path.home()
    cc = h / ".claude"
    return HarnessProfile(
        id="claude-code",
        hooks=True,
        subagents=True,
        native_memory=True,
        headless_slash=True,
        instructions_file=cc / "CLAUDE.md",
        mcp_config=h / ".claude.json",
        skills_dir=cc / "skills",
        plugins_root=cc / "plugins",
        plugins_cache=cc / "plugins" / "cache",
        installed_plugins=cc / "plugins" / "installed_plugins.json",
        user_settings=cc / "settings.json",
        project_settings_relpath=Path(".claude") / "settings.local.json",
        project_mcp_config_relpath=Path(".mcp.json"),
        project_plugins_relpath=Path(".claude") / "plugins",
        plugin_manifest_relpath=Path(".claude-plugin") / "plugin.json",
        pause_marker=cc / "thinkweave_paused.json",
        memory_projects_root=cc / "projects",
        memory_global_dir=cc / "memory",
        cli_bin="claude",
        model_flag="--model",
        prompt_flag="-p",
        bypass_permissions_flag="--dangerously-skip-permissions",
    )


PROFILES: dict[str, Callable[[], HarnessProfile]] = {"claude-code": claude_code}

#: In-process override. ``None`` means "derive from the environment".
_OVERRIDE: HarnessProfile | None = None


def active() -> HarnessProfile:
    """The profile every consumer reads.

    An unknown ``$THINKWEAVE_HARNESS`` exits rather than falling back to Claude
    Code — a mis-set harness must not quietly write into the wrong home. It
    exits with a named remedy rather than raising, because every consumer calls
    this: a typo would otherwise surface as a bare traceback from whichever
    module happened to look first.
    """
    if _OVERRIDE is not None:
        return _OVERRIDE
    name = os.environ.get("THINKWEAVE_HARNESS") or "claude-code"
    factory = PROFILES.get(name)
    if factory is None:
        print(
            f"error: unknown harness {name!r} in $THINKWEAVE_HARNESS.\n"
            f"Registered profiles: {', '.join(sorted(PROFILES))}.",
            file=sys.stderr,
        )
        sys.exit(1)
    return factory()
