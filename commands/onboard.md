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
  - AskUserQuestion
  - mem_sources_config
  - mem_landing
  - mem_concepts
description: First-run onboarding — seed vault from prior Claude Code history, bootstrap ontology, configure focus + sources (validated against user-supplied sample files), install hooks, emit landing docs.
---

# /onboard — make existing work legible to mem

Personal_mem's first-run flow. Seeds the vault from your historical
Claude Code conversations *first*, then layers ontology, focus, source
types, and per-project hooks on top of that seed.

**Posture: plan-before-execute.** Every user decision in this skill is
gated by an explicit `AskUserQuestion` call — never improvise prompts,
never make assumptions on the user's behalf. Step 3 ends with a single
plan-for-approval summary; no vault state is written until the user
has approved that summary.

The skill is invoked from a project directory but its early steps are
vault-scope. They run once across all your projects; the per-project
work at the end attaches the *current* repo to the seeded vault.

## Idempotency — what makes each step skippable

Re-runnable; later passes only do what hasn't been done yet. The skill
uses **pragmatic structural checks** instead of a manifest file — each
step inspects the vault and short-circuits if its output already
exists:

| Step | "Done" signal — skip when… |
|---|---|
| 1 — CC import | `mem_search(type=['session'], limit=1)` returns ≥1 session note (vault already seeded) |
| 2 — ontology bootstrap | `mem concepts list` has ≥10 canonical concepts AND `mem concepts proposed-counts --min-count 3` survivors list is empty |
| 3 — focus | `projects:` block already populated in `<vault>/.mem/sources.yaml` for every discovered project |
| 3 — source types | every enabled source type has its row in `sources.yaml` AND its `intake_folder` / queue path exists on disk |
| 4 — hooks | `.claude/settings.json` exists in the current repo AND contains `"hooks"` block with `personal-mem` markers (SessionStart / Stop / UserPromptSubmit / PostToolUse) |
| 5 — landing docs | `<vault>/projects/<project>/STATE.md` exists AND `mtime > 0` |

If a step's signal is satisfied, print one line ("Step N already done —
skipping.") and move on. Don't ask. Don't re-confirm.

## Prerequisites — verify, don't recover

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

**Idempotency check:** if `mem_search(type=['session'], limit=1)`
returns ≥1 hit, the vault is already seeded — print one line and
proceed to step 2.

### 1a. Dry-run the import

```bash
mem import claude-code --dry-run
```

The dry-run reports per-project session counts (project names are
auto-derived from each session's `cwd` — multi-project aware, no manual
mapping needed). Parse the output for total session count `N` and the
per-project breakdown.

