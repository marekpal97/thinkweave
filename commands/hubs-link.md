---
description: Inline temporal-DAG linkage for concept hubs — walk hubs lacking agrees/contradicts/extends flags and rewrite via the running model. The `weave hubs link --via inline` path; pairs with `--via batch` which fans out via the API wrapper.
allowed-tools: Read, Bash, mcp__thinkweave__weave_read, mcp__thinkweave__weave_update
---

# /hubs-link — Inline temporal-DAG linkage

Rewrite flat `new` flags on concept-hub `## Catalyst log` entries into
`agrees` / `contradicts` / `extends` relationships, without an API key.
Walks the same hub set `weave hubs link --via batch` processes, but
produces revisions via the running session's model.

The user invokes this from `weave hubs link --via inline` (the CLI prints
a hint pointing here) or directly as `/hubs-link [--concept <slug>]`.

## Steps

1. **Identify candidate hubs.** Run `Bash`:
   ```bash
   weave hubs link --dry-run [--concept <slug>] [--min-entries 2]
   ```
   This prints the candidate set (concept names + entry counts) and the
   first sample prompt. No API call. The output tells you which hubs
   need a linkage pass.

2. **Walk hubs one at a time.** For each candidate concept:
   - `Read` the hub file at `vault/concepts/topics/<concept>.md`.
   - Parse the `## Catalyst log` section. Each entry has shape:
     `- YYYY-MM-DD — <text> [[note-id]] · flag: <new|agrees|contradicts|extends>` (optionally with `ref: YYYY-MM-DD`).
   - Apply the linkage rubric inline (the same one in
     `operations/hubs_batch.HUB_LINKAGE_SYSTEM`):
     - Entry 1 (oldest) is always `new`.
     - For each later entry, pick the closest semantic predecessor among
       STRICTLY earlier entries; classify as `agrees` / `extends` /
       `contradicts`, plus a `ref` date and a ≥20-char verbatim `ref_quote`.
     - If no good predecessor → `new`.
   - Rewrite the entries in place with the new flags. Use `Bash` to call
     a small Python one-liner — there's no MCP tool for hub-file mutation
     today, so the safest path is:
     ```bash
     python -c "
     from thinkweave.synthesis.concept_hub import parse_concept_hub, write_concept_hub
     hub = parse_concept_hub('<vault>/concepts/topics/<concept>.md')
     # Mutate hub.log_entries[i].flag / .ref per your revisions, then:
     write_concept_hub(hub)
     "
     ```
     Or call `mcp__thinkweave__weave_update` only if the hub note carries a
     standard `id:` (some hubs do — fall back to the Python one-liner when
     they don't).

3. **Reindex.** After all hubs are updated, run
   `weave index --only-new` so the new edges land in the SQLite index.

## Notes

- This skill is conservative — when in doubt about a predecessor,
  leave the flag as `new`. Forcing a stretch link pollutes the DAG.
- The `--min-entries N` flag from the CLI (default 2) is honored by the
  candidate set — the dry-run output reflects it.
- The batch route's `--max-tokens` parameter doesn't apply here — the
  running model handles each hub in one turn, no token cap to set.
- Prefer `--via batch` (with an API key) for vaults with >50 concept hubs
  that need linkage. This skill is the no-key fallback, not the fast path.
