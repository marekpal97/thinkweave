#!/usr/bin/env bash
# Wrapper script for personal_mem hooks — ensures PYTHONPATH is set.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEM_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
export PYTHONPATH="$MEM_ROOT/src:${PYTHONPATH:-}"
exec python3 -m personal_mem.hooks.handler "$@"
