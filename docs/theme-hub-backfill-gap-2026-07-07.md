# No tooling exists for theme catalyst-log historical backfill (unlike concept hubs)

**Where:** `surfaces/cli/hubs.py` (`_hubs_plan`/`_hubs_status` — concept-only),
`surfaces/cli/themes.py` (only subcommand is `rebuild-registry`),
`synthesis/theme_candidates.py` (`extend_theme_with_sources` — steady-state
only, not a backfill path), `operations/dream.py` (`dream-theme-worker`
handles ongoing log *gaps*, not zero-catalyst backfill).

## Context

Concept hubs have a complete backfill story: `weave hubs plan` sizes the
work, `weave drain --target hubs --via batch|inline` does the extraction,
`weave hubs link` does the temporal-DAG linkage. Nothing equivalent exists
for themes. `weave themes --help` has exactly one subcommand
(`rebuild-registry`). `extend_theme_with_sources` — the function `/dream`'s
`theme_extensions` plan key calls — is built for the steady-state case
("a new source just arrived, attach it to an existing theme today"): it
hardcodes `date=today` on every entry, which is correct for that case and
wrong for reconstructing history. There's no `weave themes plan`, no
`weave drain --target theme-hubs`, no theme equivalent of `hubs_batch.py`.

On a real migrated vault (~3,500 notes, 38 themes minted from a curated
narrative ontology), this wasn't a hypothetical gap: **all 38 themes had
zero catalyst-log entries and a placeholder essence** despite years of
real vault content about most of these narratives. Nothing in the
toolkit could have filled them in — not `/dream` (only handles gaps on
themes that already have catalysts), not any CLI subcommand.

## A second, related finding: concept-tag join doesn't work for themes at all

The obvious first approach — mirror `weave hubs plan`'s mechanism, which
finds candidate notes via `note_concepts` joined on a concept's exact tag
— fails completely for themes. Each theme's narrative is defined in
`config/theme_ontology.yaml` as a list of constituent tags (e.g.
`biopharma-positioning` has 38 tags, `central-bank-policy` has 30). Even
using the *full* narrative tag list (not just the theme's own
post-mint `concepts:` frontmatter, which is usually a small subset),
**37 of 38 themes had zero notes matching any of their narrative's tags**
via exact `note_concepts` join.

This isn't a side-effect of concept-vocabulary pruning (a separate
finding from earlier in this migration) — broadening the join to the
full, never-pruned `theme_ontology.yaml` tag lists made no difference.
The real cause: `theme_ontology.yaml` is a curated planning taxonomy
(what narratives *should* group under), while `concepts:` on notes is an
emergent, LLM-assigned tagging vocabulary. The two essentially don't
overlap. Exact tag-matching, which works fine for concept hubs (a concept
hub's job is literally "every note tagged with this exact term"), is the
wrong retrieval mechanism for theme backfill, where the target is a
broader narrative rather than an exact tag.

## What worked, as a manual one-off (not proposing to upstream the script — proposing the CLI gap get filled properly)

Semantic search over cached embeddings
(`core.embeddings.EmbeddingSearch.search(query, limit, note_type=[...])`),
querying with the theme's title + full narrative tag list, filtered to a
0.35 cosine-similarity floor and capped at 25 notes/theme. This produced
honest, variable coverage that tracked real vault content — e.g.
`china-ai` decayed smoothly from 0.666 to 0.564 across a full 30 results
(genuinely well-covered), while `civil-aerospace` cliffed hard after rank
3 (0.333 → 0.276, then flatlined near the noise floor) and correctly
ended up with **zero** backfilled entries rather than being padded with
irrelevant notes. Across the 37 themes that cleared the threshold: 871
extraction requests, 0 errors, 1,501 catalyst-log entries applied with
each entry's own historical date (not the backfill run date) — then a
temporal-DAG linkage pass (reusing the exact `HUB_LINKAGE_SYSTEM` /
`validate_linkage_revision` mechanism `weave hubs link` uses for concepts,
just pointed at theme files) linked 959 of those entries into real
`agrees`/`extends`/`contradicts` chains. Spot-checked and coherent — e.g.
the `iran-war` theme now threads a real multi-week narrative evolution
across dated JPMorgan sellside notes (base-case deal read → oil-storage
negotiating cushion → past-kinetic-peak reassessment), correctly dated
and linked.

## Suggested fix

- Add a real backfill path for themes, mirroring the concept-hub one:
  `weave themes plan` (or extend `weave hubs plan --target themes`) +
  `weave drain --target theme-hubs`. Retrieval should use semantic search
  against the theme's title/tag-list as the query, **not** an exact
  `note_concepts` join — tag-join is structurally the wrong tool here for
  the taxonomy-vs-emergent-vocabulary reason above.
- The extraction and linkage machinery barely needs new code — it's
  already shared via `synthesis/hub.py` (`HubLogEntry`, `render_catalyst_log`,
  etc.) and `operations/hubs_batch.py` (`HUB_LINKAGE_SYSTEM`,
  `validate_linkage_revision`). The one real new piece is the semantic
  retrieval step and a backfill-mode writer that stamps the source note's
  own date rather than today's (same principle `hubs_batch.py` already
  applies for concept-hub bulk mode).

## Related (reinforcing, not a new finding)

While building the linkage pass for this backfill, reusing `weave hubs
link`'s exact positional-`zip(entries, revisions)` approach produced the
same revision/entry count mismatches on 8 of 37 themes (e.g. `agentic-ai`:
53 revisions for 54 entries) that were seen on the concept-hub side.
Spot-checks here also came back coherent, same as the concept-hub case —
but two independent occurrences of the same pattern is worth noting as
corroboration that this is a real, recurring model behavior (`gpt-5-mini`
occasionally drops or adds an entry in a long list), not a one-off fluke.
