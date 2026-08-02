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

# What the SessionStart hook asks `build_project_context` for. Lives here so a
# harness that has to be *told* the payload's size (`additional_context_limit`)
# derives its cap from the same number the handler spends — raising the budget
# used to leave the cap behind, silently, and the payload spilled to a temp
# file with no error (#107).
SESSION_START_BUDGET_TOKENS = 10_000

_NUDGE = (
    "If `weave_*` MCP tools are available, thinkweave (Obsidian-native memory "
    "layer) is your durable memory for this session. Prefer `weave_search` / "
    "`weave_context` / `weave_graph` over filesystem search"
)


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

    instructions_block_body: str
    """What goes between the sentinels there.

    Per-harness because the nudge has to name things the harness actually has.
    A Claude Code block ends "run ``/wrap`` before ``/clear``"; on a harness
    with no slash commands and no session-end hook that is an instruction the
    model cannot follow, so its block names the explicit ``weave_extract`` call
    instead — the epic's "documented degradation, not a broken promise"."""

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
    """Flag introducing the prompt. Empty when the harness takes it
    positionally (``codex exec <prompt>``)."""

    bypass_permissions_flag: str
    """Flag granting unattended tool use. Empty when the harness has none —
    the renderers then append nothing."""

    exec_subcommand: str = ""
    """Subcommand that puts the harness in one-shot mode, inserted right after
    the binary. Empty when a flag alone does it (``claude -p``)."""

    headless_model: str = ""
    """Model our own headless flows ask this harness for. Model names are
    per-vendor, so a shared literal cannot exist; empty means "don't pass
    ``model_flag`` at all — let the harness use its configured default"."""

    project_mcp_caveat: str = ""
    """Condition under which the harness ignores a project-scope registration,
    for ``weave doctor --mcp`` to pass on. Empty when it always honours one."""

    hooks_global_only: bool = False
    """The harness only honours hooks from its machine-scope file, so
    ``weave hooks install --scope project`` is refused rather than writing a
    file that never fires (#107; openai/codex#17532)."""

    additional_context_limit: int | None = None
    """Per-handler cap the harness needs to be told before it will pass a
    large ``additionalContext`` through to the model. ``None`` ⇒ the harness
    has no such knob and the installer writes nothing."""

    hooks_install_caveat: str = ""
    """Anything standing between "the file is written" and "the hooks fire",
    appended to the install confirmation. Empty when writing the file is the
    whole story (Claude Code)."""

    hooks_bypass_flag: str = ""
    """Flag an *unattended* run needs before the harness will run installed
    hooks at all. Empty when nothing gates them (Claude Code)."""

    display_name: str = ""
    """The harness's name as a human writes it ("Claude Code"), for messages
    that used to hardcode it. Set it on any profile whose messages reach a
    user — the ``id`` is a slug and reads as a typo in prose."""

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
        if self.exec_subcommand:
            argv.append(self.exec_subcommand)
        if model:
            argv += [self.model_flag, model]
        argv += [self.prompt_flag, prompt] if self.prompt_flag else [prompt]
        if bypass and self.bypass_permissions_flag:
            argv.append(self.bypass_permissions_flag)
        # Hook trust is a separate gate from tool approval: Codex silently
        # runs ZERO hooks until each definition is trusted via `/hooks`, and
        # an unattended run has no way to answer that prompt. Without this an
        # unattended run loses passive capture with no error at all (#107).
        if bypass and self.hooks and self.hooks_bypass_flag:
            argv.append(self.hooks_bypass_flag)
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
        display_name="Claude Code",
        hooks=True,
        subagents=True,
        native_memory=True,
        headless_slash=True,
        instructions_file=cc / "CLAUDE.md",
        instructions_block_body=f"{_NUDGE}, and run `/wrap` before `/clear`.",
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
        headless_model="sonnet",
    )


