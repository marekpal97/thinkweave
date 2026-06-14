---
name: onboard
owns_mechanic: project_bootstrap
capabilities: [bootstrap]
consumes: [weave_sources_config, weave_landing, weave_concepts]
produces: [~/.config/thinkweave/config.toml (vault_root), vault/config/sources.yaml (projects.<name>), vault/config/PRIORITIES.yaml (focus.active_projects + intake.* seeds), vault/config/ontology.yaml, .claude/settings.json or ~/.claude/settings.json hooks, per-project landing docs, scheduled jobs via `weave schedule` (crontab on Linux/macOS or Task Scheduler on Windows, opt-in)]
tools:
  - Read
  - Write
  - Edit
  - Bash
  - AskUserQuestion
  - weave_sources_config
  - weave_landing
  - weave_concepts
description: First-run onboarding — pre-flight checks, vault wiring, seed vault from prior Claude Code history, bootstrap ontology, configure focus + sources (validated against user-supplied sample files), install hooks (global by default), optionally install cron block, run end-to-end smoke test, emit landing docs.
---

# /onboard — make existing work legible to weave

Thinkweave's first-run flow. Owns vault-path selection and `weave init`,
seeds the vault from your historical Claude Code conversations, then
layers ontology, focus, source types, hooks, and (optionally) cron on
top of that seed. Ends with a five-check smoke test.

**Posture: plan-before-execute.** Every user decision in this skill is
gated by an explicit `AskUserQuestion` call — never improvise prompts,
never make assumptions on the user's behalf. Step 5 ends with a single
plan-for-approval summary; no `sources.yaml` / `PRIORITIES.yaml` state
is written until the user has approved that summary.

The skill is invoked from a project directory but its early steps are
vault-scope (or machine-scope, for hooks). They run once across all
your projects; the per-project work at the end attaches the *current*
repo to the seeded vault.

## Idempotency — what makes each step skippable

Re-runnable; later passes only do what hasn't been done yet. The skill
uses **pragmatic structural checks** instead of a manifest file — each
step inspects the world and short-circuits if its output already
exists:

| Step | "Done" signal — skip when… |
|---|---|
| 0 — Pre-flight | always runs (cheap; the checks themselves are the value) |
| 1 — Vault wiring | `is_vault_initialized(load_config())` returns True AND `~/.config/thinkweave/config.toml` exists |
| 2 — Hook scope | hooks already present at the chosen scope (plugin manifest OR `~/.claude/settings.json` OR repo `.claude/settings.json`) with `thinkweave` markers |
| 3 — CC import | `weave_search(type=['session'], limit=1)` returns ≥1 session note (vault already seeded) |
| 4 — ontology bootstrap | `weave concepts list` has ≥10 canonical concepts AND `weave concepts proposed-counts --min-count 3` survivors list is empty |
| 5 — focus | `focus.active_projects` already populated in `<vault>/config/PRIORITIES.yaml` for every discovered project |
| 5 — source types | every enabled source type has its row in `sources.yaml` AND (for intake-driven types) a non-empty entry in `PRIORITIES.yaml::intake.<type>` |
| 6 — Scheduler install | thinkweave jobs already registered (re-running `weave schedule install` replaces them; never duplicates). Linux/macOS: `# --- thinkweave cron block ---` fence in `crontab -l`. Windows: `PersonalMem\*` tasks in `schtasks /Query`. |
| 7 — Smoke test | always runs (the whole point is verification) |
| Landing docs | `<vault>/projects/<project>/STATE.md` exists AND `mtime > 0` |

If a step's signal is satisfied, print one line ("Step N already done —
skipping.") and move on. Don't ask. Don't re-confirm.

---

## Step 0 — Pre-flight

Three structured checks. Each one is a gate — failures HALT with
explicit re-run instructions; passing continues to the next.

### 0a. uv check

```bash
command -v uv >/dev/null && echo "uv ok" || echo "missing"
```

If missing, detect OS to pick the right installer line:

