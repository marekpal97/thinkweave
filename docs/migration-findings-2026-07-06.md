# Migration findings — personal_mem → thinkweave v0.1.1

Found while migrating a real ~3,500-note personal_mem vault onto thinkweave
v0.1.1 (Phase 0–2, 2026-06-22 through 2026-07-06). Each section below is
written to be liftable directly into its own GitHub issue. Ordered roughly
by severity: data-integrity bugs first, then synthesis-quality gaps, then
docs/DX gaps.

---

## 1. Theme mint doesn't write an explicit `slug:`, so `theme_registry.rebuild()` clobbers it

**Where:** `synthesis/theme_registry.py` (`rebuild()`), `operations/dream.py`
(`mint_theme_from_signal`)

**What happens:** `theme_registry.rebuild()` derives each entry's `slug`
from frontmatter — reads `slug:` if present, otherwise falls back to
`title:`. But `mint_theme_from_signal` never writes an explicit `slug:`
key onto the minted note, only `title:` (the human-readable display
string, which can differ arbitrarily from the file-derived slug — dream's
own worked example has `slug: "iran-war"` vs `title: "Iran–Israel
escalation"`). The initial `upsert()` call after minting correctly derives
and stores the slug from the filename. But any later call to `rebuild()`
re-derives every entry's slug from frontmatter, and since `slug:` was
never written, it falls back to `title:` — silently overwriting the
correct slug.

**Repro:** Mint a theme (e.g. via `mint_theme_from_signal`), confirm its
registry slug is correct. Call `theme_registry.rebuild()`. The slug is now
the raw `title:` string (spaces, capitals, punctuation intact) instead of
the URL-safe slug.

**Confirmed via:** minting `civil-aerospace`, then calling `rebuild()` —
its registry slug became `"Civil Aerospace"`.

**Impact:** Affects every real `/dream` mint, not just manual/scripted
seeding — any workflow that mints a theme and later calls `rebuild()`
(for any reason) will corrupt that theme's slug.

**Suggested fix:** Have `mint_theme_from_signal` write an explicit `slug:`
frontmatter field derived from the same logic used to name the file, so
`rebuild()` has an authoritative value to read instead of falling back to
`title:`.

---

## 2. Dream `apply()` can double-fold a theme pair that appears in both `theme_merges` and `theme_coarsenings`

**Where:** `operations/dream.py`, `apply()` (`theme_merges` applied
~line 2465, `theme_coarsenings` applied ~line 2664, both call
`merge_theme_into()`)

**What happens:** The phase-1 merge worker can legitimately propose the
same theme pair via *both* plan keys in a single dream-cycle outcome (one
as a pairwise `theme_merges` entry, one as part of an N-ary
`theme_coarsenings` cluster). `apply()` processes `theme_merges` first,
then `theme_coarsenings`, both calling the same `merge_theme_into()` — and
only rebuilds its in-memory/SQLite index once, at the very end, not
between these two steps. If both keys reference the same pair, the second
call re-runs `merge_theme_into()` on an already-tombstoned loser theme,
re-folding its catalyst log into the survivor a second time. No per-item
guard catches this because the stale in-memory index still shows both IDs
present as `type='theme'` at the time of the second call.

**Confirmed via:** a real dream cycle proposed folding `thm-a25c3782`
(auto-china-export-pressure) into `thm-5aeb0827` (china-export-pressure)
via both `theme_merges` and `theme_coarsenings` in the same outcome. Caught
and deduped by hand before calling `weave dream apply`; without that
manual step, the survivor's catalyst log would have gained duplicate
entries.

**Suggested fix:** Dedupe the merged pair set across `theme_merges` and
`theme_coarsenings` before applying either, or make `merge_theme_into`
itself a no-op (with a warning) when its "loser" side is already
tombstoned.

---

## 3. Embedding calls 400 on oversized notes instead of truncating

**Where:** the embedding path (`text-embedding-3-small` via
`weave index --embed`)

**What happens:** Roughly 40 notes out of ~3,500 (full-transcript session
notes) exceed the 8,191-token input limit and get rejected outright by
the embeddings API with a 400. Thinkweave doesn't truncate, chunk, or
otherwise pre-process oversized input before calling the embeddings
endpoint, so these notes simply never get an embedding and are invisible
to semantic/hybrid search and to dream's cosine-based drift/coarsen
detection.

**Suggested fix:** Truncate (or chunk-and-average, or summarize-then-embed)
oversized notes before the embedding call rather than letting the API
reject them. Should not require mutilating the source note — the fix
belongs in the embedding pipeline, not the vault content.

---

## 4. Pairwise drift-pair verdict memory doesn't cross-suppress the N-ary coarsen-cluster generator (or vice versa)

**Where:** `operations/dream.py` — `drift_pairs` (pairwise,
`geometry.build_concept_evidence`) vs `coarsen_clusters` (N-ary,
`geometry.build_concept_cluster_evidence`)

**What happens:** These are two independent generators over the same
concept-embedding space, each with its own verdict-memory key. A term
pair ruled "distinct" as part of an N-ary coarsen cluster in one dream
cycle can resurface as a fresh pairwise drift pair in the very next cycle,
because the pairwise generator's verdict memory has no record of the
judgment the coarsen-cluster generator already recorded for that same
pair (or vice versa).

**Confirmed via:** two consecutive real dream cycles — the term pair
`pca`/`specific-variance` was ruled "distinct" inside a coarsen cluster in
cycle 1, then resurfaced as an independent pairwise `drift_pair` in cycle
2, forcing a duplicate human review of a relationship already judged one
cycle earlier.

