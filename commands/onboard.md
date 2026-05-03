---
name: onboard
owns_mechanic: bootstrap
capabilities: [bootstrap]
consumes: [mem_sources_config]
produces: [vault/.mem/sources.yaml, vault/.mem/index.db, landing docs]
tools:
  - Read
  - Write
  - Edit
  - Bash
  - mem_sources_config
  - mem_landing
description: Bootstrap personal_mem — vault location, source types, optional retroactive Claude session import, hooks, first index, landing docs.
---

# /onboard — first-time bootstrap

This skill is the one-shot setup wizard for a fresh personal_mem
install. Run it once after `/plugin add ./.claude/plugins/personal-mem`.
It is interactive — ask the user the listed questions, then act on
their answers. Do not skip prompts; do not auto-decide for them on the
items flagged as "ask".

The end state is: a vault directory exists, `.mem/sources.yaml` is
seeded (with any user customizations layered on top), Claude Code hooks
are installed, the SQLite index is built, and the four landing docs
are generated.

## Step 1 — Pick a vault location

Ask the user where to put their vault. Default: `~/vault`.

- If the path **does not exist**: proceed to Step 2.
- If the path **exists and is empty**: proceed to Step 2.
- If the path **exists and is non-empty**: ask the user to confirm
  reuse vs. fresh init in a different directory. Do not delete or
  overwrite anything; if they choose reuse, just continue and let
  `mem init` be idempotent.

Capture the chosen path. From here on, run all `mem` commands with
`PERSONAL_MEM_VAULT=<that path>` exported in the same shell. Use Bash
to set it:

```bash
export PERSONAL_MEM_VAULT=<chosen-path>
```

(Subsequent invocations should also export it. Mention the user can
add this to their shell rc to make it permanent.)

## Step 2 — Initialize the vault

```bash
PERSONAL_MEM_VAULT=<chosen-path> uv run mem init
```

This creates the directory layout and seeds
`vault/.mem/sources.yaml` from the bundled template. Confirm via Read
that `<chosen-path>/.mem/sources.yaml` exists.

## Step 3 — Offer retroactive Claude session import

Check whether `~/.claude/projects/` exists with Bash:

```bash
ls -1 ~/.claude/projects/ 2>/dev/null | wc -l
```

If the count is > 0, tell the user: "Found N session histories under
`~/.claude/projects/`. Importing them will let your existing Claude
Code work feed the memory layer." Then ask: import now? Default **no**
— it can be expensive and can be run later via
`mem drain --source claude-history`.

If they say **yes**, run:

```bash
PERSONAL_MEM_VAULT=<chosen-path> uv run mem drain --source claude-history
```

If they say **no**, mention they can run that command later.

## Step 4 — Source types

Read the seeded sources.yaml to show the user the defaults that ship:

```
Read <chosen-path>/.mem/sources.yaml
```

Walk through each top-level entry under `sources:` (paper, repo,
article, substack, conversation, claude-history) and ask:

- "Keep `<slug>`?" — default yes. If they say no, comment out that
  block in `sources.yaml` (do not delete; comment so the default is
  recoverable).
- For source types whose schema includes `intake_folder` (paper,
  substack), ask whether the user wants to override the default path.
  Validate the path exists or offer to create it; if it doesn't and
  they don't want it, leave the default.

After walking the defaults, ask once: "Add a custom source type?" If
yes, take a single round of {slug, bucket, layout, drain_strategy}
and append it to `sources.yaml`. Remind the user that adding the
ingestion skill is a separate step (copy
`commands/_source_template.md` → `commands/<slug>.md` and edit), and
the `SourceTypeSpec` registry entry must also be added in
`src/personal_mem/sources/registry.py`. Don't try to do those two
steps from inside this skill.

## Step 5 — Project list

Detect any pre-existing projects:

```bash
ls -1 <chosen-path>/projects/ 2>/dev/null
```

For each project directory found, append an entry under `projects:` in
`sources.yaml` if not already present:

```yaml
projects:
  <name>:
    discover_strategies: [concept_coverage]
```

Ensure the `default` entry exists (the seeded template already has
it — only act if it's missing). If the user wants to customize
`discover_strategies` for any project, ask once and append.

## Step 6 — Landing-file vocabulary (optional)

The default `landing_files:` block uses `STATE.md`, `BACKLOG.md`,
`DECISIONS.md`, `THEMES.md`, `RESEARCH_FOCUS.md`. Ask the user
once: "Use the default landing-file names? (yes / customize)"

- Default yes — do nothing.
- Customize — take a single round of overrides and write them under
  `landing_files:` in `sources.yaml`.

## Step 7 — Auto-todo extraction

Ask: "Should `/mem-wrap` automatically extract `todo` items from
sessions and queue them in BACKLOG? (yes / no, default yes)" — write
the answer as `auto_todo_extraction: <bool>` in `sources.yaml`.

## Step 8 — Hooks install

```bash
PERSONAL_MEM_VAULT=<chosen-path> uv run mem hooks install
```

This registers SessionStart, Pre/PostToolUse, Stop, and
UserPromptSubmit hooks in the current project's `.claude/`
settings. Confirm with the user that the install printed no errors.

## Step 9 — First index

Check whether `OPENAI_API_KEY` is set:

```bash
test -n "$OPENAI_API_KEY" && echo "set" || echo "missing"
```

- If **set**: run a full index with embeddings:

  ```bash
  PERSONAL_MEM_VAULT=<chosen-path> uv run mem index --full --embed
  ```

- If **missing**: run a full index without embeddings:

  ```bash
  PERSONAL_MEM_VAULT=<chosen-path> uv run mem index --full
  ```

  Warn the user: "Semantic search will be FTS-only until you set
  `OPENAI_API_KEY` and re-run `mem index --embed`."

## Step 10 — First landing docs

```bash
PERSONAL_MEM_VAULT=<chosen-path> uv run mem landing --doc all
```

For an empty vault this generates a global `THEMES.md` and per-project
DECISIONS / BACKLOG / STATE skeletons (one set per `projects:` entry,
including `default`).

## Wrap-up — Suggest follow-ups

Print a short summary listing the chosen vault path, source types kept,
projects registered, and embedding status. Then suggest the user's
first three actions:

1. Edit `<vault>/sources/RESEARCH_FOCUS.md` to declare priority topics,
   then run `/discover` to seed per-project research queues.
2. Run `/research <some-url>` to ingest their first source.
3. Before the next `/clear`, run `/mem-wrap` so the session's work
   feeds back into the vault.

Done. The `/onboard` skill is one-shot — re-running it is safe but
unnecessary; vault state edits afterwards happen via `mem`,
`/mem-resolve-concepts`, or direct YAML edits to `sources.yaml`.