def codex(home: Path | None = None) -> HarnessProfile:
    """OpenAI Codex CLI's install topology and invocation shape (#106).

    Everything Codex owns lives under one root — ``$CODEX_HOME``, defaulting to
    ``~/.codex`` — so unlike Claude Code there is no config file stranded beside
    the directory. An explicit ``home`` wins over ``$CODEX_HOME`` so a caller
    that sandboxes the home (the test suite) cannot be pulled back out to the
    developer's real Codex install by a stray env var.

    Verified against codex-cli 0.146.0 on 2026-08-02: ``codex mcp add`` writes
    ``[mcp_servers.<name>]`` with ``command``/``args``/``env`` and *no* ``type``
    key (``codex exec --strict-config`` rejects one), and ``codex exec`` takes
    its prompt positionally.
    """
    cx = (
        home / ".codex"
        if home is not None
        else Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    )
    return HarnessProfile(
        id="codex",
        display_name="Codex",
        hooks=True,
        # Repo-local `.codex/` hook entries do not fire in interactive sessions
        # (openai/codex#17532) and the manual gates them on the project being
        # *trusted* besides — two ways to be silently inert, so only the
        # machine-scope file is offered (#107).
        hooks_global_only=True,
        # Codex spills any hook `additionalContext` over ~2500 tokens to a temp
        # file and shows the model a head-and-tail preview instead, so the
        # SessionStart payload needs an explicit limit or the context silently
        # never arrives. Derived from the budget, doubled: our budget is spent
        # against a chars//4 estimate, and note-id-dense markdown tokenizes far
        # worse than 4 chars/token, so a narrow margin still spills. The
        # payload is hard-capped upstream, so this stays a bounded promise, not
        # the "limit = 0" the manual warns against.
        additional_context_limit=2 * SESSION_START_BUDGET_TOKENS,
        # Codex records trust against a hash of each hook definition and skips
        # any it has not seen approved. Writing the file is therefore only half
        # the install — without this line the user gets a success message and
        # hooks that never run.
        hooks_install_caveat=(
            "\n  Codex will not run these until you trust them: open a Codex "
            "session,\n  run `/hooks`, and trust the thinkweave entries. "
            "Re-trust after every\n  `weave hooks install` — trust is keyed to "
            "the exact hook definition."
        ),
        # No verified Task-tool equivalent driving thinkweave's /drain and
        # /dream fan-out. W3's executor route is the planned answer; claiming
        # the capability before it is wired would be a broken promise.
        subagents=False,
        # Codex keeps sessions (imported by acquisition/importers/codex.py) and
        # a memories sqlite, but no markdown auto-memory corpus of the shape the
        # seam reconciles — so the seam walk correctly yields nothing.
        native_memory=False,
        # `codex exec` resolves no slash commands; skill tokens stay bare.
        headless_slash=False,
        instructions_file=cx / "AGENTS.md",
        instructions_block_body=(
            f"{_NUDGE}. This harness fires no session-end hook, so call "
            "`weave_extract` yourself before you finish — it is what persists "
            "the session's insights and decisions into the vault."
        ),
        mcp_config=cx / "config.toml",
        skills_dir=cx / "skills",
        # Codex has its own plugin/marketplace system, but thinkweave ships no
        # Codex plugin (W3 owns the Agent-Skills export). Nothing on disk
        # matches these, so every plugin scan correctly finds nothing.
        plugins_root=cx / "plugins",
        plugins_cache=cx / "plugins" / "cache",
        installed_plugins=cx / "plugins" / "installed_plugins.json",
        plugin_manifest_relpath=Path(".codex-plugin") / "plugin.json",
        # Codex reads hooks from a `hooks.json` OR an inline `[hooks]` table in
        # a config.toml sitting in the same layer — and warns when one layer
        # carries both. #106 already owns this layer's config.toml for
        # `[mcp_servers]`, so hooks take the sibling file: one representation
        # per layer, same `{"hooks": {…}}` shape Claude Code nests in its
        # settings.json, so the installer needs no second writer.
        user_settings=cx / "hooks.json",
        project_settings_relpath=Path(".codex") / "hooks.json",
        project_mcp_config_relpath=Path(".codex") / "config.toml",
        project_mcp_caveat="honoured for trusted projects only",
        project_plugins_relpath=Path(".codex") / "plugins",
        pause_marker=cx / "thinkweave_paused.json",
        memory_projects_root=cx / "projects",
        memory_global_dir=cx / "memory",
        cli_bin="codex",
        model_flag="--model",
        prompt_flag="",
        exec_subcommand="exec",
        # Upstream codex#24135: headless MCP tool approval currently requires
        # the full bypass, so an unattended run has no narrower option.
        bypass_permissions_flag="--dangerously-bypass-approvals-and-sandbox",
        hooks_bypass_flag="--dangerously-bypass-hook-trust",
    )


PROFILES: dict[str, Callable[[], HarnessProfile]] = {
    "claude-code": claude_code,
    "codex": codex,
}

#: In-process override. ``None`` means "derive from the environment".
_OVERRIDE: HarnessProfile | None = None


def _build(name: str, source: str) -> HarnessProfile:
    """Look the profile up, or exit naming the valid ones.

    An unknown name exits rather than falling back to Claude Code — a mis-set
    harness must not quietly write into the wrong home. It exits with a named
    remedy rather than raising, because every consumer calls :func:`active`: a
    typo would otherwise surface as a bare traceback from whichever module
    happened to look first.
    """
    factory = PROFILES.get(name)
    if factory is None:
        print(
            f"error: unknown harness {name!r} in {source}.\n"
            f"Registered profiles: {', '.join(sorted(PROFILES))}.",
            file=sys.stderr,
        )
        sys.exit(1)
    return factory()


def select(name: str) -> None:
    """Pin the active profile for this process — what ``--harness`` calls."""
    global _OVERRIDE
    _OVERRIDE = _build(name, "--harness")


def active() -> HarnessProfile:
    """The profile every consumer reads."""
    if _OVERRIDE is not None:
        return _OVERRIDE
    return _build(
        os.environ.get("THINKWEAVE_HARNESS") or "claude-code", "$THINKWEAVE_HARNESS"
    )
