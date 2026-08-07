@echo off
rem Portable hook launcher — native Windows half of bin/weave-hook-launch.
rem
rem Why this file exists: the canonical hooks/hooks.json fires every thinkweave
rem hook as the extensionless command "<root>/bin/weave-hook-launch". Hook
rem commands are a shell *string*, so on native Windows they run through
rem cmd.exe — and cmd.exe applies PATHEXT even to an explicit path, so it
rem resolves that same extensionless command to THIS file. That is the whole
rem trick: the committed hooks.json needs no Windows variant, and no harness
rem needs a platform-specific command key (Claude Code has no `commandWindows`;
rem verified against the shipped binary). Under Git Bash the POSIX sibling is
rem picked instead. One authored command, two implementations.
rem
rem Kept in step with bin/weave-hook-launch: same 3-tier uv resolution ladder,
rem same loud one-line exit 127 when uv is genuinely absent (#47/#52).
rem Sync-on-run is deliberately preserved here too — on the plugin route, which
rem never runs `weave install`, the launcher's implicit `uv run` sync IS the
rem dependency bootstrap. Only the machine-scope MCP entry passes uv's
rem --no-sync, having synced eagerly at install time.
setlocal EnableExtensions

rem Self-locate the project root (parent of this script's bin/), never the
rem caller's cwd. %~dp0 is this script's own directory with a trailing
rem backslash; %%~fI collapses the "...\bin\.." into a clean absolute path.
rem (Unlike the POSIX launcher's `cd -P` this does not resolve symlinks, which
rem it does not need to: Windows traverses directory symlinks transparently, so
rem a dev-linked checkout still resolves to the real pyproject.toml.)
for %%I in ("%~dp0..") do set "root=%%~fI"

rem The same three tiers, in the same order: PATH, then uv's own default
rem install dir, then an explicit $UV_INSTALL_DIR. `%%~$PATH:X` is cmd.exe's
rem BUILT-IN PATH search — deliberately used instead of `where.exe` so this
rem survives the same stripped harness PATH the POSIX launcher is written for.
rem Each tier probes .exe/.cmd/.bat, since a uv installed by scoop or a shim
rem wrapper is not always a bare .exe.
set "uv_bin="
for %%X in (uv.exe uv.cmd uv.bat) do if not defined uv_bin set "uv_bin=%%~$PATH:X"
for %%X in (uv.exe uv.cmd uv.bat) do if not defined uv_bin if exist "%USERPROFILE%\.local\bin\%%X" set "uv_bin=%USERPROFILE%\.local\bin\%%X"
for %%X in (uv.exe uv.cmd uv.bat) do if not defined uv_bin if defined UV_INSTALL_DIR if exist "%UV_INSTALL_DIR%\%%X" set "uv_bin=%UV_INSTALL_DIR%\%%X"
if not defined uv_bin (
    echo weave-hook-launch: uv not found ^(checked PATH, %USERPROFILE%\.local\bin, %%UV_INSTALL_DIR%%^); install uv from https://docs.astral.sh/uv/getting-started/installation/ or add it to PATH 1>&2
    exit /b 127
)

rem The hook phase (session_start, user_prompt_submit, post_tool_use, stop) and
rem any --harness stamp arrive as %* and are forwarded untouched, exactly as the
rem POSIX launcher forwards "$@".
rem `--no-sync` and `python -m`, matching the POSIX launchers exactly (#156
rem owns that policy: "Runtime MCP/hook launchers use --no-sync by design").
rem uv reinstalls the project by replacing .venv\Scripts\weave-*.exe, a live
rem MCP server holds its own image open, and the sync then dies with
rem `os error 32 ... used by another process` - having already deleted the OTHER
rem shims, which is why module execution is used rather than a console script.
"%uv_bin%" run --no-sync --project "%root%" --extra mcp python -m thinkweave.surfaces.hooks.handler %*
exit /b %ERRORLEVEL%
