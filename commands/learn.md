---
name: learn
owns_mechanic: vault_tutoring
consumes: [weave_concepts, weave_read, weave_create, weave_queue, weave_prompts]
produces: [learn note (type: note, kind: learn), context_served(source='learn'), probe rows]
tools:
  - Read
  - Bash
  - WebSearch
  - AskUserQuestion
  - weave_concepts
  - weave_read
  - weave_create
  - weave_queue
  - weave_prompts
description: Vault-grounded tutor — `/learn <topic>`. Teach-then-test dialogue; the vault is the syllabus, the user's own learning history is the spine, a capped acquisition fill covers gaps, the transcript is the comprehension evidence. One learn note per session. Interactive by nature (not headless).
---

# /learn <topic> — vault-grounded tutor

Interactive. The user steers; you teach in the register their own vault history shows lands for them. A memoryless tutor presupposes nothing and re-explains everything; `/learn` presupposes **exactly what the vault evidences** and nothing more. Shape exemplar: `n-75689333` (disambiguation-led, misconception-driven, code-bridged) — `weave_read` it once if you have not seen it this session.

**Deterministic steps never prompt.** Everything the rail can do by code lives in `weave learn …` (Bash); everything else is your judgment.

---

## 1. Coverage — one retrieval, partitioned by provenance

```
weave_concepts                                   # pick 1–3 ontology slugs for the topic
weave learn coverage --topic "<topic>" --concepts <slug…> --json
```

The rail runs ONE retrieval (FTS on the topic ∪ concept walk) and partitions **after** by provenance — never hand-pick note types per track:

- `trajectory` — authored by the user: session, decision, note (incl. `kind: learn`, `til`, ChatGPT imports), sorted by date. This **is** the presupposition check.
- `material` — authored by the world: source, hub essence, theme, digest.
- `mode` — `test-first` iff a prior `kind: learn` note exists on the arc (`prior_learn_notes`); else `teach-first`.
- `first_contact` + `first_contact_line` — trajectory empty.
- `fill_cap` — `[learn] fill_cap` from config (default 3).

