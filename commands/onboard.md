---
name: onboard
owns_mechanic: project_bootstrap
capabilities: [bootstrap]
consumes: [mem_sources_config, mem_landing, mem_concepts]
produces: [.claude/settings.json hooks, projects.<name> in vault sources.yaml, vault/.mem/ontology.yaml, per-project landing docs]
tools:
  - Read
  - Write
  - Edit
  - Bash
  - mem_sources_config
  - mem_landing
  - mem_concepts
description: First-run onboarding — seed vault from prior Claude Code history, bootstrap ontology, configure focus + sources, install hooks, emit landing docs.
---

# /onboard — make existing work legible to mem

Personal_mem's first-run flow. Seeds the vault from your historical
Claude Code conversations *first*, then layers ontology, focus, source
types, and per-project hooks on top of that seed. Idempotent — safe to
re-run; later passes only do what hasn't been done yet.

The skill is invoked from a project directory but its early steps are
vault-scope. They run once across all your projects; the per-project
work at the end attaches the *current* repo to the seeded vault.

**Prerequisites — verify, don't recover:**

```bash
command -v mem-mcp >/dev/null && echo "mcp ok" || echo "missing"
test -f ~/.claude.json && grep -q '"personal-mem"' ~/.claude.json && echo "registered" || echo "missing"
test -f "${PERSONAL_MEM_VAULT:-$HOME/vault}/.mem/sources.yaml" && echo "vault ready" || echo "MISSING — run mem init"
```

If any check fails, stop and tell the user which prerequisite is
missing. This skill does not own machine setup (`mem install`) or vault
init (`mem init`).

---

## Step 1 — Seed from historical Claude Code conversations (mandatory)

This is the spine. Everything else in onboarding is configured *on top*
of the seed — there's no skip, no "later." If the user has prior CC
history, importing it is what makes mem useful from the first query.
If they don't, this step short-circuits and the rest still runs.

```bash
# Discover what's available
mem import claude-code --dry-run
```

The dry-run reports per-project session counts (project names are
auto-derived from each session's `cwd` — multi-project aware, no manual
mapping needed). Show the user the breakdown:

```
Discovered N sessions across M projects:
  • project-a: 47 sessions
  • project-b: 23 sessions
  • personal-mem: 8 sessions
```

Then commit:

```bash
mem import claude-code              # inline mode — uses the running model
# or, if N > ~200 and ANTHROPIC_API_KEY is set:
mem import claude-code --via batch  # Anthropic Batches; faster on large histories
```

**If `~/.claude/projects/` doesn't exist or has no usable sessions,**
print one line ("No prior Claude Code history found — skipping seed.")
and continue to step 2. Don't ask. Don't offer alternatives. The seed
is non-negotiable when present, harmless when absent.

After import lands, the vault has session notes, decision notes, and
many `proposed_concepts:` entries from auto-enrichment. Those drive
step 2.

## Step 2 — Bootstrap the ontology

Imported sessions surface domain vocabulary as `proposed_concepts:` —
candidates that haven't earned canonical status yet. On a fresh vault
this is the moment to canonicalise the high-frequency ones in one pass,
so subsequent retrieval and hub generation operate on a real ontology
instead of an empty seed.

Use a lower threshold than periodic hygiene (3 vs. the standard 5) —
fresh vaults need a faster ramp:

```bash
uv run mem concepts proposed-counts --min-count 3
```

Pipe the output through the deterministic filter (drops domain-path
concepts, generic process terms, project-name leakage) before showing
the user:

```python
from personal_mem.synthesis.concepts import filter_promotion_candidates
surviving = filter_promotion_candidates([c for c, _ in proposed_counts])
```

Present the survivors as a compact table:

```
## Ontology bootstrap — promote N candidates?

| Concept (count) | Suggested domain |
|-----------------|------------------|
| `wandb` (12) | ml-training |
| `regime-shift` (7) | finance-markets |
```

Apply LLM judgment on the survivors only — assign each to its best
domain (use `mem concepts list` for the existing domain catalogue).
Promote on user approval:

```bash
mem concepts promote --concept <term> --domain <domain>
```

