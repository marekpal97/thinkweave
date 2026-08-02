# Harnesses

> **Scope note.** This file is owned by #105 (W1b), which will carry the full
> capability matrix across every harness. #107 (W2b) created it early because
> its spike answers had to land somewhere citable. Everything below is the
> **Codex** section plus the spike results; #105 should absorb it and add the
> Claude Code column rather than treat this shape as settled.

Per-harness facts live in code, in one place: `HarnessProfile`
(`src/thinkweave/core/harness.py`). This document is the *evidence* behind the
data in those profiles — what was measured, against which version, and what is
still unknown. When the two disagree, the profile is what runs; fix whichever
is wrong.

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
   `budget_tokens=10000`, so without an explicit `additionalContextLimit` the
   headline promise ("the session receives the context payload") fails
   *silently*. The profile sets 12000 on the two events whose handler can emit
   additional context; Codex logs a configuration warning if the key rides any
   other event.

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
correct, and for a sharper reason than "no slash commands": thinkweave also
ships no Codex skills yet (W3 owns the Agent-Skills export), so today the token
resolves to nothing at all.

*Incidental:* Codex also discovers skills from `~/.agents/skills/`, not only
`$CODEX_HOME/skills/`. Relevant to W3's export target.

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
