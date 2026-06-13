<!--
  Note-format template for the newsletter family (newsletter-events and
  newsletter-concepts). The research-newsletter-worker picks the block matching
  its source_type's grain. Seeded into your vault at
  vault/config/note_formats/newsletter.md on `weave init` — edit that copy directly
  (user-owned, survives upgrades). Sections guide the LLM writer; rename, add,
  drop, reorder.
-->

## newsletter-events (event-grain — markets, macro, dealflow)

## Lead
[Single-sentence "what the issue is about" — the angle the author is pushing]

## Key Developments
- [Specific data, quotes, sources cited]
- [Distinguish reporting from analysis/opinion]
- [What's new this issue vs. running coverage]

## Market Implication
- [Sectors, asset classes, tickers touched]
- [Direction (bullish/bearish/ambiguous) the piece argues for]
- [Timeframe the implication operates on (intraday / weeks / quarters)]

## Watchlist
- [Tickers, central banks, currencies, levels named in the piece]

## Follow-ups
- [<link 1>](url) — [one-line context, why it caught your eye]
- [<link 2>](url) — [...]
*(Secondary — not the main subject of the issue. Listed for later research.)*

## Vault Connections
- Relates to [[<theme_id>]] — [why, in 1 line]   ← only if relates_to was set
- *Theme-unfiled — review pile.*                  ← only if theme_unfiled: true

---

## newsletter-concepts (concept-grain — technical, methodology, philosophy)

## Lead
[Single-sentence "what the issue argues / explains"]

## Key Developments
- [The piece's main thread — what was built/measured/observed]
- [Specific evidence, code, benchmarks, citations]

## Why It Matters
- [Where this fits in the broader space — what it changes about practice]
- [Who should care, and for what]

## Concepts in Play
- `<ontology-concept-1>` — [how the piece touches it]
- `<ontology-concept-2>` — [...]

## Follow-ups
- [<link 1>](url) — [one-line context]
*(Secondary — listed for later research, not the main thread.)*

## Vault Connections
- Relates to [[<theme_id>]] — [why]               ← only if relates_to was set
- See concept hub [[<concept>]] for related items
