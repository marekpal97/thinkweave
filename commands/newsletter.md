---
name: newsletter
owns_mechanic: newsletter_inbox
source_type: newsletter-events, newsletter-concepts
capabilities: [acquire]
consumes: [weave_sources_config, weave_queue, weave_search, weave_concepts, weave_create, weave_link]
produces: [vault/.weave/queues/newsletter-events.jsonl, vault/.weave/queues/newsletter-concepts.jsonl, vault/sources/newsletter-events/**, vault/sources/newsletter-concepts/**]
tools:
  - Read
  - Bash
  - Task
  - ToolSearch
  - weave_search
  - weave_concepts
  - weave_create
  - weave_read
  - weave_update
  - weave_link
  - weave_queue
  - weave_sources_config
description: Orchestrator over the email-newsletter intake rails. Probes the Gmail connector, reads the per-type `mail_poll` discover-strategy plan (effective_query + processed_label), fetches threads via Gmail MCP, enqueues, drains, then applies the processed_label. Headless-safe.
---

# /newsletter — Email-newsletter intake (orchestrator)

`/newsletter` is the orchestrator that wires together three things the framework provides on different rails:

1. **Plan** — `weave discover --strategy mail_poll --source-type <slug>` returns the per-type effective Gmail query, `processed_label`, `dedup_keys`. The strategy *composes the query and validates the allowlist*; this skill *executes* it through Gmail MCP. Query composition (sender allowlist → `from:(...)`, lookback → `newer_than:Nd`, label exclusion) lives in `discover/strategies/mail_poll.py`, not here.
2. **Drain** — `/drain --source-type newsletter-*` consumes the queue and fans out `research-newsletter-worker` Sonnet subagents.
3. **Label** — apply `processed_label` server-side on every thread whose write succeeded. This is the skill's only post-drain concern (and the primary re-read guard for the next run).

Gmail access lives in this skill because the connector only exists inside the Claude Code MCP runtime — `weave discover` is pure Python and can't reach it. The OAuth grant is one-time and cached account-side; it survives headless `claude -p` firings, so **this skill is cron-safe** (see [Cron contract](#cron-contract)). Only the first-ever grant on a fresh account needs a browser.

**Arguments (all optional):**
- `<source-type>` — limit to one type, e.g. `/newsletter newsletter-events`. Default: all `newsletter-*` types from config.
- `--limit N` — forwarded to `/drain`.

---

## Step 0 — Discover the source-type set

```
weave_sources_config()
```

Pick every key under `sources.` whose slug starts with `newsletter-`. If `<source-type>` was passed, filter to one. If no `newsletter-*` types are configured, stop with `"No newsletter source types in sources.yaml — nothing to do."`.

---

## Step 1 — Reach Gmail (probe, never prompt)

For `mail_provider: outlook` or `imap` (formerly `mail_connector:`; both names accepted): not implemented in v1. Stop with `"Provider '<value>' not implemented yet — only gmail is wired."` before probing anything.

The Gmail MCP tools are deferred. Load the ones this skill uses:

```
ToolSearch(query="select:mcp__claude_ai_Gmail__search_threads,mcp__claude_ai_Gmail__get_thread,mcp__claude_ai_Gmail__label_thread,mcp__claude_ai_Gmail__list_labels,mcp__claude_ai_Gmail__create_label", max_results=5)
```

Then probe the connector with the cheapest read it offers:

```
mcp__claude_ai_Gmail__list_labels()
```

A successful call is the whole auth check — the cached grant is live, and you already hold the label list step 2 needs. **Do not call `authenticate`.** It is an interactive-consent tool; under cron there is no one to consent, and calling it turns a clean failure into a hang.

**On any failure** — the tools don't load, `list_labels` errors, the grant has lapsed — emit exactly one line and stop:

```
newsletter: Gmail MCP unavailable — <reason>
```

`<reason>` is the connector's own error text (or `tool not loadable` when ToolSearch returns nothing). Then exit **without touching queues or mail labels**: no `weave_queue` calls, no `/drain`, no `label_thread`. A run that can't read mail has nothing to record, and a half-run that labels threads it never briefed would silently lose them. Failing loudly on one line is deliberate — under cron this line is the only evidence anyone will see, and the dream-cron outage (dead three weeks, unnoticed) is why it must not be swallowed.

**First-ever grant (interactive only).** On a fresh account with no grant, `list_labels` fails and the run stops — that is correct. To establish the grant, run `/newsletter` once from an interactive session, where you may load `mcp__claude_ai_Gmail__authenticate` / `mcp__claude_ai_Gmail__complete_authentication` and walk the OAuth consent. Once granted, the token is cached account-side and every later run — interactive or cron — takes the probe path above.

If thread-search isn't discoverable but the probe succeeded, stop with `"Gmail MCP is connected but I can't find a thread-search tool. Confirm the Gmail connector is up to date and re-run."`.

---

## Step 2 — Ask the discover strategy for the plan

For each `newsletter-*` type:

```bash
weave discover --strategy mail_poll --source-type <slug>
```

The strategy returns one descriptor:

```json
{
  "strategy": "mail_poll",
  "kind": "mail_fetch_needed",
  "source_type": "<slug>",
  "connector": "gmail",
  "effective_query": "from:(s1 OR s2) is:unread -label:weave-processed newer_than:30d",
  "processed_label": "weave-processed",
  "lookback_days": 30,
  "dedup_keys": ["message_id", "url"],
  "senders": [...],
  "mail_query_extras": "is:unread"
}
```

Or, if the allowlist is empty:

```json
{"strategy": "mail_poll", "kind": "external", "status": "error",
 "source_type": "<slug>", "reason": "empty_allowlist", "hint": "..."}
```

Halt this source type on error; the hint goes verbatim to the user.

**Ensure the `processed_label` exists** (one-time setup, idempotent) — reuse the label list step 1's probe already returned:

```
if processed_label not in {l.name for l in labels}:
    create_label(name=processed_label)
```

Remember its label ID — `label_thread` takes IDs, not names.

---

## Step 3 — Fetch + enqueue (per source type)

Use the plan's `effective_query`:

```
search_threads(query=<effective_query>, max_results=<drain_batch_max>)
```

For each returned thread ID, `get_thread(thread_id)`. Newsletters are almost always single-message threads — take the first (or only) message. Multi-message threads: still process only the original (first).

**Enqueue each candidate:**

```
weave_queue(
  action="enqueue",
  source_type="<this newsletter-* slug>",
  item={
    "message_id": "<RFC822 Message-ID header, or Gmail message id as fallback>",
    "thread_id": "<Gmail thread id — needed in step 5 for label_thread>",
    "url": "<canonical post link if the email contains one, else empty>",
    "title": "<subject>",
    "publication": "<sender display name>",
    "from": "<sender email>",
    "published": "<Date header in ISO>",
    "embedded_body": "<full body — prefer text/plain, fall back to text/html→markdown>",
  }
)
```

`weave_queue(action="enqueue")` applies `dedup_keys` (from the plan) against active + recently-archived items. Re-enqueues of the same `message_id` are rejected — second of the three re-read guards (the first is the mail label, applied in step 5).

Surface a per-type tally: `enqueued: K, dedup-rejected: D, listed: L`.

---

## Step 4 — Drain via /drain

For each type with new queue items:

```
Skill(skill="drain", args="--source-type <slug> [--limit N]")
```

Under the plugin install, skills resolve namespaced — if `Skill(skill="drain")` fails with an unknown skill, retry as `thinkweave:drain`.

`/drain` runs Path B (writer-only, no triage) for newsletter types — fans out `research-newsletter-worker` subagents at `drain_parallelism`, validates allowed-failure prefixes, archives outcomes. Capture which queue items got archived `done` (you need their `thread_id`s for step 5).

---

## Step 5 — Apply `processed_label` on the mail server

Collect every queue item archived as `done` in step 4. The `thread_id` stored in each at step 3 is what `label_thread` needs.

```
label_thread(thread_id=<from queue row>, label_ids=[<processed_label_id>])
```

This is the **primary** re-read guard — the next `/newsletter` run's `effective_query` excludes the label, so the thread won't be fetched again.

If `label_thread` fails for an individual thread, log the thread_id and continue — the queue item is already archived `done`, the note is in the vault, and the worker's `weave_search` guard will catch it on a future run if the label is missing.

`fetch_failed` items get **no** label applied — they remain unprocessed in Gmail and will be re-fetched (deliberately) on the next run.

---

## Step 6 — Report

```
Newsletter intake summary:
  newsletter-events:
    plan:    <effective_query>
    fetch:   listed: L,  enqueued: K  (dedup-rejected: D)
    drain:   <accepted> ⇒ <src-IDs, max 6 then …>
             idempotent_skip: I, fetch_failed: F
    label:   <M> threads marked '<processed_label>'
  newsletter-concepts:
    [same shape]

  Themes:
    (signals surface on next `/dream` scan; no per-drain count)
```

Write this summary to stdout unconditionally, including when every tally is zero. Under cron it is the run's only trace — a silent success and a dead rail look identical in an empty log.

---

## Cron contract

`/newsletter` is unattended-safe and is a registry job (`newsletter` in `vault/config/scheduling.yaml`, daily 06:30, `serialize: true`, log `newsletter.log`). Install it with `weave schedule install --only newsletter`; it ships `enabled: false` because it needs both a `newsletter-*` sender allowlist and a Gmail grant before it can do anything.

What "unattended-safe" obliges:

- **Never prompt.** No `AskUserQuestion`, no interactive auth tool, no waiting on a decision. Every branch in this skill either proceeds or stops with a printed line.
- **Fail loudly, on one line.** The step-1 diagnostic is the contract with whoever reads `newsletter.log` next month.
- **Leave no half-state.** Stop before the first `weave_queue` write, or finish the rail. Labels go on only after a `done` archive (step 5).
- **Always print the step-6 summary** so the log distinguishes "ran, nothing new" from "never fired".

A backlogged first run (weeks of unread mail) is normal and can outrun the cadence — hence the `flock` guard. The mail label plus queue dedup make an overlap harmless anyway; the lock is for log legibility.

---

## Three-layer re-read guard recap

1. **Mail label (primary)** — `processed_label` excluded from `effective_query` in step 3 (planner) / step 3 (executor). Survives queue wipes.
2. **Queue dedup (secondary)** — `weave_queue(action="enqueue")` rejects on `dedup_keys` (`message_id`, `url`).
3. **Worker weave_search (tertiary)** — `research-newsletter-worker` `weave_search(message_id)` short-circuits to `idempotent_skip` on a hit.

In normal operation guard 1 stops every re-read at the mail layer; 2 and 3 cover label-removal / queue-replay corner cases.

---

## When to use related skills

| Skill | Best for |
|---|---|
| `/newsletter` | Plan + fetch + drain + label all `newsletter-*` queues in one shot |
| `/newsletter newsletter-events` | Same, limited to one source type |
| `weave discover --strategy mail_poll --source-type newsletter-events` | Inspect the effective Gmail query for one type (read-only) |
| `/drain --source-type newsletter-events` | Drain only (when the queue was already filled, e.g. after a crash mid-run) || `/source-fit` | Diagnose whether a new newsletter shape fits the existing two types |

---

## What this skill does NOT do

- **Compose Gmail queries.** That lives in `mail_poll` discover strategy — testable in pure Python, no MCP context needed.
- **Parse RSS.** That's the `rss_poll` strategy's job; newsletters arrive by mail, not feed.
- **Spawn writer subagents.** That lives in `/drain` Path B.
- **Auto-enqueue follow-up links from briefs into `/research` queues.** The brief lists them in `## Follow-ups` for you to scan; bridging into `/research` is an explicit future enhancement.
- **Run a Haiku admission triage.** Newsletter subscriptions are pre-curated by your sender allowlist; the user already decided this publication is worth reading.
- **Establish the first Gmail grant.** That one step is interactive by nature (OAuth consent) and belongs to a human at a terminal. Every run after it — including cron — takes the step-1 probe path.