> The similarity leg is **not live** on `weave_context` (it is FTS → concept expansion → recency; #145). The rail therefore uses FTS + concept walk, deliberately dropping the recency supplement — recency padding would fabricate a trajectory.

`weave_read` the trajectory hits (newest learn note first, then sessions/decisions) and the top material hits. You now know what the user has touched, what stuck, and what the world says.

## 2. Open — recap, or first contact

- **Trajectory non-empty** → open with the **trajectory recap**: first touch, what stuck, last shaky point (from the latest learn note's `solid`/`shaky`, from session summaries, from ChatGPT-import Key Questions = record of past confusions).
- **Trajectory empty** → print `first_contact_line` verbatim. **Never fabricate a recap.**

Then name the session goal in one line (the parking rail anchors to it) and run an opening **calibration** question via `AskUserQuestion` (multiple choice).

## 3. Fill — thin material, capped

If `material` is thin for the goal (you judge): name the gap aloud, `WebSearch`, then drain **≤ `fill_cap`** sources via the real research workers **in parallel** — `/research <url>` one-shot per source (or `weave_queue(action="enqueue", …)` + `weave drain --source-type <slug> --limit <fill_cap>`). Narrate the wait. Teach from vault + the fresh notes. Overflow candidates → `weave_queue(action="enqueue", …)` for the nightly spine. Further capped fills may be *offered* mid-session, never forced.

**Fill failure** (no hits / worker error) → teach from what exists, record the gap as a probe (§6), never stall.

## 4. Session rhythm

recap → calibration → chunk → checkpoint → judge/correct/adapt → … → closing synthesis. No fixed length; the user steers.

- **Revisit (`test-first`)**: probe the old `solid` claims first; re-teach only what decayed; then extend with what landed since (new material dated after the last learn note).
- **New (`teach-first`)**: disambiguation map → chunks.
- **Question formats follow function.** Free-text is **mandatory** at prediction checkpoints and explain-backs (the encoding event — persist verbatim). `AskUserQuestion` for opening calibration, for **disambiguation probes whose distractors are the actual named conflations** (a wrong pick localizes the live conflation), and as pacing relief.
- Socratic default; prediction before reveal; a wrong prediction slows the walkthrough.
- **Feynman bar**: explain-backs are plain-language-to-a-novice; challenge jargon. Closing pass = teach it back to a newcomer.
- **Parking rail**: tangents are always followed, but past ~2 levels flag once — *keep going or park?* Parked tangents → probe (§6), or a pre-seeded next topic linked `builds_on`.
- **Re-explain requests** are disambiguated: explanation failed → friction log; deliberate re-cementing → retrieval practice. Ask when ambiguous.
- **A question the user can't answer → probe** (§6).

## 5. Teaching contract (hard rules, per chunk)

Evidence-derived from `src-ae9cbc5c`, `src-40e37b6e`, `src-fc676ead`, `src-415f988e`, `src-12fda025`, `n-75689333`.

1. Intuition → picture → formalism, in that order.
2. Every equation ships with a symbol table; no symbol before its row.
3. One concrete numeric example per formal claim.
4. Derivations step-by-step, each step labeled by its rule; no "it can be shown".
5. Open with the disambiguation map; the traps become the checkpoints.
6. Teach against the named misconception: state it, then break it.
7. Formula→code bridge: pseudocode (and/or 3 lines of real code) + the practical trap.
8. Applies/fails boundary + nearest-neighbor contrast; variant families as orthogonal levers.
9. "Why did the field move" in systems terms (memory, compute, stability), never elegance.
10. Jargon taught, never assumed: first use = expansion + gloss.
11. Bloat solved by structure: short chunks, one idea each, checkpoint between.
12. Presuppose only what the vault evidences the user knows — the trajectory partition **is** the presupposition check; everything else gets a refresher or a calibration question.

## 6. Probes — unanswered questions and parked tangents

```
weave learn probe --session "$CLAUDE_CODE_SESSION_ID" --text "<the question, verbatim>"
```

Writes the same `prompt` + `probe` event pair `weave wrap-finalize --verdicts` persists, keyed by the harness UUID, so `weave_prompts` / the dream probe-distiller pick it up and it feeds acquisition. Unresolvable session → the rail writes nothing and says so; do not retry with a made-up key.

## 7. Close — one learn note per session

Closing synthesis, then the final **Feynman explain-back** (free text, persisted verbatim). Then:

1. `weave_create(type="note", title="Learn: <topic>", body=<coverage + chunks taught + checkpoints + corrections>, concepts=[…], frontmatter={…})` — the MCP schema nests the contract fields under `frontmatter=`; top-level kwargs are silently dropped. Contract (see [Lifecycles §Learn note](../docs/LIFECYCLES.md#learn-note-kind-learn)):

   ```yaml
   kind: learn
   topic: "<topic>"
   solid:   [{concept: <slug>, date: YYYY-MM-DD}]            # transcript answers are the evidence; your judgment, no scores
   shaky:   [{concept: <slug>, date: YYYY-MM-DD, why: "…"}]
   friction: ["<where an explanation failed and why>"]        # accumulated friction amends this contract over time
   explain_back: "<final Feynman explain-back, verbatim>"
   builds_on: [<prior learn-note ids on this arc, from prior_learn_notes>]
   questions: ["<every question asked>"]
   ```

2. `weave learn check --note <new-id>` — exit 1 lists the contract problems; fix via `weave_update` and re-check.
3. `weave learn mark --session "$CLAUDE_CODE_SESSION_ID" --note <new-id> --served <every note id you read or taught from>` — `context_served(source='learn')`.

Compaction mid-session is fine; the note is composed at the end regardless. Report: mode, trajectory/material sizes, fills run, chunks taught, solid/shaky, probes recorded, the learn-note id.

## Non-goals

No new substrates (terminal + Obsidian only), no spaced-repetition state, no auto-selected topics, no fixed session length, no schema change. `/explain-diff` lives in funloops (fl#23).