**If `~/.claude/projects/` doesn't exist or the dry-run reports zero
usable sessions,** print one line ("No prior Claude Code history found
— skipping seed.") and continue to step 2. Don't ask. Don't offer
alternatives.

### 1b. Check ANTHROPIC_API_KEY status (for --via batch viability)

```bash
test -n "$ANTHROPIC_API_KEY" && echo "key set" || echo "key missing"
```

### 1c. Decide import mode (AskUserQuestion)

If `N ≤ ~200`, the inline path is fine; skip the question and run
`mem import claude-code` directly.

If `N > ~200`, ask the user which mode to use:

```
AskUserQuestion({
  "questions": [{
    "question": "Found {N} historical sessions across {M} projects:\n\n{per-project breakdown, one line each}\n\nWhich import mode? (inline = uses the running model, ~{N*5}s rough estimate; batch = Anthropic Batches API, faster on large histories but requires ANTHROPIC_API_KEY)\n\nANTHROPIC_API_KEY status: {set | missing}",
    "header": "CC import mode",
    "options": [
      {"label": "inline", "description": "Use the running model. Slower on >200 sessions but no extra API key required."},
      {"label": "--via batch", "description": "Use Anthropic Batches API. Faster on large histories. REQUIRES ANTHROPIC_API_KEY to be set in the environment."},
      {"label": "skip", "description": "Don't import historical sessions now (you can re-run /onboard later)."}
    ],
    "multiSelect": false
  }]
})
```

If the user picks `--via batch` but the key is missing, fall back to
`inline` and tell them why in one line. If they pick `skip`, exit step
1 without importing.

### 1d. Execute the import

```bash
mem import claude-code              # if "inline"
mem import claude-code --via batch  # if "--via batch" AND key set
```

After import lands, the vault has session notes, decision notes, and
many `proposed_concepts:` entries from auto-enrichment. Those drive
step 2.

---

## Step 2 — Bootstrap the ontology

Imported sessions surface domain vocabulary as `proposed_concepts:` —
candidates that haven't earned canonical status yet. On a fresh vault
this is the moment to canonicalise the high-frequency ones in one pass,
so subsequent retrieval and hub generation operate on a real ontology
instead of an empty seed.

**Idempotency check:** if `mem concepts list` reports ≥10 canonical
concepts AND `mem concepts proposed-counts --min-count 3` returns an
empty survivor list (after filtering), skip step 2.

### 2a. Gather survivors

Use a lower threshold than periodic hygiene (3 vs. the standard 5) —
fresh vaults need a faster ramp:

```bash
uv run mem concepts proposed-counts --min-count 3
```

Pipe through the deterministic filter (drops domain-path concepts,
generic process terms, project-name leakage) before showing the user:

```python
from personal_mem.synthesis.concepts import filter_promotion_candidates
surviving = filter_promotion_candidates([c for c, _ in proposed_counts])
```

Load the existing domain catalogue (`mem concepts list`) to anchor
domain suggestions in real namespaces.

### 2b. Confirm promotions in batches (AskUserQuestion)

**Batching policy:** present survivors in batches of **5-8 per
`AskUserQuestion` call**, multi-select. One question per concept is
noise; one question for 30 concepts overflows the option list. Loop
until every survivor has been shown.

For each batch, the question is a single multi-select prompt with one
option per concept. Each option label is the concept name + suggested
domain in parentheses; the description carries the count + brief
rationale. The user checks the ones they want promoted; unchecked
concepts are left in `proposed_concepts:` (re-run pass will find them).

```
AskUserQuestion({
  "questions": [{
    "question": "Promote which of these proposed concepts to canonical? (Batch {i}/{total}, {batch_size} candidates)\n\nUnchecked concepts stay in proposed_concepts: for a future review pass.",
    "header": "Ontology bootstrap — batch {i}/{total}",
    "options": [
      {"label": "wandb (→ ml-training)", "description": "12 occurrences. ML experiment tracker. Suggested domain ml-training based on session context."},
      {"label": "regime-shift (→ finance-markets)", "description": "7 occurrences. Macro pattern. Suggested domain finance-markets."},
      ...
    ],
    "multiSelect": true
  }]
})
```

For each *checked* concept, run:

```bash
mem concepts promote --concept <term> --domain <suggested-domain>
```

If the user wants to change a suggested domain, they'll say so in the
chat after submitting the batch — handle inline with a single follow-up
`AskUserQuestion` listing existing domains as options.

---

## Step 3 — Focus & acquisition setup

This step ends with a **plan-for-approval summary** (3d). No writes to
`sources.yaml`, no hook installs, no landing docs are issued until the
user has approved that plan.

### 3a. Active-project multi-select (AskUserQuestion)

Read the projects discovered in step 1's dry-run (or, if step 1 was
skipped, run `mem project list` to enumerate). Ask which are *active
focuses* (vs. archived / one-off):

```
AskUserQuestion({
  "questions": [{
    "question": "Which projects do you want active in your vault? Active projects get landing docs (STATE/BACKLOG/DECISIONS), discovery strategies, and source-type defaults. Unchecked projects stay imported but aren't foregrounded.",
    "header": "Active projects",
    "options": [
      {"label": "project-a", "description": "47 sessions imported. Last activity: 2026-04-12."},
      {"label": "project-b", "description": "23 sessions imported. Last activity: 2026-05-20."},
      {"label": "personal-mem", "description": "8 sessions imported. Last activity: 2026-05-26."}
    ],
    "multiSelect": true
  }]
})
```

Hold the result in memory — don't write to `sources.yaml` yet. The
plan-for-approval in 3d covers all the writes at once.

### 3b. Source-type enable multi-select (AskUserQuestion)