**Suggested fix:** Share verdict-memory state between the two generators
for the same term pair — at minimum, check the other generator's recent
verdict for a pair before resurfacing it.

---

## 5. `weave concepts drift`'s near-duplicate check is pure edit-distance / substring, no semantic gate

**Where:** `surfaces/cli` — `weave concepts drift`

**What happens:** The CLI's advisory near-dupe detector flags pairs purely
by string similarity (substring containment or edit distance), with no
semantic check. This produces confident-looking but nonsensical
suggestions.

**Confirmed via:** `weave concepts drift` on a real vault flagged
`biotech` ≈ `fintech` (edit distance 2) as a merge candidate — these are
semantically unrelated. If acted on literally (`weave concepts merge
biotech fintech`), this would silently corrupt the concept vocabulary.

**Note:** dream's own concept-drift detector (`operations/dream.py`,
`drift_pairs`) already does this correctly via cosine similarity on
embeddings and does not have this problem — the CLI advisory command
should reuse that mechanism instead of (or in addition to) string
distance.

**Suggested fix:** Gate `weave concepts drift` suggestions on embedding
cosine similarity, or at least flag string-only matches as lower
confidence than embedding-corroborated ones.

---

## 6. `commands/update-hubs.md` describes `--bulk batch` as the (now-removed) OpenAI Batches API path

**Where:** `commands/update-hubs.md`

**What happens:** The skill doc says:

> `--bulk batch` — runs `weave drain --target hubs --via batch` (OpenAI
> Batches API + gpt-5-mini, 50% discount, async, no interactive review).

But `operations/hubs_batch.py`'s own module docstring says:

> The OpenAI Batches submission / polling / fetching dance was deleted
> 2026-06-06 (plan: `go-back-to-the-scalable-firefly.md` step C2). The
> orchestrator now delegates execution to
> `thinkweave.core.agent_client.batch_completions_sync`, which fires N
> async completions in parallel under a semaphore-capped concurrency
> budget. ~50% per-token discount forfeited for one code path.

So `--via batch` is actually a **synchronous, in-process concurrent
fan-out at standard (non-discounted) pricing**, not an async queued job
with a discount. This is a meaningful behavioral and cost difference that
the user-facing skill doc doesn't reflect — it directly misled us
mid-migration into expecting an hours-long async job with a 50% discount,
when in fact it's a blocking call at full price.

**Suggested fix:** Update `commands/update-hubs.md`'s `--bulk batch`
description to match current behavior (synchronous in-process fan-out,
standard pricing, `max_input_tokens` cap requiring possible multiple
invocations to drain a large plan).

---

## 7. Missing `openai` SDK fails an entire batch silently, with no diagnostic detail

**Where:** `core/agent_client.py` (`_resolve_client`), `operations/hubs_batch.py`

**What happens:** A freshly `weave dev-link`'d environment set up with
`uv sync --extra mcp` (the extra actually needed for the Claude Code MCP
surface) has **no working completions path at all** — `agent_client.py`'s
OpenAI client resolution does a bare `from openai import AsyncOpenAI`,
and the `openai` package lives behind the separate `hubs` / `embeddings`
extras in `pyproject.toml`, not `mcp`. Nothing checks or warns about this
at setup time. Worse: `operations/hubs_batch.py` calls
`batch_completions_sync(..., return_exceptions=True)` and only counts
failures — it never surfaces *what* the exception was, not even the
exception type. A full batch of 3,846 requests failed 100% with the only
output being:

```
warning: 3846 request(s) failed; rerun to retry the rest
Applied 0 new log entries.
```

We had to reproduce a single call by hand outside the batch path to
discover the actual `ModuleNotFoundError: No module named 'openai'`.
Note that embeddings had been working fine throughout the same session
despite the same missing-extras gap — that path apparently goes through a
separately-installed `httpx`, not the `openai` package — which is exactly
why this wasn't caught earlier.

**Suggested fix:**
- Since the code's own comments call `openai` "the single completion SDK"
  post-consolidation, consider promoting it out of the optional extras
  into a core dependency, or add a `weave doctor` check that fails loudly
  if the configured provider's SDK isn't importable.
- `hubs_batch.py` should log/print at least the exception type + message
  for the first N failures instead of only a bare count.

---

## 8. `gpt-5-mini`'s reasoning-token overhead can silently zero out an extraction

**Where:** `operations/hubs_batch.py` (`run_hubs_batch`, default
`max_tokens=1024`)

**What happens:** `gpt-5-mini` spends an opaque portion of the
`max_tokens` budget on hidden reasoning before emitting any visible
output. A trivial test prompt ("say hello in 3 words") burned 268 of a
500-token budget on reasoning; with `max_tokens=50` it returned fully
empty text. `hubs_batch.py`'s default of 1024 tokens was sufficient on the
one real extraction request we tested (558 tokens used — comfortable but
not huge margin), but a response that comes back empty is treated
identically to "this note taught nothing new" (0 entries appended, no
error, no warning) — a silent-failure mode structurally indistinguishable
from a legitimately uninteresting note.

**Suggested fix:** Warn (or retry with a higher token budget) when
`completion_tokens` returned is close to the requested `max_tokens`
ceiling but the parsed text is empty — that combination is a strong
signal of a truncated-by-reasoning response, not a genuinely empty
extraction.

---

## Context

Found across Phase 0–2 of migrating a real, actively-used ~3,500-note
personal_mem vault onto thinkweave v0.1.1 as a forward-compatible,
in-place migration (markdown source-of-truth, SQLite index rebuilt from
scratch, no ETL). Reported together as one findings doc; happy to split
into individual issues if that's more useful for triage.
