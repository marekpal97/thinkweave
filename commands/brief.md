---
name: brief
owns_mechanic: daily_orientation
consumes: [weave_brief_collect, weave_read, weave_create, weave_brief_mark]
produces: [digests/brief-YYYY-MM-DD-HHMM.md, context_served(source=brief)]
tools:
  - Bash
  - weave_read
  - weave_create
description: Daily orientation — a live meta layer over the nightly digests. One Bash collect (`weave brief collect --json`), one narration, one `weave_create` (the next watermark), one `weave brief mark`. Pull-only, user-invoked; flags digest/cron failures at consumption, never heals them unasked.
---

# /brief — Daily Orientation

Read the substrate fresh and tell the user where the edges are: which landings contradict a held position, whether a lane went quiet because nothing was kept or because the cron died, what they have been asking that nothing answered. **Self-contained; never prompts the user.** Composition mirrors `/wrap`: *deterministic collect → narration*, subscription compute only.

**Not a digest.** The nightly `/dream` digests summarise what happened; `/brief` reads them *against the user* (the focus model below). Nothing pedagogical here — recall and tutoring are `/learn`. No push of any kind.

## 1. Collect — one Bash call

```
weave brief collect --json
```

(`weave` is the committed launcher `bin/weave`; if `command -v weave` is empty, call `<thinkweave-repo>/bin/weave` by path — same rule as `/wrap`.)

