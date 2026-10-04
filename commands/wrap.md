---
name: wrap
owns_mechanic: session_extraction
consumes: [weave_extract, weave_concepts, weave_project_snapshot, weave_wrap_finalize]
produces: [session.md, DECISIONS.md, BACKLOG.md]
tools:
  - Read
  - Bash
  - weave_project_snapshot
  - weave_extract
  - weave_concepts
description: End-of-session memory extraction. Compose insights/decisions inline, call `weave_extract` once, then `weave wrap-finalize` (deterministic tail). Self-contained; never prompts the user.
---

# /wrap — Session-End Memory Extraction

End-of-session memory extraction for the thinkweave vault. **Self-contained and headless-safe**: never prompt the user. You decide what's worth recording (which insights, which decisions, which todos); if the user gave direction earlier in *this* session about what to capture, honor it — but do not ask.

**One inline pass.** Compose the session's insights and decisions yourself, call `weave_extract` once, then run `weave wrap-finalize` (one Bash call — prune → index → judge → landing → drift, zero model turns). For ≤5 notes the overhead of spawning a subagent exceeds the per-turn savings; do the writing inline. (An older revision of this skill spawned a Sonnet extraction subagent — that was reversed after measurement: 25 tool uses and ~8 min on a small wrap, dominated by spawn + over-verification.)

Two minor variants:
- **Live wrap** — running in-session before `/clear`. You have the conversation; that's the source.
- **Catch-up wrap** — headless (e.g. `claude -p "/wrap"`) over a session that already ended. There is no live conversation; you work from `events.jsonl` + the session note's auto-extract skeleton + `git log/diff`.

The steps below cover both. Step 1 + 2 differ in source material; everything from step 3 onward is identical.

---

## 1. Find the session note (or note its absence)

**Resolve by exact identity, in both modes — harness-neutrally.** The hooks stamp the *harness* session id as `source_session:` on the session note's frontmatter, and `weave_extract(session_id=<that raw id>)` resolves the note through that stamp — auto-creating one only if no note carries the id. But the id lives in a **different environment variable per harness** (`CLAUDE_CODE_SESSION_ID` on Claude Code, `PI_SESSION_ID` on Pi, `CODEX_SESSION_ID` on Codex), so never read `$CLAUDE_SESSION_ID` directly: it is empty on any non-Claude harness, and a wrap that reads an empty value mints a detached second note while the real hook-created one keeps none. Resolve the id once with the neutral resolver instead:

```
id=$(weave session-id)   # prints the running harness's session id; empty + exit 1 when none is set
```

**`id` is non-empty** → pass that raw id as `session_id` at step 3 and you land on *this* session's hook-created note by construction. Read it if you want its material (`commits`, `files_touched`, sometimes `## Candidate Insights`): `weave show "$id"` resolves the same way. If it is `processed: true` + `auto_extracted: true` you are in catch-up mode by definition; pass `force=true` at step 3.

Never search for the session note by recency when you have an id. Several sessions share one checkout and one vault; "most recent session in this project" returns whichever concurrent session wrote last, and a `force=true` extract onto it overwrites another session's note.

**`id` is empty** (`weave session-id` exited non-zero — a genuinely headless run, or a harness that exports no session-id variable) — only then fall back to recency, WITH the #209 identity guard. Never mint a fresh slug for a live session that already has a hook-created note; minting is for the genuine no-note case alone.
```
weave search --type session --project <project> --limit 1
```
- **Session note exists** → **identity guard first.** Read its `source_session`. If it is set and does not match a session id you can confirm, this may be another session's note: do not touch it — mint a fresh ID and proceed. Same if the note is already `processed: true` / `auto_extracted: true` and you cannot confirm the match; a wrong `force=true` here is unrecoverable.
- **No session note** → mint an ID (`<slug>-<date>`) and proceed.

Optionally add a `## Summary` section to an existing session note (2–3 sentences) by editing the markdown directly. Skip for tiny non-code conversations — `weave_extract` will set the summary from its `summary=` argument.

## 2. Gather your source material

**Live mode** — the full conversation in this turn. That's the *narrative*; `events.jsonl` is only the skeleton (raw tool events). The narrative is what makes insights non-textbook and decisions have real Context/Decision/Consequences.

**Catch-up mode** — read the session folder's `events.jsonl` (raw tool events: files edited, bash commands, commit hashes, test results), the session note's auto-extracted `## Summary` skeleton, its `commits` and `files_touched` frontmatter, and `git log`/`git diff` for the window if a commit range is obvious. Accept the quality floor of working from events + git alone — this is the headless reality.