```bash
case "$(uname -s)" in
  Linux|Darwin) echo "curl -LsSf https://astral.sh/uv/install.sh | sh" ;;
  *)            echo 'powershell -c "irm https://astral.sh/uv/install.ps1 | iex"' ;;
esac
```

Then:

```
AskUserQuestion({
  "questions": [{
    "question": "uv isn't on PATH. uv is the package manager thinkweave uses to run its CLI and MCP. Want me to install it for you?\n\nWill run: <the line above>",
    "header": "uv install",
    "options": [
      {"label": "install now", "description": "Run the installer line above via Bash. Standard install — drops binaries in ~/.local/bin (Unix) or %USERPROFILE%\\.local\\bin (Windows)."},
      {"label": "I'll do it myself, halt", "description": "Stop /onboard. Install uv on your own, then re-run /onboard."},
      {"label": "skip and let me see what breaks", "description": "Continue without uv. Most subsequent steps will fail; useful only for debugging."}
    ],
    "multiSelect": false
  }]
})
```

- **install now**: run the appropriate one-liner via Bash. After it
  completes, re-check `command -v uv`. If still missing (PATH not yet
  refreshed in this shell), HALT with: *"uv installed but not yet on
  this shell's PATH. Open a new terminal, restart Claude Code, then
  re-run /onboard."*
- **I'll do it myself, halt**: HALT with: *"Install uv from
  https://docs.astral.sh/uv/getting-started/ then re-run /onboard."*
- **skip and let me see what breaks**: print one line and continue.

### 0b. MCP roundtrip probe

Call `weave_concepts(action='list', limit=1)`. The cheapest available
MCP tool; success confirms the MCP server is wired and Claude Code can
reach it.

On failure, print exactly:

> MCP tools aren't responding. Most likely: you installed the plugin
> but didn't restart Claude Code. Restart, then re-run /onboard.

HALT. Don't try to recover; the only fix is a Claude Code restart.

### 0c. weave doctor --mcp

```bash
uv run weave doctor --mcp
```

Surface its output verbatim. This is the install diagnostic — covers
MCP registration scope (`plugin` / `user` / `project`), server health,
and config sanity.

- **PASS**: continue to Step 1.
- **FAIL**: print the doctor output's remediation lines and HALT with:
  *"Pre-flight failed at weave doctor. Address the items above, then
  re-run /onboard."*

---

## Step 1 — Vault wiring

`/onboard` owns vault-path selection. This is the seam that lets the
choice survive Claude Code restarts and propagate to the MCP server,
hooks, and CLI without any shell-rc edits.

### 1a. Detect current state

```bash
uv run python -c "from thinkweave.core.config import load_config, is_vault_initialized; cfg=load_config(); print('YES' if is_vault_initialized(cfg) else 'NO', cfg.vault_root)"
```

If output begins with `YES`, print *"Vault at `<path>` already
initialized — using."* and proceed to Step 2.

If `NO`, fall through to 1b.

### 1b. Ask for vault path (AskUserQuestion)

```
AskUserQuestion({
  "questions": [{
    "question": "Where should your thinkweave vault live? This is one directory that holds every session note, decision, source brief, and concept hub across all your projects. The choice gets persisted to ~/.config/thinkweave/config.toml so the MCP server, hooks, and CLI all agree without you having to set any environment variables.",
    "header": "Vault location",
    "options": [
      {"label": "~/vault (recommended)", "description": "Default. Lives in your home directory; survives system reinstalls if you back up $HOME."},
      {"label": "Other path", "description": "I'll ask for the path next. Use this if you keep notes in a synced folder (Dropbox, iCloud, Syncthing) or a dedicated drive."}
    ],
    "multiSelect": false
  }]
})
```

- **~/vault (recommended)**: chosen path = `$HOME/vault`.
- **Other path**: follow up with a free-form question for the path.

**Validate** the chosen path:

