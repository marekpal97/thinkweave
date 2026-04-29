---
name: discover
source_type: [paper, repo, article]
capabilities: [discover]
tools:
  - Read
  - WebSearch
  - mem_concepts
  - mem_concept_search
  - mem_concept_source_counts
  - mem_search
  - mem_read
  - mem_timeline
  - mem_create
description: Cross-project research gap analysis. Reads RESEARCH_FOCUS.md, finds under-covered concepts, and queues new leads as `todo+research` notes.
---

# /discover — Research Discovery & Gap Analysis

You are running a discovery pass across the knowledge vault to find new research leads. This skill analyzes what's in the vault, identifies gaps, and searches for papers/repos/articles to fill them. Results are added as queue items (`todo`+`research` tagged notes) for later processing by `/research --queue`.

This skill is designed to run periodically (e.g. `/loop 6h /discover`) or on demand.

## Steps

### 1. Load Research Focus and the Ontology

Three calls, all cheap — run them before any gap analysis.

**1a. Fetch the focus file through the index** — don't hardcode a filesystem path:
```
mem_read(id="n-research00")
```

This returns the `Research Focus` note, which contains:
- **Active Focus Areas** — the 4 numbered focus areas that define on-scope. Each has an `_Ontology domains:_` line listing the domains that fall under it, and some have focus-area-specific inclusion/exclusion language (e.g. focus area 2 explicitly excludes LLM-as-a-judge memory architectures, focus area 4 notes that the quant-finance domains are out of scope).
- **Concept Gaps** — populated by step 6 of previous runs. Has four sub-sections: most-active projects, load-bearing concepts by focus area, queued this run, still open.
- **Excluded** — global exclusion list (topics to skip entirely regardless of focus area match).

If `n-research00` is missing, report it and ask the user to seed one. Don't proceed without focus areas — undirected search is wasteful.

**1b. Load the concept ontology** — this is what lets step 3 deterministically map concepts to focus areas via their domains:
```
Read src/personal_mem/ontology.yaml
```
Use a repo-relative path — the repo lives under the project directory `/discover` is invoked from, not a user-specific absolute path.

**1c. Pull the current concept histogram** — used alongside the ontology to distinguish "concepts we already use" from "vocabulary targets the ontology defines but the vault hasn't adopted yet":
```
mem_concepts(min_count=2)
```

**1d. Build the focus_area → domains map in working memory.** Parse each of the 4 focus areas from step 1a and extract its `_Ontology domains:_` line:
```
focus_area_map = {
  1: {"domains": ["ai/harness", "ai/agents", "ai/tools"], "exclusions": []},
  2: {"domains": ["ai/memory", "ml/embeddings", "thinkmesh"],
      "exclusions": ["LLM-as-a-judge memory architectures"]},
  3: {"domains": ["ml/novelty", "thinkmesh"], "exclusions": []},
  4: {"domains": ["finance/research", "ai/agents"],
      "exclusions": ["finance/quant", "finance/options", "finance/markets"]},
}
```

Keep this in working memory through step 6. When step 3 needs a concept → focus-area lookup, iterate the map inline — it's 4 entries, fast enough.

### 2. Analyze Vault State (Cross-Project)

Two orientation queries only. These feed the gap-identification reasoning in step 3 — they are **not** used for dedup. Per-hit dedup happens per-gap in step 3 against concept-scoped lookups that see the entire vault regardless of its size. Do not bulk-load "recent sources" or "recent queue items" here — that approach silently misses anything outside the recency window once the vault has more than a few dozen sources.

**Concept coverage** — which concepts have source notes vs. only session/note mentions:
```
mem_concepts(prefix="", min_count=2)
```

**Recent activity** — cross-project ranking in one call, no project arg:
```
mem_timeline(days=14)
```
Returns a ranked list `{project, sessions, decisions, latest_date}`. Sessions lacking a `project` frontmatter land in the `_unscoped` bucket — treat it like any other project for ranking. Use the returned rows directly as the active-project set in step 3.1.

### 3. Identify Gaps

