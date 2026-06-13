<!--
  Note-format template for the podcast family (podcast-events and
  podcast-concepts). The research-podcast-worker picks the block matching its
  source_type's grain. Seeded into your vault at
  vault/config/note_formats/podcast.md on `weave init` — edit that copy directly
  (user-owned, survives upgrades). `MM:SS` Key Moments come from Gemini
  key_moments.
-->

## podcast-events (event-grain — markets, macro, interview shows)

## Lead
[Single-sentence "what the episode argues" — the angle the host/guest is pushing]

## Key Developments
- [Specific data, quotes, sources cited] — "<verbatim quote>" (host/guest)
- [Distinguish reporting from analysis/opinion]
- [What's new this episode vs. running coverage]

## Market / Signal Implication
- [Sectors, asset classes, tickers, currencies touched]
- [Direction (bullish/bearish/ambiguous) the piece argues for]
- [Timeframe the implication operates on]

## Key Moments
- `MM:SS` — [description from Gemini's key_moments]
- `MM:SS` — [...]

## Mentioned
- [<link 1>](url) — [one-line context, why it came up]
- [<link 2>](url) — [...]
*(Secondary — what the speakers cited; not the main subject.)*

## Vault Connections
- Relates to [[<theme_id>]] — [why, in 1 line]   ← only if relates_to was set
- *Theme-unfiled — review pile.*                  ← only if theme_unfiled: true

---

## podcast-concepts (concept-grain — deep-dives, lectures, technical explainers)

## Lead
[Single-sentence "what the episode explains / argues / demonstrates"]

## Key Developments
- [The main thread — what was built/measured/argued]
- [Specific evidence, examples, named techniques] — "<verbatim quote>"

## Why It Matters
- [Where this fits in the broader space — what it changes about practice]
- [Who should care, and for what]

## Concepts in Play
- `<ontology-concept-1>` — [how the episode touches it]
- `<ontology-concept-2>` — [...]

## Key Moments
- `MM:SS` — [description from Gemini's key_moments]
- `MM:SS` — [...]

## Mentioned
- [<link 1>](url) — [one-line context]
*(Secondary — listed for later research, not the main thread.)*

## Vault Connections
- Relates to [[<theme_id>]] — [why]               ← only if relates_to was set
- See concept hub [[<concept>]] for related items
