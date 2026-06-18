#!/usr/bin/env bash
# validate-install.sh — end-to-end capability validation for a Thinkweave install.
#
# Run this AFTER the manual setup for a route (see README of this script below):
#   1. clear any prior install         (weave uninstall --yes)
#   2. load the plugin                  (marketplace install  OR  weave dev-link)
#   3. restart Claude Code (launched with THINKWEAVE_VAULT pointed at a TEST vault)
#   4. run /thinkweave:onboard in that live session (seed + ontology + smoke + landing)
#   5. THEN run this script against the same TEST vault.
#
# It validates the layers a script CAN reach: route artifact, vault+index,
# `weave doctor`, the MCP server (real JSON-RPC handshake), all four lifecycle
# hooks, the onboard outcome (seeded notes / concepts / landing docs),
# retrieval (FTS/context/graph/timeline), the RLVR substrate, and the scheduler
# render. With --heavy it also runs a live `/thinkweave:dream` cycle.
#
# Usage:
#   THINKWEAVE_VAULT=~/tw-test-vault scripts/validate-install.sh [--heavy]
#
#   --heavy   also run a headless `/thinkweave:dream` (costs subscription usage
#             + a few minutes). Needs `claude` on PATH + an authenticated Claude
#             Code login — NO Anthropic API key (/dream runs on the session's
#             model auth, not an API call). Set VALIDATE_NEWS_URL=<url> to also
#             exercise a one-shot /news ingest.
#
# Exit code: 0 if every hard check passed, 1 otherwise. INFO checks never fail
# the run (e.g. embeddings cache empty → FTS-only, which is acceptable).

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HEAVY=0
[[ "${1:-}" == "--heavy" ]] && HEAVY=1

WEAVE=(uv run --project "$REPO" weave)
HOOK=(uv run --project "$REPO" weave-hook)

pass=0; fail=0; info=0
ok()   { printf '  \033[32m[PASS]\033[0m %s\n' "$1"; pass=$((pass+1)); }
no()   { printf '  \033[31m[FAIL]\033[0m %s\n' "$1"; fail=$((fail+1)); }
note() { printf '  \033[33m[INFO]\033[0m %s\n' "$1"; info=$((info+1)); }
hdr()  { printf '\n=== %s ===\n' "$1"; }

# ---------- guards ----------
: "${THINKWEAVE_VAULT:?set THINKWEAVE_VAULT to your TEST vault before running}"
export THINKWEAVE_VAULT
printf 'Validating Thinkweave install\n  repo : %s\n  vault: %s\n  heavy: %s\n' \
  "$REPO" "$THINKWEAVE_VAULT" "$([[ $HEAVY == 1 ]] && echo yes || echo no)"

# ---------- 0. route detection ----------
hdr "0. Route detection"
LINK="$HOME/.claude/skills/thinkweave"
if [[ -L "$LINK" ]]; then
  ok "symlink route: $LINK → $(readlink -f "$LINK")"
elif grep -q 'thinkweave' "$HOME/.claude/plugins/installed_plugins.json" 2>/dev/null; then
  ok "marketplace route: thinkweave in installed_plugins.json"
else
  no "no plugin-route artifact (neither ~/.claude/skills/thinkweave symlink nor an installed plugin)"
fi
if python3 - <<'PY' 2>/dev/null; then
import json, pathlib, sys
p = pathlib.Path.home() / ".claude.json"
d = json.loads(p.read_text()) if p.exists() else {}
sys.exit(0 if "thinkweave" in d.get("mcpServers", {}) else 1)
PY
  note "a raw ~/.claude.json thinkweave entry is ALSO present — double-registration risk; run \`weave uninstall\`"
fi

# ---------- 1. vault + index ----------
hdr "1. Vault + index"
[[ -d "$THINKWEAVE_VAULT" ]] && ok "vault dir exists" || no "vault dir missing"
[[ -f "$THINKWEAVE_VAULT/.weave/index.db" ]] && ok "index.db present" || no "index.db missing (run: weave index)"