A gap is the **intersection of two signals** — neither alone qualifies:
- **Top-down** — maps to an Active Focus Area from step 1. Out-of-scope concepts are noise even if they're active in project work.
- **Bottom-up** — load-bearing in actual project work *right now* (not vault-wide totals — recency matters).

#### Build the bottom-up signal

1. **Rank active projects** — reuse the `mem_timeline(days=14)` result from step 2. Take the top 3-5 by combined session + decision count. Projects with zero activity in the window don't contribute to gap analysis. The `_unscoped` bucket counts as a project for this step — if it's load-bearing it reflects real work that happens to lack project frontmatter.

2. **Pull load-bearing concepts per active project**:
   ```
   mem_concept_search(project="<name>", project_concepts=true)
   ```
   Merge into a cross-project histogram. **Use mention counts from the active window only, not vault-wide counts** — a concept that was hot six months ago and is dormant now is not load-bearing today. (Note: `mem_concept_search` with `project_concepts=true` returns project-wide frequencies, not window-filtered. For the active window refinement, cross-reference with recent sessions/decisions from step 3.3.)

3. **Direction of travel** — for each active project pull recent decisions and session summaries:
   ```
   mem_search(query="", type="decision", project="<name>", limit=10)
   mem_search(query="", type="session", project="<name>", limit=5)
   ```
   Note concepts that repeat across these. A concept appearing in 3+ recent decisions is **trending** — where the project is actively moving.

#### Intersect with top-down focus (ontology-backed)

4. For each load-bearing concept from step 2, use the ontology + the `domain_to_focus_areas` map from step 1d to resolve which focus areas it belongs to. The mapping is **deterministic**, not vibes-based:

   - Look up the concept in `ontology.yaml` — which domain(s) is it listed under? A single concept may be cross-listed in multiple domains (e.g. `memory-system` lives in both `ai/tools` and `ai/memory`); take the set.
   - For each of those domains, consult `domain_to_focus_areas` to get the set of focus areas the concept is on-focus for. Union across all the concept's domains.
   - If the concept isn't in the ontology at all, it's a **vault-vocabulary concept not yet anchored** — see step 4c below for how to handle it.
   - If the concept is in the ontology but none of its domains map to any focus area, it goes to the **cross-focus activity** bucket — see step 4d.

5. **A concept is a gap** when all three hold:
   - **Top-down match**: resolves to at least one focus area via step 4.
   - **Active-work match**: ≥3 active-window mentions from step 2, OR trending in recent decisions from step 3.
   - **Under-sourced**: source count < 2 per the bulk lookup below.

   **Bulk under-source lookup + dedup set collection, single call:**
   ```
   mem_concept_source_counts(concepts=[<all candidate concepts>])
   ```
   Pass every candidate concept from step 4 in one call. Returns `{concept: {count, sources: [{id, title, url}]}}`. Use:
   - `count < 2` → the concept passes the under-sourced threshold.
   - `sources[*].url` → populate `gap_sources[<concept>]`, the per-gap dedup reference used in step 4. Complete prior-art set for that concept, not a recency window.

   Then for each concept that passed, pull concept-scoped queue items to populate `gap_queue[<concept>]`:
   ```
   mem_concept_search(concepts=["<X>"], type="note")
   ```
   Filter the results in-memory to notes whose `tags` contain both `todo` and `research`. Typical combined size is 0-5 URLs per gap — small, relevant, and guaranteed-complete for that concept. (This stays as one call per surviving gap, not per candidate — it only runs after the bulk under-source check has winnowed the list.)

   When the concept maps to multiple focus areas (via cross-listing), assign it to the **highest-priority focus area** in `n-research00` order. Record the other matches as secondary in the run audit note (step 5b).

6. **Focus-area-specific exclusions**: after identifying gap candidates, apply each focus area's exclusion language from step 1a:
   - Focus area 2: drop any concept whose associated literature is primarily LLM-as-a-judge memory architectures. This is a *content* filter, applied at WebSearch time in step 4 — exclude hits whose titles/abstracts describe LLM-as-judge memory approaches even if they otherwise match `ai/memory` terms.
   - Focus area 4: drop concepts whose ontology domains are in the excluded list (`finance/quant`, `finance/options`, `finance/markets`). A concept cross-listed into both `finance/research` AND `finance/quant` still passes because one of its domains is on-scope, but a concept that ONLY lives in `finance/quant` is dropped.
   - Any other focus-area-specific exclusions from future focus areas.

