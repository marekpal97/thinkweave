# /discover — Research Discovery & Gap Analysis

You are running a discovery pass across the knowledge vault to find new research leads. This skill analyzes what's in the vault, identifies gaps, and searches for papers/repos/articles to fill them. Results are added as queue items (`todo`+`research` tagged notes) for later processing by `/research --queue`.

This skill is designed to run periodically (e.g. `/loop 6h /discover`) or on demand.

## Steps

### 1. Load Research Focus

Read the priority list:
```
Read /home/marekpal97/vault/sources/RESEARCH_FOCUS.md
```

This contains:
- **Active Focus Areas** — what topics to prioritize
- **Authors to Follow** — whose work to look for
- **Concept Gaps** — auto-populated section of underexplored areas
- **Excluded** — topics to skip entirely

If the file doesn't exist, report that and suggest the user create one. Don't proceed without focus areas — undirected search is wasteful.

### 2. Analyze Vault State (Cross-Project)

Run these queries to understand current coverage:

**Concept coverage** — which concepts have source notes vs. only session/note mentions:
```
mem_concepts(prefix="", min_count=2)
```

**Recent activity** — what's been worked on lately:
```
mem_timeline(days=14)
```

**Existing sources** — what's already been ingested:
```
mem_search(query="", type="source", limit=50)
```

**Current queue** — what's already queued:
```
mem_search(query="", tags=["todo", "research"], type="note", limit=30)
```

### 3. Identify Gaps

Cross-reference focus areas with vault state to find:

1. **Concept gaps**: Focus area concepts that have few or no source notes. Example: if `scaling-laws` appears in 12 session notes but only 1 source, that's a gap.

2. **Citation gaps**: Sources that reference papers/repos not yet in the vault. Scan recent source note bodies for URLs or paper titles that don't have corresponding source notes.

3. **Author gaps**: Authors from the focus list whose work isn't well represented.

4. **Recency gaps**: Focus areas where the newest source is >3 months old — the field may have moved on.

Rank gaps by priority:
- Gaps in active focus areas rank highest
- Gaps with many vault references (lots of notes mention the concept but no authoritative source) rank next
- Author-following and recency gaps rank lower

### 4. Search for Content

For the top 5-8 gaps, run targeted searches:

```
WebSearch("<gap-specific query>")
```

**Query construction** — be specific, not generic:
- For concept gaps: `"<concept> survey 2025 arxiv"` or `"<concept> tutorial github"`
- For author gaps: `"<author name> recent papers 2025"`
- For citation gaps: search the exact paper title or arxiv ID
- For recency gaps: `"<topic> new developments 2025 2026"`

**Filtering**:
- Skip results that match existing source note URLs (already ingested)
- Skip results that match existing queue item URLs (already queued)
- Skip results matching exclusion patterns from RESEARCH_FOCUS.md
- Prefer arxiv papers, well-known blog posts, and active GitHub repos over random pages

### 5. Create Queue Items

For each promising find, create a queue note:
```
mem_create(
  type="note",
  title="<descriptive title — what you'd learn from this>",
  body="<url>\n\nDiscovered by /discover on <today's date>.\nGap: <which gap this fills>\nRelevance: <one sentence on why this matters>",
  tags=["todo", "research"],
  frontmatter={
    "concepts": ["<relevant concepts>"]
  }
)
```

**Quality over quantity** — create 3-8 queue items per run, not 30. Each should clearly fill an identified gap. If you can't find good material for a gap, note it in the report rather than queuing low-quality leads.

### 6. Update RESEARCH_FOCUS.md

Update the **Concept Gaps** section with current findings:
- Which focus area concepts are well-covered (have 3+ sources)
- Which are underexplored (0-1 sources)
- Any new concepts emerging from recent sessions that aren't in focus areas yet

Read the current file, then use `Write` to update just the Concept Gaps section. Don't touch Active Focus Areas, Authors, or Excluded — those are user-maintained.

### 7. Report

Print a discovery summary:

```
## Discovery Report — <date>

### Gaps Identified
- <gap description> (priority: high/medium/low)

### Queued for Processing
| # | Title | URL | Gap Filled | Concepts |
|---|-------|-----|-----------|----------|
| 1 | ... | ... | ... | ... |

### Coverage Update
- Well-covered: <concepts with 3+ sources>
- Needs work: <concepts with 0-1 sources>

### Skipped
- <N> results already in vault
- <N> results already queued
- <N> results excluded by filter

### Queue Status
- Previously pending: <N>
- Newly added: <N>
- Total pending: <N>
```
