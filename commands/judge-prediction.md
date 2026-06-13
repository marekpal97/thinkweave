---
name: judge-prediction
owns_mechanic: prediction_judgment
consumes: [weave_read, weave_search, weave_context, weave_update]
produces: [prediction_history, prediction_match]
tools:
  - Read
  - Grep
  - Bash
  - weave_read
  - weave_search
  - weave_context
  - weave_update
description: Evaluate one or more decisions' predicted_outcome against current vault + filesystem evidence. Appends a {match, judged_at, reason} entry to each decision's prediction_history. Self-contained; headless-safe.
---

# /judge-prediction — Prediction verdict pass

Single inline LLM pass that judges open `predicted_outcome:` claims on
decisions against current vault + filesystem evidence. Mirrors `/dream`'s
shape: one Bash scan → inline LLM judgment per item → one `weave_update`
write-back per decision.

Self-deciding. **Never prompts the user.** Designed for `claude -p
"/judge-prediction --drain"` cron use; same procedure interactively.

## Posture

The Python side pre-resolves the worklist and the obvious evidence
pointers — what `predicted_outcome` cites, which session, which file
paths, prior `prediction_history`. Your job is the genuinely-semantic
part: read the prose claim, fetch the named evidence at the
manifestation pointer, and pick one of five verdicts. No autonomous
wandering — if the pointer cannot be resolved to a concrete check, the
verdict is `unevaluable`.

## Invocations

```
claude -p "/judge-prediction --decision <dec-id>"     # single decision, manual
claude -p "/judge-prediction --drain"                 # cron: pull worklist
claude -p "/judge-prediction --drain --max 20"        # cap worklist (default 20)
```

Default worklist cap is **20**.

## Steps

### 1. Scan (one Bash call)

```bash
uv run weave judge --drain --json --max 20
```

For `--decision <id>` mode:

```bash
uv run weave judge --decision <dec-id> --json
```

Returns a JSON array of decision items. Each item:

```json
[
  {
    "decision_id": "dec-...",
    "decision_path": "/abs/path/to/decision.md",
    "predicted_outcome": "<prose claim + manifestation pointer>",
    "supersedes": ["dec-..."],
    "supersedes_history": [
      {"match": "...", "judged_at": "...", "reason": "..."}
    ],
    "successor_decision_id": "dec-... or null",
    "source_session": "ses-...",
    "trigger": "supersession|cron|manual",
    "file_paths": ["src/..."]
  }
]
```

If the array is empty, ship a no-op pass and log it.

### 2. For each decision item — judge

Read `predicted_outcome` carefully. Identify the **manifestation
pointer** — the "check X" / "look at Y" / "after Z" clause that names
where the prediction will become observable.

Match the pointer to a tool:

- **Queue-archive pointer** (`.weave/sources/<type>/archive/...`,
  "next /drain run", "the youtube-events archive") → `Bash` with `ls`,
  `grep`, `jq` against the archive jsonl.
- **Vault-note pointer** (a concept hub, a session, "the next decision
  on X") → `weave_search`, `weave_read`, `weave_context`.
- **Git pointer** ("the next N commits", "the diff on file X") →
  `Bash` with `git log --oneline -n <N>` or `git diff` scoped to
  `file_paths`.
- **Concept-hub pointer** (`vault/concepts/topics/<name>.md`) →
  `weave_read` on the hub path.

If the pointer cannot be resolved, fall through to `unevaluable`.

### 3. Verdict rules

Five values, exactly:

- **`confirmed`** — pointer-checked evidence supports the claim.
- **`contradicted`** — pointer-checked evidence refutes the claim.
- **`stale`** — was true at the time the prediction was made, but the
  substrate has moved on. **Hard rule**: only emissible when EITHER
  - the decision's frontmatter has `supersedes:` set (i.e. this
    decision was itself superseded earlier), OR
  - the pointer references a `source_type` / queue / file_path that
    no longer exists.

  Otherwise fall back to `unevaluable` or `contradicted`.
