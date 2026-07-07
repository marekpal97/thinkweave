# Windows/git-bash runtime gaps found during personal_mem → thinkweave cutover

Found while executing the actual cutover (hooks install, scheduler install,
`weave --help`) on Windows with Claude Code's hooks/bash surface running
through git-bash, after the data-migration phases documented in
`docs/migration-findings-2026-07-06.md`, `docs/hubs-plan-singleton-preflight-2026-07-06.md`,
and `docs/theme-hub-backfill-gap-2026-07-07.md`. All three below are freshly
reproduced with full tracebacks, not carried over from memory.

---

## 1. `weave hooks install` writes a Windows backslash path that git-bash mangles into garbage

**Where:** `surfaces/hooks/install.py` (`_resolve_hook_cmd`)

**What happens:** `_resolve_hook_cmd()` resolves the `weave-hook` console
script via `shutil.which()` / `Path(sys.executable).parent`, both of which
return a Windows-native backslash path on this platform (e.g.
`C:\Users\m.paluch\source\thinkweave\.venv\Scripts\weave-hook.EXE`). That
literal string gets written straight into `settings.json` as the hook
`command`. Claude Code's hook runner executes `command` through
`/usr/bin/bash` (git-bash) even on Windows — and bash treats an unescaped
backslash before an ordinary character as an escape that just drops the
backslash and keeps the character (`\U` → `U`, `\s` → `s`, etc.), not as a
path separator. Every hook invocation (`SessionStart`, `UserPromptSubmit`,
both `PostToolUse` matchers, `Stop`) failed identically:

```
/usr/bin/bash: line 1: C:Usersm.paluchsourcethinkweave.venvScriptsweave-hook.EXE: command not found
```

**Confirmed via:** ran `weave hooks install --scope user` for real on this
machine, then started using Claude Code — every SessionStart/PostToolUse/
Stop hook fired this exact error, on every tool call, until fixed by hand.

**Fix applied by hand:** rewrote all 5 `command` strings in
`~/.claude/settings.json` to use forward slashes
(`C:/Users/m.paluch/source/thinkweave/.venv/Scripts/weave-hook.EXE`), which
both the Windows CreateProcess path and git-bash handle correctly.
Re-verified each hook subcommand (`session_start`, `post_tool_use`, `stop`)
exits 0 after the fix.

**Suggested fix:** `_resolve_hook_cmd()` should emit a forward-slash form
on Windows (`str(path).replace("\\", "/")`, or build the path with
`PurePosixPath` semantics from the start) before writing it into
`settings.json`, since the consumer is always a POSIX-style shell
(git-bash) regardless of host OS. Anyone on Windows running
`weave hooks install` today gets a hook install that's silently broken
until they notice the error spam and fix `settings.json` by hand.

---

## 2. `PERSONAL_MEM_VAULT`'s "migration fallback" priority silently misdirects commands away from the intended vault — confirmed twice, independently

**Where:** `core/config.py`, `load_config()`:
`os.environ.get("THINKWEAVE_VAULT") or os.environ.get("PERSONAL_MEM_VAULT")`

**What happens:** `PERSONAL_MEM_VAULT` is treated as an equal-priority
fallback to `THINKWEAVE_VAULT` for `vault_root` resolution. The comment in
the source even calls it out as temporary: *"the pre-rename name, honoured
as a migration fallback ... drop once shells are updated."* The problem:
this variable is not just a thinkweave legacy alias — it's **also
personal_mem's own primary (non-fallback) vault-root variable**
(`personal_mem/src/personal_mem/config.py`: `os.environ.get
("PERSONAL_MEM_VAULT")`, no fallback tier at all). During an in-place
personal_mem → thinkweave migration, this variable is realistically always
set — to whatever personal_mem still considers its live vault — for as
long as personal_mem hasn't been fully retired yet. Any thinkweave command
run from a shell/process where `THINKWEAVE_VAULT` isn't also set in that
exact process's environment silently resolves to personal_mem's vault
path instead of the intended thinkweave one, with no warning that this
happened.

**Confirmed twice, independently, on this same migration:**
1. During onboarding (2026-07-06): a restarted MCP server briefly bound to
   the live personal_mem vault instead of the intended clone, because the
   server process's environment had `PERSONAL_MEM_VAULT` but not (yet) a
   correctly-propagated `THINKWEAVE_VAULT`.