# ---------- 2. doctor ----------
hdr "2. weave doctor --mcp"
if "${WEAVE[@]}" doctor --mcp 2>&1 | grep -q "overall: PASS"; then
  ok "doctor --mcp → overall PASS"
else
  no "doctor --mcp did not report overall PASS"
fi

# ---------- 3. MCP server boot ----------
hdr "3. MCP server boot (JSON-RPC initialize)"
init_resp=$(printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"validate","version":"0"}}}' \
  | timeout 40 uv run --project "$REPO" --extra mcp weave-mcp 2>/dev/null | head -c 500)
if grep -q '"serverInfo"' <<<"$init_resp"; then
  ok "weave-mcp answered initialize ($(grep -oE '"name":"[^"]+"' <<<"$init_resp" | head -1))"
else
  no "weave-mcp did not respond to initialize"
fi

# ---------- 4. lifecycle hooks ----------
hdr "4. Lifecycle hooks (synthetic session)"
SID="validate-smoke-$$"
runhook() { echo "$1" | "${HOOK[@]}" "$2" >/dev/null 2>&1; }
runhook '{"session_id":"'"$SID"'","cwd":"'"$THINKWEAVE_VAULT"'","hook_event_name":"SessionStart","source":"startup"}' session_start \
  && ok "session_start exit 0" || no "session_start failed"
runhook '{"session_id":"'"$SID"'","cwd":"'"$THINKWEAVE_VAULT"'","hook_event_name":"UserPromptSubmit","prompt":"validate probe"}' user_prompt_submit \
  && ok "user_prompt_submit exit 0" || no "user_prompt_submit failed"
runhook '{"session_id":"'"$SID"'","cwd":"'"$THINKWEAVE_VAULT"'","hook_event_name":"PostToolUse","tool_name":"Bash","tool_input":{"command":"ls"},"tool_response":{"stdout":"x"}}' post_tool_use \
  && ok "post_tool_use exit 0" || no "post_tool_use failed"
runhook '{"session_id":"'"$SID"'","cwd":"'"$THINKWEAVE_VAULT"'","hook_event_name":"Stop"}' stop \
  && ok "stop exit 0" || no "stop failed"
if find "$THINKWEAVE_VAULT" -path "*$SID*" -name "session.md" 2>/dev/null | grep -q .; then
  ok "Stop materialized a session note from the buffer"
else
  no "no session note produced by the hook pipeline"
fi

# ---------- 5. onboard outcome ----------
hdr "5. Onboard outcome (seeded vault)"
n_sessions=$(find "$THINKWEAVE_VAULT/projects" -name "session.md" 2>/dev/null | grep -vc "$SID")
[[ "${n_sessions:-0}" -ge 1 ]] && ok "imported session notes present ($n_sessions)" \
  || no "no imported session notes — did /thinkweave:onboard run its CC import?"
if "${WEAVE[@]}" concepts list 2>/dev/null | grep -q .; then
  ok "ontology has canonical concepts"
else
  note "concepts list empty — ontology bootstrap may not have run (ok if CC history was tiny)"
fi
if find "$THINKWEAVE_VAULT/projects" \( -name "STATE.md" -o -name "DECISIONS.md" -o -name "BACKLOG.md" \) 2>/dev/null | grep -q .; then
  ok "landing docs (STATE/DECISIONS/BACKLOG) written"
else
  note "no landing docs found — onboard's landing step may have been skipped"
fi

# ---------- 6. retrieval (3 modalities) ----------
hdr "6. Retrieval"
[[ -n "$("${WEAVE[@]}" search "" --type session --limit 5 2>/dev/null)" ]] \
  && ok "FTS list-mode returns sessions" || no "FTS search returned nothing"
[[ -n "$("${WEAVE[@]}" context "project" 2>/dev/null)" ]] \
  && ok "context composition returns a blob" || note "context returned empty (sparse vault)"
[[ -n "$("${WEAVE[@]}" timeline --days 3650 2>/dev/null)" ]] \
  && ok "timeline returns a window" || note "timeline empty"
nid=$("${WEAVE[@]}" search "" --limit 20 2>/dev/null | grep -oE '[a-z]+-[0-9a-f]{6,}' | head -1)
if [[ -n "$nid" ]] && "${WEAVE[@]}" graph "$nid" --depth 1 >/dev/null 2>&1; then
  ok "graph walk from $nid succeeded"
else
  note "graph walk skipped (no id parsed / sparse graph)"
fi

# ---------- 7. RLVR substrate ----------
hdr "7. RLVR substrate"
rows=$("${WEAVE[@]}" rlvr export 2>/dev/null | grep -c . || true)
[[ "${rows:-0}" -ge 1 ]] && ok "rlvr export emitted $rows row(s)" \
  || note "rlvr export empty (no committed decisions yet — expected on a fresh vault)"

# ---------- 8. scheduler render ----------
hdr "8. Scheduler render (dry-run, no install)"
sched=$("${WEAVE[@]}" schedule install --dry-run 2>&1)
if grep -q "thinkweave" <<<"$sched"; then ok "schedule renders a thinkweave block"; else no "schedule render produced no block"; fi
if [[ -L "$LINK" ]]; then
  if grep -q "/thinkweave:dream\|thinkweave:dream" <<<"$sched"; then
    ok "plugin route → cron uses namespaced /thinkweave:dream"
  else
    no "plugin route but cron renders bare /dream — namespace-detection gap"
  fi
fi

# ---------- 9. heavy: live dream + optional news ----------
if [[ $HEAVY == 1 ]]; then
  hdr "9. Heavy — live capabilities (headless Claude Code)"
  if ! command -v claude >/dev/null 2>&1; then
    no "--heavy requested but \`claude\` not on PATH"
  else
    [[ -z "${ANTHROPIC_API_KEY:-}" ]] && note "ANTHROPIC_API_KEY unset — /dream runs on your Claude Code login auth (no key needed for interactive runs)"
    if [[ -n "${VALIDATE_NEWS_URL:-}" ]]; then
      src_before=$(find "$THINKWEAVE_VAULT" -path "*/sources/*" -name "*.md" 2>/dev/null | wc -l)
      claude -p "/thinkweave:news $VALIDATE_NEWS_URL" >/dev/null 2>&1 || true
      src_after=$(find "$THINKWEAVE_VAULT" -path "*/sources/*" -name "*.md" 2>/dev/null | wc -l)
      [[ "$src_after" -gt "$src_before" ]] && ok "/news ingested a source note ($src_before→$src_after)" \
        || no "/news produced no new source note"
    fi
    dig_before=$(find "$THINKWEAVE_VAULT/digests" -name "*.md" 2>/dev/null | wc -l)
    mlog="$THINKWEAVE_VAULT/.weave/maintenance.jsonl"
    mlines_before=$([[ -f "$mlog" ]] && wc -l <"$mlog" || echo 0)
    echo "  running /thinkweave:dream (this takes a few minutes)…"
    claude -p "/thinkweave:dream" >/dev/null 2>&1 || true
    dig_after=$(find "$THINKWEAVE_VAULT/digests" -name "*.md" 2>/dev/null | wc -l)
    mlines_after=$([[ -f "$mlog" ]] && wc -l <"$mlog" || echo 0)
    [[ "$mlines_after" -gt "$mlines_before" ]] && ok "dream wrote a maintenance.jsonl cycle line" \
      || no "dream produced no maintenance.jsonl line"
    [[ "$dig_after" -ge "$dig_before" ]] && note "digests: $dig_before→$dig_after (may no-op on a tiny vault)"
  fi
else
  hdr "9. Heavy — skipped (pass --heavy to run a live /dream + optional /news)"
fi

# ---------- summary ----------
hdr "Summary"
printf '  PASS=%d  FAIL=%d  INFO=%d\n' "$pass" "$fail" "$info"
if [[ "$fail" -gt 0 ]]; then
  printf '\n\033[31mValidation FAILED — %d hard check(s) did not pass.\033[0m\n' "$fail"
  exit 1
fi
printf '\n\033[32mValidation PASSED — every hard check is green.\033[0m\n'