## 3. Call `weave_extract` once

Apply the §C content rules below: load the concept vocabulary (`weave_concepts(min_count=5)`), then compose at most `extract.insights_cap` (default 3) insights + the decisions worth formalizing + the user's explicitly-stated future plans as `todo`-tagged insights. Then one call:

```
weave_extract(
  session_id   = <the id from `weave session-id`, else the ses-id or minted id>,
  project      = <project>,                  # required if no session note exists
  summary      = "<≤400 chars — see C0>",
  insights     = [ {title, body, concepts, tags?}, ... ],   # capped at extract.insights_cap, default 3 (todos count)
  decisions    = [ {title, rationale, outcome, file_paths, concepts, summary?, predicted_outcome?, supersedes?, cites?}, ... ],
  force        = <true if the session is already processed/auto-extracted>,
)
```

`weave_extract` is pure Python — zero API cost, one tool round-trip. It writes the notes/decisions to the session folder, indexes them, and auto-extracts any `todo` items from the body.

### Use the auto-extracted draft when it exists

If the session note has a `## Candidate Insights` section (populated when hooks ran end-of-session auto-extract), **refine it; do not start from scratch.** The candidate section already names what the session produced; your job is to add the personal-experience framing (problem/surprise/gotcha), pick concepts, and decide which entries are insights vs decisions vs cut. Composing fresh when a draft exists is the biggest avoidable output-volume cost on a wrap.

## 4. Run `weave wrap-finalize` (one Bash call)

```
weave wrap-finalize <ses-id> --project <project> [--verdicts '<json>'] [--tasks <file>]
```

