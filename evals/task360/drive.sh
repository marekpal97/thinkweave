#!/usr/bin/env bash
# The task-360 driver: real interactive sessions in a throwaway sandbox repo,
# run through herdr. Every session's hooks, MCP server and CLI calls see
# THINKWEAVE_VAULT set to a throwaway vault, so the live vault is never touched.
#
#   drive.sh setup             fresh sandbox repo, throwaway vault, herdr workspace
#   drive.sh run S2            run S2, its prerequisites first (setup when none)
#   drive.sh check [S2 ...]    the oracle's verdicts over the throwaway index
#   drive.sh finish            close the workspace and every pane in it
#
# The verbs scenarios are built from:
#   start <label> <claude|codex|pi>   split a pane and start a session in it
#   say <label> <prompt>              prompt and wait for the turn to settle
#   settle <label>                    wait until idle with no shell still running
#   wrap <label>                      settle, run the harness's wrap skill, settle
#   stop <label>                      settle, then exit the session; the pane stays
#   screen <label> [lines]            the session's recent terminal text
#
# No session starts with a permission-bypass flag: the sandbox's own project
# settings grant what the scenarios need (see RUNBOOK.md for the harness that
# cannot, and the step a human runs there).

set -uo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$HERE/../.." && pwd)
ROOT=${TASK360_ROOT:-$HOME/.local/state/task360}
STATE=$ROOT/state.json
SANDBOX=$ROOT/sandbox
VAULT=$ROOT/vault
PROJECT=task360_sandbox
REPO=task360/sandbox
TIMEOUT_MS=${TIMEOUT_MS:-1200000}   # 20 minutes per wait

declare -A WRAP=([claude]="/wrap" [codex]='$thinkweave-wrap' [pi]="/skill:wrap")
# Keys that end a session at an empty input; a slash command sent as a prompt
# reaches the model, not the harness. A spare press would exit the pane's shell.
declare -A EXIT=([claude]="ctrl+d ctrl+d" [codex]="ctrl+d" [pi]="ctrl+d")

# Each scenario's prerequisites: the scenarios whose state it builds on.
declare -A NEEDS=(
  [S0]="" [S1]=S0 [S2]=S1 [S3]=S2 [S4]=S3 [S5]=S4 [S6]=S5 [S7]=S0 [S8]=S1
  [S9]=S1 [S10]=S9 [S11]=S10 [S12]=S7 [S13]=S7 [S14]=S7 [S15]=S7
)

# ---------------------------------------------------------------------------
# Scenarios

scenario_S0() { start S0 claude && say S0 "$(prompt S0)" && stop S0; }
scenario_S1() { _solo S1; }
scenario_S2() { _solo S2; }

scenario_S3() {  # two background helpers that outlive the parent's turn
  start S3 claude && say S3 "$(prompt S3.1)" || return 1
  herdr agent wait "$(_name S3)" --until working --timeout "$TIMEOUT_MS" >/dev/null
  settle S3 && say S3 "$(prompt S3.2)" && wrap S3 && stop S3
}

scenario_S4() { _solo S4; }

scenario_S5() {
  _worker S5 claude 1 "greet --banner" \
    "Add a '--banner' flag to dogfood greet that prints a line of = above the greeting."
}

scenario_S6() {  # a correction mid-session, then late feedback as a note
  start S6 claude && say S6 "$(prompt S6.1)" && say S6 "$(prompt S6.2)" \
    && wrap S6 && stop S6 || return 1
  local task; task=$(_work_task 1)
  (cd "$SANDBOX" && _weave add "Review of #1" --project "$PROJECT" \
    -f "feedback_for=[\"$task\"]" -b "$(prompt S6.feedback)")
}

scenario_S7() { _solo S7; }

