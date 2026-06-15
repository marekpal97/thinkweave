---
description: Inline session synthesis — walk imported-but-unsynthesised Claude Code sessions, compose a summary + insights + decisions via the running model, and weave_extract one wrap-shaped session per transcript. The keyless `--via inline` half of `weave import claude-code --enrich`; pairs with `--via batch` which fans out through the API wrapper.
allowed-tools: Bash, mcp__thinkweave__weave_concepts, mcp__thinkweave__weave_read, mcp__thinkweave__weave_extract, mcp__thinkweave__weave_update
---

# /seed-enrich — Inline session synthesis

Turn imported Claude Code sessions into durable memory **without burning a
provider key**. An imported session is materialised as a verbatim transcript
dump (`enrichment` deferred); this skill synthesises each one — a summary plus
derived insight and decision notes — using the running session's model instead
of `agent_client.batch_completions_sync`.

Same spec, same writeback as the batch path: both reach `weave_extract`
(`extract_session`), so an inline-synthesised session is byte-for-byte
identical to a `--via batch` one and to a live `/weave-wrap` session —
ontology-gated concepts, commit-evidence decision flips, `processed: true`.

The user reaches this from `weave import claude-code --enrich --via inline`
(the CLI prints a hint pointing here) or directly as `/seed-enrich`.
Headless-safe: `claude -p "/seed-enrich"` works the same as in-session.

## Steps

1. **Load the ontology.** Call `mcp__thinkweave__weave_concepts(action='list', limit=1000)`. This is the canonical vocabulary; anything you propose outside it is routed to `proposed_concepts:` by the server-side gate (no need to pre-filter — proposing specific terms is encouraged).

2. **List the pending set.** Run:
   ```bash
   weave import claude-code --enrich --dry-run
   ```
   Parse the `PENDING\t<note_id>\t<project>\t<title>` lines — one per session awaiting synthesis. Honor any `--limit` the user passed by capping the list. If there are none, report "nothing to synthesise" and stop.

3. **Synthesise each session.** For each pending `<note_id>`:
   - `mcp__thinkweave__weave_read(id=<note_id>)` — the body is the verbatim `## Transcript` dump (you can fan reads out in parallel for a batch).
   - Compose, from the transcript, the same four artifacts the spec asks for:
     - **summary** — 2–4 sentences of plain prose (becomes the session body).
     - **insights** — `[{title, body, concepts}]`. Gotchas, patterns, trade-offs the session surfaced. Empty list if none stand out.
     - **decisions** — `[{title, rationale, outcome, file_paths, concepts}]` where `outcome ∈ {committed, abandoned, partial}`. Explicit choices made/accepted — skip exploratory chatter.
     - **concepts** — 2–6 specific session-level terms (`fts5`, `anthropic-batches`), not meta-terms.
   - Write it back:
     ```
     mcp__thinkweave__weave_extract(
       session_id=<note_id>,
       summary=<summary>,
       insights=[{title, body, concepts}, ...],
       decisions=[{title, rationale, outcome, file_paths, concepts}, ...],
     )
     ```
     `weave_extract` archives the transcript to a `transcript.md` companion, mints the insight/decision notes, writes the `## Summary`, and stamps `processed: true` — all in one call.
   - Tag the session note's own concepts (mirrors the batch path so both backends link the session identically):
     ```
     mcp__thinkweave__weave_update(note_id=<note_id>, frontmatter_updates={"concepts": [<session concepts>]})
     ```

4. **Report.** One line: `Synthesised N session(s); created D decision(s), I insight(s).`

## Notes

- **Idempotent.** A session synthesised once is `processed: true` and drops out of the pending list — re-running is safe and skips it.
- **Don't re-embed the transcript.** Only the structured synthesis goes into `weave_extract`; the raw transcript is preserved as the companion, not duplicated into the summary.
- **Thin sessions are fine.** If a transcript is too sparse to extract anything, pass a one-sentence `summary` with empty `insights`/`decisions` — the session still becomes `processed`.
- **Large backlogs** (hundreds of sessions, with an API key) are faster via `weave import claude-code --enrich --via batch`. This skill is the no-key route and the small-batch default.