Copy the `▶ To finalize:` line `weave_extract` printed **verbatim** — it
names the minted `ses-…` session-note id (#181), which resolves the freshly
archived events even after a forced re-extract. Do not substitute the raw
harness session id (the `weave session-id` / `$CLAUDE_SESSION_ID` UUID).

**Prompt verdicts (#101) — compose them in step 3, pass them here.** You are the prompt labeler: while composing insights/decisions, also judge each *user* prompt this session on three registers — did it clearly push back on agent work (`correction`), clearly endorse it (`confirmation`), or ask a substantive exploratory question (`probe`)? Apply §C5 below; if any non-neutral verdicts exist, add:

```
--verdicts '[{"prompt": "<the prompt'\''s opening words, verbatim>", "register": "correction", "about": "<what it was about — see C5 grounding>"}, ...]'
```

`prompt` is matched case-insensitively as a prefix against the session's captured prompt events; wrap-finalize appends the events idempotently (re-wraps never double-write) — feedback registers in the frozen `feedback` schema, `probe` as the classification event that powers probe pressure and `/discover`. No verdicts → omit the flag entirely. In catch-up mode the prompt texts are the `type: "prompt"` rows of `events.jsonl`.

**Task declaration (#189) — you judge, the pass applies.** The declaration records the session's work at task grain: which open task this session continued, what new work it started, what the user declared done. The pass makes no decisions — apply §C6, write the JSON to a temp file (`mktemp`), pass `--tasks <path>`:

```json
{"sparsity": "boundary", "declared": [
  {"continuing": "tsk-…",              // this session continued that open task…
   "title": "…", "asked": "#NNN",      // …OR a mint: title required, no continuing
   "done": false,                      // true ONLY on the user's explicit done
   "consumes": ["dec-…", "src-…"],
   "children": ["tsk-…"],              // seam children this task dispatched (see below)
   "round": {"did": {"paths": […], "commits": […], "attempts": N},
             "outputs": [{"kind": "pr|file|url|artifact|commit|note", "ref": "…",
                          "role": "deliverable|intermediate"}],
             "notes": ["n-…"],                 // several tasks only: this task's insights
             "feedback": [{"register": "…", "prompt_ref": "…", "ts": "…"}]}}  // likewise
]}
```

The task note is a **ledger**: its round points at what other surfaces own and the pass renders the body from it. `asked` is the ticket's tracker ref — `#NNN` (this repo), `github:<owner>/<repo>#NNN` or `jira:<KEY>-NNN`; a declared ref resolves to the open task already carrying it (devloop runs on that ticket included), so a ticket picked up again never needs its task id. `outputs` names what the round produced with a role — the deliverable versus scratch; off-disk products (a published deck URL, an artifact) belong here too. With **one** declared task the pass attributes every insight `weave_extract` minted and every verdict to it — omit `notes`/`feedback`. With several, list each task's own (insight ids from the `weave_extract` result); undeclared items stay unattributed.

Your two inputs: the **open tasks** served at SessionStart (continuing candidates), and the **seam children** — the per-dispatch tasks the hooks minted for this session's subagents, listed by `weave task ledger --session "$id"` (one JSON row each). Attribute each child to the declared task whose work dispatched it; timestamps cannot do this under concurrent tasks, which is why it is your call. A child you cannot attribute confidently: leave undeclared — unattached is truthful, guessed is not. No task-shaped work this session → omit the flag entirely. Catch-up mode → `"sparsity": "task-id-only"`, no `children`, no `done` (you were not present; declare only what the events show).

**CLI resolution — PATH-independent by design (#47).** The `weave` above (and in every other Bash call in this skill) is the committed launcher `bin/weave` from the thinkweave checkout: it self-locates the repo and resolves uv via the same ladder as the MCP server's `bin/weave-mcp-launch`, so it works without the venv on PATH. On the plugin route Claude Code puts the plugin's `bin/` on the Bash PATH, so the bare call just resolves. If `command -v weave` comes up empty (dev checkout wired via `.mcp.json`, or a pip install whose venv scripts dir isn't on PATH), invoke the launcher by path — `<thinkweave-repo>/bin/weave wrap-finalize …` — where `<thinkweave-repo>` is the checkout you're working in, or the `--project` value in the registered thinkweave MCP server entry (`.mcp.json` / `~/.claude.json`). Never fall back to hoping the venv's console script is on PATH: that is the asymmetry where the MCP half of `/wrap` works while the finalize half silently fails.

Does in one process, zero model turns:
- prune orphan session folders (conservative GC; this session is protected)
- incremental reindex (picks up freshly written notes, drops pruned rows)
- `judge_and_writeback` on the new decisions (verdict + status from git evidence)
- regenerate DECISIONS.md + BACKLOG.md
- concept-drift advisory (read-only — proposes nothing, just reports)

For any decision that carried a `predicted_outcome:` this wrap, `wrap-finalize` initializes `prediction_match: pending` (the pending initializer) — it does NOT evaluate the prediction itself. The `/judge-prediction` skill is the prediction judge; it runs live the next time a successor decision supersedes this one (via `/wrap`'s composer) or via the cron drain (`claude -p "/judge-prediction --drain"`). If the manifestation pointer is *immediately* checkable from this session (e.g. the prediction said "after this commit, file X has property Y" and that's verifiable right now), you MAY tail-call `/judge-prediction --decision <new-id>` after `wrap-finalize`, but you are not required to — pending is a fine default.

Add `--json` for headless flows. The CLI exits non-zero if any step errored.

**Does NOT** touch STATE.md (see step 5) and does NOT run `/tighten`. If drift surfaces a proposed concept at threshold the report mentions it; promotion is `/tighten`'s job, run separately.

## 5. STATE.md — only if the big picture changed (live mode only)

If this session opened a new area, made a major architectural shift, or otherwise changed what someone needs to know first about the project:
```
weave landing --project <project> --doc state
```
Or use `weave_landing(project=..., doc="state", state_context=true)` to get raw data and write a narrative STATE.md yourself. Routine work in existing areas — skip. Catch-up mode — always skip (a headless pass doesn't have the context to judge a big-picture change).

## 6. Done — emit nothing by default

The CLI output of `weave_extract` and `weave wrap-finalize` IS the report: session note ID, notes/decisions created with IDs, judge verdicts, per-step timing line, drift advisory. The user sees that output. **Do not restate it.** Re-formatting it into a markdown bullet list adds 1–2 KB of model output (30–60s of pure generation time) for zero new information.

Emit text only when there is something *not* in those CLI outputs that the user needs to know — an error you handled, a manual action they should take, a STATE.md change you wrote (step 5), or a wrap-flag they should know about (e.g. you noticed something during composition worth surfacing). A one-line acknowledgement is fine; anything resembling step 6 in the old skill is not.

---

## §C. Content rules

### C0. Summary field — ≤ ~400 chars
The `summary=` arg lands in the session note's frontmatter and shows up in `weave search` results, `weave_timeline` listings, and any retrieval that surfaces the session note. It's high-read, low-bandwidth. **Cap at ~400 chars (2–3 actual sentences, not five clauses each).** Name what was investigated and what changed; numbers if they fit. The decisions' rationales carry the detail — do not duplicate them here.

### C1. Load the concept vocabulary
`weave_concepts(min_count=5)` first. The lower-tail (1–4 occurrence) concepts are rarely the right pick for new notes — proposed_concepts catches anything missing automatically — and the `min_count=5` payload is roughly half the `min_count=2` payload, which compounds when wraps run many times a day. Reuse existing labels — don't invent a new concept when one fits.

### C2. Write insights — `weave_extract` `insights=[...]`
Max 3. Quality over quantity. **Body cap: ~1000 chars per insight (≈ 6 short lines).** Over-writing is the dominant model-turn latency cost in a small wrap — a 50%-overlong composition adds 30–90s of pure output time, and that's the *visible* part of `/wrap` the wrap-finalize fix can't touch. If an insight won't fit in 1K it's two insights or a session-note narrative, not one insight.

Each insight captures **personal experience**, not textbook facts:
- what problem or surprise led to it; what was tried that didn't work, and why; the non-obvious implication or gotcha.

**BAD**: "SQLite WAL mode allows concurrent readers while one writer holds the lock."
**GOOD**: "WAL mode was the fix for index corruption when hooks and CLI ran simultaneously. The default rollback journal blocks concurrent readers, so the indexer failed silently when a hook was mid-write. Switching to WAL eliminated this — but WAL doesn't help with concurrent *writers*, only concurrent reads during a write."

**Tags policy — minimal by default.** Only two tags are mechanical: `todo` (explicit future-plan tracking, never reflexive) and `probe` (insights prompted by a substantive user question). Everything else (`debugging`, `performance`, `refactor`, etc.) is optional and usually *not* worth adding — concepts already carry the semantic load, and each reflex tag adds payload across every wrap. Omit `tags=[]` entirely unless you have `todo` or `probe`.

**Probes**: tag `probe`, title = the question, body = what was learned (not a textbook restatement). One probe per question — don't also make a separate insight for the same thing.

**Future plans**: things the user explicitly wants tracked → insights tagged `todo`. Never add `todo` otherwise. Todos count toward the max-3 cap.

### C3. Write decisions — `weave_extract` `decisions=[...]`
Real Context / Decision / Consequences, not just the conclusion:
- **Context**: what problem forced this; alternatives considered and rejected.
- **Decision**: what was chosen and WHY (not just WHAT).
- **Consequences**: trade-offs accepted; what got harder, what got easier.

**Rationale cap: ~1500 chars** — one paragraph per C/D/C section. File paths and test references carry the rest; do not re-narrate the implementation in prose. The `file_paths` array points to the code; the rationale points to the *why*.

Per decision dict: `title`, `rationale` (the C/D/C prose), `outcome` (`committed`/`abandoned`/`partial`), `file_paths` (relevant paths), `concepts` (≥2), optional `summary` (one sentence — powers DECISIONS.md), optional `supersedes`/`cites`, and **optional `predicted_outcome`** — a single prose sentence carrying BOTH a claim AND a manifestation pointer (where to look, when, what query verifies it). If you cannot articulate a checkable pointer in one sentence, **omit the field entirely**. Boilerplate like "tests will pass after this fix" or "this will land" has no pointer and will sit `unevaluable` forever; better to record nothing.

  - **GOOD**: `"After the transcript-first ladder ships, the next /drain on the 3 queued AI Engineer videos archives all 3 as accepted (0 gemini_refused). Check the youtube-events queue archive after the next drain run."` — concrete claim + named pointer (queue archive) + window (next drain).
  - **GOOD**: `"Within a week, weave_search for 'wrap-finalize' returns ≥1 decision with verdict=kept and zero with verdict=reverted, indicating the deterministic tail held up under real wraps."` — concrete claim + checkable query + window.
  - **BAD**: `"tests will pass after this fix"` — no pointer, no window, will never resolve past `unevaluable`.
  - **BAD**: `"this should improve performance"` — no measurable claim, no manifestation pointer.

  Do NOT also restate the prediction in the rationale — the field IS the prediction. `wrap-finalize` will initialize new predictions to `prediction_match: pending`; the `/judge-prediction` skill takes over from there (live during a future `/wrap` that supersedes the decision, or via the cron drain).

### C4. Concepts are mandatory
Every insight and every decision: a `concepts` array, **≥2**, from the vocabulary loaded in C1. Pick concepts that connect this note to *other* notes (thematic, not descriptive). Prefer specific domain terms (`fts5`, `write-ahead-log`) over generic ones (`architecture`, `testing`). Test: "would another note about this topic share this concept?" Terms not in the ontology are accepted automatically into `proposed_concepts:` by the server — you don't pre-canonicalise.

### C5. Prompt verdicts — grounded, or not at all
Downstream consumers trust these labels (RLVR export for feedback; probe pressure and `/discover` for probes), so a false non-neutral is worse than a miss. Two channels: **feedback**, whose sign is the register (`correction` = negative, `confirmation` = positive), and **probe**.
- `correction` = the user pushed back on something the agent did or concluded ("no, that's wrong", "revert that", "actually…"-redirections). A new instruction, a scope change, or "carry on" is **neutral**.
- `confirmation` = the user explicitly endorsed agent work ("looks good", "ship it", "exactly"). A hedged endorsement ("looks good except…") is **neutral**.
- `probe` = a question whose *answer the user would want to keep or have researched* — they were trying to understand or decide something, not just steer the agent. The test is retention, not task-distance: "how does drift-v2 decide merges?", "what are practical RL exercises before the Orbis project?", "is something wrong with the hubs?", "do you reckon devloop can run autonomously?" are all probes even when they arise mid-task. Neutral: pure directives ("fix X"), yes/no confirmations ("does this compile?"), and operational where-is/what-is-the-state lookups ("what's uncommitted?"). When torn, **label it** — a probe feeds a research lead the user can ignore; a miss starves the behavioural acquisition rail (observed 2026-08-23: one probe label in the whole vault vs 25 feedback labels). A probe verdict labels the prompt event; it is independent of whether you also write a `probe`-tagged insight (§C2) for it.

**Grounding rule — every verdict carries `about`, or is discarded.** You hold the whole conversation; use it. `about` is one clause naming the concrete referent, resolved from session context, in vault vocabulary where it fits:
- feedback: *which agent action or conclusion* was corrected/endorsed — "endorsed the verdict-rail design for wrap-finalize", not "user said looks good". Resolve pronouns and deixis: a bare "yes, do that" grounds to whatever "that" was.
- probe: *what the question actually sought*, restated self-contained — "how drift-v2 cosine gating decides merges", not "asked a question about drift".

If you cannot name the referent from session context — generic courtesy ("thanks!", "sounds good" as conversation lubricant), a reaction whose antecedent is ambiguous, enthusiasm about nothing in particular — **emit no verdict for that prompt**. An ungrounded label is noise downstream: a reward event nobody can attribute, a probe nobody can research. Grounded-or-dropped, never padded.

- Machine-generated prompt text never gets a verdict: `<task-notification>`, `<agent-message>`, `<system-reminder>`, pasted logs/output, slash-command boilerplate.
- Most sessions have zero or few non-neutral prompts. That's the expected output, not a failure.

### C6. Task declaration — continuity is your judgment, take the user's cue
A work-grain task is a durable unit of work that outlives sessions — "ship the wrap task pass", not "answer a question". **One entry per distinct deliverable**: a CLI feature and a pitch deck are two tasks even when one session did both, and folding them into one entry merges their rounds and commits for good. Pure Q&A declares nothing; slicing one deliverable into several mints is the opposite error.
- **The Open Tasks rules** — the SessionStart Open Tasks section states these three, in these words:
  - Work on a listed task's ref continues that task: /wrap declares `continuing: <tsk-id>`, never a second task for the ref.
  - A request to finish a listed ref closes its task: /wrap declares `done: true` on it, with no tracker search.
  - A dispatch closed in this session is still its work task's child: /wrap lists it in `children`.
- **Continuing vs mint**: the round appends to the continued note; a second note for the same work poisons playback. Mint only for genuinely new work. The user's framing wins: "back to X" = continuing, even if the angle changed; a user saying a task is finished = `done: true`, and nothing else closes a task. A minted task's `title` names the ticket's intent ("dogfood greet CLI"), not this session's first slice of it ("fix greet typo").
- **Re-wraps restate, never add**: each round describes this session's whole work on **its own** task. A second wrap of the same session replaces each declared task's earlier round and reuses any task it minted under the same title. So re-declare **every** task the session touched — the ones the earlier wrap declared, with their full rounds, plus any new ones — never one merged entry for the whole session.
- **`round.did`** is the segment's ledger: files actually touched, commit hashes this task produced (from the session note's `commits` frontmatter — attribute per task when the session interleaved several), fix-round count in `attempts`.
- **`children`**: attribution, not bookkeeping — say which declared task each dispatched subagent served, open or closed; a dispatch that already finished is still listed. Declaring a closed child only sets its parent; it never reopens it or adds a round. Confidence rule as in step 4: unattributable children stay undeclared.
- **Never**: reopen a closed task, or declare tasks for pure Q&A sessions to have something to declare.