The payload is the whole source; do not fan out MCP calls to "double-check" it. Keys (full schema in `operations/brief.py`'s docstring):

| key | what it is |
|---|---|
| `since` / `since_reason` / `watermark` | the window — the previous brief note (`kind: brief`), or now−24h on a first run (`first_run_24h` → say so) |
| `health` / `banner` | `weave health --json` + the loud line when the nightly digest is stale/missing |
| `timeline` | sessions + decisions since the watermark |
| `landings` / `lanes` | notes that landed, grouped by `source_type`; per-lane verdict `kept` · `ran_nothing_kept` · `dead` · `unknown` — a lane binds every cron job that feeds it (`jobs`), and one stale/missing feeder (`dead_jobs`) makes it `dead` |
| `focus.concepts` / `focus.active_projects` / `focus.asked_below_floor` | the ranked concept vector (asked ▸ done ▸ declared) + active projects — **14-day window** (`salience.activity_window_days`), not since-watermark like everything else |
| `catalysts` / `contradictions` / `theme_movements` / `essence_rewrites` | agrees/contradicts/extends entries (all), the contradicts-then-extends subset you render, theme-log deltas, hub essence rewrites since the watermark |
| `attention` | predictions due, proposed concepts one short of promotion, probed concepts with zero landings |
| `connections` / `connections_reason` | ≤2 strong new↔old similarity hits, or why there are none |
| `render_plan` | **the sections you render, in order** |
| `served_ids` | every id the payload surfaces — passed to `mark` in step 3 |

### The focus model (what "the user cares about" means)

`focus.concepts` is a merge of three layers with a precedence order: **asked** (probe-classified prompts, with the verbatim `probes`) leads, **done** (concept edges on this window's sessions/decisions) is second, **declared** (`RESEARCH_FOCUS.md` `## Concept Gaps`, `PRIORITIES.yaml` `focus.*`) is only a floor. Rows with `declared_only: true` never rank; they surface as *"you said you wanted X — here's what landed against it"* (or "nothing has"). This vector drives FOCUS, the PAPERS ranking, and the probe half of ATTENTION.

## 2. Narrate — render exactly `render_plan`

Render the sections in `render_plan`, in that order, and **nothing else** — an absent key means that section is empty today, and an empty section is not rendered. A quiet day is a five-line brief; do not pad it. Second person, knowledge voice, `[[id]]` wikilinks on every note you cite.

| section | how to render it |
|---|---|
| `in_brief` | 1–3 sentences: the window (`since`, first run → "first brief, last 24h"), the one thing that matters most today, and "nothing landed" when `landings` is empty. Always present. |
| `health` | **First content line, before anything else.** `banner` verbatim-ish: what is stale, since when, likely cause (the cron died / never ran). Then `health.flags` (stale or missing jobs, hook errors). Offer to compose the digest on demand — **but do not run `/dream` or heal anything unless the user says so**, and still brief from the raw landings below. |
| `contradictions` | **Leads the content** — the vault's most distinctive signal. Render the `contradictions` key as given (already `contradicts` first, then `extends`; do not re-filter `catalysts`): which hub (`hub`, concept or `thm-`), the cited note, the entry `text`. Say what held position it pushes against. |
| `theme_movements` | `theme_movements` grouped by theme, most-moved first — **skip rows with `shown_in_contradictions: true`** (they are theme-hub contradicts/extends catalysts already rendered above; a theme hub's catalyst renders once). A theme whose only rows are already shown gets no line. |
| `no_news` | Only lanes with state `dead`: *"<lane>: nothing since <since> and `<dead_jobs>` is stale/missing — the cron likely died"* (name the dead feeder(s), not the healthy ones). Fold `ran_nothing_kept` lanes into one sentence ("paper/article ran and kept nothing"). `unknown` lanes get nothing. |
| `papers` | `landings.paper`, ranked by overlap with `focus.concepts` (higher-ranked concept wins; ties by order landed). One line each: `[[id]] title — concepts`. |
| `understanding_shifted` | `essence_rewrites`: which hubs had their essence rewritten; read one with `weave_read` only if you need the new essence to say what shifted. |
| `focus` | Top 5 of `focus.concepts` with their `asked`/`done` counts and one verbatim probe where present (say it is the 14-day view); then the declared-vs-landed line for every `declared_only` row; then one clause *"asked once: X, Y"* from `focus.asked_below_floor` (mentioned, never ranked); then `active_projects`. |
| `acquisition_outlook` | Queue depths (`queues`, flag `backlog`), `strategies` configured, what the next drains will pull. |
| `attention` | `predictions_due` (judge them — `/judge-prediction`), `proposed_near_threshold` (one short of `dream.promotion_threshold` — name at most 5, then *"N near threshold (showing ≤20)"* from `proposed_near_threshold_total`, point at `/tighten`), `pressured_unanswered` (probes with pressure and no landing — name the probe text). |
| `connections` | ≤2 lines: `[[new_id]] ↔ [[old_id]] (score)` and why it matters. Skip the section when empty; never narrate `connections_reason` unless health is already flagged. |

## 3. Persist — the brief IS the next watermark

One `weave_create`, then one Bash call:

```
weave_create(type="digest", title="brief-<YYYY-MM-DD-HHMM>", body=<the rendered brief>,
             frontmatter={"kind": "brief", "since": <payload.since>, "served": <served_ids>})
weave brief mark --note <dig-id> --session "$CLAUDE_CODE_SESSION_ID" --served <served_ids…>
```

- The title **must** start with `brief-` (use `generated_at` in UTC) — the vault files it at `digests/brief-<stamp>.md`, and the `brief-` prefix keeps it out of `weave health`'s nightly-digest freshness check. `kind: brief` is what the next run's watermark query looks for. No new note type.
- `mark` logs every surfaced id as `context_served(source='brief')` — a retrieval event in the harness session's buffer (archived to `retrieval_log.jsonl` at Stop, re-projected on index) plus the immediate rows keyed by the session note. Always pass `--session "$CLAUDE_CODE_SESSION_ID"` (the Bash env's name; the CLI also falls back to `$CLAUDE_SESSION_ID`). If it prints *nothing logged*, the session note does not exist yet — that is fine, do not invent an id; the rows are skipped rather than fabricated.

## 4. Done — the brief is the output

Print the rendered brief once (step 2). Do not restate the `weave_create`/`mark` output; a one-line acknowledgement is fine only if something went wrong.
