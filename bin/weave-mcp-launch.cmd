@echo off
rem Portable MCP-server launcher — native Windows half of bin/weave-mcp-launch.
rem
rem Unlike the hook launcher, this one is NOT reached by PATHEXT. An MCP server
rem is spawned as command + args with no shell in between, and a direct
rem CreateProcess does not consult PATHEXT — so the extensionless
rem "bin/weave-mcp-launch" in .mcp.json / plugin.json fails outright on native
rem Windows (WinError 2) rather than falling through to this file. Those
rem manifests are shared with POSIX and stay as they are; the supported native
rem Windows routes are:
rem
rem   * `weave install` (machine scope) — writes an absolute uv.exe entry
rem     directly, which needs no launcher at all. This is the recommended path,
rem     and what `weave doctor --mcp` points you at.
rem   * a hand-edited project/plugin entry naming THIS file explicitly:
rem       "command": "bin/weave-mcp-launch.cmd"
rem
rem Kept in step with bin/weave-mcp-launch: same 3-tier uv resolution ladder,
rem same loud one-line exit 127 (#47/#52), and the same guarded one-time
rem bootstrap sync (#164) so the plugin route — which never runs
rem `weave install` — still populates its clone's venv on first launch.
rem
rem `@echo off` above is load-bearing, not cosmetic: this process speaks the MCP
rem stdio protocol on stdout, so a single echoed command line would corrupt the
rem very first JSON-RPC frame.
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
    echo weave-mcp-launch: uv not found ^(checked PATH, %USERPROFILE%\.local\bin, %%UV_INSTALL_DIR%%^); install uv from https://docs.astral.sh/uv/getting-started/installation/ or add it to PATH 1>&2
    exit /b 127
)

rem One-time dependency bootstrap (#164, matches the POSIX launchers): a
rem fresh plugin/marketplace clone has no venv, and `uv run --no-sync` would
rem fabricate an EMPTY one and die with ModuleNotFoundError. When the venv
rem lacks the project's own console script, run the ONE sanctioned sync
rem (dec-3d4f8ce9): --extra all, never per-call, never a narrower extra set.
rem Output to stderr: stdout is this process's MCP JSON-RPC stdio channel.
if not exist "%root%\.venv\Scripts\weave.exe" (
    "%uv_bin%" sync --project "%root%" --extra all 1>&2
    if errorlevel 1 exit /b 1
)

rem `--no-sync` and `python -m`, matching the POSIX launchers exactly (#156
rem owns that policy: "Runtime MCP/hook launchers use --no-sync by design").
rem uv reinstalls the project by replacing .venv\Scripts\weave-*.exe, a live
rem MCP server holds its own image open, and the sync then dies with
rem `os error 32 ... used by another process` - having already deleted the OTHER
rem shims, which is why module execution is used rather than a console script.
"%uv_bin%" run --no-sync --project "%root%" --extra mcp python -m thinkweave.surfaces.mcp.server %*
exit /b %ERRORLEVEL%
