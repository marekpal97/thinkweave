# SQL schema — the derived index

**Markdown is the source of truth; every table below is a derived, rebuildable
projection.** Nothing here is authoritative — `weave index --full` drops and
rebuilds the lot from the vault's markdown (and from the per-session JSONL event
logs, which are themselves truth for the event-derived tables). If a table and a
note ever disagree, the note wins. Treat SQLite as a query accelerator, not a
database of record.

Two SQLite files live under `vault/.weave/`:

- **`index.db`** — the knowledge index. 11 base tables + 1 FTS5 virtual table.
  Schema in `src/thinkweave/core/indexer.py` (`SCHEMA_SQL` + `FTS_SCHEMA_SQL`).
- **`embeddings.db`** — the embedding cache, kept separate so it can be deleted /
  recomputed without touching the index. Schema in
  `src/thinkweave/core/embeddings.py` (`EMBEDDINGS_SCHEMA`).

What is **not** in SQL: the acquisition queues (`vault/.weave/queues/*.jsonl`),
per-session events / retrieval logs (`events.jsonl`, `retrieval_log.jsonl`), and
batch buffers are all JSONL — acquisition and event state are not knowledge, and
the queue directory is explicitly excluded from the index. The `prompts`,
`prompt_concepts`, and `context_served` tables are *projections* of those JSONL
logs for join/analytics use; the JSONL stays truth.

## `index.db`

| Table | Purpose | Key columns | Defined at |
|---|---|---|---|
| `notes` | Every vault note (one row per markdown file). The spine all other tables hang off. | `id` PK, `type`, `path` UNIQUE, `project`, `date`, `content_hash` (incremental-skip), `body_text`, `frontmatter` (JSON) | `indexer.py:40` |
| `edges` | Typed graph edges between notes (`derived_from`, `supersedes`, `relates_to`, `cites`, `implements`, `builds_on`, plus concept/tag co-occurrence). `weight` carries tie-strength for ranked graph walks. | `(source, target, edge_type)` PK, `weight`, `metadata` (JSON) | `indexer.py:59` |
| `note_concepts` | Note ↔ concept (many-to-many). Drives concept-hub catalysts and concept-walk graph queries. | `(note_id, concept)` PK, `domain` | `indexer.py:77` |
| `decision_files` | Decision ↔ file-path, from decision frontmatter `file_paths`. One JOIN answers "every decision that touched this file." | `(decision_id, file_path)` PK | `indexer.py:92` |
| `note_tags` | Note ↔ tag (many-to-many). Broad filter facets, distinct from concepts. | `(note_id, tag)` PK | `indexer.py:101` |
| `concept_hierarchy` | Concept ↔ ancestor with `depth`, materialized from the ontology tree. Lets a query reach all descendants of a namespace. | `(concept, ancestor)` PK, `depth` | `indexer.py:110` |
| `context_served` | Notes served to a session, projected from `retrieval_log.jsonl`. `source` ∈ `startup` / `onthefly` / `prompttime` / `loop-prime` (issue-loop claim-time priming, #57) — each push source stays distinct from agent-pulled `onthefly` to preserve the agent-judgment signal for the RLVR export. | `(session_id, note_id, source)` PK, `ts` | `indexer.py:126` |
| `graph_ranks` | Per-concept-induced-subgraph PageRank (and future centrality schemes). `rank_type` keyed `pagerank:{concept}`. Computed in the dream apply phase; consumed by `weave_concepts(action='canonical_for')`. | `(note_id, rank_type)` PK, `score` | `indexer.py:142` |
| `hub_log_entries` | **The evolution DAG.** One row per catalyst-log entry on a concept hub (`hub_kind='concept'`, `hub_id`=vocabulary term) **or** theme (`hub_kind='theme'`, `hub_id`=`thm-id`). `ref_date` is the intra-log predecessor an `*extends/agrees/contradicts <date>*` entry points at; `cited_note_id` the citation; `seq` the stable order. This is the SQL substrate that reconstructs the temporal/evolutionary DAG for both hub families. | `hub_id`, `hub_kind`, `entry_date`, `flag`, `ref_date`, `cited_note_id`, `seq` | `indexer.py:161` |
| `prompts` | User prompts projected from `events.jsonl` — the question stream as a queryable surface. `classification` is the probe heuristic's verdict (`probe` / NULL). | `(session_id, seq)` PK, `text`, `classification`, `project` | `indexer.py:190` |
| `prompt_concepts` | Concept attribution for *probe* prompt rows, so probes JOIN against `note_concepts` / `hub_log_entries` without re-deriving. Feeds probe-pressure salience. | `(session_id, seq, concept)` PK | `indexer.py:202` |
| `notes_fts` | FTS5 virtual table over `notes(id, title, body_text, tags)`, external-content (`content='notes'`). Custom tokenizer (`unicode61 remove_diacritics 2 tokenchars '-_'`) keeps hyphenated concepts/ids whole. FTS5 manages its own shadow tables (`notes_fts_data`, `_idx`, `_docsize`, `_config`) automatically. | `id` UNINDEXED, `title`, `body_text`, `tags` | `indexer.py:213` |

## `embeddings.db`

| Table | Purpose | Key columns | Defined at |
|---|---|---|---|
| `embeddings` | API-computed embedding cache. `content_hash` dedups recompute on unchanged notes; `model` records which embedder produced the vector. Kept in its own DB so it's disposable. | `note_id` PK, `content_hash`, `embedding` (BLOB), `model` | `embeddings.py:19` |

## Rebuild & migration notes

- **Full rebuild:** `weave index --full` recreates every table from markdown +
  JSONL. Incremental indexing uses `notes.content_hash` / `file_mtime` to skip
  unchanged files.
- **FTS tokenizer migration:** on connect, the indexer detects a pre-A4
  `notes_fts` built with the SQLite default tokenizer and recreates it with the
  hyphen-preserving tokenizer above (data is safe — only the FTS index is
  regenerated). Marker: `indexer.py` `_FTS_TOKENIZER_MARKER`.
- **Adding a table:** add the `CREATE TABLE IF NOT EXISTS` to `SCHEMA_SQL` (or
  `EMBEDDINGS_SCHEMA`), populate it in the indexer's per-note pass, and add a row
  to the table above. There is no separate migrations directory — `IF NOT EXISTS`
  + full-rebuild idempotency is the migration story.
