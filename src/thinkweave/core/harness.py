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

import importlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from thinkweave.core.plugin_route import PLUGIN_NAME, namespace_prompt, plugin_namespace

# What the SessionStart hook asks `build_project_context` for. Lives here so a
# harness that has to be *told* the payload's size (`additional_context_limit`)
# derives its cap from the same number the handler spends — raising the budget
# used to leave the cap behind, silently, and the payload spilled to a temp
# file with no error (#107).
SESSION_START_BUDGET_TOKENS = 10_000

#: The canonical lifecycle vocabulary — Claude Code's, exactly the events
#: authored in ``hooks/hooks.json`` (the conformance suite pins them equal).
#: E3 shims translate other harnesses' native names onto these
#: (dec-5a076384); ``operations.hook_events`` is the swap.
CANONICAL_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop")

_NUDGE = (
    "If `weave_*` MCP tools are available, thinkweave (Obsidian-native memory "
    "layer) is your durable memory for this session. Prefer `weave_search` / "
    "`weave_context` / `weave_graph` over filesystem search"
)


@dataclass(frozen=True)
class Degradation:
    """One capability this harness does not deliver, stated out loud.

    The epic's anti-goal is a capability silently faked; a profile therefore
    carries its gaps as data, rendered into install output and the generated
    HARNESSES.md matrix — never implied by an absent feature (#191).
    """

    capability: str
    mode: str
    """``documented`` — the gap has a stated manual fallback. ``refuse`` — the
    operation errors out rather than writing config that never fires."""
    note: str
    upstream_ref: str = ""
    """The evidence: an issue, a blueprint note id, or a doc section."""


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
    ``/dream`` worker topology). Harness adapters may use different invocation
    syntax while preserving the shared worker contract."""

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
    Rich extraction belongs to the harness's wrap workflow at a genuine session
    boundary; raw ``weave_extract`` is only the fallback when neither that
    workflow nor a trusted session-end hook is available.

    A ``{weave}`` placeholder is substituted at install time with the absolute
    ``bin/weave`` launcher path (``install._render_claude_md_block``): bare
    ``weave`` only resolves inside Claude Code's plugin shell, so a CLI
    fallback the block names must carry a path any harness shell can run —
    and every fallback it names is conformance-pinned against the real
    argparse, because documented fallback commands rot (review r3)."""

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

    hook_windows_command_key: str = ""
    """Key carrying a Windows-only override of a hook entry's ``command``, or
    empty when the harness has no such concept.

    Codex documents one: *"``commandWindows`` is an optional Windows-only command
    override. In TOML, use ``command_windows`` or ``commandWindows``."* thinkweave
    writes ``hooks.json``, so the camelCase spelling is the one used. Claude Code
    has **no** equivalent — verified against the shipped 2026-07-25 ``claude.exe``:
    zero occurrences of ``commandWindows``, against 49 for ``UserPromptSubmit``.
    Hence a per-profile field rather than a shared constant; writing the key on a
    harness that ignores it is the "config that parses and never fires" failure
    this module exists to prevent.

    The override is per hook entry and sits beside ``command``, so ``command``
    keeps the POSIX launcher for WSL/Linux and this carries the ``.cmd`` one."""

    display_name: str = ""
    """The harness's name as a human writes it ("Claude Code"), for messages
    that used to hardcode it. Set it on any profile whose messages reach a
    user — the ``id`` is a slug and reads as a typo in prose."""

    ships_skills: bool = False
    """thinkweave ships slash-command skills (``/onboard``, ``/wrap``, …) for
    this harness, so post-install instructions may tell the user to run one.

    False for Codex's raw installer route: the full Codex-native bundle lives
    under ``skills/`` and is discovered through the plugin, while ``weave
    install`` does not export it. Post-install instructions must not claim a
    skill was installed. ``weave install`` printed "3. /onboard" to every harness
    regardless — a next step a Codex user could not take, on the one screen whose
    whole job is telling them what to do next. Distinct from
    :attr:`headless_slash`, which is about one-shot *invocation* rather than
    whether the skills exist at all; flip this when the export lands.

    Pi keeps it False as well, deliberately: its skills ARE exported (as
    root-file links, :attr:`root_file_skills`), but ``/onboard``'s contract
    is Claude-Code-shaped (hook wiring without ``--harness``, Claude Code
    history import), so the next-steps screen lists the CLI equivalents and
    names only ``/skill:wrap``."""

    # --- dispatch-contract data (#191, dec-5a076384) ----------------------
    # The flat fields above predate the re-scope and group as the decision's
    # install/runtime sub-records in spirit; folding them into nested records
    # is rename-only churn across every consumer, deliberately not paid.

    eligibility: str = "E0"
    """Where this harness sits on the ladder (dec-5a076384): ``E0`` steerable
    (skills dir + instructions file + rendered degradations — an OFFICIAL
    supported tier per dec-2fa074a0), ``E1`` headless worker, ``E2``
    dispatcher, ``E3`` captured (lifecycle hooks feed the vault)."""

    detect_dir: Path | None = None
    """The directory whose existence signals this harness is installed on the
    machine — what an auto-detector probes, and what ``weave doctor`` can name."""

    hook_mechanism: str = "none"
    """How lifecycle hooks reach this harness: ``plugin`` (an active plugin
    manifest owns registration, settings files are swept — Claude Code),
    ``file`` (the installer writes the settings file — Codex), ``extension``
    (the installer writes a one-line loader stub into the harness's
    extensions dir that re-exports the repo's shim module,
    :attr:`hook_extension_source` — Pi), ``none`` (no hook path exists or
    none is shipped yet; capture degrades to explicit ``/wrap``-style
    invocation). Must agree with :attr:`hooks`."""

    hook_extension_source: str = ""
    """Repo-relative path of the shim module an ``extension``-mechanism
    harness loads (``shims/pi/thinkweave-pi.ts``). The installed stub is one
    re-export line carrying the absolute repo path — the only
    machine-state artifact, so a reinstall rewrites the stub and nothing
    else. Empty on every other mechanism."""

    hook_events: dict[str, str | None] = field(default_factory=dict)
    """Canonical event name → this harness's native one (None: no native
    equivalent is *verified* to exist — refusing to guess is the lesson of
    claude-mem's silently-dead OpenCode plugin, #2462). Claude Code's
    vocabulary is canonical; ``operations.hook_events`` does the swapping."""

    fires_verified: dict[str, str] = field(default_factory=dict)
    """Canonical event → ISO date a real run was *observed* firing it. Absent
    key = wired but unobserved (Pi's events until the shim probe lands a
    dated capture), never assumed. Codex's ``Stop`` sat here for a month
    until the 2026-09-07 raw capture dated it — that is the bar."""

    context_channel: str = ""
    """How the SessionStart payload reaches the model: ``additionalContext``
    (hook reply field), ``context-injection`` (Pi's context-handler message
    prepend), ``message-transform`` (OpenCode's transform hook)."""

    transcript_glob: str = ""
    """Absolute glob over this harness's on-disk session transcripts."""

    transcript_format: str = ""
    """Open enum tag naming the transcript's shape. Format-plural on purpose —
    #199's SQLite-backed store is one more tag, not a rewrite."""

    transcript_parser: str = ""
    """Entry point of this format's parser as ``module:callable``, or empty
    while no importer exists (then a ``transcript``/``import`` degradation
    must say so — conformance-enforced). A dotted path rather than an import:
    the parsers live in ``onboarding``/``acquisition``, which the package-edge
    contract keeps out of every ranked package's import graph, and the profile
    stays pure data either way. Resolve via :meth:`load_transcript_parser`;
    the conformance suite pins the exact modules these strings may reach."""

    transcript_importer: str = ""
    """Entry point of the batch importer behind ``weave import <id>``, as
    ``module:callable`` taking ``(cfg, sessions_root=…, **filters)``. Empty
    while none exists — the install next-steps then must not name the
    command. Resolve via :meth:`load_transcript_importer`."""

    evidence: str = ""
    """Provenance of this row's facts, rendered into the generated matrix:
    measured against a live harness (say when/where) or declared from a
    blueprint note (say which, and that it is unverified). Facts presented in
    the same register regardless of provenance are the quiet cousin of a
    capability silently faked."""

    context_served_source: str = "startup"
    """``context_served.source`` value for this harness's SessionStart
    payload. Codex delivers the payload under a materially different contract
    (visible developer message, spill cap) so it logs distinctly; anything
    else pools into plain ``startup``. Values must stay inside the closed
    CHECK in ``core/indexer.py`` — a NEW distinct value needs that table's
    drop+recreate migration, not just a row edit here."""

    session_id_scheme: str = ""
    """How the harness mints session ids, for importers and dedup keys."""

    session_id_env: str = ""
    """Environment variable the harness exports carrying the current session's
    id — ``CLAUDE_SESSION_ID`` on Claude Code, ``PI_SESSION_ID`` on Pi. Empty
    when the harness sets none: Codex passes ``session_id`` only as a hook
    *payload* field (docs/HARNESSES.md §Codex Q4), never an env var, and
    OpenCode has none either. This is what a wrap running as a *model turn*
    reads to land on the note the hooks already created: there is no
    ``--harness`` argv in that turn, and ``$THINKWEAVE_HARNESS`` is usually
    unset, so the env var each harness exports is the actual signal. Resolved
    harness-neutrally by ``weave session-id``; a harness that declares none
    falls back to recency + the #209 identity guard rather than minting a
    fresh slug for a live session that already has a note."""

    native_memory_artifact: Path | None = None
    """The on-disk memory corpus the seam reconciles, or None. Must agree
    with :attr:`native_memory` — the seam gates on a *declared artifact*, not
    a bare boolean promise (#109)."""

    harness_flag: str = ""
    """Argv suffix telling a ``weave`` subprocess which harness drives it
    (``--harness codex``). Empty on Claude Code only: it is the vocabulary's
    authored shape, and its hook commands stay byte-identical to
    ``hooks/hooks.json`` so the plugin route keeps agreeing with the
    installer (docs/HARNESSES.md §"Why the handler reads argv")."""

    windows_cli_shim: bool = False
    """This harness's sandbox cannot reach the repo venv's ``weave`` on
    native Windows, so ``weave install`` writes a machine-scope ``weave.cmd``
    launcher for it (Codex; Claude Code resolves through Git Bash)."""

    mcp_servers_key: str = ""
    """The key its MCP config nests server entries under. Usually implied by
    the file format (``mcpServers``/``mcp_servers``) — OpenCode is the
    counterexample that makes this a profile fact: JSON file, ``mcp`` key."""

    mcp_entry_shape: str = "command-args"
    """Which documented body ``mcp_config.canonical`` renders the server
    entry into — one of ``mcp_config.ENTRY_SHAPES``.

    ``command-args`` — Claude Code's authored split shape (``type: stdio``,
    ``command`` string, ``args`` list, ``env`` map). Pi's MCP client is the
    ``pi-mcp-adapter`` extension, which reads the same standard ``.mcp.json``
    shape and accepts any record as a server entry (``isServerEntry`` is
    ``isRecord`` in its ``config.ts``, 2.32.1) — the ``type`` key is simply
    ignored, transport being inferred from ``command`` vs ``url``. Codex's
    TOML differs only by the format-level trims the writer already applies.
    ``argv-array`` — OpenCode's documented
    ``mcp`` body (opencode.ai/docs/mcp-servers/, fetched 2026-08-24 into
    blueprint n-767d66b4 §4): ``type: local``, launcher and argv merged into
    ONE ``command`` array, optional ``environment`` map. A harness's own
    published schema is a truth source for declared profile data
    (dec-2fa074a0, owner override 2026-08-29); whether the written entry
    PARSES on a live install stays owed to #114/#195."""

    mcp_via_cli: str = ""
    """The harness-native registration command (``claude mcp add`` /
    ``codex mcp add``) when one exists — preferred over hand-splicing where
    the writer does not already reproduce its output byte-for-byte."""

    legacy_mcp_config: Path | None = None
    """A file an EARLIER version of this row wrote the registration into and
    the harness never read (Pi's ``settings.json`` ``mcpServers`` key —
    parsed, silently ignored, falsified live 2026-09-03, n-fb74c7d0).
    ``weave install`` and ``weave uninstall`` both sweep the thinkweave entry
    out of it, so a dead block does not outlive the row that wrote it. None
    when the location never moved."""

    mcp_client_package: str = ""
    """The harness ships NO MCP client of its own; this names the package
    (its own package-manager vocabulary, e.g. an npm name) that supplies one
    as an extension. ``weave doctor --mcp`` then adds a check that the
    package is installed — a registration nobody reads is the silent
    failure this module exists to prevent. Empty when the client is
    built in."""

    mcp_client_install_cmd: str = ""
    """The one-line remedy the doctor prints when :attr:`mcp_client_package`
    is missing (``pi install npm:pi-mcp-adapter``)."""

    mcp_entry_extras: dict[str, object] = field(default_factory=dict)
    """Client-specific keys merged onto the canonical server entry after
    :func:`mcp_config.canonical` shapes it — options the harness's MCP
    client documents beside ``command``/``args`` (pi-mcp-adapter's
    ``lifecycle`` / ``directTools`` / ``toolPrefix``). Empty for a client
    that needs none. The doctor's fingerprint ignores them: they configure
    how the client exposes the server, not what it launches."""

    packages_root: Path | None = None
    """Where the harness unpacks installed packages (Pi: ``~/.pi/agent/npm/
    node_modules``), so the doctor can corroborate a settings entry against
    the files on disk. None when no such directory is documented."""

    root_file_skills: bool = False
    """The harness discovers root-level ``*.md`` files in :attr:`skills_dir`
    as individual skills when they carry ``name`` + ``description``
    frontmatter (Pi docs/skills.md §Locations). ``weave install`` then links
    every worker-less ``commands/<name>.md`` there by its own name —
    pointing at the canonical contract, NOT at the Codex ``skills/`` bundle:
    a Codex projection says "read ../../docs/CODEX-SKILL-PROJECTION.md" and
    resolves that path from the skill's directory, which under
    ``~/.pi/agent/skills`` is nothing (the 2026-09-05 breakage that
    motivated the flag). Worker-backed commands are skipped when
    :attr:`subagents` is False — there is nothing to dispatch them onto."""

    skill_prefix: str = "/"
    """How a user invokes a shipped skill by name: ``/`` on Claude Code,
    ``/skill:`` on Pi (prompt expansion, no Skill tool). Used wherever
    post-install text names one."""

    degradations: tuple[Degradation, ...] = ()

    @property
    def dev_link(self) -> Path:
        """Where ``weave dev-link`` symlinks a checkout so the harness loads it
        as a plugin."""
        return self.skills_dir / PLUGIN_NAME

    @property
    def transcript_root(self) -> Path:
        """The static directory prefix of :attr:`transcript_glob` — where a
        walker starts. Derived, so glob and root cannot drift apart."""
        return Path(self.transcript_glob.split("*", 1)[0].rstrip("/\\"))

    @staticmethod
    def _load_entry(ref: str) -> Callable | None:
        if not ref:
            return None
        module, name = ref.split(":")
        return getattr(importlib.import_module(module), name)

    def load_transcript_parser(self) -> Callable[[Path], object] | None:
        """Resolve :attr:`transcript_parser`, or None when no parser exists."""
        return self._load_entry(self.transcript_parser)

    def load_transcript_importer(self) -> Callable | None:
        """Resolve :attr:`transcript_importer`, or None when none exists."""
        return self._load_entry(self.transcript_importer)

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
        ships_skills=True,
        eligibility="E3",
        detect_dir=cc,
        hook_mechanism="plugin",
        hook_events={e: e for e in CANONICAL_EVENTS},
        # Literal per-event entries, not a comprehension: each date is one
        # recorded observation (here: all four seen firing in live sessions
        # on the dev machine on 2026-08-29) and future re-verifications must
        # be able to move independently.
        fires_verified={
            "SessionStart": "2026-08-29",
            "UserPromptSubmit": "2026-08-29",
            "PostToolUse": "2026-08-29",
            "Stop": "2026-08-29",
        },
        context_channel="additionalContext",
        transcript_glob=str(cc / "projects" / "*" / "*.jsonl"),
        transcript_format="jsonl-flat",
        transcript_parser="thinkweave.onboarding.claude_code_seed:parse_session",
        transcript_importer="thinkweave.onboarding.claude_code_seed:import_claude_code",
        session_id_scheme="uuid4",
        session_id_env="CLAUDE_SESSION_ID",
        native_memory_artifact=cc / "projects",
        mcp_servers_key="mcpServers",
        mcp_via_cli="claude mcp add",
        evidence="measured — daily live use on the dev machine; suite drives the handler end-to-end",
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
        # Interactive skills project their shared worker contracts into
        # Codex's native spawn_agent/followup_task/wait_agent topology. The
        # separate headless executor remains #110's concern.
        subagents=True,
        # Codex keeps sessions (imported by acquisition/importers/codex.py) and
        # a memories sqlite, but no markdown auto-memory corpus of the shape the
        # seam reconciles — so the seam walk correctly yields nothing.
        native_memory=False,
        # `codex exec` resolves no slash commands; skill tokens stay bare.
        headless_slash=False,
        instructions_file=cx / "AGENTS.md",
        # #107 gave this harness hooks, but they are trust-gated. Routine thin
        # capture belongs to Stop; rich synthesis belongs to the wrap skill.
        # Keep raw extraction as a boundary-only fallback, never an ambient
        # mid-session obligation.
        instructions_block_body=(
            f"{_NUDGE}. Do not call `weave_extract` during ordinary or "
            "mid-session work. Routine capture belongs to the trusted "
            "session-end hook; rich insight and decision synthesis belongs to "
            "`$thinkweave-wrap`, used once at a genuine session boundary. If "
            "that skill is unavailable and session-end hooks are not installed "
            "and trusted, call `weave_extract` once at the boundary as the "
            "fallback."
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
        # Codex is the only harness with a Windows command override. Unlike
        # Claude Code it does not resolve hook commands through Git Bash, so
        # without this a Windows Codex user's hooks get a `#!/bin/sh` script
        # handed to cmd.exe.
        hook_windows_command_key="commandWindows",
        eligibility="E3",
        detect_dir=cx,
        hook_mechanism="file",
        hook_events={e: e for e in CANONICAL_EVENTS},
        # SessionStart/UserPromptSubmit: observed live on 2026-09-05 (two
        # interactive, authenticated codex-cli 0.146.0 sessions; first seen
        # in the 2026-08-02 credential-less spike). PostToolUse/Stop: raw
        # envelopes captured on 2026-09-07 by a sentinel hook teeing stdin
        # in two headless `codex exec` sessions (exec `Bash` incl. a 20 s
        # command, MCP `weave_search`, Stop with `last_assistant_message`,
        # SessionEnd ~2 s later) — fixture
        # tests/fixtures/harness_envelopes/codex/envelopes-2026-09-07.jsonl,
        # docs/HARNESSES.md §"2026-09-07 instrumented headless run".
        fires_verified={
            "SessionStart": "2026-09-05",
            "UserPromptSubmit": "2026-09-05",
            "PostToolUse": "2026-09-07",
            "Stop": "2026-09-07",
        },
        context_channel="additionalContext",
        transcript_glob=str(
            cx / "sessions" / "*" / "*" / "*" / "rollout-*.jsonl"
        ),
        transcript_format="jsonl-rollout",
        transcript_parser="thinkweave.acquisition.importers.codex:parse_rollout",
        transcript_importer="thinkweave.acquisition.importers.codex:import_codex",
        session_id_scheme="uuid7",
        # session_id_env stays empty: an env-dumping SessionStart hook saw no
        # CODEX_SESSION_ID or equivalent (docs/HARNESSES.md §Codex Q4) — Codex
        # delivers session_id only as a hook payload field, which a wrap model
        # turn cannot read, so wrap falls back to recency + the #209 guard.
        harness_flag="--harness codex",
        windows_cli_shim=True,
        mcp_servers_key="mcp_servers",
        mcp_via_cli="codex mcp add",
        context_served_source="codex-startup",
        evidence=(
            "measured — codex-cli 0.146.0: credential-less spike 2026-08-02, "
            "two live interactive sessions 2026-09-05, two instrumented "
            "headless sessions 2026-09-07 with every envelope captured raw "
            "(docs/HARNESSES.md)"
        ),
        degradations=(
            Degradation(
                "Stop capture",
                "documented",
                "fires at every turn end — measured interactively 2026-09-05 "
                "(hook/started at each task_complete) and headless 2026-09-07 "
                "(raw envelope with last_assistant_message; SessionEnd ~2 s "
                "later, which thinkweave does not hook). The first Stop "
                "materialises the note and later turns fold in place. Still "
                "unmeasured: whether an interactive TUI exit delivers a "
                "final Stop of its own beyond the last turn's, so a rich "
                "end-of-session capture in the TUI still rides "
                "`$thinkweave-wrap`",
                "docs/HARNESSES.md §2026-09-07 instrumented headless run",
            ),
            Degradation(
                "SessionStart context delivery",
                "documented",
                "additionalContext renders as a visible developer message, "
                "not a silent system one",
                "openai/codex#16933",
            ),
            Degradation(
                "headless skill invocation",
                "documented",
                "codex exec resolves no slash commands; a $name mention is a "
                "hint the model acts on by reading the skill file itself",
                "docs/HARNESSES.md §Q2",
            ),
        ),
    )