Skip terms the user rejects. Re-runnable: a future `/onboard` pass
finds whatever's still proposed.

## Step 3 — Focus & acquisition setup

Read the projects discovered in step 1. Ask the user which are *active
focuses* (vs. archived / one-off):

```
Which projects do you want active in your vault?
(Active projects get landing docs, discovery, and source-type defaults.)

  [1] project-a (47 sessions)   [yes / archive]
  [2] project-b (23 sessions)   [yes / archive]
  [3] personal-mem (8 sessions) [yes / archive]
```

For each "yes," ensure an entry under `projects:` in
`<vault>/.mem/sources.yaml` (Edit, not Write — preserve existing
config). For "archive," skip — the import is still in the vault, just
not foregrounded.

Then walk the user through **source types**: which kinds of external
content they want mem to acquire. List the registered types via
`mem_sources_config()` and ask what they want enabled:

```
Available source types (papers, repos, articles ship by default):
  paper, repo, article, conversation, substack, news

Want to enable any optional ones?
  - substack — newsletters via disk drop (~/inbox/substack/)
  - news — RSS-pulled outlet queue with Haiku triage + Sonnet writer
    fan-out. Requires `uv pip install -e .[news]` (feedparser +
    readability-lxml + httpx) and a cron line for
    `scripts/pull_news_feeds.py`. See README §News module +
    scripts/example-crontab.
```

For each enabled type, confirm the intake path / queue location and
write to `sources.yaml` (the spec already documents the keys —
`intake_folder`, `queue`, `dedup_keys`). Don't synthesise config from
scratch; reuse the templates in the registry.

If the user describes an input shape that *isn't* covered (e.g.
podcast transcripts, email digests, kindle highlights), point them at
`/source-fit "<one-sentence description>"` to diagnose, then
`/source-scaffold <slug>` if it genuinely needs a new type. Don't try
to scaffold during `/onboard` — that's a separate skill with its own
flow.

## Step 4 — Per-project wiring

For the *current* repo (the directory where `/onboard` was invoked),
plus any other active projects from step 3, do the per-project setup:

```bash
# 4a. Register the project entry (idempotent)
# Already done by step 3's sources.yaml writes.

# 4b. Install hooks for THIS repo
PERSONAL_MEM_VAULT=<vault> uv run mem hooks install
```

Hooks land in `.claude/settings.json` of the current repo: SessionStart,
UserPromptSubmit, PostToolUse, Stop. Confirm the install printed no
errors. Other active projects need their own hook install run from
their respective repos — point the user at this when they next open
those projects:

```
You'll want to run `/onboard` once in each active project so its hooks
get installed locally.
```

## Step 5 — First landing docs

For each active project:

```bash
PERSONAL_MEM_VAULT=<vault> uv run mem landing --project <project> --doc all
```

Generates `STATE.md` / `BACKLOG.md` / `DECISIONS.md` under
`<vault>/projects/<project>/`. With imported content, these aren't
empty — they reflect actual prior work.

Also refresh global `THEMES.md`:

```bash
uv run mem landing --doc themes
```

## Wrap-up

Print a summary tailored to what just happened:

```
You're set up.

Imported:    N sessions across M projects
Promoted:    K concepts to canonical ontology
Active:      <list of active projects>
Hooks:       installed in <current_project>
Landing:     STATE/BACKLOG/DECISIONS for active projects, THEMES global

The next time you sit down to work in this repo:

  • /mem-wrap         before /clear, so the session feeds the vault
  • /research <url>   to ingest a paper / repo / article
  • /ingest <thing>   for anything else (file, text, ID)

Cross-vault hygiene (run when things feel noisy):

  • /mem-resolve-concepts   — concept dedup, ontology pruning
  • /themes-resolve         — theme dormancy, candidate promotion

If you hit a new input shape that the defaults don't cover:
  • /source-fit "<description>"
  • /source-scaffold <slug>      (only if /source-fit says you need to)
```

Print exactly that — verbatim plus per-run substitutions. The whole
point of the wrap-up is that the user can copy the next command without
re-reading.