7. **Lenient emerging signals** (from step 4): any load-bearing concept that doesn't map to a focus area goes in this bucket. Two sub-cases, both surfaced in the run audit note under a single "Emerging signals" section with a one-line type annotation per entry:
   - Concept is in the ontology but its domains don't intersect any focus area → annotate `off-focus`
   - Concept isn't in the ontology at all → annotate `off-ontology`

   **Do not queue items for either case** — they aren't on-focus. This section is your early-warning for emerging focus areas: if the same signal shows up in 3+ consecutive discover runs with growing counts, that's a cue to either update RESEARCH_FOCUS.md (for `off-focus`) or add the term to `ontology.yaml` (for `off-ontology`).

8. **Citation gaps** — scan the 10 most recent source notes (`mem_search(query="", type="source", limit=10)`) for paper titles or arxiv IDs not yet in the vault. Verify absence with `mem_search("<title>")`. Queue these only if they resolve to a focus area via step 4 (i.e. their likely ontology concepts map to a focus area).

9. **Recency gaps** — for each focus area where the newest mapped source is >3 months old, the field may have moved on. Queue a "recent developments <area>" search.

#### Rank and cap

Order gaps by, in this priority:
1. **Focus area priority** — the order of the 4 areas as listed in `n-research00` (agentic harnesses > principled memory > novelty detection > finance research).
2. **Load-bearing weight** — sum of active-window mentions across projects. Heavier = more urgent.
3. **Source deficit** — 0 sources before 1 source before 2 sources.

**Cap at 3-5 gaps per run.** /discover produces a small focused queue, not a sprawl — the matching /research cadence is `/loop 30m /research --queue --batch 3`, so 3-5 discovered items per run is the right feed rate.

### 4. Search for Content

For the top 5-8 gaps, run targeted searches:

```
WebSearch("<gap-specific query>")
```

**Query construction** — be specific, not generic:
- For concept gaps: `"<concept> survey 2025 arxiv"` or `"<concept> tutorial github"`
- For citation gaps: search the exact paper title or arxiv ID
- For recency gaps: `"<topic> new developments 2025 2026"`

**Filtering** — per-gap URL/ID match (primary) + vault-wide title match (backstop):

- **URL/ID match — source hit**: Skip results whose URL, arxiv ID, or repo URL appears in `gap_sources[<current-concept>]` from step 3. Log the skip reason as `already ingested`. This set is concept-scoped and complete — it sees every prior source for this concept regardless of when it was ingested.
- **URL/ID match — queue hit**: Skip results whose URL, arxiv ID, or repo URL appears in `gap_queue[<current-concept>]` from step 3. Log as `already queued`.
- **Title match (backstop)**: For each result that survives both URL checks, run two cheap cross-concept lookups:
  - `mem_search(query="<paper or article title>", type="source", limit=1)` — catches material already ingested but tagged under a *different* concept than the current gap. Log as `already ingested (cross-concept)`.
  - `mem_search(query="<paper or article title>", tags=["todo", "research"], type="note", limit=1)` — catches a queue item added by an earlier `/discover` run under a different concept that hasn't been ingested yet. Without this the same URL can sit in the queue twice under two concepts, and `/research` will waste a batch slot re-ingesting it. Log as `already queued (cross-concept)`.

  These are the only filters that query the entire corpus; do them last so they only run on the handful of results that already passed the cheap concept-scoped checks.
- **Exclusion patterns**: Skip results matching exclusion patterns from RESEARCH_FOCUS.md, plus any focus-area-specific exclusion language from step 1a (e.g. LLM-as-a-judge hits under focus area 2).
- **Quality preference**: Prefer arxiv papers, well-known blog posts, and active GitHub repos over random pages.

Track the skip reason per filtered result. These roll up into the aggregate skipped counts in step 5b and make dedup behavior auditable across runs.

### 5. Create Queue Items

