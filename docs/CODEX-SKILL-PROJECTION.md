# Codex skill projection contract

This adapter translates harness vocabulary only. The command and worker files
linked by each generated skill remain the semantic source of truth.

- Read/search capabilities map to Codex filesystem tools; `Bash` maps to the
  shell runner; `Write`/`Edit` map to `apply_patch`; `WebFetch`/`WebSearch` map
  to the web tool; `AskUserQuestion` maps to the user-input tool.
- A canonical `/name` reference means: read and follow the sibling
  `../thinkweave-name/SKILL.md`. `$thinkweave-name` is the user-facing Codex
  invocation spelling.
- Translate every canonical `Task` dispatch to native `spawn_agent`. Read the
  linked worker contract completely and include its body plus the task-specific
  prompt in `message`; generic subagents do not inherit contracts by name.
- Replace hyphens with underscores for `task_name`. Use `followup_task` for a
  worker retry and `wait_agent` for fan-in or dependency waves.
- Worker frontmatter is the Claude Code projection. Do not forward its Claude
  model name or tool names; omit `model` so the Codex subagent inherits the
  current model, and translate capabilities as above.

- Run every `weave` CLI call as `THINKWEAVE_HARNESS=codex weave …`. The
  prefix makes `weave session-id` read `$CODEX_SESSION_ID` first and stamps
  the wrap's task rounds with the `codex` harness; without it a variable
  leaked from a parent Claude Code session wins.
- Call `weave_extract` only inside `$thinkweave-wrap`, once, with the id
  `weave session-id` prints. Never call it mid-task or with an invented
  session id: an id no session note carries mints a stray second note.

Do not emit Claude Code Task-call syntax from a Codex session.
