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

**Live wrap with no prior session note** (the common case — hooks haven't yet created one, or this is a non-code conversation): skip this step. Mint an ID (`<slug>-<date>` or `CLAUDE_SESSION_ID`) and go to step 3; `weave_extract` auto-creates the note. No `weave search` round-trip.

**Catch-up wrap** (headless, or you suspect an auto-extracted session note already exists):
```
weave search --type session --project <project> --limit 1
```
- **Session note exists** → read it. Frontmatter has `commits`, `files_touched`, sometimes `## Candidate Insights`. If `processed: true` and `auto_extracted: true` you're in catch-up mode by definition; pass `force=true` to `weave_extract` at step 3.
- **No session note** → mint an ID and proceed.

Optionally add a `## Summary` section to an existing session note (2–3 sentences) by editing the markdown directly. Skip for tiny non-code conversations — `weave_extract` will set the summary from its `summary=` argument.

## 2. Gather your source material

**Live mode** — the full conversation in this turn. That's the *narrative*; `events.jsonl` is only the skeleton (raw tool events). The narrative is what makes insights non-textbook and decisions have real Context/Decision/Consequences.

**Catch-up mode** — read the session folder's `events.jsonl` (raw tool events: files edited, bash commands, commit hashes, test results), the session note's auto-extracted `## Summary` skeleton, its `commits` and `files_touched` frontmatter, and `git log`/`git diff` for the window if a commit range is obvious. Accept the quality floor of working from events + git alone — this is the headless reality.

## 3. Call `weave_extract` once

Apply the §C content rules below: load the concept vocabulary (`weave_concepts(min_count=5)`), then compose at most `extract.insights_cap` (default 3) insights + the decisions worth formalizing + the user's explicitly-stated future plans as `todo`-tagged insights. Then one call:

```
weave_extract(
  session_id   = <ses-id or minted id>,
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
weave wrap-finalize <session_id> --project <project>
```

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