List registered types from `mem_sources_config()` and ask which ones to
enable. `paper` / `repo` / `article` ship enabled-by-default — surface
them as already-checked but still selectable.

```
AskUserQuestion({
  "questions": [{
    "question": "Which source types should mem actively acquire for you? You can enable more later by editing <vault>/.mem/sources.yaml or re-running /onboard.\n\nDefault-on: paper, repo, article (research URLs you'll hit /research on). Opt-in: the rest.",
    "header": "Source types",
    "options": [
      {"label": "paper", "description": "Arxiv / OpenReview / PDF papers. /research <url> dispatches here. Disk intake folder."},
      {"label": "repo", "description": "GitHub / GitLab repos. /research <url> dispatches here."},
      {"label": "article", "description": "Web articles / blog posts. /research <url> dispatches here."},
      {"label": "substack", "description": "Substack newsletters via disk drop folder (~/inbox/substack/). /substack drains."},
      {"label": "news", "description": "RSS-pulled outlet queue with Haiku triage + Sonnet writer fan-out. Needs feedparser deps + cron line."},
      {"label": "newsletter-events", "description": "Event-grain email newsletters (market commentary, news digests) via Gmail label. Requires Gmail MCP OAuth."},
      {"label": "newsletter-concepts", "description": "Concept-grain email newsletters (tech analysis, deep dives) via Gmail label. Requires Gmail MCP OAuth."},
      {"label": "youtube-events", "description": "Event-grain YouTube channels via RSS. Headless-safe."},
      {"label": "youtube-concepts", "description": "Concept-grain YouTube channels via RSS. Headless-safe."},
      {"label": "podcast-events", "description": "Event-grain podcasts via RSS. Workers transcribe via Gemini Flash."},
      {"label": "podcast-concepts", "description": "Concept-grain podcasts via RSS. Workers transcribe via Gemini Flash."}
    ],
    "multiSelect": true
  }]
})
```

If the user mentions an input shape that *isn't* covered (podcast
transcripts via a custom feed reader, kindle highlights, RSS for a
non-listed pattern), point them at `/source-fit "<description>"` after
`/onboard` completes — don't try to scaffold mid-flow.

### 3c. Sample-file validation per enabled type (AskUserQuestion, one per type)

**This is the trust-but-validate turn.** For each source type the user
enabled in 3b, ask them to point to a concrete sample on disk / paste
a real URL / pick an option. The skill then validates the spec
end-to-end against that sample *before* anything gets written to
`sources.yaml`.

Loop over enabled types. For each, issue an `AskUserQuestion`
appropriate to its shape:

#### paper

```
AskUserQuestion({
  "questions": [{
    "question": "Drop a PDF paper into the paper intake folder (default ~/vault/intake/paper/) and enter its filename — or paste an arxiv / OpenReview URL you want the framework to handle as a sanity check.",
    "header": "Sample for: paper",
    "options": [
      {"label": "file on disk", "description": "I've placed a PDF in the intake folder. I'll give you the filename next."},
      {"label": "URL", "description": "Paste an arxiv / OpenReview / PDF URL instead."},
      {"label": "skip validation", "description": "Trust the defaults; don't validate."}
    ],
    "multiSelect": false
  }]
})
```

On `file on disk`: follow up with a free-form question asking for the
filename, then run `test -f "<intake_folder>/<filename>"` to confirm.
On `URL`: ask for the URL, then run `curl -sI -o /dev/null -w "%{http_code}"
"<url>"` to confirm it resolves (200 / 301 / 302 ok). If a one-item
dry-run is available (`mem queue add paper <url> --dry-run` returning
spec-parse success), use that. Print "spec OK" or "spec WARN: <reason>"
and continue.

#### repo

```
AskUserQuestion({
  "questions": [{
    "question": "Paste a GitHub or GitLab URL you'd run /research on as a sanity check.",
    "header": "Sample for: repo",
    "options": [
      {"label": "GitHub URL", "description": "Paste a github.com/<owner>/<repo> URL."},
      {"label": "GitLab URL", "description": "Paste a gitlab.com/<owner>/<repo> URL."},
      {"label": "skip validation", "description": "Trust the defaults; don't validate."}
    ],
    "multiSelect": false
  }]
})
```