scenario_S8() {  # the devloop route: a trajectory payload lands as a round
  local payload=$ROOT/s8-payload.json id
  sed "s#__REPO__#$REPO#g" "$HERE/s8-payload.json" >"$payload"
  local -a fm
  mapfile -t fm < <(jq -r '.frontmatter | to_entries[]
    | "-f", "\(.key)=\(if (.value|type)=="string" then .value else (.value|tojson) end)"' "$payload")
  id=$(cd "$SANDBOX" && _weave add "loop trajectory #1: greet CLI" --project "$PROJECT" \
    --tags loop-run "${fm[@]}" -f 'concepts=["task-trajectory","agentic-development"]' \
    | grep -o '\[[^]]*\]' | tr -d '[]')
  (cd "$SANDBOX" && _weave task record-run "$payload" --trajectory "$id" --project "$PROJECT")
}

scenario_S9() {  # a second wrap must change nothing
  start S9 claude && say S9 "$(prompt S9)" && wrap S9 || return 1
  oracle snapshot | jq --arg r "github:$REPO#1" \
    '[.tasks[] | select(.grain=="work" and .asked==$r)][0]' >"$ROOT/s9-before.json"
  wrap S9 && stop S9
}

scenario_S10() { _solo S10; }
scenario_S11() { :; }  # read-back only: the checkpoint is the scenario
scenario_S12() { _solo S12 codex; }

scenario_S13() {
  _worker S13 codex 3 "version --pretty" "Add --pretty to 'dogfood version --json'."
}

scenario_S14() {
  _worker S14 pi 3 "version --indent" "Add --indent N to 'dogfood version --json'."
}

scenario_S15() { _solo S15 codex; }

_solo() {  # _solo <label> [harness]: one prompt, a wrap, a stop
  start "$1" "${2:-claude}" && say "$1" "$(prompt "$1")" && wrap "$1" && stop "$1"
}

_worker() {  # _worker <label> <worker kind> <ticket> <title> <work>
  local label=$1
  start "$label" claude || return 1
  say "$label" "$(TICKET=$3 TITLE=$4 WORK=$5 KIND=$2 WORKER="$(_name "$label")w" \
    envsubst '${TICKET} ${TITLE} ${WORK} ${KIND} ${WORKER}' <"$HERE/prompts/worker.txt")" \
    || return 1
  note "$label" task_id "$(screen "$label" 200 | grep -o 'TASK_ID=tsk-[0-9a-f]*' | tail -1 | cut -d= -f2)"
  wrap "$label" && stop "$label"
}

# ---------------------------------------------------------------------------
# Run, check, setup, finish

run() {  # run <label>: prerequisites first, each once per setup
  local label=${1:?run <label>} need
  [ -n "${NEEDS[$label]+x}" ] || { echo "unknown scenario $label" >&2; return 2; }
  [ -f "$STATE" ] || setup || return 1
  [ "$(_get "labels.$label.done")" = true ] && return 0
  for need in ${NEEDS[$label]}; do run "$need" || return 1; done
  echo "== $label"
  "scenario_$label" || { echo "$label: scenario failed; panes stay open for a look" >&2; return 1; }
  _state "s['labels'].setdefault('$label', {})['done'] = True"
  check "$label" || true
}

check() { oracle check "$@"; }

oracle() { uv run --project "$REPO_ROOT" --no-sync python "$HERE/oracle.py" --root "$ROOT" "$@"; }

setup() {  # a fresh sandbox repo and an empty throwaway vault, in a new workspace
  if [ -e "$ROOT" ] && [ ! -f "$STATE" ] && [ -n "$(ls -A "$ROOT")" ]; then
    echo "refusing to wipe $ROOT: it is not a task-360 run root" >&2; return 1
  fi
  finish 2>/dev/null
  rm -rf "$ROOT" && mkdir -p "$ROOT" "$VAULT"
  cp -r "$HERE/sandbox" "$SANDBOX" && mv "$SANDBOX/gitignore" "$SANDBOX/.gitignore"
  git -C "$SANDBOX" init -q -b main
  git -C "$SANDBOX" remote add origin "git@github.com:$REPO.git"
  git -C "$SANDBOX" add -A && git -C "$SANDBOX" commit -q -m scaffold
  local live
  live=$(env -u THINKWEAVE_VAULT -u THINKWEAVE_WEAVE_DIR \
    uv run --project "$REPO_ROOT" --no-sync weave config show | sed -n 's/^vault_root: //p')
  _weave init >/dev/null
  # The live ontology, read-only, so concept gating behaves as it does live.
  cp "$live/config/ontology.yaml" "$live/config/concept_aliases.yaml" "$VAULT/config/"
  local out ws pane
  out=$(herdr workspace create --cwd "$SANDBOX" --label task360 --no-focus \
    --env THINKWEAVE_VAULT="$VAULT" --env THINKWEAVE_PROJECT="$PROJECT" \
    --env PYTHONPATH="$REPO_ROOT/src") || return 1
  ws=$(jq -r .result.workspace.workspace_id <<<"$out")
  pane=$(jq -r .result.root_pane.pane_id <<<"$out")
  _state "s.update(root='$ROOT', sandbox='$SANDBOX', vault='$VAULT', live_vault='$live',
    project='$PROJECT', repo='$REPO', workspace='$ws', root_pane='$pane', last_pane='',
    labels={})"
  echo "task360: herdr workspace '$ws' (label task360) holds every session; root pane $pane"
  echo "task360: sandbox $SANDBOX, throwaway vault $VAULT"
}