```bash
PARENT="$(dirname "<chosen-path>")"
test -d "$PARENT" && test -w "$PARENT" && echo "ok" || echo "bad-parent"
case "<chosen-path>" in /tmp/*) echo "tmp-forbidden" ;; esac
```

On `bad-parent`: print *"Parent directory `<parent>` doesn't exist or
isn't writable."* and re-issue 1b. On `tmp-forbidden`: print *"Vaults
under `/tmp` are not allowed — they vanish on reboot."* and re-issue
1b.

### 1c. Persist the choice

```bash
uv run python -c "from pathlib import Path; from thinkweave.core.config import write_user_config; write_user_config(Path('<chosen-path>'))"
```

Writes `~/.config/thinkweave/config.toml` (XDG-respectful, atomic).
Confirm the file exists:

```bash
test -f ~/.config/thinkweave/config.toml && echo "persisted" || echo "FAILED"
```

### 1d. Run `weave init`

```bash
THINKWEAVE_VAULT=<chosen-path> uv run weave init
```

This seeds `<vault>/config/sources.yaml`, `PRIORITIES.yaml`,
`ontology.yaml`, `scheduling.yaml`, and the rest of the template tree.
(Feed registries — news/podcast outlets, youtube channels, newsletter
senders — all live under `PRIORITIES.yaml::intake`; there are no
standalone `*_feeds.yaml` files.)

### 1e. Confirm

Print verbatim:

> Vault at `<chosen-path>` initialized. Persisted to
> `~/.config/thinkweave/config.toml` — this choice will survive
> Claude Code restarts.

---

## Step 2 — Hook scope

Determine whether hooks should fire in **every** Claude Code session on
this machine (global) or **only** sessions launched from the current
repo (per-project).

### 2a. Detect current install scope

Parse `weave doctor --mcp` output from Step 0c for the scope summary
line. Three cases:

- Contains `plugin` scope (e.g. `1 scope (plugin)`): the plugin
  manifest wires **both** the MCP server *and* the four hook events
  (SessionStart, UserPromptSubmit, PostToolUse × {Write|Edit|Bash,
  mcp__thinkweave__.*}, Stop) globally via `.claude-plugin/plugin.json`.
  Print *"Hooks installed via plugin manifest — already global."* and
  proceed to Step 3. **No `weave hooks install` needed for plugin users.**
- Contains `user` scope already: print *"Global hooks already
  installed at ~/.claude/settings.json."* and proceed to Step 3.
- Otherwise (non-plugin install / legacy / per-project): fall through
  to 2b.

### 2b. Ask scope (AskUserQuestion)

```
AskUserQuestion({
  "questions": [{
    "question": "Capture every Claude Code session you start, or only sessions in this repo?",
    "header": "Capture scope",
    "options": [
      {"label": "Every session (recommended)", "description": "Hooks fire in every CC session on this machine. Vault-existence gate (set up in Step 1) means hooks no-op silently in repos without your vault. Different repos auto-separate inside the vault by cwd."},
      {"label": "Only this repo", "description": "Hooks land in .claude/settings.local.json of the current repo. Opt-in per project."}
    ],
    "multiSelect": false
  }]
})
```

### 2c. Show diff first if a target settings file already exists

Determine the target path:

- **Every session** → `~/.claude/settings.json`
- **Only this repo** → `$(pwd)/.claude/settings.json`

If that file already exists, run the install in dry-run mode first so
the user sees what would change:

```bash
# Every session:
uv run weave hooks install --scope user --dry-run

# Only this repo:
uv run weave hooks install --dry-run
```

Display the planned diff, then ask:

```
AskUserQuestion({
  "questions": [{
    "question": "The diff above shows what weave hooks install would change in <target>. Apply it?",
    "header": "Apply hook install?",
    "options": [
      {"label": "yes, apply", "description": "Run the install for real. The merge logic preserves any non-thinkweave hooks already in the file."},
      {"label": "skip", "description": "Don't touch the settings file. Re-run /onboard or `weave hooks install` later."}
    ],
    "multiSelect": false
  }]
})
```

