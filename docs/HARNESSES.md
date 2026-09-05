# Harnesses

Per-harness facts live in code, in one place: `HarnessProfile`
(`src/thinkweave/core/harness.py`). The capability matrix below is
**generated** from those profiles (#191, subsuming the hand-written half of
#105) — the conformance suite fails when it goes stale. Everything after it
is the *evidence* behind the data in the profiles — what was measured,
against which version, and what is still unknown. When the two disagree, the
profile is what runs; fix whichever is wrong.

<!-- weave:harness-matrix:start — GENERATED from core/harness.py profiles; edit the profile, then `uv run python -m thinkweave.core.harness_docs --write` -->

## Capability matrix

| | Claude Code | Codex | Pi | OpenCode |
|---|---|---|---|---|
| evidence | measured — daily live use on the dev machine; suite drives the handler end-to-end | measured — codex-cli 0.146.0 spike, 2026-08-02 (docs/HARNESSES.md) | declared — blueprint n-a1d3beba (2026-08-24); NOT verified on a live install | declared — blueprint n-767d66b4 (2026-08-24); NOT verified on a live install |
| eligibility (dec-5a076384 ladder) | E3 | E3 | E0 | E0 |
| detected by | `~/.claude` | `~/.codex` | `~/.pi` | `~/.config/opencode` |
| lifecycle hooks | plugin | file | none | none |
| subagent fan-out | yes | yes | no | no |
| headless slash skills | yes | no | no | no |
| native memory seam | `~/.claude/projects` | — | — | — |
| context channel | `additionalContext` | `additionalContext` | `context-injection` | `message-transform` |
| dispatch | `claude -p <prompt>` | `codex exec <prompt>` | `pi -p <prompt>` | `opencode run <prompt>` |
| transcripts | `~/.claude/projects/*/*.jsonl` (jsonl-flat) | `~/.codex/sessions/*/*/*/rollout-*.jsonl` (jsonl-rollout) | `~/.pi/agent/sessions/*/*.jsonl` (jsonl-tree) | `~/.local/share/opencode/storage/session/*/*.json` (json-records) |
| session ids | `uuid4` | `uuid7` | `uuid (session-header id)` | `ses_<12-hex><14-base62> (ULID-style sortable)` |
| MCP config | `~/.claude.json` · key `mcpServers` | `~/.codex/config.toml` · key `mcp_servers` | `~/.pi/agent/settings.json` · key `mcpServers` | `~/.config/opencode/opencode.json` · key `mcp` |
| MCP native CLI | `claude mcp add` | `codex mcp add` | — | — |
| instructions file | `~/.claude/CLAUDE.md` | `~/.codex/AGENTS.md` | `~/.pi/agent/AGENTS.md` | `~/.config/opencode/AGENTS.md` |
| skills dir | `~/.claude/skills` | `~/.codex/skills` | `~/.pi/agent/skills` | `~/.config/opencode/skills` |

### Hook events (canonical → native, with observed-fire dates)

| canonical | Claude Code | Codex | Pi | OpenCode |
|---|---|---|---|---|
| SessionStart | ✓ 2026-08-29 | ✓ 2026-08-02 | `session_start` (declared) | `experimental.chat.messages.transform` (declared) |
| UserPromptSubmit | ✓ 2026-08-29 | ✓ 2026-08-02 | `before_agent_start` (declared) | `chat.message` (declared) |
| PostToolUse | ✓ 2026-08-29 | wired, unverified | `tool_result` (declared) | `tool.execute.after` (declared) |
| Stop | ✓ 2026-08-29 | wired, unverified | `agent_end` (declared) | — (no verified equivalent) |

### Documented degradations

Nothing below is silently faked (#103 anti-goal): a listed capability
degrades exactly as stated. On a **measured** row (see the evidence
row above) everything unlisted works as on Claude Code; on a
**declared** row, unlisted means *not yet checked*, not *works*.

#### Claude Code

None — the reference harness.

#### Codex

- **Stop capture** — documented: wired but unobserved on a live run — the 2026-08-02 spike aborted at auth before any turn completed; SessionEnd did fire and is the fallback if Stop proves unreliable headlessly (docs/HARNESSES.md §Spike answers)
- **SessionStart context delivery** — documented: additionalContext renders as a visible developer message, not a silent system one (openai/codex#16933)
- **headless skill invocation** — documented: codex exec resolves no slash commands; a $name mention is a hint the model acts on by reading the skill file itself (docs/HARNESSES.md §Q2)

#### Pi

- **lifecycle hooks** — documented: the Pi extension shim is not yet shipped, so passive capture does not run; end sessions with an explicit weave_extract (#114)
- **MCP registration** — documented: the written entry follows Pi's documented mcpServers block (command string + args list + env map, n-a1d3beba §4) apart from an extra `type: stdio` key Pi's field list does not name; NOT yet verified to parse on a live install — #114 owns the live verification (n-a1d3beba §4)
- **subagent fan-out** — documented: Pi ships no first-party subagent tool, so the /drain and /dream worker topology has nothing to dispatch onto (n-a1d3beba §2)
- **skill invocation** — documented: no Skill tool — /skill:name is prompt-expansion, and the bootstrap must say read-the-SKILL.md, not invoke (n-a1d3beba §4)
- **transcript import** — documented: session files are parentId trees, not flat JSONL; no importer walks them yet (n-a1d3beba §6)

#### OpenCode

- **lifecycle hooks** — documented: the OpenCode plugin shim is not yet shipped, so passive capture does not run; end sessions with an explicit weave_extract (#195)
- **MCP registration** — documented: weave install writes OpenCode's documented schema under the `mcp` key (type local, command as one array, environment map when non-empty — opencode.ai/docs/mcp-servers/ via n-767d66b4 §4); NOT yet verified to parse on a live install — #195 owns the live verification (n-767d66b4 §4)
- **Stop capture** — documented: no verified Stop-equivalent event — claude-mem's plugin subscribed to bus events that never fire and captured nothing silently; only session.idle/session.deleted are confirmed real (claude-mem#2462)
- **subagent fan-out** — documented: no hook fires on subagent dispatch/completion in the docs or any reference plugin (n-767d66b4 §2)
- **transcript import** — documented: sessions are per-record JSON files (session/message/part); no importer reads them yet (n-767d66b4 §6)

<!-- weave:harness-matrix:end -->

## Codex

All findings verified against **codex-cli 0.146.0** on **2026-08-02**, on Linux
(WSL2). Sources are labelled: `[manual]` =
<https://learn.chatgpt.com/docs/codex-manual.md> / `/docs/hooks`; `[binary]` =
strings & embedded JSON schemas extracted from the 0.146.0 executable;
`[measured]` = observed from a real CLI run against a throwaway `$CODEX_HOME`
with no credentials (every such run terminates in a 401 — no model was
invoked).

### Hooks

Codex's hook system is a close clone of Claude Code's, closer than #107's issue
text assumed. What the port actually needed was small; what it needed and the
issue did *not* predict is listed under "Deltas" below.

**Config location.** `[manual]` Hooks load from either a `hooks.json` or an
inline `[hooks]` table in a `config.toml`, in any active config layer. The four
useful spots are `~/.codex/hooks.json`, `~/.codex/config.toml`,
`<repo>/.codex/hooks.json`, `<repo>/.codex/config.toml`. "If a single layer
contains both `hooks.json` and inline `[hooks]`, Codex loads both and warns."

thinkweave writes `$CODEX_HOME/hooks.json`. #106 already owns that layer's
`config.toml` for `[mcp_servers]`, so using the sibling file keeps one
representation per layer — and `hooks.json`'s body is the same
`{"hooks": {Event: [{matcher, hooks: [...]}]}}` object Claude Code nests in its
`settings.json`, so the existing installer needed no second writer.

**Registration ownership.** Claude Code's active plugin is the sole owner when
present: it loads the committed `hooks/hooks.json`, and `weave hooks install`
writes no settings file. Instead it sweeps stale thinkweave entries out of
*every* scope it can address — machine and project both, regardless of the
`--scope` asked for — because a registration left behind in the other scope
fires alongside the plugin's and delivers every event twice (#161). On the
MCP-only/manual route the installer owns registration. Codex has no shipped
ThinkWeave plugin, so its machine-scope `hooks.json` remains installer-owned.

**Extraction ownership.** The installed AGENTS.md nudge never asks Codex to
extract during ordinary or mid-session work. A trusted Stop hook owns routine
thin capture; `$thinkweave-wrap` owns rich insight/decision synthesis once at a
genuine session boundary. Direct `weave_extract` is retained only as the
boundary fallback when the wrap skill is unavailable and the session-end hooks
are not installed and trusted.

The handler deduplicates replayed envelopes by delivery receipt, but only for
events that carry the harness's own per-delivery id (`tool_use_id`,
`turn_id`) — that is a defence against harness retry, not a second registrar.
Claude Code stamps no such id on SessionStart or UserPromptSubmit, and those
are written unconditionally: nothing on the wire separates a duplicate
delivery from the user genuinely sending the same prompt twice, or from a
resume that must re-inject context. Single-owner registration is what keeps
those from arriving twice.

**Scope.** thinkweave installs **machine scope only** (`--scope project` is
refused). `[manual]` project `.codex/` layers load only for *trusted* projects;
openai/codex#17532 additionally reports repo-local hooks not firing in
interactive sessions. Two ways to end up with config that parses and never
runs. `weave doctor --mcp` gains a `hook scope` check that flags hooks found in
either repo-local representation. The refusal is install-side only —
`weave hooks uninstall --scope project` is how you clear what that check
flags.

**Event names.** `[manual]``[binary]` Identical to Claude Code's:
`PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`,
`SessionStart`, `SessionEnd`, `SubagentStart`, `SubagentStop`,
`UserPromptSubmit`, `Stop`. All four thinkweave installs exist under the same
names — the port renames nothing.

**Matchers.** `[manual]` A regex over the tool name; `""`, `"*"`, or omitted
matches everything. MCP tools are namespaced `mcp__<server>__<tool>`, exactly
as in Claude Code — so `mcp__thinkweave__.*` needs no translation. (The issue
predicted a `<server>:<tool>` rename. **That prediction is wrong** for
0.146.0.) For `apply_patch`, `Edit` and `Write` are documented matcher aliases,
so `Write|Edit|Bash` is already a correct Codex matcher. `UserPromptSubmit` and
`Stop` ignore `matcher` entirely.

**Wire format.** `[binary]``[measured]` The stdin object and the
`hookSpecificOutput` / `additionalContext` reply are field-for-field what
`surfaces/hooks/handler.py` already spoke. A measured SessionStart envelope:

```json
{
  "session_id": "019fc43a-b029-7542-8626-884213ed5cee",
  "transcript_path": "$CODEX_HOME/sessions/2026/08/02/rollout-….jsonl",
  "cwd": "…", "hook_event_name": "SessionStart",
  "model": "gpt-5.6-sol", "permission_mode": "bypassPermissions",
  "source": "startup"
}
```

### Deltas that cost real work

1. **`apply_patch` is the only file-edit tool.** `[manual]` Codex routes every
   edit through one `apply_patch` call and reports *that* as `tool_name`;
   `Edit`/`Write` are matcher aliases that never appear in the payload. There
   is no `file_path` — the patch text arrives as `tool_input.command`, using
   `*** Add File: ` / `*** Update File:` / `*** Delete File: ` / `*** Move to: `
   markers `[binary]`. Until #107 read that envelope, `files_touched` was empty
   for every Codex session. One call can touch N files where Claude Code would
   have fired N hooks.

2. **`additionalContext` is capped at ~2500 tokens by default.** `[manual]`
   Above it Codex "spills": saves the full text to
   `<temp_dir>/hook_outputs/<session_id>/<uuid>.txt` and shows the model a
   head-and-tail preview. thinkweave's SessionStart payload is built with
   `budget_tokens=SESSION_START_BUDGET_TOKENS` (`core/harness.py`), so without
   an explicit `additionalContextLimit` the headline promise ("the session
   receives the context payload") fails *silently*. The profile derives its
   limit from that same constant — `2 *` it, since the budget is spent against
   a chars//4 estimate that note-id-dense markdown beats — and writes it on the
   two events whose handler can emit additional context; Codex logs a
   configuration warning if the key rides any other event.

3. **Hooks are trust-gated.** `[manual]``[measured]` "Before a non-managed
   command hook can run, Codex requires you to review and trust the exact hook
   definition", hashed. Measured: an identical `codex exec` run with
   `--dangerously-bypass-hook-trust` removed fired **zero hooks and printed
   nothing about it**. Writing the file is half the install, so
   `weave hooks install` prints the `/hooks` instruction, and unattended runs
   append the bypass flag. Trust is keyed to the definition, so **re-trust
   after every `weave hooks install`**.

4. **Visible-context caveat.** openai/codex#16933 — Codex currently renders
   injected `additionalContext` as a *visible developer message* rather than a
   silent system message. Combined with (2) and (3), Codex's SessionStart is a
   materially different delivery contract from Claude Code's, which is why it
   registers its own `context_served.source` value, `codex-startup`, instead of
   being pooled into `startup`. **Not yet done:** trimming the Codex-profile
   SessionStart budget for readability. The payload is delivered in full today;
   whether ~10k visible tokens is *pleasant* is unmeasured.

### Spike answers (#107)

**Q1 — Do hooks fire under `codex exec`? YES.** `[measured]` A credential-less
`codex exec --strict-config --dangerously-bypass-hook-trust --skip-git-repo-check`
run with sentinel hooks fired `SessionStart`, `UserPromptSubmit` and
`SessionEnd`, all before the 401. `--strict-config` also accepted the hook
config, so the written artifact validates.

*Still open:* `Stop` did **not** fire in that run, and neither did
`PostToolUse` — but the turn aborted at auth before any tool ran or any turn
completed, so this is not evidence that `Stop` never fires; it is simply
unobserved. Since thinkweave materialises the session note at `Stop`, **whether
a Codex cron completes its capture is unverified** and needs one authenticated
run to settle. Note `SessionEnd` *did* fire and is a plausible fallback if
`Stop` turns out unreliable headlessly.

**Q2 — Can a skill be invoked from `codex exec`? Only as a hint.** `[manual]`
Codex uses `$name` mentions, not `/name` ("ChatGPT supports `@` mentions, while
Codex supports `$` mentions for skills"). `[measured]` via
`codex debug prompt-input` (which renders the model-visible prompt without
calling a model), with a sentinel skill installed: Codex lists **every**
discovered skill as a `name: description (file: path)` catalog entry, and
passes the user prompt through **verbatim**. Neither `$sentinel` nor
`/sentinel` inlined the skill body.

So a `$wrap` token is a hint the model must act on by reading the file itself —
not harness-side expansion the way Claude Code's headless slash resolution
injects a skill body. `headless_slash=False` on the Codex profile is therefore
correct. The complete supported command surface now projects from
`commands/**/*.md` into `skills/` with explicit `thinkweave-*` names. The
adapters point back to the canonical command instead of copying its prompt.
Worker-backed skills read the shared `agents/*.md` contract and pass it in full
to Codex-native `spawn_agent`; retries and fan-in map to `followup_task` and
`wait_agent`. The raw `weave install` route still does not export this bundle;
the Codex plugin owns discovery.

`/dream` and `/drain` remain visibly degraded for unattended/headless runs
until #110 supplies the dedicated executor. Their interactive worker fan-out
is supported and discoverable; they are not omitted and do not claim cron
parity.

**Q3 — PostToolUse matcher-semantics parity? Yes, with one asterisk.**
`[manual]` Same regex-over-tool-name semantics and the same `mcp__server__tool`
namespacing, so thinkweave's matchers port verbatim. The asterisk is the tool
*vocabulary*, not the matcher engine: `Bash` covers shell and unified exec,
`apply_patch` covers all file edits, MCP tools match their full canonical name,
and other local function tools match their own name (`spawn_agent` also matches
`Agent`). Hosted tools such as `WebSearch` do **not** run the local hook path
at all. `[manual]` "Treat tool hooks as a useful guardrail, not a complete
enforcement boundary."

**Q4 (not asked, needed anyway) — Is there a session-id environment variable?
No.** `[measured]` An env-dumping SessionStart hook saw no `CODEX_SESSION_ID`
or equivalent; the only Codex variable present was `CODEX_HOME`, and it was
inherited from the invoking shell. This is a non-issue: `session_id` is a
*required* field on every Codex hook input `[binary]`, so the handler's
`CLAUDE_SESSION_ID` fallback is simply never reached. No profile-declared env
var is needed — which is what the issue text proposed.

### Why the handler reads argv, not the profile

The issue proposed making the tool-name gate and `_is_internal`'s ignore-paths
profile data. It is not, deliberately: an installed hook command carries no
`$THINKWEAVE_HARNESS`, so `harness.active()` *inside a hook fired by Codex*
resolves the Claude Code profile and would pick the wrong list. Those two lists
are unions of both harnesses' vocabularies instead — the vocabularies never
collide, so a union is correct under either harness with no plumbing.

Where the harness identity genuinely matters (stamping `context_served.source`)
the installer writes `--harness <id>` into the command it generates, and the
handler reads its own argv. Claude Code's command is left unstamped, so the
plugin route — which loads `hooks/hooks.json` directly, unstamped — keeps
agreeing with what the installer writes.

### What is NOT verified here

Everything above is measured pre-auth or read from the manual/binary. **No
authenticated interactive Codex session was run** (that would spend the owner's
quota), so the issue's headline acceptance criterion — "an interactive Codex
session receives the SessionStart context payload and Stop writes+indexes a
session note into the vault" — is verified only at its seams: the artifact
Codex's own `--strict-config` accepts, hooks observed firing pre-auth, and the
handler driven end-to-end on real captured envelopes into a tmp vault
(`tests/test_codex_hooks.py`). The end-to-end claim itself is untested.

The RLVR retrieval-served `PostToolUse` gate is a partial exception. Codex
namespaces MCP tools with the same `mcp__thinkweave__weave_search` strings the
closed `RETRIEVAL_TOOLS` set already holds, and the handler is tested against a
Codex-shaped MCP envelope, so the gate is verified at that seam. What is *not*
measured is Codex's real `tool_response` shape for an MCP call — the 0.146.0
schema types it "any JSON" `[binary]` and no MCP tool ran in a credential-less
session. `retrieval_log.response_text` therefore hardcodes no key list and
harvests every string in the object, whatever its shape; if a live run shows
Codex wrapping results in something that still defeats note-id extraction, the
symptom will be retrieval rows with an empty `returned_ids`, and that is where
to look.

That recovery is scoped to the retrieval path only. The action path keeps
`_extract_tool_output_text`, which recognises `stdout`/`stderr` and returns `""`
for anything else — Claude Code's `Write`/`Edit` `tool_response` echoes back the
file just written (`content`, `originalFile`), so mining it for text would feed
whole files to `_extract_insight_blocks` and re-capture any `★ Insight` block
living in the source on every single touch.

## Native Windows

Verified on **Windows 11** with **Claude Code (native `claude.exe`, 2026-07-25
build)** and **uv 0.10.9** on **2026-08-03**. Sources are labelled the same way
as the Codex section: `[binary]` = strings extracted from the shipped
`claude.exe`; `[measured]` = observed from a real subprocess run on this host.

This is a *platform* axis, not a harness axis — everything here applies to any
harness running outside Git Bash / WSL. "Native Windows" throughout means
cmd.exe / CreateProcess, **not** Git Bash, which runs the POSIX launchers
unchanged.

### The one finding everything else follows from

**Hooks and MCP servers are spawned by different mechanisms, and only one of
them consults PATHEXT.**

| Mechanism | PATHEXT applies? |
|---|---|
| A command **string** through a shell (cmd.exe) | **Yes** `[measured]` |
| Direct `CreateProcess`, no shell | **No** `[measured]` |

`[measured]` `cmd /c "<dir>\probe" hello` resolves `probe.cmd`; a shell-less
`subprocess.run(['./probe'])` raises `WinError 2` for the same file.

**Which mechanism each harness surface uses is a separate question, and the
answer for Claude Code is "a shell, for both."** `[measured]`
`claude mcp list` reports BOTH committed registrations — project-scope
`bin/weave-mcp-launch` and the plugin-route
`…/skills/thinkweave/bin/weave-mcp-launch` — as **Connected** on native
Windows. Claude Code resolves an MCP `command` through Git Bash
(`CLAUDE_CODE_GIT_BASH_PATH`, 6 hits `[binary]`), so the extensionless POSIX
launcher works as committed and **needs no Windows-specific entry**. An earlier
draft of this section inferred otherwise from the shell-less `subprocess`
result above; that was a proxy measurement generalised past what it showed.

`bin/*.cmd` therefore exist as a **fallback for a shell-less or cmd.exe-only
spawn** (an environment without Git Bash, or a harness that does not shell out),
not as a repair for a demonstrated Claude Code breakage. What follows is why
each surface is shaped the way it is:

* **Hooks need no config change at all.** `hooks/hooks.json` fires the
  extensionless `"${CLAUDE_PLUGIN_ROOT}/bin/weave-hook-launch"`, and cmd.exe
  resolves that to `weave-hook-launch.cmd` while Git Bash picks the shell
  script. One authored command, two implementations. `[measured]` end-to-end:
  that exact command string (with the mixed separators `_localize_command`
  produces) returns a well-formed SessionStart `additionalContext` payload.
  `tests/test_install.py::TestWindowsLaunchers` pins the command extensionless —
  committing `…-launch.cmd` there would fix Windows and break every POSIX host.
* **MCP entries need no change either, and must not be "fixed".** The
  committed manifests work as-is (see the measurement above). `weave doctor
  --mcp` deliberately does **not** reject an extensionless command on Windows: a
  gate there red-flags a working install, which is strictly worse than the
  false green it appears to prevent. Re-running `weave install` to obtain a
  machine-scope `uv.exe` entry is **not** required and would add a third
  registration beside the two that already work.

  `check_launcher_resolves` used to probe by spawning **without** a shell, so on
  Windows an extensionless launcher raised an unhandled
  `OSError: [WinError 193] %1 is not a valid Win32 application` and aborted the
  whole doctor — a probe artefact reported as a broken install. **#156** fixes
  that by probing through Git Bash the way the harness does. `claude mcp list`
  remains the authoritative cross-check.

### `commandWindows`: Codex has it, Claude Code does not

This is a **per-harness** fact, and an earlier revision of this file got it wrong
by measuring one harness and generalising to both. Both halves are now measured
separately.

**Codex: yes.** Its hooks documentation states verbatim — *"`commandWindows` is
an optional Windows-only command override. In TOML, use `command_windows` or
`commandWindows`."* It is per hook entry and sits beside `command`. thinkweave
writes `hooks.json`, so it uses the camelCase spelling, and it keeps `command`
pointing at the POSIX launcher so WSL/Linux is unaffected while
`commandWindows` names the `.cmd` sibling. Codex does **not** resolve hook
commands through Git Bash the way Claude Code does, so without this a Windows
Codex user's hooks hand a `#!/bin/sh` script to cmd.exe.

**Claude Code: no.** `[binary]` `commandWindows` appears **0** times in the
shipped 2026-07-25 `claude.exe`, against 49 hits for `UserPromptSubmit`. (The
same grep finds 0 for `additionalContextLimit`, correctly — that key is
Codex-only — which is what validates the method.) Writing it there would be
config that parses and never fires, and it is unnecessary anyway: Claude Code
shells out to Git Bash, so the extensionless command already works.

Hence `HarnessProfile.hook_windows_command_key` rather than a shared constant —
the key is written only for the harness that documents one. The correction also
retires the claim that "Codex's hook schema is field-for-field Claude Code's":
it is *nearly* so, and this is one of the places it is not.

**Method note.** The earlier error was grepping `claude.exe` and treating the
result as a statement about hooks in general. When a fact is per-harness, a
measurement of one harness is evidence about that harness only — and Codex could
not be measured here at all, because the CLI is not installed on the test host
(see "What is NOT verified"). The documentation was the right fallback, and the
right one to have consulted first.

### Line endings are load-bearing

`core.autocrlf=true` is the default on a Windows Git install, and the repo had
**no `.gitattributes`** — so a fresh Windows clone checked the POSIX launchers
out with CRLF. msys2's bash tolerates a CRLF shebang, which is exactly why this
hid: the launchers kept working locally while the same clone shared into WSL was
already broken with `bad interpreter: /bin/sh^M`. It also left
`bin/weave-{hook,mcp}-launch` permanently dirty in `git status` on every Windows
checkout. `.gitattributes` now pins the POSIX launchers to `eol=lf` and `*.cmd`
to `eol=crlf` (a LF-only `.cmd` mis-parses the multi-line `if (…)` block in the
resolution ladder).

### `--no-sync`, and where it belongs

The machine-scope MCP entry passes `uv run --no-sync`; the launchers deliberately
do not. `weave install` has already run `uv sync` eagerly, so re-resolving at
every session start buys nothing — and on Windows it is a real hazard, since uv
wants to rewrite `.venv\Scripts\weave-mcp.exe` while a previously-spawned server
still holds that image open. The launchers must keep syncing: on the plugin
route nothing ever runs `weave install`, so that implicit sync is the route's
only dependency bootstrap. `mcp_doctor._key` normalises the flag away so the two
shapes still fingerprint as one invocation.

`[measured]` `--no-sync` coexists with `--extra mcp` (no conflict, uv 0.10.9) and
is ~2× faster warm (0.27s vs 0.60s).

**Amended 2026-08-03 — the launchers skip it too, and the hazard is no longer
hypothetical.** Editing `pyproject.toml` with a session running reproduced it
exactly: the PostToolUse hook fired, its `uv run` reinstalled the project, and
the sync died with

```
error: failed to remove file `.venv/Lib/site-packages/../../Scripts/weave-mcp.exe`:
The process cannot access the file because it is being used by another process. (os error 32)
```

Two live `weave-mcp` servers held that image open. Every subsequent hook fired
and failed the same way, so the PostToolUse capture was lost for the rest of the
session. The launchers therefore pass `--no-sync` unconditionally at runtime — that half
is **#156**'s ("Runtime MCP/hook launchers use `--no-sync` by design"); this
section documents the same policy for the machine-scope entry `weave install`
writes, and for the `.cmd` siblings, which match it.

The same failure produced a second lesson. uv had already deleted
`weave-hook.exe` before it hit the locked `weave-mcp.exe`, leaving a venv that
imported perfectly but had **no hook console script** — a state a console-script
launcher can never recover from on its own. Every launch surface therefore runs
`python -m thinkweave.surfaces.{mcp.server,hooks.handler}` instead of
`weave-mcp`/`weave-hook`. Module execution needs only an importable package,
which is what `uv run` already guarantees. This was Codex's original
recommendation, initially rejected here as churn on the grounds that the console
script worked at the time; the lock failure is the case that argument missed.

**Amended 2026-08-29 (#164) — unconditional `--no-sync` deleted the plugin
route's only dependency bootstrap, so the launchers now carry a guarded one.**
"The launchers must keep syncing" (above) and "the launchers pass `--no-sync`
unconditionally" (the 2026-08-03 amendment) were each half right: the implicit
sync WAS the marketplace clone's only bootstrap, and removing it meant
`uv run --no-sync` on a venv-less clone fabricated an empty venv and died with
`ModuleNotFoundError`. The resolution is a third state: every launcher (three
POSIX + the two `.cmd` twins) checks for an installed thinkweave distribution
and, only when none exists, runs the one sanctioned sync
(`uv sync --extra all`, dec-3d4f8ce9) before its unchanged `--no-sync` exec.

The sentinel is `site-packages/thinkweave-*.dist-info` (either venv layout),
**not** the console scripts, for exactly the reason this section records: the
2026-08-03 incident showed uv deletes the shims *first* during a reinstall,
leaving a venv that imports fine but has no `weave-hook.exe` — a state
`python -m` survives, and which therefore must not re-trigger a sync while
live servers hold the shims. dist-info is the marker both editable installs
(what `uv sync` produces on the dev and plugin routes — there is no
`site-packages/thinkweave/` then) and regular installs share. A sync killed
mid-flight can transiently remove dist-info too; the bootstrap then re-fires
on the next launch and converges via uv's venv lock and wheel cache.

A cold `uv sync --extra all` routinely outlives hook timeouts, so
`hooks/hooks.json` raises SessionStart from 60s to 300s: SessionStart is the
first hook to fire on a fresh clone and the natural place for the bootstrap to
converge, a healthy SessionStart still finishes in about a second (the ceiling
only binds when the hook genuinely runs long, which was previously a
guaranteed kill), and the 30s hooks stay put — by the time they fire, the venv
has either converged or the cache is warm enough that the next attempt
finishes. Each attempt logs a `first-run bootstrap` breadcrumb to stderr so a
hook killed at its timeout is diagnosable rather than silent.

### A latent bug this surfaced

(This one is fixed here; the launcher and probe items above are #156's, and the
`mcpServers`-as-string crash is #155's. Kept in one place because the *findings*
belong together even though the fixes ship separately.)

`mcp_doctor._key` fingerprinted commands with a bare `Path(command).name`. On
Windows `shutil.which("uv")` returns `C:\…\uv.EXE`, so the machine entry keyed as
`uv.EXE` while the launcher branch hardcoded `uv` — meaning any Windows install
carrying **both** a machine entry and the committed `.mcp.json` was reported by
`weave doctor --mcp` as a cross-scope conflict that did not exist. Latent since
#52; fixed by `_command_stem`, which strips a Windows executable suffix and
case-folds (Windows paths are case-insensitive; POSIX names are left alone).

### What is NOT verified here

The `weave doctor --mcp` probe, per the known gap above — it cannot execute an
extensionless POSIX launcher on Windows, so the doctor's own verdict on that
entry is unavailable (the harness's verdict, via `claude mcp list`, is
Connected).

No **Codex on Windows** run at all — the CLI is not installed on the test host
(`~/.codex/config.toml` carries no `[mcp_servers]`), so every Codex×Windows claim
above is inherited from the shared installer code plus the test suite, not
measured. The `.cmd` launchers' resolution ladder *is* measured
(`tests/test_{hook,mcp}_launcher.py` now run the native implementation on
Windows), but no native-Windows **MCP server** has been driven end-to-end
through a real harness session; the hook path has.