finish() {  # close the run's workspace; the run root stays for inspection
  local ws; ws=$(_get workspace)
  [ -n "$ws" ] && herdr workspace close "$ws" >/dev/null && echo "task360: closed workspace $ws"
}

# ---------------------------------------------------------------------------
# Session verbs

start() {  # start <label> <claude|codex|pi>
  local label=$1 kind=$2 name out pane from dir
  name=$(_name "$label")
  from=$(_get last_pane); dir=down
  [ -z "$from" ] && { from=$(_get root_pane); dir=right; }
  out=$(herdr pane split --pane "$from" --direction "$dir" --cwd "$SANDBOX" \
    --env THINKWEAVE_VAULT="$VAULT" --env THINKWEAVE_PROJECT="$PROJECT" \
    --env PYTHONPATH="$REPO_ROOT/src") || return 1
  pane=$(jq -r .result.pane.pane_id <<<"$out")
  _state "s['last_pane'] = '$pane'
s['labels'].setdefault('$label', {}).update(harness='$kind', name='$name', pane='$pane')"
  if ! herdr agent start "$name" --kind "$kind" --pane "$pane" --timeout 60000 >/dev/null 2>&1; then
    _answer_trust "$label" || { echo "$label: start failed"; screen "$label" 40; return 1; }
  fi
  _steady "$label"
  echo "$label: $kind started in workspace $(_get workspace), pane $pane"
}

say() {  # say <label> <prompt>
  local name out; name=$(_name "$1")
  out=$(herdr agent prompt "$name" "$2" --wait --timeout "$TIMEOUT_MS" 2>&1)
  if grep -q agent_prompt_stalled <<<"$out"; then
    # An autocomplete popup (Codex `$skill`) swallowed the submit; submit again.
    herdr agent send-keys "$name" enter >/dev/null
    out=$(herdr agent wait "$name" --timeout "$TIMEOUT_MS" 2>&1)
  fi
  while [ "$(jq -r '.result.agent.agent_status // empty' <<<"$out" 2>/dev/null)" = blocked ]; do
    echo "HUMAN STEP: $1 is blocked on an approval in pane $(_get "labels.$1.pane")" \
      "(workspace $(_get workspace)); answer it there." >&2
    out=$(herdr agent wait "$name" --until idle --until done --timeout "$TIMEOUT_MS" 2>&1)
  done
  jq -c '.result.agent.agent_status // .error // .' <<<"$out" 2>/dev/null || echo "$out"
  _rekey "$1"
}

settle() {  # settle <label>: idle, and no shell left running under the agent
  local name pid deadline=$((SECONDS + TIMEOUT_MS / 1000))
  name=$(_name "$1")
  pid=$(herdr pane process-info --pane "$(_get "labels.$1.pane")" \
    | jq -r .result.process_info.foreground_process_group_id)
  while [ "$SECONDS" -lt "$deadline" ]; do
    herdr agent wait "$name" --until idle --until done --timeout "$TIMEOUT_MS" >/dev/null 2>&1
    if [ "$(_shells "$pid")" -eq 0 ]; then
      sleep 5  # a finished background shell may re-invoke the agent
      [ "$(_shells "$pid")" -eq 0 ] && [ "$(_status "$name")" != working ] && return 0
    fi
    sleep 5
  done
  echo "$1: still busy after ${TIMEOUT_MS}ms" >&2
  return 1
}

wrap() {  # wrap <label>
  settle "$1" && say "$1" "${WRAP[$(_get "labels.$1.harness")]}" && settle "$1"
}

stop() {  # stop <label>: the session exits, its pane stays open until finish
  settle "$1" || return 1
  # shellcheck disable=SC2086
  herdr agent send-keys "$(_name "$1")" ${EXIT[$(_get "labels.$1.harness")]} >/dev/null
  echo "$1: session ended; pane $(_get "labels.$1.pane") stays open"
}

