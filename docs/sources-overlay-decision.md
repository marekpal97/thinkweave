# Decision: keep `sources.yaml` + `source_types.yaml` split

**Status:** decided 2026-06-03 (Phase 2 item 2.4 of the post-code-review action list)
**Decider:** marekpal97
**Outcome:** **Keep the split.** Document the open/closed asymmetry more prominently. Do not migrate.

## Context

Today `vault/.mem/` holds two separate user-overlay files for source-type configuration:

| File | What it overlays | Loaded by | When you edit it |
|---|---|---|---|
| `source_types.yaml` | The `SourceTypeSpec` registry — declares a new source type's identity (`slug`, `bucket`, `layout`, `aliases`, `skills`, `temporal_grain`) | `sources/registry.load_user_specs` | When adding a new source type |
| `sources.yaml` | Per-type **behaviour** (`queue` path, `dedup_keys`, `drain_strategy`, `intake_folder`, `url_patterns`, plus per-source-type knobs like `feed_config`, `triage_model`, `drain_parallelism`, `subagent_type`, …) and global config (`projects.<name>.discover_strategies`, `landing_files.*`, `auto_todo_extraction`) | `sources/config.load_user_config` | When tuning how a declared type is processed |

The split has been in place since `source_types.yaml` was added to support `/source-scaffold`. The Phase 2 audit asked whether the two should collapse into one file with a `declared: bool` flag.

## The asymmetry the split encodes

personal_mem deliberately separates two concerns:

- **Identity is open-world.** `VaultManager.create_note` accepts any `source_type` string — unregistered types fall through to a `folder` layout with an empty bucket (`sources/<slug>/source.md`). This makes ad-hoc experimentation cheap: you can ingest a one-off with a new `source_type` before you've registered it anywhere.
- **Behaviour is closed-world.** `mem drain --source-type <undeclared>` errors. Production paths (queue, dispatch, dedup) need the spec to know where to look.

The two files reflect this asymmetry **structurally**:

- A `SourceTypeSpec` is a `@dataclass(frozen=True)` with validated fields (`_VALID_LAYOUTS`, `_VALID_TEMPORAL_GRAINS`). Adding one is a deliberate act of declaration.
- A `sources.yaml` entry is open-set — it can hold any per-type knob a downstream consumer wants (the `news` entry alone has 14 keys). It's policy, not identity.

## Why a `declared: bool` flag would be worse

A merged file with `declared: bool` per entry would either:

1. **Force users to write `declared: false` for ad-hoc experimentation** — eliminates the whole "type a slug, write a note, sort it out later" ergonomic that the open-world design exists for.
2. **Treat `declared: bool` as implicit** (defaulting `false` for unmerged-from-registry entries) — but then the flag is just a name for "do I have a `SourceTypeSpec`?", which is exactly what the two-file split already encodes by file existence. We'd have collapsed the surface without removing the concept.

Neither variant pays for the migration cost.

## Concrete reasons to keep the split

1. **Different change cadences.** The registry rarely changes — declare a type once and forget it. Behaviour changes often (queue paths, dedup keys, triage model, parallelism). Different files keep diffs focused.
2. **Different consumers.** Registry is read by `VaultManager.create_note` and `mem sources show`. Behaviour is read by `Queue`, `mem drain`, `mem_sources_config` MCP, and the per-source skill files. Co-locating them creates unnecessary coupling on the loader path.
3. **Validation surfaces differ.** Registry entries are validated against the `Layout` and `TemporalGrain` enums at load time, with stderr warnings on invalid entries. Behaviour entries are intentionally open-set — any string a downstream consumer wants. Merging would muddle the validation rules.
4. **Tooling already encodes the split.** `/source-scaffold` writes to both files at once when a new source type is created. `mem sources show <slug>` reads from the registry. `mem_sources_config` MCP returns the behaviour overlay. Users rarely touch either file by hand — the split is invisible until you're adding a new type.
5. **Backwards compatibility.** Every existing vault has a `sources.yaml`; many have a `source_types.yaml` from `/source-scaffold` runs. A collapse migration would need a deprecation path for both files and a doctor-time migration step. The user surface gain is marginal; the migration tax is real.

## What changes as a result

**No code changes.** The split stays. But the asymmetry should be documented more prominently:

1. **CLAUDE.md §4 / ARCHITECTURE.md "User configuration"** — already mention the split. ARCHITECTURE_NOTES.md §"sources.yaml vs source_types.yaml" (added in Phase 2.1) now carries the full asymmetry explanation. ✓ done.
2. **`/source-scaffold` skill** — should mention in its kickoff that it writes two files (it already does this implicitly; making it explicit would help users who later inspect the diff).
3. **`mem doctor`** — could surface a diagnostic when `source_types.yaml` declares a slug but `sources.yaml` has no behaviour overlay for it (the type is declared but un-configured — likely a partial scaffold run). Optional follow-up.

## Rejected alternative — full collapse

If we ever revisit, the migration sketch would be:

1. New file: `vault/.mem/source_config.yaml` with top-level structure:
   ```yaml
   types:
     <slug>:
       declared: true
       spec: {bucket, layout, aliases, skills, temporal_grain}
       config: {queue, dedup_keys, drain_strategy, …}
   projects: {…}
   landing_files: {…}
   ```
2. `mem doctor --migrate` step: read both legacy files, fold into the new shape, leave both in place for one release with a deprecation warning.
3. Remove `sources/registry.load_user_specs` and `sources/config.load_user_config` after the deprecation window.
4. Update `/source-scaffold` to write the unified file.
5. Update `mem_sources_config` MCP to read from the new file.

Cost: ~3 sessions of work + a deprecation cycle. Benefit: ~one fewer file in `vault/.mem/`. Verdict: not worth it.

## See also

- `ARCHITECTURE.md` §"User configuration — `sources.yaml`" — the short-form description.
- `ARCHITECTURE_NOTES.md` §"sources.yaml vs source_types.yaml" — the open/closed asymmetry deep dive.
- `src/personal_mem/acquisition/sources/registry.py:load_user_specs` — overlay loader for `source_types.yaml`.
- `src/personal_mem/acquisition/sources/config.py:load_user_config` — overlay loader for `sources.yaml`.