**Runtime order**: step 5b (create run audit note) runs *before* step 5. The run note ID returned from 5b is used as the `<run-note-id>` back-reference in every queue item created below. This is the only cross-step dependency — the rest of step 5 is independent.

---

For each promising find from step 4, create a queue note:

```
mem_create(
  note_type="note",
  title="<descriptive title — what you'd learn from this>",
  body="""<url>

Discovered by /discover on <today's date> — see [[<run-note-id>]] for the full run log.

Gap: [[<ontology-concept-name>]] (focus area <N>)
Relevance: <one sentence on why this matters, grounded in the focus area description>
""",
  tags=["todo", "research"],
  concepts=["<ontology-term-1>", "<ontology-term-2>", ...]
)
```

**Three rules for concept assignment** — these are what make `/discover`'s output ontology-consistent with `/research`'s source notes:

1. **Use ontology terms only.** Every entry in the `concepts` array must exist in `ontology.yaml`. If the gap concept itself is an ontology term (the common case), include it. Pull additional concepts from the same ontology domain(s) the gap maps to — minimum 2, ideally 3-4, matching `/research`'s standard.
2. **If a queue item genuinely requires a concept that isn't in the ontology yet**, put it in `proposed_concepts` frontmatter field instead of `concepts`. Mirror `/research`'s pattern:
   ```
   mem_create(
     ...,
     concepts=["<ontology-terms-only>"],
     frontmatter={"proposed_concepts": ["<new-candidate>"]}
   )
   ```
   These proposed concepts get picked up by `/mem-resolve-concepts` for formal canonicalization. Don't invent proposed concepts freely — if the gap itself is in the ontology, you rarely need new ones.
3. **The `Gap:` line in the body must be a wikilink to an ontology concept.** That's what turns the queue item into a graph-linked artifact: the wikilink creates an edge to the concept hub page in Obsidian and a wikilink edge in the SQLite index. Write it exactly as `Gap: [[<concept-name>]] (focus area <N>)`.

