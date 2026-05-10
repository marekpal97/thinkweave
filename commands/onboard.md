---
name: onboard
owns_mechanic: project_bootstrap
capabilities: [bootstrap]
consumes: [mem_sources_config]
produces: [.claude/settings.json hooks, projects.<name> in vault sources.yaml, per-project landing docs]
tools:
  - Read
  - Write
  - Edit
  - Bash
  - mem_sources_config
  - mem_landing
description: Wire this repo into an existing personal_mem vault — hooks, project registration, optional Claude Code seed, first landing docs.
---

# /onboard — wire this project into the vault

Project-scope skill. Run **once per repository** where you want
personal_mem hooks active. Idempotent: re-running is safe.

**Prerequisites** — run these first if you haven't:

- `pipx install personal-mem` (or `pip install -e .[all]` from a clone)
- `mem install` — registers the personal-mem MCP server in
  `~/.claude.json` (machine-scope, one time)
- `mem init` — initialise your vault (one time per knowledge home)

If `mem-mcp` isn't on PATH or `~/.claude.json` lacks the personal-mem
entry, stop and tell the user to run `mem install` before continuing.
Verify with:

```bash
command -v mem-mcp >/dev/null && echo "mcp ok" || echo "missing"
test -f ~/.claude.json && grep -q '"personal-mem"' ~/.claude.json && echo "registered" || echo "missing"
```

If `mem init` hasn't been run (`$PERSONAL_MEM_VAULT/.mem/sources.yaml`
doesn't exist), stop and tell the user to run it. This skill does not
own vault initialisation.

---

## Step 1 — Confirm vault target

Read `$PERSONAL_MEM_VAULT` (or default `~/vault`). Confirm with the user
that this is the vault they want this project to write into.

```bash
echo "Vault: ${PERSONAL_MEM_VAULT:-~/vault}"
ls "${PERSONAL_MEM_VAULT:-~/vault}/.mem/sources.yaml" >/dev/null 2>&1 && echo "ready" || echo "MISSING — run mem init"
```

If missing, stop. Don't try to recover; tell the user to run `mem init`.

## Step 2 — Register this project in the vault

Derive the project name from the repo's basename (lowercase, `-` → `_`):

```bash
basename "$(pwd)" | tr 'A-Z-' 'a-z_'
```

Confirm the derived name with the user; let them override.

Append the project under `projects:` in the vault `sources.yaml` if not
already present, with the default discover strategy:

```yaml
projects:
  <project>:
    discover_strategies: [concept_coverage]
```

Use Edit (not Write) — preserve the rest of the file.

## Step 3 — Install hooks for this project

```bash
PERSONAL_MEM_VAULT=<vault> uv run mem hooks install
```

This registers SessionStart, Pre/PostToolUse, Stop, and
UserPromptSubmit hooks in *this repo's* `.claude/settings.json`. Confirm
with the user the install printed no errors.

## Step 4 — Optional: seed from prior Claude Code conversations

If `~/.claude/projects/` exists with > 0 entries, ask:

> "Found N session histories under `~/.claude/projects/`. Importing
> them seeds the vault with knowledge from your past Claude Code work.
> This is a one-shot vault-scope op — it walks every project, not just
> this one. Run now? (yes / no / later)"

Default **no**. If yes, dispatch to:

```bash
mem import claude-code [--via inline|batch] [--dry-run]
```

- `--via inline` (default): uses the running Claude model. Slow but no
  API key needed. Recommended for first run.
- `--via batch`: requires `ANTHROPIC_API_KEY`. Faster for large
  histories (hundreds of sessions), runs unattended.

Run `--dry-run` first so the user sees per-project counts before
committing. Project discovery is automatic from the `cwd` field of
each session's events.

## Step 5 — First landing docs for this project

```bash
PERSONAL_MEM_VAULT=<vault> uv run mem landing --project <project> --doc all
```

Generates `STATE.md` / `BACKLOG.md` / `DECISIONS.md` for this project
under `<vault>/projects/<project>/`.

## Wrap-up

Print a summary:

- Project registered: `<project>`
- Vault: `<vault>`
- Hooks installed: yes/no
- Claude Code seed: skipped / dry-run / imported N sessions

Then print a "what's next" block. Tailor it to what just happened:

```
You're set up. The next time you sit down to work in this project:

  • /mem-wrap         before /clear, so the session feeds the vault
  • /research <url>   to ingest a paper / repo / article

If you want to bring a new KIND of input into the vault — something
the defaults don't cover (e.g. podcast transcripts, email digests,
tweets you save, Notion exports, your kindle highlights) — run:

  • /source-fit "<one-sentence description of the input>"
       → tells you if an existing source type already handles it,
         needs a config tweak, or genuinely needs a new type
  • /source-scaffold <slug>
       → only if /source-fit said you need a new type

These two are vault-scope, not per-project. Run them once when you
hit a new input shape; the new /<slug> skill is then available in
every project.

If you skipped the Claude Code seed in step 4 and want it later:
  • mem import claude-code --dry-run        # see per-project counts
  • mem import claude-code                  # materialize
  • mem import claude-code --enrich --via batch   # optional, costs API
```

Print exactly that block — verbatim plus any per-project context. Do
not paraphrase or expand. The whole point of the wrap-up is that the
user can copy the next command without re-reading.
