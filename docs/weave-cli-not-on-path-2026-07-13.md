# `weave wrap-finalize` fails silently when the CLI isn't on `PATH`

Found during a live `/wrap` on a fresh Windows shell (2026-07-13): `weave_extract`
(the MCP half of `/wrap`) succeeded, but the finalize step reported "the `weave`
CLI isn't available in this environment/PATH — the MCP tools worked (extraction
succeeded), but the finalize step's shell command isn't reachable from here."

## Root cause

The `weave` / `weave-hook` / `weave-mcp` console scripts are installed only into
this repo's virtualenv (`.venv/Scripts` on Windows, `.venv/bin` on POSIX). Nothing
in `weave install` or the onboarding flow adds that directory to the user's
`PATH`, so a bare `weave …` invocation only resolves in a shell where that
`Scripts`/`bin` dir happens to already be on `PATH` (e.g. one where the venv was
manually activated).

This bit two different call sites the same day:

- `weave install` itself refuses to run (`error: required console scripts
  missing from PATH`) unless `Scripts`/`bin` is already on `PATH`.
- `/wrap`'s deterministic tail (`commands/wrap.md`, step 4:
  `weave wrap-finalize <session_id> --project <project>`) is a bare shell
  call, so it hits the same resolution failure.

Both failures are asymmetric with the MCP half of the system: `weave_extract`
and friends are launched by Claude Code itself via an absolute command
(`uv run --project <repo> --extra mcp weave-mcp`, registered in `.mcp.json` /
`~/.claude.json`), which never touches `PATH` — so a fresh shell can look
completely broken for the CLI half while the MCP half works perfectly, which is
confusing to debug from the error message alone.

## Workaround (applied here)

Added the venv's `Scripts` directory to the user-level persistent `PATH`
environment variable on this machine. Any new shell now resolves `weave`
directly; existing/spawned shells with sessions that predate the change still
need it exported manually:

```bash
export PATH="<repo>/.venv/Scripts:$PATH"
```

## Suggested fix (not implemented here)

1. **`weave install`** already checks for the console scripts on `PATH`
   (`_check_scripts` in `src/thinkweave/surfaces/cli/install.py`) and aborts
   with a clear message if they're missing — but it only tells the user to
   `pip install -e .[all]`, which doesn't address `PATH` at all if the scripts
   already exist in the venv. It could detect "scripts exist but aren't on
   `PATH`" as a distinct case and offer to persist the venv's `Scripts`/`bin`
   dir the same way it already offers to splice the `CLAUDE.md` nudge
   (preview + `--yes` gate).
2. **`/wrap`'s finalize step** could avoid depending on `PATH` at all by
   invoking through the same absolute path the MCP entry already uses (e.g.
   `uv run --project <repo> --extra mcp weave wrap-finalize …` instead of a
   bare `weave wrap-finalize …`), removing the asymmetry between the MCP and
   CLI halves of the skill.