**Quality over quantity** — create 3-5 queue items per run (aligned with step 3's gap cap), not 30. Each should clearly fill an identified gap. If you can't find good material for a gap, note it in the run audit note (step 5b) under a "Gaps without hits" sub-section rather than queuing low-quality leads.

### 5b. Create the Run Audit Note

Per-run point-in-time audit trail, created BEFORE step 5 so step 5's queue items can reference it. From a source note six months from now you can walk back through the queue item's `[[<run-id>]]` wikilink to this audit note and see exactly why the material was queued — what the focus areas pointed at, which gap it filled, what search query found it, what got filtered out.

**Concepts**: the gap concepts identified this run (ontology terms), 3-5 entries matching the gap cap. Not meta-labels like `discover-run` or `audit-trail` — those are tags. Concepts drive the knowledge-graph edges, so they must be the substantive domain vocabulary. If a run identifies zero gaps, `concepts` is an empty list and the run note becomes a concept-island — intentional, makes empty-gap runs visible in graph walks.

**Single creation call** — no `mem_update` follow-up, the note is complete at create time:
```
mem_create(
  note_type="note",
  title="discover run <ISO-8601 UTC timestamp, e.g. 2026-04-12T13:45Z>",
  body="<see template below>",
  tags=["discover-run", "audit"],
  concepts=["<gap-concept-1>", "<gap-concept-2>", ...]
)
```

The "Queue items created" data *isn't* in the run note body — it's recoverable via the back-references in the queue items themselves (`mem_search(query="<run-id>", type="note")` or a graph walk via wikilink edges). The body only needs to capture what won't exist elsewhere: the *gap-identification* reasoning.

**Project**: do not pass `project=` at all. Discover runs are cross-project observations; they land in the default standalone location (`sessions/misc/` under whatever project context `/discover` is invoked from). Administrative, not semantic.

#### Body template

```markdown
# Discover Run — <ISO timestamp>

## Focus areas at run time
Snapshot of [[n-research00]] at <ISO date>:
1. Agentic coding harnesses & advanced Claude Code techniques → `ai/harness`, `ai/agents`, `ai/tools`
2. Memory & retrieval frameworks — principled only → `ai/memory`, `ml/embeddings`, `thinkmesh` (exclude: LLM-as-a-judge)
3. Novelty detection for Thinkmesh → `ml/novelty`, `thinkmesh`
4. Deep research frameworks for finance → `finance/research`, `ai/agents`

## Bottom-up signal

### Most-active projects (last 14 days)
- `personal_mem` — 12 sessions, 5 decisions
- `thinkmesh` — 4 sessions, 2 decisions
- `research_assistant` — 2 sessions, 1 decision

### Load-bearing concept histogram (active-window counts only)
Merged across the active projects above:
- `memory-system`: 14 mentions
- `knowledge-graph`: 11 mentions
- `claude-code`: 9 mentions
- `agent-harness`: 5 mentions
...

### Direction of travel
Trending concepts (appearing in 3+ recent decisions or session summaries):
- `memory-system` — [[dec-xxxx1]], [[dec-xxxx2]], [[dec-xxxx3]], [[ses-yyyy1]]
- `knowledge-graph` — [[dec-xxxx4]], [[ses-yyyy1]], [[ses-yyyy2]]

## Gaps identified

Ranked by focus area priority, then load-bearing weight, then source deficit. Each gap has a `Status:` line — either `queued (N items)` meaning step 5 created queue items for it, or `no hits — searched: <queries>` meaning step 4 tried but found nothing on-focus.

### 1. [[agent-harness]] → Focus Area 1 (agentic harnesses)
- Load-bearing weight: 8 active-window mentions across 2 projects
- Existing sources: 0 (under-sourced)
- Cross-listed focus areas: none
- Status: **queued (2 items)**

### 2. [[graph-memory]] → Focus Area 2 (principled memory)
- Load-bearing weight: 6 active-window mentions across 1 project
- Existing sources: 1 (under-sourced)
- Cross-listed focus areas: 3 (via `thinkmesh`)
- Status: **queued (1 item)**

### 3. [[equity-research]] → Focus Area 4 (finance research)
- Load-bearing weight: 0 active-window mentions (top-down-only gap — focus area 4 has no bottom-up traction yet)
- Existing sources: 0
- Status: **no hits** — searched `"equity research agent framework 2025"`, `"deep research finance arxiv"`, `"fundamental analysis agent github"`. Most results were ChatGPT/Claude deep-research feature announcements, which don't match the focus area.

## Emerging signals
<!-- Load-bearing concepts that don't map to a focus area. Two sub-cases:
     off-focus  = in ontology, but domain doesn't intersect any focus area
     off-ontology = not in ontology at all
     If the same entry shows up in 3+ consecutive runs with growing counts,
     consider updating RESEARCH_FOCUS.md or ontology.yaml respectively. -->

- `dag-analysis` (14 mentions) — `off-focus` (ontology domain: `swe/*`)
- `project-management` (49 mentions) — `off-ontology`
- `research-agent` (3 mentions) — `off-ontology`

## WebSearch queries tried

For each gap, the query string + kept/filtered/excluded breakdown.

### Gap: [[agent-harness]] (focus area 1)
- `"agent harness patterns 2025 arxiv"` — 12 hits, 2 kept, 8 filtered (dedup), 2 excluded (out-of-focus)
- `"claude code agent harness github"` — 7 hits, 0 kept, 6 filtered (dedup), 1 excluded

### Gap: [[graph-memory]] (focus area 2)
- `"graph memory llm agents arxiv"` — 8 hits, 1 kept, 5 filtered (dedup), 2 excluded (LLM-as-judge — focus area 2 exclusion)

## Skipped (aggregate)
- 14 results matched per-gap source URLs (already ingested under the same concept)
- 5 results matched by title backstop (already ingested under a different concept)
- 6 results matched per-gap queue URLs (already queued under the same concept)
- 3 results matched focus-area-specific exclusion language (2 LLM-as-judge for focus area 2, 1 pure-quant for focus area 4)
- 2 results matched the global Excluded list (crypto/blockchain)

## Queue status delta
- Pending research queue before run: 47 items
- Newly added this run: 3
- Pending after run: 50
- Untouched >7 days: 12 items (candidates for re-prioritization in /mem-wrap)
```

### 6. Update RESEARCH_FOCUS.md

Write findings back into the **Concept Gaps** section of `n-research00`. Use `Edit` with an anchored replacement on just that section — **never use `Write`** on this file, which would clobber the user-maintained Active Focus Areas and Excluded sections.

The section has four sub-sections to populate. Overwrite all four on each run; they are fully regenerated, not incremental.

**Most-active projects (last 14 days)** — from step 3.1, top 3-5 with activity counts:
```
### Most-active projects (last 14 days)
- personal_mem — 12 sessions, 5 decisions
- thinkmesh — 4 sessions, 2 decisions
- research_assistant — 2 sessions, 1 decision
```

**Load-bearing concepts by focus area** — for each of the 4 focus areas, list the active-work concepts that map to it, annotated with `N mentions / M sources`. Use the focus area headings exactly as they appear in step 1:
```
### Load-bearing concepts by focus area

**1. Agentic coding harnesses & advanced Claude Code techniques**
- agent-harness: 8 mentions / 1 source
- claude-code-skills: 5 mentions / 0 sources
- multi-agent-orchestration: 12 mentions / 2 sources

**2. Memory & retrieval frameworks — principled only**
- graph-memory: 6 mentions / 1 source
- rl-memory: 3 mentions / 0 sources
_(llm-as-judge-memory concepts excluded per focus area 2)_

**3. Novelty detection for Thinkmesh**
- out-of-distribution-detection: 4 mentions / 0 sources

**4. Deep research frameworks for finance**
- equity-research-agent: 2 mentions / 0 sources
```

Focus areas with no load-bearing concepts in the window should still be listed with `_no active concepts this window_` to make the absence visible.

**Queued this run** — titles of the queue items created in step 5, grouped by which gap they fill. Include note IDs for traceability, and **link to the run audit note from step 5b at the top of the section** — that's what makes the "Queued this run" writeback forensically recoverable later:
```
### Queued this run
Full run log: [[<run-note-id>]]

- **Gap: [[claude-code-skills]]** (focus area 1)
  - [[n-abc12345]] Advanced Claude Code slash-command patterns (GitHub)
  - [[n-def67890]] Claude Code hooks cookbook
- **Gap: [[graph-memory]]** (focus area 2)
  - [[n-ghi11111]] Graph-structured memory for LLM agents (arxiv)
```

Note that the gap names themselves are now **wikilinks to ontology concepts** (matching the `Gap: [[concept]]` format in the queue item bodies from step 5). This means RESEARCH_FOCUS.md itself becomes a walkable graph entry-point: from a focus-area hub in Obsidian, follow `n-research00` → concept-gaps section → gap concept → run audit note → individual queue items.

**Still open from previous runs** — count + breakdown of `todo`+`research`-tagged notes still in the backlog:
```
mem_search(query="", tags=["todo","research"], type="note", limit=100)
```
Report as:
```
### Still open from previous runs
- Total pending: 47 items
- Untouched >7 days: 12 items (candidates for re-prioritization or drop)
```

Finally, update the `_Last analyzed:_` line to today's date in ISO format (`_Last analyzed: 2026-04-12_`).

**Edit anchor** — use the HTML comment rubric block as the stable anchor for the Edit. The `old_string` should start at `<!-- Populated by /discover` and end at the line before `## Excluded`. This way the Active Focus Areas and Excluded sections are provably untouched.

### 7. Report

Print a discovery summary:

```
## Discovery Report — <date>

### Most-active projects (last 14 days)
- <project> — <N> sessions, <M> decisions

### Gaps Identified
- **<concept>** → focus area <N>: <load-bearing weight> mentions / <source count> sources

### Queued for Processing
| # | Title | URL | Focus Area | Gap Filled | Concepts |
|---|-------|-----|------------|-----------|----------|
| 1 | ... | ... | ... | ... | ... |

### Skipped
- <N> results already in vault
- <N> results already queued
- <N> results excluded by filter or focus-area exclusion language

### Queue Status
- Previously pending: <N>
- Newly added: <N>
- Total pending: <N>
- Untouched >7 days: <N>
```
