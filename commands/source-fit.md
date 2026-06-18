---
name: source-fit
owns_mechanic: source_diagnosis
capabilities: []
consumes: [weave_sources_config]
produces: []
tools:
  - Read
  - Bash
  - weave_sources_config
description: Read-only diagnostic — given a free-form description of an input shape, classify it against existing source types. Returns covered / adapt / scaffold.
---

# /source-fit — does an existing source type cover this?

Vault-scope skill. Reads-only — never writes anything. Run from any
project; the result is the same.

**Arguments**: `<free-form description>` — the user's goal, e.g.
*"podcast transcripts from Overcast"*, *"my Substack reading list"*,
*"YouTube video summaries"*, *"GitHub trending repos in ML"*.

---

## Step 1 — Load the source landscape

Single MCP call:

```
weave_sources_config()
```

This returns the merged registry + sources.yaml config. The keys you
care about for each source type:

- `url_patterns` — domains the type already matches
- `dedup_keys` — what makes two items "the same"
- `intake_folder` — does this type have a disk inbox?
- `description` — one-liner from the registry

Also Read `commands/` to see which skills are wired:

```bash
ls commands/*.md
```

## Step 2 — Classify the user's description

Apply this decision tree against the description:

### A. Is it a URL the existing patterns match?

If the description includes a sample URL (or a domain), check
`url_patterns` across all source types. Hit → return:

```
covered: <slug>
how:    /research <the-url>   (or /<slug> if there's a dedicated skill)
why:    URL matches `url_patterns` for <slug>
```

### B. Is it a folder of files the user wants drained?

Look at `intake_folder` configs. If the description is "a folder of X
files" and an existing type has `intake_folder` configured for a
similar shape (substack disk inbox, paper PDF inbox), return:

```
adapt: <slug>
how:    point intake_folder at <user-folder> in vault/config/sources.yaml
why:    <slug> already drains a disk inbox; only the path differs
```

### C. Is it conceptually similar but with a different format?

Examples: "podcast transcripts" is text-shaped (like article) but the
fetch/parse logic differs. "Email digests" are URL-less but
text-shaped. Return:

```
adapt: <slug>
how:    create vault/config/sources.yaml override for <slug> with new dedup_keys
        (and possibly intake_folder); reuse the existing skill
why:    same shape, different keys
```

### D. None of the above — genuinely new shape

Suggest a slug derived from the description (lowercase, single word,
no punctuation):

```
scaffold: <slug>
how:    /source-scaffold <slug>
why:    no existing source type has matching url_patterns, intake_folder,
        or shape
```

## Step 3 — Output

Print exactly one verdict block (covered / adapt / scaffold). No
prose around it. The user reads it and decides what to do.

If the user description is too vague to classify (e.g. *"some new
data"*), ask **one** clarifying question and stop. Don't fan out into
a long Q&A.

## What this skill never does

- Never write to the vault.
- Never edit `sources.yaml` or `source_types.yaml`.
- Never call `weave sources scaffold`. That's `/source-scaffold`'s job.
- Never recommend forking the thinkweave repo. All adaptations live
  in the user's vault overlay or in `~/.claude/commands/`.