On `skip`: print *"Skipped hook install — re-run /onboard or `weave
hooks install` when ready."* and proceed to Step 3.

### 2d. Apply

```bash
# Every session:
uv run weave hooks install --scope user

# Only this repo:
uv run weave hooks install
```

If no existing settings file was present in 2c, run directly here
without the dry-run/confirm dance.

Confirm no errors printed. If the user chose **Only this repo**, flag
in the eventual wrap-up that other active projects need their own
`/onboard` run from their respective repos.

---

## Step 3 — Seed from historical Claude Code conversations (mandatory)

This is the spine. Everything else in onboarding is configured *on top*
of the seed — there's no skip, no "later." If the user has prior CC
history, importing it is what makes weave useful from the first query.
If they don't, this step short-circuits and Step 4 (ontology) is
skipped too.

**Idempotency check:** if `weave_search(type=['session'], limit=1)`
returns ≥1 hit, the vault is already seeded — print one line and
proceed to Step 4.

### 3a. Dry-run the import

```bash
weave import claude-code --dry-run
```

The dry-run reports per-project session counts (project names are
auto-derived from each session's `cwd` — multi-project aware, no manual
mapping needed). Parse the output for total session count `N` and the
per-project breakdown.

**Empty-history branch.** If `~/.claude/projects/` doesn't exist or
the dry-run reports zero usable sessions, print verbatim:

> No prior CC history found. To seed your vault: run `/research <url>`
> on three sources you care about, then re-run `/onboard` to bootstrap
> the ontology from them.

Then **skip Step 4 entirely** (ontology bootstrap has nothing to chew
on) and proceed directly to Step 5.

### 3b. Check ANTHROPIC_API_KEY status (for --via batch viability)

```bash
test -n "$ANTHROPIC_API_KEY" && echo "key set" || echo "key missing"
```

### 3b'. Decide import scope (AskUserQuestion — only if `N > ~500`)

A heavy CC user can have multi-thousand-session history. Surface scope
controls before the mode question so the user isn't forced into a full
backfill on first run. Skip this sub-step entirely when `N ≤ 500` —
small histories should just import.

```
AskUserQuestion({
  "questions": [{
    "question": "Found {N} historical sessions ({oldest_date} → {newest_date}). How much should this run import?",
    "header": "Import scope",
    "options": [
      {"label": "everything", "description": "Import all {N} sessions. Newest-first ordering still applies inside the run; just no cap."},
      {"label": "recent only (--since)", "description": "Set a date floor — sessions older than that are skipped. I'll ask for the date next."},
      {"label": "sample 50 first (--sample-only)", "description": "Materialise the 50 newest sessions for an ontology preview. After Step 4 you can re-run /onboard to ingest the rest."},
      {"label": "cap at N (--limit)", "description": "Materialise the K newest, where K is the cap I'll ask for next."}
    ],
    "multiSelect": false
  }]
})
```

- **everything** → no scope flags.
- **recent only**: follow up with a free-form date prompt (`YYYY-MM-DD`),
  validate the format, then pass `--since <date>`.
- **sample 50 first**: pass `--sample-only`. Print a one-liner reminder
  *"You'll be prompted to re-run /onboard for the full backfill after
  Step 4."*
- **cap at N**: follow up with a free-form integer prompt, then pass
  `--limit <N>`.

Hold the chosen flags in memory; they pipe into 3d.

### 3c. Decide import mode (AskUserQuestion)

Recompute `N_effective` = sessions that survive the 3b' scope filter
(use the dry-run output filtered locally; no need to re-shell).

If `N_effective ≤ ~200`, the inline path is fine; skip the question and
run `weave import claude-code` directly with the 3b' scope flags.

If `N_effective > ~200`, ask the user which mode to use:

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
`inline` and tell them why in one line. If they pick `skip`, exit Step
3 without importing.

### 3d. Execute the import

Append any scope flags chosen in 3b' to the command:

```bash
weave import claude-code [--since YYYY-MM-DD] [--sample-only] [--limit N]
weave import claude-code --via batch [--since YYYY-MM-DD] [--sample-only] [--limit N]
```

After import lands, the vault has session notes, decision notes, and
many `proposed_concepts:` entries from auto-enrichment. Those drive
Step 4.

If the user picked `sample 50 first` in 3b', flag at the start of the
wrap-up: *"Sampled 50 sessions for ontology bootstrap. Re-run `/onboard`
(or `weave import claude-code`) to materialise the remaining {N-50}."*

---

## Step 4 — Bootstrap the ontology

Imported sessions surface domain vocabulary as `proposed_concepts:` —
candidates that haven't earned canonical status yet. On a fresh vault
this is the moment to canonicalise the high-frequency ones in one pass,
so subsequent retrieval and hub generation operate on a real ontology
instead of an empty seed.

**Skipped entirely** if Step 3 took the empty-history branch.

**Idempotency check:** if `weave concepts list` reports ≥10 canonical
concepts AND `weave concepts proposed-counts --min-count 3` returns an
empty survivor list (after filtering), skip Step 4.

### 4a. Gather survivors

Use a lower threshold than periodic hygiene (3 vs. the standard 5) —
fresh vaults need a faster ramp:

```bash
uv run weave concepts proposed-counts --min-count 3
```

Pipe through the deterministic filter (drops domain-path concepts,
generic process terms, project-name leakage) before showing the user:

```python
from thinkweave.synthesis.concepts import filter_promotion_candidates
surviving = filter_promotion_candidates([c for c, _ in proposed_counts])
```

Load the existing domain catalogue (`weave concepts list`) to anchor
domain suggestions in real namespaces.

### 4b. Confirm promotions in batches (AskUserQuestion)

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
weave concepts promote --concept <term> --domain <suggested-domain>
```

