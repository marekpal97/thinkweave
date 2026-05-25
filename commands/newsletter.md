---
name: newsletter
owns_mechanic: newsletter_inbox
source_type: newsletter-events, newsletter-concepts
capabilities: [acquire]
consumes: [mem_sources_config, mem_queue, mem_search, mem_concepts, mem_create, mem_link]
produces: [vault/.mem/queues/newsletter-events.jsonl, vault/.mem/queues/newsletter-concepts.jsonl, vault/sources/newsletter-events/**, vault/sources/newsletter-concepts/**]
tools:
  - Read
  - Bash
  - Task
  - ToolSearch
  - mem_search
  - mem_concepts
  - mem_create
  - mem_read
  - mem_update
  - mem_link
  - mem_queue
  - mem_sources_config
description: Drain email-newsletter labels into the vault. Mail-connector intake → enqueue → writer fan-out → label processed. Topic-agnostic over `newsletter-*` source types.
---

# /newsletter — Email-newsletter intake

End-to-end orchestrator for the `newsletter-*` family of source types. One run does:

1. Authenticate the configured mail connector (Gmail today).
2. For each registered `newsletter-*` source type, query its label and enqueue new messages.
3. Fan out `research-newsletter-worker` subagents in parallel; archive queue items per outcome.
4. Apply the `processed_label` on the mail server to every successfully-written message.

This skill mirrors `/news`'s shape (writer fan-out from a JSONL queue) but owns the mail-side concerns itself rather than threading them through `/drain` — Gmail OAuth lives here, `/drain` stays mail-agnostic.

**Arguments (all optional):**
- `<source-type>` — limit to one type, e.g. `/newsletter newsletter-events`. Default: all `newsletter-*` types from config.
- `--limit N` — cap items per type to fewer than `drain_batch_max`.

---

## Step 0 — Load config

```
mem_sources_config()
```

Discover the set to process: every key under `sources.` whose slug starts with `newsletter-`. If `<source-type>` was passed, filter to just that one. For each, pull:

| Key | Used for |
|---|---|
| `mail_connector` | Selects connector — gmail (today) / outlook / imap (future) |
| `senders` | **Canonical allowlist.** List of addresses / bare domains to fetch from |
| `mail_query` | Optional extra filter (Gmail: `is:unread`, etc.), ANDed onto the sender allowlist |
| `processed_label` | Applied at end of run; query excludes it |
| `lookback_days` | Bounds backfill — translated to provider syntax |
| `queue` | JSONL path |
| `subagent_type` | Should be `research-newsletter-worker` |
| `subagent_model` | `sonnet` |
| `drain_parallelism` | Max concurrent writers per type |
| `drain_batch_max` | Cap items per drain per type |
| `dedup_keys` | `[message_id, url]` — enforced by `mem_queue(action="enqueue")` |

If no `newsletter-*` types are configured, stop with `"No newsletter source types in sources.yaml — nothing to do."`

---

## Step 1 — Connect to the mail provider

Branch on `mail_connector`. For `gmail`:

The Gmail MCP tools are deferred — the auth tools (`authenticate`, `complete_authentication`) load at session start; after successful auth, the remaining thread-based tools (`search_threads`, `get_thread`, `label_thread`, `list_labels`, `create_label`) become available but their schemas aren't loaded until you ask for them.

```
ToolSearch(query="select:mcp__claude_ai_Gmail__authenticate,mcp__claude_ai_Gmail__complete_authentication", max_results=2)
```

Then call `mcp__claude_ai_Gmail__authenticate` and follow its instructions. The connector will either return "already authenticated" or walk you through OAuth via `mcp__claude_ai_Gmail__complete_authentication`. The user runs this skill interactively — that is expected; headless cron use needs the `imap` connector, not in scope here.

After authentication, discover the thread-based search/get/label tools. The claude.ai Gmail connector is **thread-oriented** (not message-oriented) — newsletters are typically single-message threads, so this is fine, just take the first message of each thread.

```
ToolSearch(query="select:mcp__claude_ai_Gmail__search_threads,mcp__claude_ai_Gmail__get_thread,mcp__claude_ai_Gmail__label_thread,mcp__claude_ai_Gmail__list_labels,mcp__claude_ai_Gmail__create_label", max_results=5)
```

The five tools you need:
- `search_threads(query, ...)` — list thread IDs matching the effective query
- `get_thread(thread_id)` — fetch the messages in one thread
- `label_thread(thread_id, label_ids)` — apply `processed_label` after a successful write
- `list_labels()` — to check whether `processed_label` already exists as a Gmail label
- `create_label(name)` — to create `processed_label` if it doesn't

If the names differ in your connector version, search by keyword and adapt. If you cannot find a thread-search tool at all, stop with:

> "Gmail MCP is connected but I can't find a thread-search tool. Confirm the Gmail connector is up to date and re-run."

**Ensure the `processed_label` exists** (one-time setup, idempotent):

```
labels = list_labels()
if processed_label not in {l.name for l in labels}:
    create_label(name=processed_label)
```

Remember its label ID — `label_thread` takes IDs, not names.

For `mail_connector: outlook` or `imap`: not implemented. Stop with `"Connector '<value>' not implemented yet — only gmail is wired."`

---

## Step 2 — Fetch + enqueue (per source type)

For each `newsletter-*` type:

**Build the effective query** from `senders` (canonical allowlist) + optional `mail_query` extras + the processed-label exclusion + the lookback window.

1. **Sender clause from `senders: [s1, s2, ...]`.** Each entry is either a full address (`alice@example.com`) or a bare domain (`example.com`). Gmail's `from:` operator accepts both. Compose:

   ```
   from_clause = "from:(" + " OR ".join(senders) + ")"
   ```

2. **Append the optional extras.** If `mail_query` is non-empty (e.g. `is:unread`), AND it onto the sender clause:

   ```
   base = from_clause + " " + mail_query   # if mail_query else just from_clause
   ```

3. **Append the processed-label exclusion and the lookback window:**

   ```
   effective_query = base + " -label:<processed_label> newer_than:<lookback_days>d"
   ```

**Guard — refuse to fan out across the whole inbox.** If `senders` is empty **and** `mail_query` is empty, halt this source type with:

> "Source type '<slug>' has no `senders:` allowlist and no `mail_query` — nothing to fetch. Add senders to vault/.mem/sources.yaml."

This is deliberate: a misconfigured newsletter type would otherwise return everything in the user's inbox. The sender allowlist is the canonical source of truth for "what counts as a newsletter we want"; it lives in config (auditable, under git) rather than in Gmail filter rules.

The Gmail-specific `newer_than:Nd` and `-label:…` tokens are the only provider-coupled bits. For Outlook/IMAP later, the `senders` list translates to that provider's native sender filter (IMAP `FROM`, Microsoft Graph `from/emailAddress/address`) — only this composition block changes.

**List candidate threads.**

```
search_threads(query=effective_query, max_results=drain_batch_max)
```

For each returned thread ID, fetch via `get_thread(thread_id)`. Newsletters are almost always single-message threads — take the first (or only) message. If a thread has multiple messages (rare — a reply chain), still process only the original (first) message; threads aren't the unit you want to brief.

**Enqueue each candidate** (one queue item per email):

```
mem_queue(
  action="enqueue",
  source_type="<this newsletter-* slug>",
  item={
    "message_id": "<RFC822 Message-ID header, or Gmail message id as fallback>",
    "thread_id": "<Gmail thread id — needed in step 4 for label_thread>",
    "url": "<canonical post link if the email contains one, else empty>",
    "title": "<subject>",
    "publication": "<sender display name, e.g. 'Stratechery'>",
    "from": "<sender email>",
    "published": "<Date header in ISO>",
    "embedded_body": "<full body — prefer text/plain, fall back to text/html→markdown>",
  }
)
```

`mem_queue(action="enqueue")` applies the configured `dedup_keys: [message_id, url]` against active + recently-archived items. Re-enqueues of the same message_id are rejected — the second of the three re-read guards.

Surface a per-type tally: `enqueued: K, dedup-rejected: D, listed: L`.

If `K == 0` for a type, skip step 3 for it.

---

## Step 3 — Fan out writer subagents (per source type)

For each type with new queue items:

```
mem_queue(action="peek", source_type="<slug>", n=<drain_batch_max>)
```

For each peeked item, spawn one Task subagent in batches of `drain_parallelism`:

```
Task({
  subagent_type: "research-newsletter-worker",
  model: "sonnet",
  description: "Write newsletter brief: <short title>",
  prompt: "<queue item dict, plus the spec's source_type and temporal_grain>\n\nProcess this queue item end-to-end per your spec. Return a single-line JSON outcome as the final non-empty line of your response."
})
```

The prompt must embed:
- The full queue item dict (id, message_id, url, title, publication, from, published, embedded_body).
- `source_type: <newsletter-events or newsletter-concepts>`.
- `temporal_grain: <event or concept>` — the worker branches on this for theme attachment.

Collect each worker's final JSON line. Recognised outcomes (see `.claude/agents/research-newsletter-worker.md`):

| Status | Meaning | Archive |
|---|---|---|
| `accepted` | New note written | `mem_queue(action="archive", item_id=..., status="done")` |
| `idempotent_skip` | Existing note matched `message_id` (3rd-layer guard fired) | `status="done"` — successful no-op |
| `fetch_failed` | Empty body or `mem_create:` error | `status="failed", reason="<from worker>"` |

The `idempotent_skip` arm is what makes this safe to re-run after a crash mid-batch: a worker that finds a note for its `message_id` from a previous run silently succeeds, the queue item is archived `done`, and the mail label gets applied in step 4 — no duplicate notes, no orphaned queue items.

---

## Step 4 — Apply `processed_label` on the mail server

Collect every queue item archived as `done` in step 3. When you enqueued each item in step 2, you stored its `thread_id` alongside `message_id` — those are what `label_thread` needs.

```
label_thread(thread_id=<from queue row>, label_ids=[<processed_label_id>])
```

`<processed_label_id>` is the ID you cached in step 1 when ensuring the label exists.

This is the **primary** re-read guard — the next `/newsletter` run's `effective_query` excludes the label, so the thread will not be fetched again.

If `label_thread` fails for an individual thread, log the thread_id and continue — the queue item is already archived `done`, the note is in the vault, and the mem_search guard in the worker will catch it on a future run if the label is missing.

`fetch_failed` items get **no** label applied — they remain unprocessed in Gmail and will be re-fetched (deliberately) on the next run.

---

## Step 5 — Report

```
Newsletter drain summary:
  newsletter-events:
    listed: L,  enqueued: K  (dedup-rejected: D)
    workers: <accepted> ⇒ <src-IDs, max 6 then …>
    idempotent_skip: I
    fetch_failed: F
    mail_labelled: M  (of K+I done items)
  newsletter-concepts:
    [same shape]

  Themes:
    candidate stubs floated: <count from events-grain auto-fire>
    (run `/themes-resolve` to review)
```

The candidate-stub count comes from `VaultManager.create_note`'s auto-fire — the worker doesn't need to invoke it explicitly. Stubs land at `vault/themes/_candidates/cand-XXXX-*.md`.

---

## Three-layer re-read guard recap

For your own debugging — if you ever wonder why a re-run skipped or wrote something:

1. **Mail label (primary)** — `processed_label` excluded from `effective_query` in step 2. Survives queue wipes.
2. **Queue dedup (secondary)** — `mem_queue(action="enqueue")` rejects any item whose `message_id` matches an active or recently-archived queue row.
3. **Worker mem_search (tertiary)** — `research-newsletter-worker` step 2 does `mem_search(message_id)` and short-circuits to `idempotent_skip` on a hit.

In normal operation guard 1 stops every re-read at the mail layer; 2 and 3 cover label-removal / queue-replay corner cases.

---

## When to use related skills

| Skill | Best for |
|---|---|
| `/newsletter` | Drain all `newsletter-*` queues from mail |
| `/newsletter newsletter-events` | Drain just the event-grain queue |
| `/drain --source-type paper\|repo\|article\|news` | Other source types (newsletter does NOT route through `/drain`) |
| `/themes-resolve --promote <cand-id>` | Promote a floated candidate stub into a canonical `thm-` theme |
| `/source-fit` | Diagnose whether a new newsletter shape fits the existing two types |

---

## What this skill does NOT do

- Auto-enqueue follow-up links from briefs into `/research` queues. The brief lists them in `## Follow-ups` for you to scan; bridging into `/research` is an explicit future enhancement, not core scope.
- Run a Haiku admission triage. Newsletter subscriptions are pre-curated by your label choice; the user already decided this publication is worth reading.
- Modify `/drain`. Drain stays mail-agnostic; this skill owns mail-connector concerns end-to-end.
- Support headless cron. The Gmail connector uses interactive OAuth; cron use needs the `imap` connector, which is not implemented in this version.
