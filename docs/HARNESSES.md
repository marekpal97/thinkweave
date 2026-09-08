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
| evidence | measured — daily live use on the dev machine; suite drives the handler end-to-end | measured — codex-cli 0.146.0: credential-less spike 2026-08-02, two live interactive sessions 2026-09-05, two instrumented headless sessions 2026-09-07 with every envelope captured raw (docs/HARNESSES.md) | measured — Pi 0.84.4: live trial 2026-09-03 (E0 floor verified, settings-MCP falsified, n-fb74c7d0), headless events probe 2026-09-05 (all four native events fired), and an interactive session 2026-09-05 through pi-mcp-adapter 2.32.1 (17 bare-named weave_* tools, direct calls, /skill:wrap end-to-end on a hook-captured session) | declared — blueprint n-767d66b4 (2026-08-24); NOT verified on a live install |
| eligibility (dec-5a076384 ladder) | E3 | E3 | E3 | E0 |
| detected by | `~/.claude` | `~/.codex` | `~/.pi` | `~/.config/opencode` |
| lifecycle hooks | plugin | file | extension | none |
| subagent fan-out | yes | yes | no | no |
| headless slash skills | yes | no | no | no |
| native memory seam | `~/.claude/projects` | — | — | — |
| context channel | `additionalContext` | `additionalContext` | `context-injection` | `message-transform` |
| dispatch | `claude -p <prompt>` | `codex exec <prompt>` | `pi -p <prompt>` | `opencode run <prompt>` |
| transcripts | `~/.claude/projects/*/*.jsonl` (jsonl-flat) | `~/.codex/sessions/*/*/*/rollout-*.jsonl` (jsonl-rollout) | `~/.pi/agent/sessions/*/*.jsonl` (jsonl-tree) | `~/.local/share/opencode/storage/session/*/*.json` (json-records) |
| session ids | `uuid4` | `uuid7` | `uuid (session-header id)` | `ses_<12-hex><14-base62> (ULID-style sortable)` |
| MCP config | `~/.claude.json` · key `mcpServers` | `~/.codex/config.toml` · key `mcp_servers` | `~/.pi/agent/mcp.json` · key `mcpServers` | `~/.config/opencode/opencode.json` · key `mcp` |
| MCP native CLI | `claude mcp add` | `codex mcp add` | — | — |
| instructions file | `~/.claude/CLAUDE.md` | `~/.codex/AGENTS.md` | `~/.pi/agent/AGENTS.md` | `~/.config/opencode/AGENTS.md` |
| skills dir | `~/.claude/skills` | `~/.codex/skills` | `~/.pi/agent/skills` | `~/.config/opencode/skills` |

### Hook events (canonical → native, with observed-fire dates)

| canonical | Claude Code | Codex | Pi | OpenCode |
|---|---|---|---|---|
| SessionStart | ✓ 2026-08-29 | ✓ 2026-09-05 | `session_start` ✓ 2026-09-05 | `experimental.chat.messages.transform` (declared) |
| UserPromptSubmit | ✓ 2026-08-29 | ✓ 2026-09-05 | `before_agent_start` ✓ 2026-09-05 | `chat.message` (declared) |
| PostToolUse | ✓ 2026-08-29 | ✓ 2026-09-07 | `tool_result` ✓ 2026-09-05 | `tool.execute.after` (declared) |
| Stop | ✓ 2026-08-29 | ✓ 2026-09-07 | `agent_end` ✓ 2026-09-05 | — (no verified equivalent) |

### Documented degradations

