# devloop/ boundary spec — module map, Gate protocol, trajectory module, index_client contract

**Status:** accepted design (issue #93, epic #88 wave 2). This document is the
bridge context for #94 (mechanical extraction) and the boundary authority for
the wave-3/4 issues that redesign inside these modules (#95, #98, #99, #100).
It specifies *where seams go and what each interface promises* — no code moves
here; #94 owns the moves, behavior-frozen.

Vocabulary: *module* = interface + implementation (scale-agnostic); *interface*
= everything a caller must know (signatures, invariants, error modes, config);
*seam* = where an interface lives; *deep* = much behavior behind a small
interface. Terms per the codebase-design doctrine; goals per epic #88's
north-star block (fewer POCs; deep but interpretable modules with boundaries at
likely redesign points; conceptual fidelity; generic utils never beside key
logic; no contract asserted in prose without an enforcing seam).

## 1. The two planes and the external seam

The loop is two planes with one seam between them:

- **Judgment plane** — the `/issue-loop` command (`issue-loop.command.md`): an
  LLM orchestrator that dispatches implementer/judge/reviewer subagents,
  composes traces, applies labels, opens PRs.
- **Deterministic plane** — the `devloop/` package: graph math, gate
  execution, classification, payload assembly. Stdlib-only, never imports
  `thinkweave`, no LLM calls.

The seam between the planes is the **CLI subcommand surface** (JSON on stdout,
exit codes): `config · plan · claim · release · check · validate · prime ·
triage · trajectory`. That surface is the package's one external interface — the
orchestrator knows nothing else. #94 keeps it byte-compatible;
`scripts/issue_loop.py` survives as a thin shim (§8).

Everything below the CLI is interior. Modules receive resolved config
*sections* and plain dicts as parameters — no module below `cli` reads
`loop.toml`, touches `gh`, or resolves paths on its own (accept dependencies,
don't create them).

## 2. Package map

```
devloop/
  __init__.py        (empty)
  cli.py             entry point: argparse, config resolution, dispatch
  dag.py             tracker-as-DAG math + the body-grammar it parses
  gates.py           Gate protocol + deterministic executors
  triage.py          risk-lane classification of shipped PRs
  paths.py           leaf util: the three-form path matcher
  github.py          gh plumbing: issue snapshot + tracker mutations
  index_client.py    the sqlite-RO seam into the derived index
  trajectory/
    __init__.py      re-exports the module's public interface
    mint.py          write face: trajectory-note payload assembly
    prime.py         read face: claim-time prior-trajectory context
scripts/issue_loop.py    thin shim → devloop.cli.main
```

Eight public names. `trajectory/` is **one module** with two implementation
files — its interface is what `trajectory/__init__.py` re-exports; `mint.py`
and `prime.py` are internal seams, not siblings (§4). There is no `config.py`,
no `utils.py`, no `git.py` (§2.1, §6).

Per-module interfaces:

**`cli.py`** — owns `DEFAULT_CONFIG`, `load_config`, `parse_override`,
`apply_overrides`, `build_arg_parser`, `main`. Config lives here deliberately:
resolution-and-override is run-posture, exercised only at the entry point;
`--set` typo-rejection and "gates are file-only" are CLI contract lines.
`cli.py` is also the imperative shell: the `trajectory` subcommand's git reads
(branch, log, numstat) and file-argument loading happen here, feeding the pure
`build_trajectory` — subprocess git is argument-gathering, not a module.

**`dag.py`** — `parse_blockers`, `parse_wave`, `parse_parallel_safe`,
`all_blockers`, `compute_components`, `scope_to_dag`, `apply_assume_done`,
`compute_frontier`. Pure over issue-snapshot dicts (shape owned by `github`,
§ below). The `Blocked-by:` regexes are **domain grammar, not generic
parsing** — they are the serialization of DAG edges and live beside the
frontier math that consumes them. #95 deletes the grammar half in this one
file; the native-edge path (`native_blockers` keys on the snapshot) already
flows through unchanged.

**`gates.py`** — the Gate protocol (§3) plus `run_command_gate`,
`evaluate_diff_gate` (pure), `run_diff_gate`. Runs its own `git diff`
subprocess — execution is the gate's job.

**`triage.py`** — `classify_pr`, the signal enums, `TRIAGE_LABELS`.
Fail-closed posture on the three safety-critical signals is part of the
interface, stated in the docstring and pinned by the existing tests.

**`paths.py`** — `match(path, pattern)` (dir-prefix / glob / bare-basename
dispatch by pattern shape) and `hits(files, patterns)`. The only leaf util
(§6). Two callers: `triage` (sensitive/watched paths) and the diff gate
(`forbidden_paths` — unified by #94; all shipped `forbidden_paths` entries end
in `/`, where `match` is exactly `startswith`. The unification means
`forbidden_paths` *adopts* the three-form convention; a future non-slash entry
gets basename/glob semantics, documented in loop.toml when #94 lands).

**`github.py`** — `run(args)` (the one subprocess seam to `gh`),
`fetch_issues()` (REST snapshot + native-dependency enrichment),
`fetch_labels(number)`. This module owns the **issue-snapshot dict shape** —
`{number, title, state, labels, assignees, body, native_blocked_count,
native_blockers?}` — which is the `github`↔`dag` contract; `dag` never sees
`gh` output, only these dicts. Tracker *mutations* for claim/release stay in
`cli.py` composed from `github.run`: the assign-vs-label claim convention is
loop policy, not gh plumbing, and one-call-site wrappers would fail the
deletion test.

**`index_client.py`** — §5.

**`trajectory/`** — §4.

### 2.1 Deliberate non-modules

- **No `config.py`** — the issue's module list omits it on purpose; config is
  a `cli` concern (above).
- **No `git.py`** — two call sites (`gates`, `cli`) with two-line bodies each;
  a wrapper would be a pass-through (deletion test fails).
- **No `utils.py`** — §6.

## 3. The Gate protocol

A gate is a config entry (`loop.toml [[gates]]`) with a `kind`. The protocol's
structural claim: **every kind has exactly one verb, and which verb it has
states which plane runs it.**

- **Deterministic kinds** (`command`, `diff`) — the rail *executes*:
  `execute(gate_cfg, ctx) -> GateResult`, where ctx is the worktree cwd (+
  base ref for diff).
- **Judgment kinds** (`acceptance`, `review`, `simplify`) — the rail never
  executes; the orchestrator dispatches a subagent and the rail *validates*
  the subagent's return: `validate(gate_cfg, raw) -> GateResult`, rejecting
  schema-violating returns (#99's re-ask loop keys off the rejection).

`GateResult` is a plain dict shape, not a class: `{id, kind, passed, summary,
detail}` — already what both executors emit and what the trajectory
frontmatter stores; the shape is shared by both verbs so downstream consumers
(`--gates-json`, the PR evidence table) never care which plane produced it.

Structurally, `gates.py` carries one registry per verb:

```python
DETERMINISTIC = {"command": run_command_gate, "diff": run_diff_gate}
JUDGMENT = {"acceptance": validate_acceptance, "review": validate_review,
            "simplify": validate_simplify}
```

The `check` subcommand dispatches **only** through `DETERMINISTIC`; any other
kind — judgment-side or typo — gets the existing "LLM-judged — run it from
the /issue-loop command" error (previously an `else` branch; the registry
promotes it from error-message prose to structure, byte-identical output).
The `validate` subcommand (#99) dispatches **only** through `JUDGMENT`, and
the two registries are pinned disjoint + covering the shipped pipeline.

A judgment result is `GateResult` plus `reasons`, and that key carries the
whole execute-vs-validate difference: **empty `reasons` = a verdict**
(`passed: false` is a fix round), **non-empty `reasons` = the return never
became a verdict** — each entry names an offending field path and value, and
the orchestrator re-asks the same subagent. `validate` maps this onto
`check`'s exit codes with rejection on the error rung: `0` passed, `1` failed,
`2` re-ask.

*(Amended 2026-08-01, owner ruling during #94: the original draft shipped
`JUDGMENT` as a data-only set at #94 time. Both the review and simplify gates
independently flagged that as the epic's own half-mechanism shape, and the
owner sided with the anti-goal over the draft.)*

**No class hierarchy.** A dict registry + one shared result shape state the
split completely; `typing.Protocol` machinery would be interface without
behavior.

## 4. The trajectory module — one primitive, two faces

The trajectory note is one primitive with a write face and a read face, and
**one module owns both** because both faces share one vocabulary: the
frontmatter keys (`outcome`, `primed`, `served`, `builds_on`, `trace`), the
`loop-run` tag, the v1-Lessons/v2-insights duality. Split mint from prime into
sibling modules and that vocabulary needs a third home or gets duplicated —
the exact "retrieval, triage, trajectory composition share a bucket" failure
inverted. Prime is a *trajectory mechanic*: it reads what mint writes.

Public interface (re-exported by `trajectory/__init__.py`, everything else
internal):

- `build_trajectory(issue, *, branch, commits, numstat, gates, fix_rounds,
  outcome, ...) -> dict` — pure; emits the weave_create-shaped payload.
  (mint face)
- `build_prime_payload(issue_number, run_id, concepts, *, conn, holdout,
  limit, budget_chars, decisions) -> dict` — the claim-time payload.
  (prime face)
- `append_served_event(buffer_path, run_id, issue_number, served, session_id)`
  + `LOOP_PRIME_TOOL` — the served-context write-through to the session
  buffer JSONL.

Internal to `mint.py`: the trace normalizers (`_normalize_trace*`,
`_as_int_or_none`) and the skill projection — since #99 `_normalize_trace` is
a documented backstop (enforcement moved to the `validate` seam, §3) and the
skill projection is scoped to *stage dispatches*, with generic capture-all
parked in `issue-loop-memory.md`. Internal to `prime.py`:
`is_holdout` (the sha1 holdout — `build_prime_payload` computes it; moved
off the public list 2026-08-01, it had no external consumer),
`extract_lessons` + the v1 fallback branch (#98 deletes both, single-file
change), `_coerce_builds_on`, the outcome-rank table, `render_prime_block`,
and — transitionally, §5 — `query_trajectories` / `resolve_insights`.

Two interface-level invariants, stated on the module:

- **Prime never crashes the loop.** Any index problem (missing db, corrupt
  file, schema drift) degrades to `primed=false` with a `note` — the
  orchestrator dispatches unchanged. This is a contract line callers build
  on, not an implementation nicety.
- **Prime writes nothing to the index.** Its only side effect is the
  served-event append to the *session buffer* (markdown-adjacent log); the
  index stays strictly read-only (§5).

## 5. The index_client contract

`index_client.py` is the package's **single seam into the derived SQLite
index** — stdlib-only, read-only, never imports `thinkweave`.

Interface now (#94):

- `resolve_db_path(db: str | None, vault: str | None) -> str | None` — `--db`
  wins; else the vault's `weave_dir` override from `config/config.toml` /
  legacy `.weave/config.toml` (mirroring `core.config` resolution), else
  `THINKWEAVE_INDEX_DB`; `None` when nothing resolves (never guess a path).
- `open_ro(db_path) -> sqlite3.Connection` — URI `mode=ro`, `Row` factory.
- `Error = sqlite3.Error`, `Connection = sqlite3.Connection` — aliases so no
  other module ever imports `sqlite3` (cli's degrade guard now, prime's
  annotations post-#100), keeping the importer-allowlist seam tight.

End state (**#100 completes it**): every SQL string in `devloop` lives in
`index_client`; its query surface is *index-vocabulary-shaped* (tags,
concepts, FTS match, ids → bodies), returning plain dicts with frontmatter
already JSON-parsed — schema knowledge inside, domain knowledge outside.
Trajectory-domain judgment (which tag is `loop-run`, outcome ranking, color
filtering, budgeting) stays in `trajectory/prime.py`, composing over the seam.

Transitional state (#94 is move-only): `query_trajectories` and
`resolve_insights` move to `prime.py` **unchanged** — splitting their SQL from
their domain filtering is a real refactor and #94 may not do it. The seam
finishes forming at the natural redesign point: #100 rewrites the retrieval
query anyway (FTS+concept fusion), and writes the new query *in*
`index_client`, deleting prime's inline SQL as it goes. This spec is the
notice; #100's implementer should treat the SQL migration as in-scope.

Two enforcing seams (both land in #94; prose alone is banned by the epic):

1. **Schema-pin test** — builds a real fixture index by importing the
   *thinkweave indexer* (tests run in the dev env; only `devloop` itself is
   import-restricted), then exercises every devloop SQL path against it:
   `resolve_db_path` + `open_ro` + the trajectory queries (`notes`,
   `note_tags`, `note_concepts` tables and the columns they read). Indexer
   schema drift now fails this test instead of silently degrading prime to
   unprimed forever. The existing hand-built-schema tests in
   `test_issue_loop.py` remain as fast unit checks; the pin test is the
   contract's anchor because *both sides* of the seam appear in it.
2. **Importer-allowlist test** — asserts which devloop modules import
   `sqlite3`: `{index_client, trajectory.prime}` after #94, tightened to
   `{index_client}` by #100. Five lines, and the transition is enforced
   rather than remembered.

## 6. Leaf-util doctrine

The north-star bans generic utils beside key logic; the anti-goal bans
speculative structure. The reconciling rule: **a leaf util exists only when
two modules need the same semantics** (one caller = hypothetical seam; two =
real).

- `paths.py` qualifies today — `triage` and the diff gate, unified by #94.
- Nothing else does. `_split_csv` (cli), `_as_int_or_none` (mint's trace
  semantics: bool-is-not-int), `_coerce_builds_on` (prime's wikilink
  tolerance) are *domain-shaped* coercions with one home each; they stay
  module-private where used. If a later wave gives one a second
  cross-module caller with identical semantics, mint the leaf then.
- No `utils.py`, ever — a junk drawer is the "generic beside key logic"
  violation with a folder around it.

## 7. Redesign-point ledger

The boundary placement is justified by what it localizes:

| Issue | Change | Touches |
|---|---|---|
| #95 | delete body-regex DAG grammar (native deps) | `dag.py` only |
| #98 | delete v1 Lessons fallback | `trajectory/prime.py` only |
| #99 | judgment-gate validators + normalize-to-backstop | `gates.py` (introduces the `JUDGMENT` validator registry + its tests), `trajectory/mint.py` |
| #100 | prime v3 retrieval (FTS+concept via the seam) | `index_client.py`, `trajectory/prime.py` |
| #102 | rework-evidence stamps | `trajectory/mint.py` (+ orchestrator) |

Any of these needing a third module is a boundary bug — file it against this
spec.

## 8. Migration map for #94 (behavior-frozen)

Function → destination, exhaustive; everything is a pure move unless marked:

| Today (`scripts/issue_loop.py`) | Destination |
|---|---|
| `DEFAULT_CONFIG`, `load_config`, `parse_override`, `apply_overrides` | `cli.py` |
| `_HEADER_RE` … `_PARALLEL_RE`, `parse_blockers`, `parse_wave`, `parse_parallel_safe`, `all_blockers`, `compute_components`, `scope_to_dag`, `apply_assume_done`, `compute_frontier` | `dag.py` |
| `run_command_gate`, `evaluate_diff_gate`, `run_diff_gate` | `gates.py` (+ the `DETERMINISTIC` registry; `check` dispatch via registry, byte-identical output) |
| `_VALID_*`, `_RED_*`, `TRIAGE_LABELS`, `classify_pr` | `triage.py` |
| `_path_matches`, `_path_hits` | `paths.py` as `match`, `hits` (**the one unification**: diff gate's inline `startswith` loop → `paths` call) |
| `_normalize_skill`, `_as_int_or_none`, `_normalize_trace*`, `build_trajectory` | `trajectory/mint.py` |
| `is_holdout`, `_LESSONS_RE`, `extract_lessons`, `_coerce_builds_on`, `resolve_insights`, `_OUTCOME_RANK`, `_outcome_rank`, `query_trajectories`, `render_prime_block`, `build_prime_payload`, `LOOP_PRIME_TOOL`, `_append_served_event` | `trajectory/prime.py` (SQL pair migrates onward in #100) |
| `_open_index_ro`, `_read_weave_dir_override`, `_resolve_index_db` | `index_client.py` as `open_ro`, `resolve_db_path` (+ private helper) |
| `_gh`, `fetch_issues`, `_fetch_labels` | `github.py` as `run`, `fetch_issues`, `fetch_labels` |
| `_split_csv`, `build_arg_parser`, `main` | `cli.py` |

Shim: `scripts/issue_loop.py` keeps only sys.path setup + `from devloop.cli
import main` + the `__main__` block. The three test files that load the script
via `importlib.spec_from_file_location` (`test_issue_loop.py`,
`test_vault_issue_contract.py`, `test_context_served.py`) repoint their
imports at the devloop modules — mechanical, part of #94.

Acceptance restated: whole suite green; CLI surface byte-compatible (including
the judgment-kind error message and `--help` text); new tests = schema-pin,
importer-allowlist; no semantic diff beyond the paths
unification.
