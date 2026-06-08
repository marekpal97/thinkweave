#!/usr/bin/env sh
# personal-mem hook wrapper — resolves `uv` across PATH and common install
# locations, then exec's uv with the original arguments.
#
# Why this exists:
#   Claude Code dispatches hooks (and MCP servers) via the spawned shell's
#   PATH. When personal-mem is delivered as a plugin, `.claude-plugin/
#   plugin.json` ships hook entries that call `uv` directly — but the
#   spawned shell may not inherit the user's PATH (depends on how `claude`
#   was launched). If `uv` isn't found, hooks silently no-op and the
#   user's session events never reach the vault.
#
#   This wrapper is the fallback path: search a fixed set of known uv
#   install locations, then exec. Used by plugin.json for both the MCP
#   server entry and every hook entry, so the plugin install never
#   depends on the user's shell PATH being correctly configured for
#   spawned subshells.
#
# Exit semantics:
#   - uv found:      exec's uv with the requested args. Inherits uv's
#                    exit code.
#   - uv not found:  prints a one-line diagnostic to stderr and exits 0.
#                    Returning non-zero would cause CC to surface the
#                    hook as failed, which is the wrong signal — if uv
#                    truly isn't installed, the user has a bigger
#                    problem than this hook firing.
#
# Search order:
#   1. command -v uv          — PATH (the common case)
#   2. ~/.local/bin/uv        — official installer default on Unix
#   3. ~/.cargo/bin/uv        — cargo install (rare but happens)
#   4. /opt/homebrew/bin/uv   — Homebrew on Apple Silicon
#   5. /usr/local/bin/uv      — Homebrew on Intel macOS, manual installs
#
# Windows (PowerShell/cmd) variant: TODO, paired with the Task Scheduler
# work tracked in .claude/plans/windows-scheduler.md. Until then,
# Windows users without WSL hit the non-plugin install path
# (`mem install` + `mem hooks install --scope user`), which resolves
# uv at install time via `shutil.which`.

UV=""

if command -v uv >/dev/null 2>&1; then
    UV=$(command -v uv)
else
    for candidate in \
        "$HOME/.local/bin/uv" \
        "$HOME/.cargo/bin/uv" \
        "/opt/homebrew/bin/uv" \
        "/usr/local/bin/uv"; do
        if [ -x "$candidate" ]; then
            UV="$candidate"
            break
        fi
    done
fi

if [ -z "$UV" ]; then
    echo "personal-mem: uv not found on PATH or in ~/.local/bin, ~/.cargo/bin, /opt/homebrew/bin, /usr/local/bin. Install uv (https://docs.astral.sh/uv/getting-started/) or symlink it into one of those locations." >&2
    exit 0
fi

exec "$UV" "$@"