2. During final cutover (2026-07-07): `weave index` / `weave index --embed
   --only-new`, run from a shell whose `THINKWEAVE_VAULT` wasn't set (a
   persistent Windows User-scope `THINKWEAVE_VAULT` existed, but this
   particular git-bash process predated it being set and hadn't picked it
   up), silently indexed and embedded into the *legacy* vault's stray,
   already-known-unused `.weave/index.db` instead of the intended clone —
   confirmed by comparing `index.db` mtimes across both vault directories
   after the fact. No error, no warning; the command reported a plausible-
   looking success (`Indexed: 3692, ...`) against the wrong vault.

**Impact:** No data was lost in either incident (both were caught by
independently cross-checking file mtimes / vault stats after the fact,
not by anything in `weave`'s own output), but the second incident shows
this isn't a one-off environment quirk — it's a structural footgun that
will keep recurring for any long-running personal_mem → thinkweave
migration, and it's exactly the kind of mistake that's easy to make with a
destructive command (`weave dream apply`, a hub-merge, a bulk edit) instead
of a read-then-reindex one.

**Suggested fix:**
- At minimum, have `load_config()` print a one-line stderr notice when
  `vault_root` was resolved via the `PERSONAL_MEM_VAULT` fallback tier
  specifically (not the normal `THINKWEAVE_VAULT` path), so it's visible
  in every command's output rather than silent.
- Consider gating the fallback on a companion signal that the vault is
  actually a not-yet-migrated personal_mem vault (e.g. presence of `.mem/`
  without `config/config.toml`), rather than accepting any
  `PERSONAL_MEM_VAULT` value unconditionally — a personal_mem vault and a
  thinkweave vault are not interchangeable once migration has begun.

---

## 3. `weave --help` (and any subcommand's `--help`) crashes with `ValueError: unsupported format character 'A'`

**Where:** `surfaces/cli/_parser_basics.py:340` — the `config` subcommand's
help text:

```python
help="Inspect or set the user config (vault path) — platform-resolved "
"location (XDG on Linux/macOS, %APPDATA% on Windows).",
```

**What happens:** `argparse.HelpFormatter._expand_help` does
`self._get_help_string(action) % params` — a raw `%`-style format
operation against the action's help string. The literal `%APPDATA%` in
this help text is interpreted by that format operation as the start of a
conversion spec (`%A`), and `'A'` isn't a valid format character, so
`_expand_help` raises instead of returning text. Since top-level
`weave --help` recursively formats the help of *every* registered
subcommand (including `config`) to build its summary listing, this one
subcommand's help string is enough to crash `weave --help` entirely, not
just `weave config --help`.

**Confirmed via:** `weave --help` and `weave config --help`, this session,
full traceback:

```
  File ".../argparse.py", line 627, in _expand_help
    return self._get_help_string(action) % params
           ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^~~~~~~~
ValueError: unsupported format character 'A' (0x41) at index 95
```

**Impact:** `weave --help` is one of the most basic discovery tools for
the CLI — it's completely unusable on the current release for any user
who reaches for it, on any platform (the crash is triggered by building
the *complete* top-level help text, which always includes the `config`
subcommand's summary regardless of host OS).

**Suggested fix:** Escape the literal percent signs in that help string as
`%%APPDATA%%` (the standard argparse escape for a literal `%` in help
text), or avoid embedding `%ENV_VAR%`-style Windows syntax in help strings
at all (e.g. spell it "the APPDATA environment variable" instead). Worth a
quick repo-wide grep for other literal, un-escaped `%` characters in
`help=`/`description=` strings — this class of bug is invisible until
`--help` is actually invoked for the affected subcommand (or, as here, for
anything that recursively renders it), so it can ship silently.

---

## Context

Found during the actual cutover step (hooks install, scheduler install,
first post-migration CLI usage) of the personal_mem → thinkweave migration
documented across the three companion findings docs listed at the top.
Unlike those, all three items here are Windows/git-bash-specific runtime
issues rather than data-migration issues — grouped separately since they
affect any thinkweave user on this platform, not just someone migrating
from personal_mem.