screen() {  # screen <label> [lines]: the pane's text, also after the session ended
  herdr pane read "$(_get "labels.$1.pane")" --source recent-unwrapped --lines "${2:-60}"
}

note() {  # note <label> <key> <value>: record a fact a check needs
  _state "s['labels'].setdefault('$1', {})['$2'] = '$3'"
}

prompt() { cat "$HERE/prompts/$1.txt"; }

# ---------------------------------------------------------------------------
# Plumbing

_name() {  # the herdr agent name: unique per run, since herdr keeps names after exit
  echo "t360-$(_get workspace | tr -d :)-$(tr '[:upper:]' '[:lower:]' <<<"$1")" | tr '[:upper:]' '[:lower:]'
}

_status() { herdr agent get "$1" | jq -r '.result.agent.agent_status // empty'; }

_rekey() {  # record the harness session id once herdr reports it
  local key
  key=$(herdr agent get "$(_name "$1")" | jq -r '.result.agent.agent_session.value // empty')
  [ -n "$key" ] && [ "$key" != "$(_get "labels.$1.session")" ] || return 0
  note "$1" session "$key" && echo "$1: session $key"
}

_answer_trust() {  # answer a harness's one-time folder-trust dialog, then wait for ready
  local name text downs; name=$(_name "$1")
  text=$(screen "$1" 40)
  grep -qi trust <<<"$text" || return 1
  # Move the menu cursor (❯ or ›) from its option down to the "Yes" option.
  downs=$(awk '/^ *[❯›]? *([0-9]\. *)?(Yes|No)([ ,]|$)/ {
      n++; if ($0 ~ /[❯›]/) cur = n; if (!yes && $0 ~ /Yes/) yes = n }
    END { print (yes && cur) ? yes - cur : 0 }' <<<"$text")
  local -a keys=()
  for ((i = 0; i < downs; i++)); do keys+=(down); done
  herdr agent send-keys "$name" "${keys[@]}" enter >/dev/null
  herdr agent wait "$name" --until idle --timeout 120000 >/dev/null
}

_steady() {  # wait until the pane stops changing: herdr reports ready while a
  # harness still initialises, and a prompt pasted then is silently dropped
  local prev="" cur i
  for ((i = 0; i < 30; i++)); do
    cur=$(herdr pane read "$(_get "labels.$1.pane")" --source visible)
    [ "$cur" = "$prev" ] && return 0
    prev=$cur; sleep 3
  done
}

_shells() {  # count shell commands still running anywhere under process $1
  ps -eo pid=,ppid=,args= | awk -v root="$1" '
    { pid = $1; par[pid] = $2; sub(/^ *[0-9]+ +[0-9]+ +/, ""); cmd[pid] = $0 }
    END {
      n = 0
      for (p in par) {
        if (cmd[p] !~ /^(\/usr)?(\/bin\/)?(ba|z)?sh -l?c/ || cmd[p] ~ /mcp/) continue
        for (q = par[p]; q > 1; q = par[q]) if (q == root) { n++; break }
      }
      print n
    }'
}

_work_task() {  # the work task for ticket #n, by tracker ref, from the index
  oracle snapshot | jq -r --arg r "github:$REPO#$1" \
    '.tasks | to_entries[] | select(.value.grain=="work" and .value.asked==$r) | .key' | head -1
}

_weave() {  # the weave CLI, always against the throwaway vault
  THINKWEAVE_VAULT=$VAULT THINKWEAVE_PROJECT=$PROJECT uv run --project "$REPO_ROOT" --no-sync weave "$@"
}

_get() {  # _get <dotted.path> from the run state ("" when absent)
  [ -f "$STATE" ] || return 0
  jq -r --arg p "$1" 'getpath($p | split(".")) // empty' "$STATE"
}

_state() {  # _state <python statements over s, the state dict>
  mkdir -p "$ROOT"
  python3 - "$STATE" "$1" <<'EOF'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
s = json.loads(p.read_text()) if p.exists() else {"labels": {}}
exec(sys.argv[2])
p.write_text(json.dumps(s, indent=1))
EOF
}

[ "${BASH_SOURCE[0]}" = "$0" ] && { cmd=${1:?usage: drive.sh setup|run|check|finish|<verb> ...}; shift; "$cmd" "$@"; }
