---
description: Inline concept enrichment — walk notes lacking concepts and assign ontology-gated concepts via the running model, one batch at a time. The /weave-enrich --via inline path; pairs with `weave enrich --via batch` which fans out via the API wrapper instead.
allowed-tools: Bash, mcp__thinkweave__weave_concepts, mcp__thinkweave__weave_read, mcp__thinkweave__weave_update, mcp__thinkweave__weave_search
---

# /enrich-notes — Inline concept enrichment

Concept enrichment without burning a provider key. Walks the same candidate set
that `weave enrich --via batch` would process (`SELECT * FROM notes WHERE
(SELECT COUNT(*) FROM note_concepts WHERE note_id = n.id) < 1`), but assigns
concepts via the running session's model instead of going through
`agent_client.batch_completions_sync`.

Headless-safe: invoked from `claude -p "/enrich-notes"` works the same as
in-session use.

## When to pick this route

- No provider key, or the user explicitly opted for `--via inline`.
- Candidate count is small (the size threshold in
  `operations/_backfill_route.choose_route` defaults to 200; below that, the
  per-turn overhead of the running model beats the wrapper's fan-out).
- You want to step through the candidate stream interactively — `--via batch`
  is fire-and-forget; this one prints decisions per batch.

## Steps

1. **Load the ontology.** Call `mcp__thinkweave__weave_concepts(action='list', limit=1000)`. This is the vocabulary you may assign — anything outside it goes into `proposed_concepts:` (the strict ontology gate handles this server-side on `weave_update`, but seeing the list calibrates your output).

2. **List candidates.** Run `weave enrich --dry-run --project <P>` to print the candidate set (note IDs + titles). Cap at the user's `--limit` if they passed one.

3. **Walk in batches of 25.** For each batch:
   - Call `mcp__thinkweave__weave_read(id=<note_id>)` for each note in the batch (you can fan out reads via parallel tool calls).
   - For each note, propose 2–4 concepts. Prefer specific terms (`ml/transformer` over `ml/`). New terms are OK — the gate routes them to `proposed_concepts:`.
   - Call `mcp__thinkweave__weave_update(note_id=<id>, frontmatter_updates={"concepts": [...]})` per note. The server-side gate is the source of truth — non-canonical terms shunt automatically.

4. **Report.** After the loop, print a one-line summary: `Enriched N notes; M concepts assigned (K shunted to proposed_concepts).`

## Notes

- Don't reindex from inside this skill — `weave enrich --via inline` (the CLI hint that dispatches here) prints a follow-up instruction the user can run when ready.
- The `force` flag is honored by the candidate query — if the user passed `--force`, candidates include notes that already have concepts.
- For very large vaults, the user should prefer `--via batch` (with an API key). This skill is the no-key fallback, not the fast path.