If the user wants to change a suggested domain, they'll say so in the
chat after submitting the batch — handle inline with a single follow-up
`AskUserQuestion` listing existing domains as options.

---

## Step 5 — Focus & acquisition setup

This step ends with a **plan-for-approval summary** (5d). No writes to
`sources.yaml` / `PRIORITIES.yaml`, no landing docs are issued until
the user has approved that plan.

### 5a. Active-project multi-select (AskUserQuestion)

Read the projects discovered in Step 3's dry-run (or, if Step 3 was
skipped via empty-history, run `weave project list` to enumerate — likely
empty, in which case ask the user to name the current repo's project
as a free-form follow-up). Ask which are *active focuses* (vs.
archived / one-off):

```
AskUserQuestion({
  "questions": [{
    "question": "Which projects do you want active in your vault? Active projects get landing docs (STATE/BACKLOG/DECISIONS), discovery strategies, and source-type defaults. Unchecked projects stay imported but aren't foregrounded.",
    "header": "Active projects",
    "options": [
      {"label": "project-a", "description": "47 sessions imported. Last activity: 2026-04-12."},
      {"label": "project-b", "description": "23 sessions imported. Last activity: 2026-05-20."},
      {"label": "thinkweave", "description": "8 sessions imported. Last activity: 2026-05-26."}
    ],
    "multiSelect": true
  }]
})
```

Hold the result in memory — don't write to `sources.yaml` yet. The
plan-for-approval in 5d covers all the writes at once.

### 5b. Source-type enable multi-select (AskUserQuestion)

List registered types from `weave_sources_config()` and ask which ones to
enable. `paper` / `repo` / `article` ship enabled-by-default — surface
them as already-checked but still selectable.