Validate by checking the URL parses against the repo `url_patterns`
regex and the host responds 200 to `curl -sI`.

#### article

```
AskUserQuestion({
  "questions": [{
    "question": "Point me at an article you want the framework to handle: paste a URL, or give me the path to a saved .md / .html on disk.",
    "header": "Sample for: article",
    "options": [
      {"label": "URL", "description": "Paste an article URL."},
      {"label": "saved .md", "description": "Path to a saved markdown file on disk."},
      {"label": "saved .html", "description": "Path to a saved HTML file on disk."},
      {"label": "skip validation", "description": "Trust the defaults; don't validate."}
    ],
    "multiSelect": false
  }]
})
```

Validate: URL → `curl -sI` 200; file path → `test -f` exists and
non-empty.

#### substack

```
AskUserQuestion({
  "questions": [{
    "question": "Drop a substack clip (saved .html or .md from a substack post) into the substack inbox folder (default ~/vault/intake/substack/) and enter its filename, so I can confirm the disk-drop path works end-to-end.",
    "header": "Sample for: substack",
    "options": [
      {"label": "file in inbox", "description": "I've placed a clip in the inbox. I'll give you the filename next."},
      {"label": "skip validation", "description": "I'll clip my first one later."}
    ],
    "multiSelect": false
  }]
})
```

Validate: `test -f "<intake_folder>/<filename>"` exists, file is
non-empty, and the substack `intake_folder` resolves on disk.

#### news

```
AskUserQuestion({
  "questions": [{
    "question": "Paste an outlet RSS feed URL you want news to pull from (e.g. https://www.ft.com/rss/home). I'll add it to news_feeds.yaml after the plan is approved.",
    "header": "Sample for: news",
    "options": [
      {"label": "RSS URL", "description": "Paste the feed URL."},
      {"label": "skip validation", "description": "I'll edit news_feeds.yaml later."}
    ],
    "multiSelect": false
  }]
})
```

Validate: `curl -sI "<url>"` returns 200 AND the body contains
`<rss` or `<feed` (catch obvious not-an-RSS errors).

#### newsletter-events / newsletter-concepts

```
AskUserQuestion({
  "questions": [{
    "question": "Paste a sender email address you've already labeled in Gmail with the processed-label for {newsletter-events | newsletter-concepts} (e.g. memos@matt-levine.example). This anchors the mail_poll allowlist; I'll add more in the same shape later.",
    "header": "Sample for: {source_type}",
    "options": [
      {"label": "sender email", "description": "Paste the from-address."},
      {"label": "skip validation", "description": "I'll configure mail_poll later."}
    ],
    "multiSelect": false
  }]
})
```

Validate: regex-check the address parses as RFC-5322 (`<local>@<domain>`).
If the user hasn't authenticated Gmail yet, flag — the `/newsletter`
skill will OAuth-prompt on first run; doesn't block `/onboard`.

#### youtube-events / youtube-concepts

```
AskUserQuestion({
  "questions": [{
    "question": "Paste a YouTube channel ID (e.g. UCxxxxxx, from the channel URL) or full RSS URL for a channel you want to track as {source_type}.",
    "header": "Sample for: {source_type}",
    "options": [
      {"label": "channel ID", "description": "Paste the UC... ID."},
      {"label": "RSS URL", "description": "Paste the full RSS URL."},
      {"label": "skip validation", "description": "I'll add channels later."}
    ],
    "multiSelect": false
  }]
})
```

Validate: channel ID matches `^UC[A-Za-z0-9_-]{22}$`; RSS URL returns
200 + body contains `<feed`.

#### podcast-events / podcast-concepts

```
AskUserQuestion({
  "questions": [{
    "question": "Paste a podcast RSS feed URL you want to track as {source_type}.",
    "header": "Sample for: {source_type}",
    "options": [
      {"label": "RSS URL", "description": "Paste the feed URL."},
      {"label": "skip validation", "description": "I'll add shows later."}
    ],
    "multiSelect": false
  }]
})
```

Validate: 200 from `curl -sI`, body contains `<rss` and at least one
`<enclosure` tag (audio episodes present).