def pi(home: Path | None = None) -> HarnessProfile:
    """Pi (badlogic/pi-mono) — an E3 (captured) row.

    Blueprint n-a1d3beba (2026-08-24) distilled the facts; two live rounds
    against Pi 0.84.4 then measured them: the 2026-09-03 trial verified the
    E0 floor (AGENTS.md read and acted on, CLI fallback, universal skills
    dir) and FALSIFIED the settings-route MCP claim (n-fb74c7d0), and the
    2026-09-05 events probe observed every native event in ``hook_events``
    firing in a headless run. Lifecycle capture rides the extension shim
    (``shims/pi/thinkweave-pi.ts``, #114) on the shim-core kernel; the
    ``hook_events`` map is the translation table the shim and the
    conformance suite share.

    MCP is not Pi's: the community ``pi-mcp-adapter`` extension supplies
    the client and reads the standard ``mcpServers`` JSON from (lowest to
    highest precedence) ``~/.config/mcp/mcp.json``, ``~/.agents/mcp.json``,
    ``~/.pi/agent/mcp.json``, project ``.mcp.json``, project
    ``.pi/mcp.json`` (its README, 2.32.1). The row registers into the
    Pi-global file and reads the project ``.mcp.json`` this repo commits.

    Skills: Pi discovers root ``*.md`` files in ``~/.pi/agent/skills`` as
    individual skills (docs/skills.md §Locations) and expands
    ``/skill:<name>`` as a prompt. The installer links the canonical
    ``commands/*.md`` there directly — the Codex ``skills/`` bundle must
    NOT be what Pi gets, its relative ``../../docs`` pointer breaks from
    the Pi skills dir.
    """
    h = home or Path.home()
    agent = h / ".pi" / "agent"
    return HarnessProfile(
        id="pi",
        display_name="Pi",
        hooks=True,
        subagents=False,
        native_memory=False,
        headless_slash=False,
        instructions_file=agent / "AGENTS.md",
        # E3 posture: the extension shim captures passively, so the block's
        # job is to keep the model OUT of the capture path — no per-turn
        # weave_extract — and to name the one explicit boundary skill. Not
        # built on _NUDGE: its "if available" hedge is wrong here (the tools
        # are served, by a named extension), and the text is the one
        # live-verified on the dev machine's AGENTS.md (2026-09-05).
        instructions_block_body=(
            "Thinkweave (Obsidian-native memory layer) is your durable "
            "memory. Its `weave_*` tools are served through the "
            "pi-mcp-adapter extension. Prefer `weave_search` / "
            "`weave_context` / `weave_graph` over filesystem search; never "
            "crawl the filesystem with find/grep/ls looking for thinkweave "
            "or vault files. Lifecycle hooks capture this session passively "
            "(context at start, prompts, tool events, session note at stop). "
            "NEVER call `weave_extract` mid-session or per turn; "
            "end-of-session extraction is the explicit `/skill:wrap`. If "
            "`weave_*` tools are absent, fall back to the CLI: "
            "`{weave} add <title> -t note -p <project> -b <body>` persists "
            "a note and `{weave} search <query>` retrieves."
        ),
        # The adapter's Pi-global file. settings.json stays the home of
        # `packages` (installed_plugins) and any hook-ish settings, and is
        # swept as the LEGACY registration location: the pre-2026-09-05 row
        # wrote a `mcpServers` block there that Pi core parses and ignores.
        mcp_config=agent / "mcp.json",
        legacy_mcp_config=agent / "settings.json",
        skills_dir=agent / "skills",
        plugins_root=agent / "extensions",
        plugins_cache=agent / "extensions" / "cache",
        installed_plugins=agent / "settings.json",
        plugin_manifest_relpath=Path("package.json"),
        user_settings=agent / "settings.json",
        project_settings_relpath=Path(".pi") / "settings.json",
        project_mcp_config_relpath=Path(".mcp.json"),
        project_plugins_relpath=Path(".pi") / "extensions",
        packages_root=agent / "npm" / "node_modules",
        mcp_client_package="pi-mcp-adapter",
        mcp_client_install_cmd="pi install npm:pi-mcp-adapter",
        # Adapter-only keys (types.ts ServerEntry, 2.32.1): without
        # `directTools` the server hides behind one `mcp` proxy tool, and with
        # it the tools come out as `thinkweave_weave_search` unless
        # `toolPrefix: none` — the skills name bare `weave_*`. `eager`
        # because SessionStart already spawns the handler; a lazy server
        # would add its cold start to the first retrieval instead.
        mcp_entry_extras={
            "lifecycle": "eager",
            "directTools": True,
            "toolPrefix": "none",
        },
        root_file_skills=True,
        skill_prefix="/skill:",
        pause_marker=agent / "thinkweave_paused.json",
        memory_projects_root=agent / "projects",
        memory_global_dir=agent / "memory",
        cli_bin="pi",
        model_flag="--model",
        prompt_flag="-p",
        bypass_permissions_flag="",
        hooks_install_caveat=(
            "\n  The extension loads on the next Pi session; sessions already "
            "running keep\n  going without it."
        ),
        eligibility="E3",
        detect_dir=h / ".pi",
        hook_mechanism="extension",
        hook_extension_source="shims/pi/thinkweave-pi.ts",
        hook_events={
            "SessionStart": "session_start",
            "UserPromptSubmit": "before_agent_start",
            "PostToolUse": "tool_result",
            "Stop": "agent_end",
        },
        context_channel="context-injection",
        transcript_glob=str(agent / "sessions" / "*" / "*.jsonl"),
        transcript_format="jsonl-tree",
        transcript_parser="thinkweave.acquisition.importers.pi:parse_session",
        transcript_importer="thinkweave.acquisition.importers.pi:import_pi",
        session_id_scheme="uuid (session-header id)",
        # Live env dump 2026-09-05: Pi exports PI_SESSION_ID (the session uuid,
        # what the shim also stamps as source_session) and PI_SESSION_FILE.
        session_id_env="PI_SESSION_ID",
        harness_flag="--harness pi",
        mcp_servers_key="mcpServers",
        evidence=(
            "measured — Pi 0.84.4: live trial 2026-09-03 (E0 floor verified, "
            "settings-MCP falsified, n-fb74c7d0), headless events probe "
            "2026-09-05 (all four native events fired), and an interactive "
            "session 2026-09-05 through pi-mcp-adapter 2.32.1 (17 bare-named "
            "weave_* tools, direct calls, /skill:wrap end-to-end on a "
            "hook-captured session)"
        ),
        # Headless events probe 2026-09-05 (all four; SessionStart injection
        # quoted real note ids back) + interactive session 2026-09-05 (prompt
        # events archived, Stop materialised the folder /skill:wrap extended).
        fires_verified={
            "SessionStart": "2026-09-05",
            "UserPromptSubmit": "2026-09-05",
            "PostToolUse": "2026-09-05",
            "Stop": "2026-09-05",
        },
        degradations=(
            Degradation(
                "hook latency",
                "documented",
                "shim-core's 800 ms telemetry budget sits below this "
                "launcher's floor on the dev machine (a no-op Stop is ~1.8 s "
                "warm, ~6 s under load — WSL2, vault on /mnt/c), so the shim "
                "carries per-event budgets (UserPromptSubmit 2.5 s, Stop 6 s); "
                "a hook that still outlives its budget finishes as shim-core's "
                "documented orphan — capture is complete, Pi shows one "
                "'hook timeout' notice per event per session, and only the "
                "prompt-time enrichment block that reply would have carried "
                "is lost",
                "#114, docs/HARNESSES.md §Pi",
            ),
            Degradation(
                "MCP registration",
                "documented",
                "Pi core ships no MCP client — a settings.json mcpServers "
                "block parses and is silently ignored (falsified live on "
                "0.84.4, 2026-09-03). The registration is served through the "
                "community pi-mcp-adapter extension instead: `weave install "
                "--harness pi` writes the standard mcpServers block (plus "
                "lifecycle/directTools/toolPrefix) to ~/.pi/agent/mcp.json, "
                "the adapter also reads the project .mcp.json, and `weave "
                "doctor --mcp --harness pi` fails with `pi install "
                "npm:pi-mcp-adapter` when the package is absent; the CLI "
                "fallback in the instructions block covers a session where "
                "the tools still did not load",
                "#114, n-fb74c7d0",
            ),
            Degradation(
                "subagent fan-out",
                "documented",
                "Pi ships no first-party subagent tool, so the /drain and "
                "/dream worker topology has nothing to dispatch onto",
                "n-a1d3beba §2",
            ),
            Degradation(
                "skill invocation",
                "documented",
                "no Skill tool — /skill:<name> is prompt expansion. Skills "
                "are root-file links `weave install --harness pi` creates in "
                "~/.pi/agent/skills, one <name>.md per canonical "
                "commands/*.md; worker-backed commands (/drain, /dream, "
                "/news, /newsletter, /podcast, /youtube, /seed-enrich, …) are "
                "not linked because Pi has no subagents to run them",
                "Pi docs/skills.md §Locations",
            ),
        ),
    )