```
AskUserQuestion({
  "questions": [{
    "question": "Which source types should weave actively acquire for you? You can enable more later by editing <vault>/config/sources.yaml or re-running /onboard.\n\nDefault-on: paper, repo, article (research URLs you'll hit /research on). Opt-in: the rest.",
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

**Output-format note (advisory, not gating).** Each enabled source
type ships with a default brief structure baked into its writer skill
(`research-paper`, `research-news-worker`, `research-newsletter-worker`,
etc.). The shipped formats are opinionated — a starting point, not a
constraint — but they live inside the plugin's `commands/`, so editing
them in place is **not** upgrade-safe (a `weave`/plugin upgrade overwrites
them). The upgrade-safe way to shape what gets extracted is vault config,
not skill edits: tune the per-type knobs in `vault/config/sources.yaml`,
or `/source-scaffold` your own variant (its skill lands in
`~/.claude/commands/`, which upgrades don't touch). Full per-source brief
templating (vault-side override templates, per-project overrides) is the
designed-but-deferred feature in `.claude/plans/source-output-customization.md`.
Mention this once in the wrap-up; don't drag it into a confirmation step.

### 5c. Sample-file validation per enabled type (AskUserQuestion, one per type)

**This is the trust-but-validate turn.** For each source type the user
enabled in 5b, ask them to point to a concrete sample on disk / paste
a real URL / pick an option. The skill then validates the spec
end-to-end against that sample *before* anything gets written to
`sources.yaml`.

Hold onto sample URLs for paper/article in particular — Step 7's
smoke test re-uses them for the "one sample brief lands" check.

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
dry-run is available (`weave queue add paper <url> --dry-run` returning
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
    "question": "Paste an outlet RSS feed URL you want news to pull from (e.g. https://www.ft.com/rss/home). I'll seed it into PRIORITIES.yaml::intake.news.outlets after the plan is approved.",
    "header": "Sample for: news",
    "options": [
      {"label": "RSS URL", "description": "Paste the feed URL."},
      {"label": "skip validation", "description": "I'll edit PRIORITIES.yaml later."}
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

### 5d. Plan-for-approval summary (AskUserQuestion)

Aggregate everything from 5a / 5b / 5c into a single plan. Show
counts, paths, and validation outcomes:

```
AskUserQuestion({
  "questions": [{
    "question": "Plan summary — about to apply the following:\n\n  Active projects:    {comma-list, e.g. project-a, thinkweave}\n  Source types:       {N enabled} ({comma-list})\n  Samples validated:  {K of N}\n  Writes to <vault>/config/sources.yaml:\n    - projects.<name> blocks ({new + updated entries})\n    - source-type blocks ({list of slugs})\n  Writes to <vault>/config/PRIORITIES.yaml:\n    - focus.active_projects: {active list}\n    - intake.news.outlets ({M outlets from samples})\n    - intake.{newsletter|youtube|podcast}_{events|concepts} ({per validated sample})\n  Landing docs:       STATE / BACKLOG / DECISIONS per active project, THEMES global\n\nConfirm?",
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

On `confirm`: apply via Edit on `vault/config/sources.yaml` (preserve
existing config) AND on `vault/config/PRIORITIES.yaml`
(`focus.active_projects` + the per-type `intake.<type>` seeds from
validated samples in 5c). Both writes target the canonical
`vault/config/` location — never `vault/.weave/` (which raises
`LegacyConfigLocationError` on read since Phase 3.1B).

Then issue the landing docs pass:

```bash
# Per active project (skip any whose STATE.md already exists):
THINKWEAVE_VAULT=<vault> uv run weave landing --project <project> --doc all

# Global themes refresh:
uv run weave landing --doc themes
```

Landing docs are derived and idempotent — the user already approved
them as part of 5d, so no separate confirmation.

On `edit`: the user describes what to change in chat. Loop back to
the appropriate sub-step (5a / 5b / 5c) for the affected slice, then
re-show 5d.

On `cancel`: print "Cancelled. No writes applied." and exit the skill.
Steps 1-4 outputs stay (vault wiring + hook scope + CC import + ontology
are already committed; only the source-config writes are reverted by
cancelling here).

---

## Step 6 — Scheduler install (opt-in, with explicit consent)

Opt-in scheduling for the long-running automation: embeddings keep-warm,
dream cycle, and per-source-type drain flows. **Cross-platform** — the
`weave schedule` command renders the one `vault/config/scheduling.yaml` job
registry onto whatever the host provides: crontab on Linux/macOS, Windows
Task Scheduler (via `schtasks`) on Windows. The job bodies are identical
across OSes; only the trigger mechanism differs. Idempotent.

### 6a. Compose the `--only` job list

Pick which registry jobs to install from what the user enabled in Step 5
(naming a job via `--only` installs it regardless of its default `enabled`
flag in the template):

- **Always** include `embeddings-keepwarm` and (if any active project
  exists from 5a) `dream`. Also include `weekly-hygiene` — concept/theme
  dedup is always safe.
- **If any acquire source type was enabled** in 5b, include
  `daily-research` (the discover → drain → hub-refresh flow).
- **If `news` was enabled** in 5b, also include `news-poll` and
  `news-cycle`.

Build a comma-separated `ONLY` string from the selected names, e.g.
`embeddings-keepwarm,dream,weekly-hygiene`.

### 6b. Preview (dry-run, OS-aware)

```bash
uv run weave schedule install --dry-run --only "<ONLY>"
```

This prints the resolved scheduler entries for **this** OS (the crontab
fence block, or the `schtasks /Create` task list) plus any
`warning: … declares <VAR> but it is unset` lines for missing env vars.
Surface that output verbatim — on Windows, an unset `OPENAI_API_KEY` is a
real prerequisite for the embeddings job; an unset `ANTHROPIC_API_KEY` is
advisory if Claude Code is authed via subscription/OAuth.

### 6c. Ask consent (AskUserQuestion)

```
AskUserQuestion({
  "questions": [{
    "question": "Install the scheduled jobs shown above? Without them, embeddings keep-warm and the dream cycle don't run, and any enabled feed sources won't auto-drain.",
    "header": "Install scheduler?",
    "options": [
      {"label": "yes, install", "description": "Install via the native scheduler (crontab on Linux/macOS, Task Scheduler on Windows). Idempotent — re-running replaces the thinkweave entries, never duplicates."},
      {"label": "no thanks, I'll handle it myself", "description": "Nothing gets scheduled. You can run `weave schedule install` later, or edit vault/config/scheduling.yaml first."}
    ],
    "multiSelect": false
  }]
})
```

### 6d. Apply on "yes, install"

```bash
uv run weave schedule install --only "<ONLY>"
```

Confirm by re-running `weave schedule list` (shows the backend) and, on
Linux/macOS, `crontab -l | grep thinkweave` for the fence; on Windows,
`schtasks /Query /TN "PersonalMem\*"`.

### 6e. On "no thanks"

Print one line:

> Skipped scheduling. Run `weave schedule install` when ready, or edit
> `vault/config/scheduling.yaml` to toggle jobs first
> (`weave schedule list` shows the menu).

---

## Step 7 — End-to-end smoke test

Five checks, each one cheap. All must pass before wrap-up; any failure
HALTs with a remediation line.

### 7a. MCP responding

Call `weave_concepts(action='list', limit=1)`. PASS on no error.
FAIL → *"MCP didn't respond. Restart Claude Code and re-run /onboard."*

### 7b. Vault writable

```bash
touch <vault>/.test-write-probe && rm <vault>/.test-write-probe && echo "ok" || echo "fail"
```

PASS on `ok`. FAIL → *"Vault root `<vault>` isn't writable. Check
permissions on the parent directory."*

### 7c. Index queryable

Call `weave_search(query='', mode='fts', limit=1)`. PASS on no error
(zero results is fine on a cold vault). FAIL → *"SQLite index isn't
queryable. Run `weave index --full` and re-run /onboard."*

### 7d. Hooks firing

```bash
ls -t <vault>/sessions/*/*/events.jsonl 2>/dev/null | head -1
```

If the result is a non-empty file, hooks are firing (the current
`/onboard` session itself has been emitting events since SessionStart).
FAIL → *"No events.jsonl found under <vault>/sessions/. Hooks aren't
firing in this session. Restart Claude Code and re-run /onboard."*

### 7e. Sample brief landed (conditional)

Only if the user provided a sample paper/article URL in 5c (and didn't
pick `skip validation`):

Call `weave_search(query='<sample-title>', limit=1, type=['source'])`.
Note: this check is best-effort — if the sample URL was provided but
the source brief hasn't been generated yet (the user hasn't run
`/research <sample-url>` between 5c and now), report it as INFO not
FAIL: *"Sample URL noted but no source note yet — run `/research
<sample-url>` to verify the brief generation path."*

### 7e'. Embedding posture (INFO, never FAIL)

```bash
uv run weave doctor 2>/dev/null | sed -n '/^Embedding posture:/,/^$/p'
```

Surface the `Embedding posture:` block verbatim. This is **INFO**, never a
FAIL — keyword search (BM25/FTS) always works, so a vault with no embedding
key is fully functional, just without semantic/hybrid recall. The block
already carries the free keyless fallback (local `sentence_transformer`) when
`OPENAI_API_KEY` is missing on the default OpenAI provider. Don't editorialize
beyond it; the user picks whether the free local path is worth the extra
install.

### 7f. Print checklist

```
Verifying everything is wired:
  ✓ MCP responding
  ✓ Vault writable
  ✓ Index queryable
  ✓ Hooks firing
  ✓ Sample brief landed   (or INFO line if no /research run yet)
  · Embedding posture     (INFO — semantic on, or BM25-only + free local path)
```

On all PASS (or PASS + INFO), proceed to wrap-up. On any FAIL, print
the remediation line above and HALT.

---

## Post-install / restart caveats

Surface these as part of the wrap-up — they're the difference between
"install reported success" and "install actually takes effect in your
session."

- The subagent registry is captured at claude-process start. Worker
  agent files that just became available (the `dream-*-worker` set,
  any research workers — `agents/` in the plugin, `.claude/agents/`
  on a clone) won't load until restart — `/clear` does **not** reload
  them. Exit claude and re-launch after `/onboard` completes. Plugin
  installs register them namespaced (`thinkweave:<worker>`).
- The MCP server is process-bound. Any future `weave install` upgrade
  or change to MCP-exposed schemas/enums (new `NoteType` values, new
  tools, new enum members) requires the same restart to pick up.
- The cron `claude -p` path (used by the dream cycle and drain flows
  installed in Step 6) is a fresh process per run — unaffected by
  either caveat. The restart requirement only bites interactive
  users.

---

## Wrap-up

Print a summary tailored to what just happened:

```
You're set up.

Imported:    N sessions across M projects
Promoted:    K concepts to canonical ontology
Active:      <list of active projects>
Sources:     <list of enabled source types> (samples validated: <K of N>)
Hooks:       installed (scope: <global|repo>)   (or "skipped per your choice")
Landing:     STATE/BACKLOG/DECISIONS for active projects, THEMES global

Your config:
  vault_root:   <path>      (~/.config/thinkweave/config.toml)
  hook scope:   <global|repo>
  scheduler:    <installed via crontab|Task Scheduler|skipped>

The next time you sit down to work in this repo:

  • /weave-wrap         before /clear, so the session feeds the vault
  • /research <url>   to ingest a paper / repo / article
  • /ingest <thing>   for anything else (file, text, ID)

Cross-vault hygiene (run when things feel noisy):

  • /tighten              — concept + theme dedup, ontology pruning, essence

Reset / debug:

  • weave doctor --all      — full health check
  • /onboard              — idempotent; safe to re-run

If you hit a new input shape that the defaults don't cover:
  • /source-fit "<description>"
  • /source-scaffold <slug>      (only if /source-fit says you need to)
```

Print exactly that — verbatim plus per-run substitutions. The whole
point of the wrap-up is that the user can copy the next command without
re-reading.