**On any validation failure:** print the failure reason in one line
and re-issue the same `AskUserQuestion` (with `skip validation`
explicitly available). Don't bail out of the whole step — single bad
sample shouldn't block the rest.

### 3d. Plan-for-approval summary (AskUserQuestion)

Aggregate everything from 3a / 3b / 3c into a single plan. Show
counts, paths, and validation outcomes:

```
AskUserQuestion({
  "questions": [{
    "question": "Plan summary — about to apply the following:\n\n  Active projects:    {comma-list, e.g. project-a, personal-mem}\n  Source types:       {N enabled} ({comma-list})\n  Samples validated:  {K of N}\n  Writes to <vault>/.mem/sources.yaml:\n    - projects: block ({new + updated entries})\n    - source-type blocks ({list of slugs})\n  Hook install in:    {current repo path}\n  Landing docs:       STATE / BACKLOG / DECISIONS per active project, THEMES global\n\nConfirm?",
    "header": "Plan for approval",
    "options": [
      {"label": "confirm", "description": "Apply the plan as shown."},
      {"label": "edit", "description": "I want to change something. Tell me what."},
      {"label": "cancel", "description": "Don't write anything; exit /onboard."}
    ],
    "multiSelect": false
  }]
})
```

On `confirm`: proceed to apply via Edit on `sources.yaml` (preserve
existing config), then step 4.

On `edit`: the user describes what to change in chat. Loop back to
the appropriate sub-step (3a / 3b / 3c) for the affected slice, then
re-show 3d.

On `cancel`: print "Cancelled. No writes applied." and exit the skill.
Steps 1 + 2 outputs stay in the vault (already committed).

---

## Step 4 — Per-project wiring

For the *current* repo (the directory where `/onboard` was invoked),
plus any other active projects from step 3a, do the per-project setup.

### 4a. Project entry in sources.yaml

Already covered by step 3d's Edit pass. Move on.

### 4b. Hook install — guard against silent overwrites (AskUserQuestion)

Before running `mem hooks install`, check if `.claude/settings.json`
exists in the current repo:

```bash
test -f "$(pwd)/.claude/settings.json" && echo "exists" || echo "absent"
```

If **absent**: install directly (no question).

```bash
PERSONAL_MEM_VAULT=<vault> uv run mem hooks install
```

If **exists**: ask the user how to proceed.

```
AskUserQuestion({
  "questions": [{
    "question": "{repo}/.claude/settings.json already exists. Installing personal-mem hooks will modify it (mem hooks install is conservative — merges into the existing hooks block, doesn't replace). Proceed?",
    "header": "Hook install — existing settings.json",
    "options": [
      {"label": "merge", "description": "Run mem hooks install. It merges into the existing hooks block; non-personal-mem entries are preserved."},
      {"label": "skip", "description": "Don't touch settings.json. I'll wire hooks manually later."},
      {"label": "show diff first", "description": "Print what mem hooks install would change, then re-ask."}
    ],
    "multiSelect": false
  }]
})
```

On `merge`: run the install command. Confirm no errors printed.

On `show diff first`: run `PERSONAL_MEM_VAULT=<vault> uv run mem hooks install --dry-run` (if supported; otherwise read the planned template and diff against current settings.json), display the diff, then re-issue the question with `show diff first` removed.

On `skip`: print "Skipped hook install — re-run /onboard or `mem hooks install` when ready."

Hooks land in `.claude/settings.json` of the current repo: SessionStart,
UserPromptSubmit, PostToolUse, Stop. Other active projects need their
own hook install run from their respective repos — flag this in the
wrap-up:

```
You'll want to run /onboard once in each active project so its hooks
get installed locally.
```

---

## Step 5 — First landing docs

**Idempotency check:** for each active project, if
`<vault>/projects/<project>/STATE.md` exists, skip that project. Bulk
the survivors:

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

No `AskUserQuestion` here — landing docs are derived, idempotent, and
the user already approved them in 3d.

---

## Wrap-up

Print a summary tailored to what just happened:

```
You're set up.

Imported:    N sessions across M projects
Promoted:    K concepts to canonical ontology
Active:      <list of active projects>
Sources:     <list of enabled source types> (samples validated: <K of N>)
Hooks:       installed in <current_project>   (or "skipped per your choice")
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
