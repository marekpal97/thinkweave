---
name: explain
owns_mechanic: explanation_artefacts
consumes: [weave_concepts, weave_search, weave_read, weave_graph, weave_create, weave_update]
produces: [explain note (type: note, kind: explain), optional HTML explainer beside it]
tools:
  - Read
  - Write
  - Bash
  - weave_concepts
  - weave_search
  - weave_read
  - weave_graph
  - weave_create
  - weave_update
description: One-shot explainer — `/explain` (the current session's knowledge) or `/explain diff [ref | #pr]` (one code change). Writes a `kind: explain` note in the session folder, optionally with an HTML explainer beside it; a re-run on the same target updates that note. Ends with non-blocking check questions. Never prompts.
---

# /explain — explain the session or one diff as a vault note

The user cannot absorb agent output as fast as it lands. `/explain` turns one body of work into an explanation the user can read at their own pace. It is one-shot: compose, write, report. It never asks the user anything and never runs a quiz loop. The check questions at the end are an invitation, not a gate. The user asks follow-ups in the conversation.

**Deterministic steps never prompt.** Everything code can do lives in `weave explain …` (Bash). Everything else is your judgement.

## Invocation

| Command | Scope | Target |
|---|---|---|
| `/explain` | `session` | `all` |
| `/explain session "<hint>"` | `session` | the hint, verbatim |
| `/explain diff` | `diff` | the current branch name; the diff is that branch against `main`, uncommitted changes included |
| `/explain diff <ref>` | `diff` | the ref, verbatim (for example `loop/issue-298` or `a1b2c3d..e4f5g6h`) |
| `/explain diff #<pr>` | `diff` | `#<pr>` |

The session scope explains *knowledge*: the concepts and mechanisms the session worked with. It is not a change log, because `/wrap` records the change log. A flag `--html` or `--md` overrides the form choice in step 5.

## Flow

### 1. Resolve the scope and the target

Parse the invocation with the table above. Resolve the session id and the vault root once:

```
id=$(weave session-id)          # empty + exit 1 when headless
weave config show               # prints vault_root
```

An empty id means no live session. Then the note goes to `sessions/misc/`, and step 2 has nothing to match: always create.

### 2. Run `locate`

```
weave explain locate --session "$id" --scope <session|diff> --target "<target>"
```

It prints the id of the explain note for this (session, scope, target), or nothing. An id means this is a **re-run**: step 6 updates that note. Nothing means a **first run**: step 6 creates one. Never decide this from search hits.

### 3. Gather the material

- **Session scope.** The conversation is the main source. Add the vault notes the session read, through `weave_read`. With a hint, keep to the part of the session the hint names.
- **Diff scope.** Read the change without changing the working tree:

  ```
  git diff main...HEAD; git diff HEAD          # /explain diff (branch + uncommitted)
  git diff <a>..<b>  |  git show <ref>         # /explain diff <ref>
  gh pr view <n> --json title,body,files; gh pr diff <n>   # /explain diff #<n>
  ```

  Read the changed files where the diff alone hides the shape. Read the test fixtures for the changed functions: step 5 grounds its samples in them. Look up vault decisions for the main changed files with `weave_graph(file_path=…, filter='decisions_for_file')`.
- **Both scopes.** Load the ontology labels with `weave_concepts` before you choose concepts.
- **Re-run.** `weave_read` the existing note. The new material is what the user asked since it landed.

### 4. Name the topics in one line

Print one line before you compose: `Explaining: <topic>, <topic>, <topic>.` It tells the user what is coming. Do not wait for an answer.

### 5. Compose

Follow the content outline and the register rules below. Choose the form by the content shape:

- **Markdown** for prose-shaped knowledge: a mechanism, a decision, a sequence.
- **HTML plus markdown** when the explanation needs motion or interaction: data moving through a function, a matrix operation, a parameter that changes an output.

The markdown note always exists. It holds the essence, the check questions and a link to the HTML file, so search, the graph and `/learn` can see the explanation.

On a re-run, compose only the follow-up: what the user asked, and the answer. Do not rewrite the first explanation.

### 6. Create or update the note

**First run.** Create the note in the session folder:

```
weave_create(
  type="note",
  title="Explain: <target or topic>",
  body=<the composed markdown>,
  project=<project>,
  session_id="$id",
  frontmatter={
    "kind": "explain",
    "scope": "<session|diff>",
    "target": "<target, exactly as passed to locate>",
    "concepts": [<≥2 ontology slugs>],
  },
)
```

The MCP schema nests the contract fields under `frontmatter=`. A top-level `concepts=` is dropped. The result names the note path, relative to the vault root. For the HTML form, Write `<vault_root>/<note folder>/<note-file-stem>.html`. Then set `html_path` to that file name: `weave_update(id=<note-id>, frontmatter={"html_path": "<note-file-stem>.html"})`.

**Re-run.** Update the note that `locate` returned:

```
weave_update(id=<note-id>, body_append=<follow-up text>)
```

The first re-run appends a `## Follow-ups` section. Later re-runs append one `### <YYYY-MM-DD> — <the follow-up question>` entry to that section. When the note has an HTML sibling, rewrite it with Write.

### 7. Run `check`

```
weave explain check --note <note-id>
```

Exit 1 lists the contract problems. Fix each with `weave_update` (or by writing the missing HTML file), then run `check` again.

### 8. Report

Report in a few lines: the note id, created or updated, the form, the HTML path when one exists, and the number of check questions. Invite follow-ups in one sentence.

## Content outline

Use these sections, in this order. Leave a section out when the material has nothing for it.

1. **`## In one paragraph`** — the essence. What is true now, in four sentences or fewer.
2. **`## The picture`** — the intuition before any formalism. A small diagram, a table or an analogy that matches the real mechanism.
3. **`## How it works`** — the mechanism, one short chunk per idea. Each chunk ends where the next idea starts.
4. **`## Terms`** — a table: term, plain meaning, where it appears. Every term the note uses that a newcomer would not know.
5. **`## Check questions`** — four to six questions, with the answers in a collapsed callout (`> [!note]- Answers`). Mix recall, prediction ("what happens if …") and contrast ("how does X differ from Y"). Nothing records the answers.
6. **`## Read next`** — the vault notes and files to open next, as `[[path|id]]` links or file paths.

## Diff additions

A diff explanation also holds these sections, after `## The picture`:

- **`## Contract`** — the input→output contract of the changed code. State what goes in and what comes out, before and after the change. A table with one row per changed entry point works well: entry point, input, output before, output after.
- **`## Before and after`** — one small, realistic sample that shows the change. Show data move: a dataframe before and after the changed function, which rows and columns of a matrix interact, a record before and after a pipeline step. Ground the sample in the test fixtures of the changed code where they exist. Label every sample **illustrative**, in its heading or its first line. Do not run the code at either ref.
- **`## Walkthrough`** — the diff in reading order, not file order. Each step names the file and the function, says what changed and why. Quote only the lines that carry the change.

## Register rules

These rules govern every sentence of the note and the HTML file. They are stated here because a harness output style never reaches Codex, Pi or subagents.

- One idea per sentence. Keep each sentence under 20 words. Never exceed 25.
- Use the active voice and the present tense.
- Use one term for one thing. Do not vary a term for style.
- Expand jargon and acronyms at first use, or replace them with plain words.
- Use stateful wording: say what is true now and what changed. Do not narrate what was attempted.
- Keep the density low: one claim per paragraph, white space between ideas.
- Avoid idioms and figurative language. State the fact directly.
- Before you write the note, read ten of its sentences again. Split any sentence over 25 words.

## HTML form

Load the `frontend-design` and `dataviz` skills if they are available. Without them, follow these rules:

- One self-contained `.html` file. Vanilla JavaScript and inline SVG; no build step.
- Mathematics through KaTeX from a CDN, with the plain-text formula as the fallback.
- A light theme and a dark theme, through `prefers-color-scheme`.
- The register rules above apply to every label and paragraph.
- Pick from these visual primitives, as the content needs:
  - a matrix-operation stepper that highlights which rows and columns interact at each step;
  - a dataframe before/after view, with the changed cells marked;
  - a pipeline stepper that shows one record move through each stage;
  - parameter sliders that recompute an output live;
  - a self-check quiz with the same check questions as the note, answers revealed on click.
- Samples in the HTML file carry the same **illustrative** label as in the note.

No viewer is required. The file opens directly in a browser.

## Non-goals

- No interactive question loop. That is `/learn`.
- No `concept` or free-text scope. Only `session` and `diff`.
- No traced execution of the code at the base or head ref.
- No new note type. `kind: explain` on a `type: note` is the discriminator.