def opencode(home: Path | None = None) -> HarnessProfile:
    """OpenCode (sst/opencode) — an official E0 (steerable) row.

    Distilled from the evidence blueprint n-767d66b4 (2026-08-24: upstream
    docs + three working plugins read in code). Not measured against a live
    install; the plugin shim is #195. XDG env overrides of the config/data
    homes are deliberately ignored here — the profile takes one ``home`` knob,
    like every other row.
    """
    h = home or Path.home()
    cfg = h / ".config" / "opencode"
    data = h / ".local" / "share" / "opencode"
    return HarnessProfile(
        id="opencode",
        display_name="OpenCode",
        hooks=False,
        subagents=False,
        native_memory=False,
        headless_slash=False,
        instructions_file=cfg / "AGENTS.md",
        instructions_block_body=(
            f"{_NUDGE}. This harness fires no thinkweave lifecycle hooks, so "
            "call `weave_extract` yourself before you finish — it is what "
            "persists the session's insights and decisions into the vault. "
            "If the `weave_*` tools did not load, fall back to the CLI: "
            "`{weave} add <title> -t note -p <project> -b <body>` persists "
            "a note (`-t decision` for a decision) and "
            "`{weave} search <query>` retrieves."
        ),
        # NB: mcp_config, user_settings and installed_plugins all resolve to
        # this one opencode.json — same merge-never-regenerate constraint as
        # the Pi row when #195 flips hooks on.
        mcp_config=cfg / "opencode.json",
        skills_dir=cfg / "skills",
        plugins_root=cfg / "plugins",
        plugins_cache=h / ".cache" / "opencode" / "node_modules",
        installed_plugins=cfg / "opencode.json",
        plugin_manifest_relpath=Path("package.json"),
        user_settings=cfg / "opencode.json",
        project_settings_relpath=Path("opencode.json"),
        project_mcp_config_relpath=Path("opencode.json"),
        project_plugins_relpath=Path(".opencode") / "plugins",
        pause_marker=cfg / "thinkweave_paused.json",
        memory_projects_root=cfg / "projects",
        memory_global_dir=cfg / "memory",
        cli_bin="opencode",
        model_flag="--model",
        prompt_flag="",
        exec_subcommand="run",
        bypass_permissions_flag="--auto",
        eligibility="E0",
        detect_dir=cfg,
        hook_events={
            "SessionStart": "experimental.chat.messages.transform",
            "UserPromptSubmit": "chat.message",
            "PostToolUse": "tool.execute.after",
            # Only session.idle / session.deleted are *confirmed* bus events;
            # a Stop mapping stays None until one is proven to fire.
            "Stop": None,
        },
        context_channel="message-transform",
        transcript_glob=str(data / "storage" / "session" / "*" / "*.json"),
        transcript_format="json-records",
        session_id_scheme="ses_<12-hex><14-base62> (ULID-style sortable)",
        harness_flag="--harness opencode",
        mcp_servers_key="mcp",
        mcp_entry_shape="argv-array",
        evidence="declared — blueprint n-767d66b4 (2026-08-24); NOT verified on a live install",
        degradations=(
            Degradation(
                "lifecycle hooks",
                "documented",
                "the OpenCode plugin shim is not yet shipped, so passive "
                "capture does not run; end sessions with an explicit "
                "weave_extract",
                "#195",
            ),
            Degradation(
                "MCP registration",
                "documented",
                "weave install writes OpenCode's documented schema under the "
                "`mcp` key (type local, command as one array, environment "
                "map when non-empty — opencode.ai/docs/mcp-servers/ via "
                "n-767d66b4 §4); NOT yet verified to parse on a live "
                "install — #195 owns the live verification",
                "n-767d66b4 §4",
            ),
            Degradation(
                "Stop capture",
                "documented",
                "no verified Stop-equivalent event — claude-mem's plugin "
                "subscribed to bus events that never fire and captured "
                "nothing silently; only session.idle/session.deleted are "
                "confirmed real",
                "claude-mem#2462",
            ),
            Degradation(
                "subagent fan-out",
                "documented",
                "no hook fires on subagent dispatch/completion in the docs "
                "or any reference plugin",
                "n-767d66b4 §2",
            ),
            Degradation(
                "transcript import",
                "documented",
                "sessions are per-record JSON files (session/message/part); "
                "no importer reads them yet",
                "n-767d66b4 §6",
            ),
        ),
    )


PROFILES: dict[str, Callable[..., HarnessProfile]] = {
    "claude-code": claude_code,
    "codex": codex,
    "pi": pi,
    "opencode": opencode,
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
