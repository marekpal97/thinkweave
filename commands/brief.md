---
name: brief
owns_mechanic: daily_orientation
consumes: [weave_health, weave_timeline, weave_search, weave_prompts, weave_concepts, weave_read, weave_create, weave_brief_mark]
produces: [digests/brief-YYYY-MM-DD-HHMM.md, context_served(source=brief)]
tools:
  - Bash
  - Read
  - weave_timeline
  - weave_search
  - weave_prompts
  - weave_concepts
  - weave_read
  - weave_create
description: Daily orientation — a live meta layer over the nightly digests. One Bash call (`weave health --json`), a handful of weave_* retrievals, judgment narration, one `weave_create` (the next watermark), one `weave brief mark`. Pull-only, user-invoked; flags digest/cron failures at consumption, never heals them unasked.
---

# /brief — Daily Orientation

Read the substrate fresh and tell the user where the edges are: what landed and what it changes, whether a lane went quiet because nothing was kept or because the cron died, what they have been asking that nothing answered. **Self-contained; never prompts the user.**

**Composition (dec-696bacfb): existing surfaces + your judgment.** The only bespoke rail is `weave health` (evidence no retrieval tool serves) and the `mark` write at the end. Everything else is the retrieval tools you already have — there is no collect payload, and no Python decides what is worth saying.

**Not a digest.** The nightly `/dream` digests summarise what happened; `/brief` reads them *against the user*. Nothing pedagogical here — recall and tutoring are `/learn`. No push of any kind.

## 1. Health — one Bash call

```
weave health --json
```

(`weave` is the committed launcher `bin/weave`; if `command -v weave` is empty, call `<thinkweave-repo>/bin/weave` by path — same rule as `/wrap`.)

Deterministic evidence nothing else serves: cron jobs joined to run evidence (`stale`/`missing`), per-lane queues carrying `jobs`/`dead_jobs` (every cron feeder bound to the lane; one dead feeder starves it — weakest link), hook errors, nightly-digest freshness. If the digest is stale or missing, open the brief with a loud one-line banner (the `/dream` cron likely died) — then brief from raw landings anyway.

## 2. Retrieve — existing tools, one pass

1. **Watermark** — `weave_search(query="", type="digest", limit=10)`: the newest note titled `brief-…` is the previous brief; its date is `since`. None → first run, use the last 24h and say so.
2. **Timeline** — `weave_timeline(days=<since→now>)`: sessions + decisions in the window.
3. **Landings** — `weave_search(query="", type="source", since=<watermark>)`: what the acquisition spine kept, grouped by `source_type`. Combined with health's lanes: zero landings + healthy feeders = *ran, kept nothing*; zero landings + a dead feeder = *starved* — say which.
4. **Asked** — `weave_prompts`: recent probe-classified prompts (the questions the user actually asked). Declared focus is only a floor: `Read` `PRIORITIES.yaml` `focus.*` and `RESEARCH_FOCUS.md` `## Concept Gaps` — precedence **asked > done > declared** (behavioral-over-declared, dec-549194d3). "Done" is the concept edges you can already see on the window's sessions/decisions from step 2.
5. **Optional, by judgment** — `weave_read` a landing that looks load-bearing; `weave_concepts`/`weave_search(mode='similar')` when a new↔old connection seems worth one check. Hub/theme movement is *garnish, not a section*: mention a catalyst only when you happened to see one worth citing — hubs are brittle; never fan out to hunt for them.

Keep it to one pass — no re-retrieval loops to "double-check" a quiet day.

## 3. Narrate — your judgment

Render only what is worth saying, roughly in this register: **in brief** (2–3 lines), **health** (only when flagged), **what landed & what it changes**, **quiet lanes** (kept-nothing vs starved — name the dead cron), **focus** (asked-but-unanswered leads; "you said you wanted X — nothing landed against it" for declared-only), **anything due** (a decision whose prediction looks judgeable, a proposed concept at the promotion edge — only if you noticed it, never as a required sweep).

A quiet day is a five-line brief; do not pad it. Second person, knowledge voice, `[[id]]` wikilinks on every note you cite.

## 4. Persist — the brief IS the next watermark

One `weave_create`, then one Bash call:

```
weave_create(type="digest", title="brief-<YYYY-MM-DD-HHMM>", body=<the rendered brief>,
             frontmatter={"kind": "brief", "since": <since>, "served": <cited ids>})
weave brief mark --note <dig-id> --session "$CLAUDE_CODE_SESSION_ID" --served <cited ids…>
```

- The title **must** start with `brief-` (use UTC) — the vault files it at `digests/brief-<stamp>.md`, and the `brief-` prefix keeps it out of `weave health`'s nightly-digest freshness check. `kind: brief` is what the next run's watermark lookup matches. No new note type.
- `mark` logs every id you cited as `context_served(source='brief')` — a retrieval event in the harness session's buffer (archived at Stop, re-projected on index) plus the immediate rows keyed by the session note. Always pass `--session "$CLAUDE_CODE_SESSION_ID"` (the Bash env's name; the CLI also falls back to `$CLAUDE_SESSION_ID`). If it prints *nothing logged*, the session note does not exist yet — that is fine, do not invent an id.

## 5. Done — the brief is the output

Print the rendered brief once (step 3). Do not restate the `weave_create`/`mark` output; a one-line acknowledgement is fine only if something went wrong.