- **`pending`** — pointer references something not yet observable
  (the manifestation hasn't happened yet). Cron re-picks this on a
  future drain.
- **`unevaluable`** — pointer is broken, ambiguous, or the dependency
  has been removed in a way that doesn't fit `stale`.

`partial` is NOT a valid verdict.

### 4. Apply (one weave_update per decision)

Build the full appended `prediction_history` list — read the existing
entries from the decision (already in the scan payload would mean
re-loading; the simplest is `weave_read(<dec-id>)` once when uncertain)
and append the new entry. Then one call:

```
mcp__thinkweave__weave_update(
  note_id = "<dec-id>",
  frontmatter_updates = {
    "prediction_history": [<full appended list including new entry>],
    "prediction_match":   "<new verdict>",
    "judged_at":          "<iso timestamp>"
  }
)
```

New entry shape:

```json
{"match": "<verdict>", "judged_at": "<iso>", "reason": "<one sentence citing concrete evidence>"}
```

The frontmatter dict mirrors what `synthesis/prediction.append_verdict`
returns. The skill constructs the appended list itself — don't shell
out to the Python helper, because you're the one making the verdict
call.

### 5. Worked examples

#### Example A — `confirmed` from a queue-archive pointer

**Input** (one item from the scan payload):

```json
{
  "decision_id": "dec-7a3f12",
  "predicted_outcome": "After the transcript-first ladder ships, the next /drain on the 3 queued AI Engineer videos archives all 3 as accepted (0 gemini_refused). Check the youtube-events queue archive after the next drain run.",
  "supersedes": [],
  "file_paths": ["src/thinkweave/acquisition/sources/extractors/transcript_extract.py"],
  "trigger": "cron"
}
```

**Evidence trace**:

```bash
ls .weave/sources/youtube-events/archive/ | tail -5
# 2026-05-24-AI_Engineer-prompt_caching.jsonl
# 2026-05-24-AI_Engineer-rlvr_demo.jsonl
# 2026-05-24-AI_Engineer-claude_skills.jsonl

grep -c '"outcome": "accepted"' .weave/sources/youtube-events/archive/2026-05-24-AI_Engineer-*.jsonl
# 3

grep -c '"outcome": "gemini_refused"' .weave/sources/youtube-events/archive/2026-05-24-AI_Engineer-*.jsonl
# 0
```

**Output**:

```
dec-7a3f12 → confirmed: 3/3 AI Engineer youtube-events archived as accepted (0 gemini_refused) in .weave/sources/youtube-events/archive/2026-05-24-AI_Engineer-*.jsonl
```

#### Example B — `contradicted` from a git-log pointer

**Input**:

```json
{
  "decision_id": "dec-9b2e44",
  "predicted_outcome": "The next 3 commits will all touch src/thinkweave/synthesis/judge.py — the family-dispatch refactor needs three more passes to land.",
  "supersedes": [],
  "file_paths": ["src/thinkweave/synthesis/judge.py"],
  "trigger": "cron"
}
```

**Evidence trace**:

```bash
git log --oneline -n 3
# 3712968 New source families: newsletter-{events,concepts} + youtube-{events,concepts}
# 7b5a6ea News pipeline: worker-bug retry, weave news-stats, OpenAI provider swap
# 4ce31aa /weave-wrap: tighten content rules for output-volume reduction

git log --oneline -n 3 -- src/thinkweave/synthesis/judge.py
# (empty)
```

**Output**:

```
dec-9b2e44 → contradicted: last 3 commits (3712968, 7b5a6ea, 4ce31aa) touched source families, news pipeline, and /weave-wrap content rules — none touched src/thinkweave/synthesis/judge.py
```

#### Example C — `stale` from a supersession

**Input**:

```json
{
  "decision_id": "dec-1c4d88",
  "predicted_outcome": "The test/commit family regex tables will catch >50% of predictions correctly within the first week of cron operation. Check predicted_outcome dispatch verdicts via weave rlvr export after 7 days.",
  "supersedes": [],
  "supersedes_history": [],
  "successor_decision_id": "dec-3e5a01",
  "file_paths": ["src/thinkweave/synthesis/judge.py"],
  "trigger": "supersession"
}
```

The scan payload reports `trigger: "supersession"` and a non-null
`successor_decision_id`. Confirm by reading the successor:

```
weave_read(note_id="dec-3e5a01")
# successor frontmatter has: supersedes: [dec-1c4d88]
# successor rationale describes deleting the regex tables in favor of
# structured {family, polarity} dispatch
```

`Bash` confirm the substrate move:

```bash
grep -n "FAMILY_REGEX" src/thinkweave/synthesis/judge.py
# (empty — tables removed)
```

The successor's frontmatter shows `supersedes: [dec-1c4d88]`; the
predicted manifestation (regex-dispatch verdicts via `weave rlvr export`)
no longer exists. Hard rule satisfied: decision was superseded AND the
pointer references a removed dependency.

**Output**:

```
dec-1c4d88 → stale: superseded by dec-3e5a01; FAMILY_REGEX tables deleted from src/thinkweave/synthesis/judge.py before this prediction's evaluation window
```

### 6. Output

One stdout line per decision, in the order processed:

```
<dec-id> → <verdict>: <one-sentence reason citing concrete evidence>
```

This is what cron's stdout capture logs. Nothing else; no markdown
formatting, no recap.

**Error handling**. If `Bash`/MCP evidence-fetch fails for an item
(file gone, network error, malformed jsonl), emit:

```
<dec-id> → unevaluable: evidence fetch failed: <brief>
```

…and continue with the next decision. Don't crash the whole drain. The
`weave_update` write-back still runs for that decision with the
`unevaluable` verdict.

## Constraints

- **No subagents.** Single inline pass. The Python scan already
  resolved the structural pre-work; you do only semantic judgment.
- **Verdict reason must cite concrete evidence** — a path, a count, a
  commit hash, a missing file. Never "based on the context" or
  "appears to be."
- **`stale` only under the hard rule.** Decision's frontmatter has
  `supersedes:` set, OR the pointer references a removed
  source_type/queue/file_path. Otherwise fall through to
  `unevaluable`.
- **Never invent a manifestation pointer.** If `predicted_outcome` is
  missing or empty, emit `unevaluable` with reason
  `"no predicted_outcome to evaluate"`.
- **One `weave_update` per decision.** Even on `unevaluable` — the
  history entry records that the judge looked and couldn't decide,
  which prevents the same item resurfacing every drain.