Nothing below is silently faked (#103 anti-goal): a listed capability
degrades exactly as stated. On a **measured** row (see the evidence
row above) everything unlisted works as on Claude Code; on a
**declared** row, unlisted means *not yet checked*, not *works*.

#### Claude Code

None — the reference harness.

#### Codex

- **Stop capture** — documented: fires at every turn end — measured interactively 2026-09-05 (hook/started at each task_complete) and headless 2026-09-07 (raw envelope with last_assistant_message; SessionEnd ~2 s later, which thinkweave does not hook). The first Stop materialises the note and later turns fold in place. Still unmeasured: whether an interactive TUI exit delivers a final Stop of its own beyond the last turn's, so a rich end-of-session capture in the TUI still rides `$thinkweave-wrap` (docs/HARNESSES.md §2026-09-07 instrumented headless run)
- **SessionStart context delivery** — documented: additionalContext renders as a visible developer message, not a silent system one (openai/codex#16933)
- **headless skill invocation** — documented: codex exec resolves no slash commands; a $name mention is a hint the model acts on by reading the skill file itself (docs/HARNESSES.md §Q2)

#### Pi

- **hook latency** — documented: shim-core's 800 ms telemetry budget sits below this launcher's floor on the dev machine (a no-op Stop is ~1.8 s warm, ~6 s under load — WSL2, vault on /mnt/c), so the shim carries per-event budgets (UserPromptSubmit 2.5 s, Stop 6 s); a hook that still outlives its budget finishes as shim-core's documented orphan — capture is complete, Pi shows one 'hook timeout' notice per event per session, and only the prompt-time enrichment block that reply would have carried is lost (#114, docs/HARNESSES.md §Pi)
- **MCP registration** — documented: Pi core ships no MCP client — a settings.json mcpServers block parses and is silently ignored (falsified live on 0.84.4, 2026-09-03). The registration is served through the community pi-mcp-adapter extension instead: `weave install --harness pi` writes the standard mcpServers block (plus lifecycle/directTools/toolPrefix) to ~/.pi/agent/mcp.json, the adapter also reads the project .mcp.json, and `weave doctor --mcp --harness pi` fails with `pi install npm:pi-mcp-adapter` when the package is absent; the CLI fallback in the instructions block covers a session where the tools still did not load (#114, n-fb74c7d0)
- **subagent fan-out** — documented: Pi ships no first-party subagent tool, so the /drain and /dream worker topology has nothing to dispatch onto (n-a1d3beba §2)
- **skill invocation** — documented: no Skill tool — /skill:<name> is prompt expansion. Skills are root-file links `weave install --harness pi` creates in ~/.pi/agent/skills, one <name>.md per canonical commands/*.md; worker-backed commands (/drain, /dream, /news, /newsletter, /podcast, /youtube, /seed-enrich, …) are not linked because Pi has no subagents to run them (Pi docs/skills.md §Locations)

#### OpenCode

- **lifecycle hooks** — documented: the OpenCode plugin shim is not yet shipped, so passive capture does not run; end sessions with an explicit weave_extract (#195)
- **MCP registration** — documented: weave install writes OpenCode's documented schema under the `mcp` key (type local, command as one array, environment map when non-empty — opencode.ai/docs/mcp-servers/ via n-767d66b4 §4); NOT yet verified to parse on a live install — #195 owns the live verification (n-767d66b4 §4)
- **Stop capture** — documented: no verified Stop-equivalent event — claude-mem's plugin subscribed to bus events that never fire and captured nothing silently; only session.idle/session.deleted are confirmed real (claude-mem#2462)
- **subagent fan-out** — documented: no hook fires on subagent dispatch/completion in the docs or any reference plugin (n-767d66b4 §2)
- **transcript import** — documented: sessions are per-record JSON files (session/message/part); no importer reads them yet (n-767d66b4 §6)

<!-- weave:harness-matrix:end -->

## Codex

All findings verified against **codex-cli 0.146.0** on **2026-08-02**, on Linux
(WSL2), re-checked against two live authenticated interactive sessions on
**2026-09-05** (§"2026-09-05 live sessions"), and closed out by two
instrumented authenticated headless sessions on **2026-09-07** whose every
hook envelope was captured raw (§"2026-09-07 instrumented headless run" —
the fixture is `tests/fixtures/harness_envelopes/codex/envelopes-2026-09-07.jsonl`).
Later sections supersede the "unobserved"/"inferred" claims made earlier in
this one. Sources are labelled: `[manual]` =
<https://learn.chatgpt.com/docs/codex-manual.md> / `/docs/hooks`; `[binary]` =
strings & embedded JSON schemas extracted from the 0.146.0 executable;
`[measured]` = observed from a real CLI run — on 2026-08-02 against a
throwaway `$CODEX_HOME` with no credentials (every such run terminated in a
401 — no model was invoked); on 2026-09-05 and 2026-09-07 authenticated, model
`gpt-5.6-sol`.

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

*2026-08-02:* `Stop` did **not** fire in that run, and neither did
`PostToolUse` — the turn aborted at auth before any tool ran or any turn
completed, so both were simply unobserved. *2026-09-05:* both fire
interactively, per turn (§"2026-09-05 live sessions"). ***Settled
2026-09-07:*** in two authenticated headless `codex exec` sessions with a
sentinel hook teeing stdin, `Stop` fired at turn completion in both (keys
`session_id`, `turn_id`, `transcript_path`, `cwd`, `hook_event_name`,
`model`, `permission_mode`, `stop_hook_active: false`,
`last_assistant_message`), and `SessionEnd` followed ~2 s later
(`reason: "other"`); `PostToolUse` fired for every unified-exec command (as
`Bash`, once, on completion — including a 20 s one) and for the MCP
`weave_search` call. A Codex cron (`codex exec`) therefore completes its
capture on `Stop`; `SessionEnd` is not a hook thinkweave installs and is not
needed as a fallback. See §"2026-09-07 instrumented headless run".

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
inherited from the invoking shell. For the *hook handler* this is a non-issue:
`session_id` is a *required* field on every Codex hook input `[binary]`, so the
handler's env-var fallback is never reached.

It **does** matter for `/wrap`, which runs as a model turn — not a hook — and
so cannot read the hook payload. `HarnessProfile.session_id_env` is `None` for
Codex accordingly (`CLAUDE_SESSION_ID` for Claude Code, `PI_SESSION_ID` for Pi),
and `weave session-id` returns empty here. A Codex wrap therefore falls back to
recency **with** the #209 identity guard, never to minting a fresh slug for a
live session that already has a hook-created note. See [§Session identity and
the wrap resolver](#session-identity-and-the-wrap-resolver). This closes the
mint-a-detached-slug fragmentation observed on Codex (2026-09-05:
`codex-diagnose-mcp-startup-20260905` → ses-dec974a7, a second note beside the
hook-created one).

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

### Session identity and the wrap resolver

The hooks stamp the *harness* session id as `source_session:` on the session
note, and `weave_extract(session_id=<that raw id>)` resolves the note through
that stamp — auto-creating one only when no note carries the id. So a wrap that
passes the current harness session id lands on the hook-created note by
construction; a wrap that passes the *wrong* id (or an invented slug) takes the
auto-CREATE branch and produces a second, detached note, leaving the real one
without the insights and `wrap-finalize --verdicts` with no `events.jsonl` to
match.

The id lives in a **different environment variable per harness**, held as
`HarnessProfile.session_id_env`:

| Harness | `session_id_env` | Notes |
|---|---|---|
| Claude Code | `CLAUDE_SESSION_ID` | uuid4 |
| Pi | `PI_SESSION_ID` | the session uuid; also `PI_SESSION_FILE` (path). The shim stamps `PI_SESSION_ID` as `source_session` |
| Codex | *(none)* | `session_id` arrives as a hook *payload* field only (§Codex Q4); a model turn cannot read it |
| OpenCode | *(none)* | no session-id env var documented |

A wrap runs as a **model turn**, not a hook: there is no `--harness` argv, and
`$THINKWEAVE_HARNESS` is usually unset, so `harness.active()` reports Claude
Code regardless of the real harness. `/wrap` therefore does **not** read
`$CLAUDE_SESSION_ID` — that is Claude Code's variable alone, and reading it on
Pi/Codex silently minted the detached note. Instead the skill calls the
harness-neutral resolver `weave session-id`, which tries the active profile
first (honouring `$THINKWEAVE_HARNESS` when set) and then scans every registered
profile, printing the first declared `session_id_env` that carries a value —
and exiting non-zero with empty output when none does.

**Wrap resolution order** (`commands/wrap.md` step 1):

1. `id=$(weave session-id)` → **non-empty**: pass that raw id to
   `weave_extract` and land on the hook-created note by construction.
2. **empty** (`weave session-id` exited non-zero — Codex, or a genuinely
   headless run): fall back to `weave search --type session --limit 1` **with**
   the #209 identity guard — check `source_session` before any `force=true`,
   and mint a fresh id only when no session note exists at all. Never mint for
   a live session that already has a hook-created note.

The hook *handler* keeps its own resolution (`session_id` payload field, with
`session_id_env` as a harness-neutral backstop resolved from its `--harness`
argv) — the handler always has the payload, so its fallback is rarely reached;
the wrap resolver is the path that actually needed fixing.

This closed the mint-a-detached-slug fragmentation observed on both non-Claude
harnesses: Pi 2026-09-08 (`wrap-pi-harness-scope-2026-09-08` → ses-e5a02d7a,
beside the hook-created ses-851a2f5c whose `source_session` equals
`$PI_SESSION_ID`; verdicts came back 0 written / 3 unmatched) and Codex
2026-09-05 (`codex-diagnose-mcp-startup-20260905` → ses-dec974a7).

### 2026-09-05 live sessions

Two interactive, authenticated **codex-cli 0.146.0** sessions on the dev
machine (WSL2, TUI, code mode — every tool call is a `custom_tool_call`
named `exec` whose JavaScript calls `tools.exec_command`, `tools.apply_patch`
and `tools.mcp__thinkweave__*`). Sources for this section: the rollouts under
`~/.codex/sessions/2026/09/05/`, Codex's own `~/.codex/logs_2.sqlite`, the
vault's session folders and `context_served` projection, and the handler's
`hooks.log`. No hook envelope was dumped raw in these sessions — the
2026-09-07 run (next section) closed that gap.

| Event | What was observed | Status in the profile |
|---|---|---|
| SessionStart | 45 `context_served` rows, `source='codex-startup'`, at 19:39:39Z and 19:59:33Z — one per session, seconds after each `session_meta` | verified, dated 2026-09-05 |
| UserPromptSubmit | prompt events with `delivery_id: user_prompt_submit:<uuid7>:<turn_id>` in both sessions' `events.jsonl` | verified, dated 2026-09-05 |
| PostToolUse | 43 tool events in session A's `events.jsonl`, `delivery_id: post_tool_use:<uuid7>:exec-<uuid>:<n>` — `apply_patch` fan-out to `Edit` rows and `Bash` rows (`pytest`, `git push`), each within 1s of the rollout's `patch_apply_end` / `custom_tool_call_output`. Codex's log shows `hook/started` 1:1 with `exec_command` completions in the window where its app-server trace was on (20:06–20:08Z: 10 exec + 1 MCP + UPS + Stop = 13) | fires; raw envelope captured 2026-09-07 → verified, dated 2026-09-07 |
| Stop | `hook/started` at both `task_complete` timestamps of session B (20:03:00Z, 20:08:29Z); session A's first turn archived its startup + first prompt into a session folder that only a Stop writes | fires per turn; raw headless envelope captured 2026-09-07 → verified, dated 2026-09-07 |

**PostToolUse was never dead.** The 2026-09-05 diagnosis that opened this
section read `files_touched: []` and an absent first prompt as "PostToolUse
captured nothing". Three separate things produced that picture, none of them
the hook:

1. **Stop fires at every turn end, and the first one latched the note.** The
   handler marked the note `processed` and archived the buffer at turn 1
   (19:44:56Z); every later turn's events went to a fresh live buffer that
   nothing folded back, so the note kept turn 1's `files_touched: []` while
   `events.jsonl` in the *eventual* folder held 15 distinct files. Claude Code
   has the same latch — a census of the dev machine's buffer dir found 677
   live buffers, 568 older than 30 days. Fix: `_fold_processed_session` —
   a Stop on a processed note recomputes the deterministic evidence over
   archived ∪ live events, writes only the evidence fields (never the body),
   mirrors the buffer's action/prompt lines into `events.jsonl` (append-unique,
   buffer kept live for the prompt-time ledger), and re-indexes.
2. **The wrap minted a second note and the GC ate the first.** `$thinkweave-
   wrap` passed the raw uuid7; `extract_session` did not resolve
   `source_session` and auto-minted `ses-fa4c3b44` (23:03Z), archiving the
   live buffer there. `wrap-finalize`'s prune then found the turn-1 folder
   (`events.jsonl` < 500 bytes, empty `files_touched`, > 1h old) and deleted
   it — that is where `ses-3cb0f16c` / `ses-9e592da1` went, and why their 45
   `context_served` rows point at nothing. PR #209 owns the resolver half
   (extract resolves through `source_session` before minting); the fold above
   removes the "empty evidence" that made a real session look like an orphan.
   `context_served.session_id` is *always* the `ses-` note id — Claude Code's
   `startup` rows key the same way — so "SessionStart served under a minted
   id" was the projection, not a second identity.
3. **`_is_significant_command` dropped session B's exec calls.** All ten were
   `sed`/`rg`/`node`/`perl` reads; only `git commit|push`, `pytest`, `python`,
   `uv run`, `make`, `npm`, `deploy` prefixes are kept. Correct behaviour,
   invisible from `files_touched`.

What PostToolUse *did* lose: no `test_run` on any Codex `pytest` row and no
commit parse. This section's first draft blamed the response shape: the
rollouts' `custom_tool_call_output` bodies show the model receiving one JSON
object (`{"chunk_id","wall_time_seconds","exit_code","original_token_count",
"output"}`) and upstream `JsonToolOutput::post_tool_use_response` appeared to
hand that value to hooks unchanged. **The 2026-09-07 raw capture falsified
that inference** — `tool_response` for a unified-exec `Bash` call is the plain
combined-output *string* (next section), which `_extract_tool_output_text`
already returned as-is. The gap that actually mattered sat in front of it:
every Codex test command was `PYTHONPATH=src /repo/.venv/bin/pytest -q …`,
which the `pytest`-prefix classifiers never matched — the rows were kept only
because `"pythonpath…".startswith("python")`, and never classified as a test
run. `_command_head` now strips leading `VAR=value` tokens and basenames the
executable before the significant / test / git-commit prefix checks. The
helper also still reads a dict with a string `output` — Codex's `apply_patch`
reply takes that shape, and so would a harness build that forwards the
unified-exec object (Claude Code's `Write`/`Edit` echoes carry no such key, so
the file-echo hazard below is untouched). The other 2026-09-05 caveat — a
command that outlives its code-mode `yield_time_ms` delivering its output via
an unhooked `wait` — was tested with a 20 s command on 2026-09-07: PostToolUse
fired exactly once, on completion, with the full output.

**Guardian sidecars.** Codex 0.146 spawns an approval-judge thread per parent
session with its own rollout — `session_meta.payload.source ==
{"subagent": {"other": "guardian"}}`, `thread_source: "subagent"`,
`parent_thread_id` set, `base_instructions` opening "You are judging one
planned coding-agent action". Its turns are the parent's transcript pasted in
as a prompt plus a one-line verdict. `weave import codex` now skips any rollout
whose meta carries a `subagent` key (or `thread_source == "subagent"`) after a
first-line peek — `skipped_subagent` in the stats, `--include-subagents` to
import them stamped `codex_subagent: <kind>`. `[manual]` says subagent *hooks*
"use the parent session id", so a guardian turn's own hook deliveries, if any,
would land on the parent's buffer; the vault folders show no trace of one.

**The 2026-09-05 `Broken pipe` lines are not Codex.** `hooks.log` shows 44
`[Errno 32]` pairs that day, 38 in 20:11–20:19Z, when no Codex turn was active
(both rollouts idle; guardian turns are seconds long and hook-free). Their
five-second prompt→stop cadence is a concurrent Claude Code flow whose reader
closed early; the two `post_tool_use` lines at 20:17:54Z are in that cluster.

The instrumented run this section asked for — a sentinel hook teeing every
envelope, a long-running command, a `Stop` vs `SessionEnd` comparison — was
done on 2026-09-07; it is the next section.

### 2026-09-07 instrumented headless run

Two authenticated **codex-cli 0.146.0** sessions, `codex exec
--dangerously-bypass-hook-trust --skip-git-repo-check -s workspace-write`
from the repo checkout, model `gpt-5.6-sol`, session ids
`01a07a9d-ddb5-78b2-bf6d-7a4ad816b26f` (A) and
`01a07a9e-2262-7073-a264-48408a2d46fe` (B). A sentinel hook registered
alongside thinkweave's `tee`d the raw stdin of **every** event to a file;
the file is committed verbatim as
`tests/fixtures/harness_envelopes/codex/envelopes-2026-09-07.jsonl` (one
`{"probe_event","probe_ts","envelope"}` object per line) and
`tests/test_codex_hooks.py` drives the handler with those envelopes
unmodified. The prompt asked for exactly three actions and no edits: a pytest
run, `sleep 20; echo waited-ok`, and one `weave_search` MCP call. Census: 1
SessionStart + 1 UserPromptSubmit (session A's opening envelopes were lost to
a file reset in the probe itself), 4 PostToolUse `Bash`, 2 PostToolUse
`mcp__thinkweave__weave_search`, 2 Stop, 2 SessionEnd.

| Event | Measured | Consequence |
|---|---|---|
| PostToolUse (unified exec, `Bash`) | `tool_input: {"command": "PYTHONPATH=src .venv/bin/pytest tests/test_shim_core.py -q"}`, **`tool_response` is a plain string** `"....  [100%]\n4 passed in 0.06s\n"`, `tool_use_id: "exec-<uuid>"`, plus `session_id`/`turn_id`/`model`/`permission_mode`/`transcript_path`/`cwd` | The `{chunk_id, exit_code, output, …}` object this PR first inferred from upstream source is what the *model* sees, not what hooks get. `_extract_tool_output_text`'s string branch is the Codex path; `_command_head` is what makes the row a `test_run`. |
| PostToolUse, slow command | `sleep 20; echo waited-ok` → exactly one envelope per session, ~26 s after the previous tool's, `tool_response: "waited-ok\n"`; no partial or second envelope | A command that outlives any code-mode yield still yields one PostToolUse on completion with its full output. Nothing rides `wait`. (The handler then drops it as insignificant — correct.) |
| PostToolUse (MCP) | `tool_name: "mcp__thinkweave__weave_search"`, `tool_input` = the raw arguments dict `{"query": "pi-mcp-adapter", "mode": "fts", "limit": 2}`, `tool_response: {"content": [{"type": "text", "text": "…"}], "isError": false}` | The MCP-shape question in the old "NOT verified" paragraph is closed: `retrieval_log.response_text` mined it live — both sessions' `retrieval_log.jsonl` carry `returned_ids: ["dec-e3525a07", "n-a1d3beba"]`. |
| Stop | fired in both sessions at turn completion, keys `session_id`, `turn_id`, `transcript_path`, `cwd`, `hook_event_name`, `model`, `permission_mode`, `stop_hook_active: false`, `last_assistant_message` (the model's final answer) | Headless `codex exec` capture completes on `Stop`. `fires_verified["Stop"] = 2026-09-07`. |
| SessionEnd | ~2 s after each Stop; keys `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `reason: "other"` | Not a thinkweave hook (no `session_end` branch in the handler, none registered in `hooks/hooks.json`); nothing rides it. |
| SessionStart / UserPromptSubmit (session B) | same shapes as 2026-08-02, `permission_mode: "default"`, `source: "startup"`; the prompt arrives verbatim under `prompt` | unchanged, dates stay 2026-09-05 |

**Live capture on the pre-#212 handler** (the main checkout's, read-only
evidence; the vault is not touched by this PR). The index holds one session
note per probe session — `ses-78b81a0a` (A, 06:46:11Z) and `ses-566031c4`
(B, 06:46:28Z), `source_session` set to the Codex uuid, `processed: true` —
each with 45 `codex-startup` and 2 `onthefly` `context_served` rows. Each
folder's `events.jsonl` has exactly two rows, the prompt and the pytest
`Bash` row (`delivery_id: post_tool_use:<uuid7>:exec-<uuid>:0`, ~100 ms after
the envelope's `probe_ts`); the `sleep` row was dropped as insignificant. Each
`retrieval_log.jsonl` has a `startup` row and the `weave_search` `retrieval`
row with the two note ids above. `files_touched: []` is correct (the prompt
forbade edits). What the live handler did *not* produce is a `test_run` on the
pytest row — the `PYTHONPATH=src .venv/bin/pytest` head is exactly the
`_command_head` gap this PR closes; `TestMeasuredHeadlessReplay` replays the
same envelopes through the fixed handler and gets `test_runs: [{"command":
…, "passed": 4}]`. So end to end: one Codex session → one note, kept by Stop,
with prompt, tool, retrieval and (now) test evidence.

### What is NOT verified here

**The interactive TUI exit.** Every Stop measured so far is a *turn-end* Stop
(interactive 2026-09-05, headless 2026-09-07). Whether quitting the TUI
delivers a further Stop of its own — as opposed to only `SessionEnd`, which
thinkweave does not hook — is unmeasured. Nothing is lost by it (the last
turn's Stop has already folded the session's evidence, and the note is kept
current turn by turn), but a rich end-of-session synthesis in the TUI still
rides `$thinkweave-wrap` rather than a hook. This is the one remaining Codex
degradation in the profile.

**`apply_patch`'s raw envelope.** The 2026-09-07 prompt forbade edits, so no
`apply_patch` PostToolUse was captured raw. Its vault footprint is measured
(the 2026-09-05 `Edit`/`Write` rows fanned out from one `apply_patch`
delivery, above), and the handler's parser is pinned to the 0.146.0 binary's
patch markers, but the `tool_response` for it (`{"output": "Success. Updated
the following files: …"}` per the manual) is documented, not captured.

The action path keeps `_extract_tool_output_text`, which recognises a bare
string, `stdout`/`stderr`, and a string `output`, and returns `""` for
anything else — Claude Code's `Write`/`Edit` `tool_response` echoes back the
file just written (`content`, `originalFile`), so mining it for text would
feed whole files to `_extract_insight_blocks` and re-capture any `★ Insight`
block living in the source on every single touch. The retrieval path stays
shape-agnostic (`retrieval_log.response_text` harvests every string in the
object); the measured MCP shape above is one it handles, not the only one it
would.

## Pi

Verified against **Pi 0.84.4** (badlogic/pi-mono `@earendil-works/pi-coding-agent`)
on Linux (WSL2). Sources are labelled: `[docs]` = the package's own
`docs/*.md` (`packages.md`, `skills.md`, `extensions.md`); `[adapter]` = the
`pi-mcp-adapter` **2.32.1** README and source read from its installed package
(`config.ts`, `types.ts`); `[measured]` = observed on a live Pi run — the
2026-09-03 trial (vault note **n-fb74c7d0**) and the 2026-09-05 events probe
that landed the E3 shim (PR #207).

### No native MCP client

`[measured]` A `mcpServers` block in `~/.pi/agent/settings.json` parses and is
**silently ignored**: no server is spawned, no tool appears, no error is raised.
The 2026-08-24 blueprint (n-a1d3beba) had asserted the settings route from a
Pi issue titled "Add MCP *extension* example" — desk research read the word
"extension" past. Ten minutes of `npm i -g` plus one session falsified it
(n-fb74c7d0), which is also why the row's evidence is labelled *measured* and
why `weave doctor` gained a check nothing else would have raised.

### The adapter route

`[adapter]` MCP on Pi is the community extension `pi-mcp-adapter`
(`pi install npm:pi-mcp-adapter`). It reads the standard `{"mcpServers": {…}}`
JSON from, lowest to highest precedence: `~/.config/mcp/mcp.json`,
`~/.agents/mcp.json`, `~/.agents/mcp/mcp.json`, `~/.pi/agent/mcp.json`,
project `.mcp.json`, project `.pi/mcp.json`. Later files overlay earlier ones
**per field**, so the repo's committed `.mcp.json` (relative
`bin/weave-mcp-launch`) wins on `command` inside the checkout while
inheriting the adapter-only keys from the global file.

thinkweave's row therefore puts `mcp_config` at **`~/.pi/agent/mcp.json`**
(the adapter's Pi-global file) and `project_mcp_config_relpath` at the
standard `.mcp.json`. `settings.json` is kept as `legacy_mcp_config`: `weave
install --harness pi` and `weave uninstall --harness pi` both sweep a
thinkweave entry out of it, so the dead block the earlier row wrote does not
outlive that row. `settings.json` remains `user_settings` /
`installed_plugins` (it is where `pi install` records `packages`).

The entry body is Claude Code's split shape plus three adapter keys carried
as profile data (`mcp_entry_extras`): `"lifecycle": "eager"` (SessionStart
already spawns the handler; a lazy server would add its cold start to the
first retrieval instead), `"directTools": true` (without it the server hides
behind one `mcp` proxy tool), `"toolPrefix": "none"` (with `directTools` the
adapter otherwise names tools `thinkweave_weave_search`; the skills name bare
`weave_*`). The extra `"type": "stdio"` key is tolerated — `isServerEntry`
is `isRecord` in `config.ts`, transport being chosen from `command`/`url` —
so the writer keeps Claude Code's shape unchanged.

**Doctor.** `weave doctor --mcp --harness pi` leads with an `MCP client
extension` row. `[docs]` `pi install npm:<pkg>` appends `"npm:<pkg>[@ver]"`
to the `packages` array of `~/.pi/agent/settings.json` (or `.pi/settings.json`
with `-l`) and unpacks it under `~/.pi/agent/npm/node_modules/<pkg>`. The
check reads those two arrays (string or `{"source": …}` filtering form,
scoped names allowed) and, for a machine-scope listing, corroborates the
unpacked `package.json`; it FAILs naming `pi install npm:pi-mcp-adapter`.
The adapter also reads the `~/.config/mcp` and `~/.agents` files, which the
doctor does not scan — a registration living only there reports as
"not registered" while working.

The same report carries an `extension stub` row on every
`hook_mechanism == "extension"` harness. `weave hooks install --harness pi`
writes one loader line, `export { default } from "<repo>/shims/pi/thinkweave-pi.ts"`
(absolute path), into `~/.pi/agent/extensions/thinkweave.ts`; the doctor
parses that path back out and **FAILs** when the file is gone, naming the
checkout and branch the stub points at and the reinstall that rewrites it.
No stub at all is a passing **WARN** ("hooks not installed"), consistent
with how the doctor treats an uninstalled registration elsewhere. The row
exists because the dev checkout was twice in one day switched to a branch
without `shims/pi/`, and Pi loaded the stub, found nothing, and ran without
capture and without a word.

### Skills are root-file links

`[docs]` Pi discovers **root `*.md` files** in `~/.pi/agent/skills/` (and
`.pi/skills/`) as individual skills when they carry `name` + non-empty
`description` frontmatter, alongside the usual `<dir>/SKILL.md` layout.
Skills are invoked as `/skill:<name>` — **prompt expansion**; there is no
Skill tool — and *relative paths inside a skill resolve from the skill's
directory*.

That last rule is what broke the first attempt: the Codex projections under
`skills/thinkweave-*/SKILL.md` say "read `../../docs/CODEX-SKILL-PROJECTION.md`",
which from `~/.pi/agent/skills/thinkweave-wrap/` is nothing. Symlinking the
Codex bundle into Pi therefore produced 31 skills that each failed on first
use. The Codex bundle is untouched for Codex; it just must not be what Pi
gets.

`weave install --harness pi` (the command that already owns per-harness
machine wiring — Codex's Windows launcher, the instructions block — with a
previewed `uninstall` that reverses it) now links the **canonical**
`commands/<name>.md` files: `~/.pi/agent/skills/<name>.md -> <repo>/commands/<name>.md`,
one per command whose frontmatter declares no `workers:` (via
`skill_projection.iter_command_contracts`, the same parser the Codex projector
uses). Nested commands (`commands/research/research-article.md`) link flat by
their own name. `hubs-link`, `import-chatgpt` and `seed-enrich` gained a
`name:` key for this — Pi names a nameless root file from its parent
directory, and two files named `skills` collide. Worker-backed commands
(`/drain`, `/dream`, `/news`, `/newsletter`, `/podcast`, `/youtube`,
`/seed-enrich`, `/research-podcast`, `/research-youtube`) are skipped because
Pi has no subagents (`subagents=False` on the row; a row that had them would
link everything). The install is idempotent, re-points links aimed at an old
checkout, drops links to commands that no longer exist, sweeps
`thinkweave-*` links into the Codex bundle, and leaves any file that is not
one of its links alone. `weave uninstall --harness pi` removes exactly the
command links.

`weave dev-link` refuses on this row and points at `weave install --harness
pi`. dev-link is the Claude Code plugin route — one whole-checkout symlink
that the plugin runtime turns into MCP + hooks + commands — and Pi has no
plugin runtime; what it would do instead is discover every `SKILL.md`
directory under the linked checkout recursively, re-exposing the Codex
bundle by the back door. The install command already gives the property
dev-link exists for (links straight into the working tree, live edits).
Before the gate, dev-link on Pi died on the missing `package.json` manifest
with "run from a thinkweave checkout" — a refusal by accident, and a wrong
message.

A known cosmetic gap: `commands/learn.md` carries one relative doc pointer
(`../docs/LIFECYCLES.md`), which does not resolve from the Pi skills dir. It
is a reference for humans, not a load-bearing read; nothing in the contract
depends on it.

### E3 posture

`[measured]` All four lifecycle events fire through the extension shim
(`shims/pi/thinkweave-pi.ts`, PR #207): `session_start → SessionStart`,
`before_agent_start → UserPromptSubmit`, `tool_result → PostToolUse`,
`agent_end → Stop`, with the SessionStart payload prepended as a synthetic
user message by the `context` handler (Pi has no `additionalContext`
channel). Capture is therefore passive, exactly as on Claude Code, and the
instructions block (`~/.pi/agent/AGENTS.md`, `[measured]` read and acted on
unprompted in the 2026-09-03 trial) says so: the model must **never** call
`weave_extract` mid-session or per turn; end-of-session extraction is the
explicit `/skill:wrap`; retrieval is `weave_search` / `weave_context` /
`weave_graph`, never a filesystem crawl; and if the `weave_*` tools are absent
the CLI fallback (`<repo>/bin/weave add …` / `search …`, absolute path) is the
persistence path. The block does not reuse the shared `_NUDGE` opener: its
"if available" hedge is wrong on a row whose tools are served by a named
extension, and the text is the one verified live on the dev machine.

### Live interactive run (2026-09-05)

`[measured]` One interactive session on the dev machine (Pi 0.84.4,
pi-mcp-adapter 2.32.1, model claude-sonnet-5) after `weave install --harness
pi`-equivalent wiring: the adapter's startup notice reported the thinkweave
server connected with **17 tools**, `/mcp tools` listed them under their bare
`weave_*` names (the `toolPrefix: "none"` key is what makes that so — the
adapter's default would have exposed `thinkweave_weave_search`), and the model
called `weave_search`, `weave_timeline`, `weave_prompts`, `weave_read`,
`weave_create`, `weave_concepts` and `weave_extract` directly, never through
the adapter's `mcp` proxy tool. `/skill:wrap` loaded the root-file link to
`commands/wrap.md`, called `weave_extract` once and `weave wrap-finalize`
once, and made no mid-session extraction call on the earlier turns. The
session folder it wrote holds the prompt events the `before_agent_start`
capture had buffered and `agent_end` had materialised — passive capture and
the explicit wrap met on the same session id (Pi's `PI_SESSION_ID`).

That "met on the same session id" is what the wrap resolver now guarantees
harness-neutrally. When `/wrap` still read the Claude-only `$CLAUDE_SESSION_ID`
directly it read empty on Pi and minted a detached slug instead
(2026-09-08: `wrap-pi-harness-scope-2026-09-08` → ses-e5a02d7a beside the
hook-created ses-851a2f5c). `commands/wrap.md` now resolves the id with
`weave session-id`, which reads Pi's `session_id_env` (`PI_SESSION_ID`). See
[§Session identity and the wrap resolver](#session-identity-and-the-wrap-resolver).

### Hook budgets vs the measured floor

`[measured]` The same session logged one "thinkweave UserPromptSubmit hook
timeout" and one "Stop hook timeout" notice, and `hooks.log` gained 44
`[Errno 32] Broken pipe` entries in the day (22 stop, 20 user_prompt_submit,
2 post_tool_use). Both are one mechanism: shim-core reaps the launcher at its
800 ms telemetry budget and destroys its pipes; the Python handler finishes
the work as the documented orphan, then its final reply write hits the closed
pipe. Nothing was lost — the archived events prove the captures landed — but
the routine case was being reported as a failure. Measured on this host
(WSL2, vault on `/mnt/c`): a no-op Stop through `bin/weave-hook-launch` costs
~1.8 s warm and ~6 s under load; `uv run` itself is 0.02 s and the handler
import 0.07 s, so the floor is vault I/O and index access, not process spawn.
Two changes follow: the handler swallows `BrokenPipeError` on its reply write
(the harness already gave up; there is nothing to report), and the Pi shim
passes per-event budgets above the floor (UserPromptSubmit 2.5 s, Stop 6 s).
The orphan policy is unchanged — a hook that outlives even those still
completes; only the notice and the prompt-time enrichment reply are what a
timeout costs.

### What is NOT verified here

Two earlier entries in this list were written before the run's index rows
had been tied back to its Pi session id; the evidence gathered afterwards
closes them, and it is recorded here rather than silently deleted:

- **SessionStart injection in the interactive run — now verified.** The
  index's `context_served` table holds 45 `startup` rows stamped
  `2026-09-05T22:05:36Z` under session note `ses-887e3f7c`, whose frontmatter
  `source_session` is the Pi session id `01a07399-4e6f-70b0-8e69-46bb56d87142`.
  The startup payload reached that session; the 5 s SessionStart budget was
  not the problem.
- **PostToolUse on Pi — now verified as capture.** The two hook-materialised
  notes of that session, `ses-887e3f7c` and `ses-9ec873ae` (same
  `source_session`), report "2 tool events recorded" and "1 tool events
  recorded": the `tool_result` event fired and the handler wrote the events.
  What remains unverified is narrower — whether adapter-served `weave_*`
  calls reach the **retrieval log** (`retrieval_log.jsonl`) under the
  `mcp__thinkweave__` namespace the shim now restores, i.e. whether the
  handler's retrieval gate classifies them as retrieval rather than as plain
  tool events. No Pi session has yet been inspected for retrieval-log rows.
- **Session fragmentation.** That one Pi session produced **three** session
  notes: two hook-materialised ones 7 s apart (`ses-887e3f7c` at 22:05:40 and
  `ses-9ec873ae` at 22:05:47) plus the wrap's `ses-e108081f`. This is the
  class PR #209 (open, `fix(wrap,judge): resolve session notes by exact id`)
  and #183's logical-session chain address, not a Pi-specific defect; it is
  not fixed here and is the next Pi gap to close once those land.
- **The `pi -p` print-mode path** with the raised budgets — only the
  interactive TUI was driven.
- **Stub staleness in a real branch switch** is covered by the doctor's
  `extension stub` row (below), not by a hook that re-points itself: a Pi
  session started while the dev checkout sits on a branch without
  `shims/pi/` still runs without capture until `weave doctor --mcp --harness
  pi` is consulted or the stub is rewritten.

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
