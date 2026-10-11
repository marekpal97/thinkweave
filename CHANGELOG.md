# Changelog

Notable changes to ThinkWeave. Versioning is SemVer on a 0.x contract: the
`weave_*` MCP + CLI surface may still break between minors.

## [0.3.0] — 2026-10-11

Two headlines. **Harness profiles:** Claude Code, Codex and Pi are each
one declarative `HarnessProfile`, with conformance tests over every row
and a shared TypeScript shim runtime. A new harness is now a new
profile, not a fork. **Task trace:** one durable `kind: task` note
follows a unit of work across in-session subagents, `/wrap`, and devloop
runs. The trace is verified end to end by the task-360 evaluation suite
on real Claude and Pi sessions.

### Upgrade notes

- Run `weave doctor --migrate` once. It folds stored task asks to
  `github:owner/repo#N` refs, rejoins split task notes, and upgrades
  pre-#260 devloop rounds.
- The Codex skill bundle moved from `skills/` to `codex/skills/` (PR
  #276). Re-point hand-made links in `~/.codex/skills/` to
  `<repo>/codex/skills/thinkweave-*`.
- Pi needs the `pi-mcp-adapter` extension for MCP
  (`pi install npm:pi-mcp-adapter`). `weave doctor --mcp --harness pi`
  checks for it.

### Added

- **`HarnessProfile` contract** (#191, PR #201): Claude Code, Codex, Pi
  and OpenCode are profile data. A parametrised conformance suite and an
  AST guard against per-harness forks enforce it. `docs/HARNESSES.md`
  generates its capability matrix from the profiles, and install output
  names every degradation.
- **shim-core** (#194, PR #201): a shared TypeScript translator runtime
  for harnesses that expose lifecycle events but not Claude Code hooks.
- **Pi at E3 (captured)** (#114, PRs #207, #211): the Pi shim captures
  prompts, tool calls and stops, and injects SessionStart context.
  `weave import pi` imports Pi transcripts. MCP routes through
  `pi-mcp-adapter`, and skills link into `~/.pi/agent/skills`.
- **Task trace** (#184 stack, PRs #222, #227, #229, #240, #260, #261,
  #263, #277, #301): the `kind: task` note contract; live subagent
  boundary capture; `weave task open|close|render`; a task note as a
  ledger of rounds; a task pass in the `/wrap` finalize tail;
  devloop rounds that land on the epic's task; `child_of` edges; and
  an Open Tasks section in SessionStart. The outcome-judge closes tasks
  whose tracker issue closed, and files PR review comments as
  `feedback_for` notes.
- **task-360 evaluation suite** (PRs #261, #264, #267, #272, #301):
  `evals/task360/` drives named scenarios through Claude Code, Codex and
  Pi in a throwaway vault. An oracle scores each scenario from the
  index.
- **`/explain`** (#298, PR #300): composes a session or diff explainer
  into one vault note. `weave explain locate|check` finds and validates
  these notes.
- Session notes record their harness and model (#285, PR #301).

### Changed

- **Leaner Claude Code context** (PR #276): Claude Code no longer lists
  each skill twice, and SessionStart drops placeholder sections.
  `weave_project_snapshot` still serves the full payload.
- **Index-backed retrieval** (PR #206): timelines, prompts and landings
  query the index instead of scanning the vault. `weave timeline` drops
  from over 60 s to about 0.04 s.
- Plugin-route launchers bootstrap their dependencies once (#164, PR
  #201).

### Fixed

- **`/wrap` fails loudly** (PR #278): a dropped `weave_create`, a no-op
  `weave_extract`, or an empty finalize now reports an error instead of
  exit 0.
- **Codex capture fidelity** (PRs #208, #212): PostToolUse events land,
  one session maps to one note, and guardian sidecars are filtered out.
- Wrap and judge resolve session notes by exact id and the harness
  session id (PRs #209, #214, #233).
- shim-core builds and its test suite runs again: `npm test` passes a
  file glob on Node 24, and the subagent hooks have timeout budgets
  (PR #213).

### Internal

- The devloop pin follows funloops main, and `loop.toml` uses the
  four-gate pipeline (PRs #216, #242, #260).

## [0.2.1] — 2026-08-31

The release headline: **one logical conversation, however many session
UUIDs the harness spends on it.** Compaction, resume, clear and replay
each mint a new UUID, but the hooks, the wrap verdict join, and
`context_served` all keyed on a single one — the logical-session chain
(#183 stack, PR #203) fixes all three keying bugs, and reverses a v0.2.0
behavior along the way: SessionStart context is now served **once** per
session, never re-injected on resume/compact. `/brief`, `/learn`, and
`weave health` ride along (feature work on a patch release — a deliberate
call on the 0.x contract; the v0.3 milestone continues separately).

### Changed

- **Serve-once SessionStart** (#175, PR #203): `startup`/`clear` serve the
  full snapshot exactly once — replay identity survives double
  registration and buffer archival; `resume`/`compact` inject nothing,
  recording a visible `skipped_lifecycle` event. Resume replays the
  transcript, and post-compaction retrieval is on-demand via the `weave_*`
  surface. Reverses v0.2.0's "resume/compact always re-inject" (#161):
  that fix ended silent holes, this one ends the ~25k-char snapshot
  re-delivery those injections cost — the biggest daily token waste in the
  system — by deletion (−585 lines), not by delta.
- **Acquisition lanes tightened** (#192, PR #192): event lanes gain
  freshness windows (news 7d, podcast/youtube events 14d) and the Haiku
  stage-1 funnel extends to podcast + YouTube; the repo queue actually
  dedups (`url` joined `dedup_keys`); `feed_errors_detail` names the
  failing feed; daily `/update-hubs` in cron no longer stalls on a
  question.

### Added

- **`weave health`** (#120, PR #193): deterministic system-health
  collector — cron jobs joined to run evidence, per-lane queue depth,
  drain-backlog advisories (never the exit code), hook errors, digest
  freshness; `--json` is `/brief`'s contract.
- **`/brief`** (#170, PR #193): daily orientation over the nightly
  digests — a watermarked deterministic collect (health, timeline,
  per-lane landings with bound cron feeders and weakest-link state,
  three-layer focus merge, attention, catalysts) rendered exactly per its
  render_plan.
- **`/learn`** (#171, PR #193): vault-grounded tutor — one retrieval
  partitioned into the user's own trajectory vs world material, test-first
  on revisit, a learn-note validator, and unanswered questions feeding the
  probe rail.
- **The `logical_session` chain primitive** (#180, PR #203): hooks stamp
  each segment note from the transcript's bridge-session row; wraps
  record a durable `segments:` union on the primary note; the one
  chain-membership rule (shared key or `segments:` match, minus siblings
  a real wrap already labeled) lives on `core.vault.is_chain_sibling`,
  called by both the verdict join and the RLVR exposure union.

### Fixed

- **Wrap verdict rail** (#181, PR #203): one verdict labels exactly one
  prompt (identical-text collapse, exact match over prefix, per-channel
  label separation so re-wraps are a fixed point); events-file resolution
  matches both session identities and ranks prompt-bearing archives over
  recreated live buffers; prune can no longer GC the folder a wrap just
  wrote verdicts into; the join spans the whole compaction-segment chain.
- **The probe rail actually flows** (#192): wrap catch-up sees live
  buffers, not just archived ones (645 had piled up invisible); prompt
  labels reach the index (archive-before-index + explicit reprojection —
  the priority worker had been consuming pre-#101 heuristic fossils);
  the labeler's probe bar rewritten around retention.
- **`bin/weave` no longer re-syncs (and prunes) the venv on every call**
  (#164 straggler, PR #192): the silent `--extra mcp` sync uninstalled
  the other extras — the bug that killed the news pull for 11 days;
  `weave doctor --mcp` gains a venv-extras check, and the check itself
  survives absent parent packages (PR #203 pre-slice).

### Internal

- Four zero-risk deletions from the purity sweep (#15, PR #197);
  dev-link upgrade docs standardize on `uv sync --extra all` (PR #182).

## [0.2.0] — 2026-08-20

The release headline: **ThinkWeave slims to the memory layer.** The
issue-to-PR dev loop that was built and matured inside this repo has been
carved out to [funloops](https://github.com/marekpal97/funloops) as the
`devloop` package; ThinkWeave now *consumes* it as a git-pinned dev
dependency and keeps only the host surfaces (loop config, memory feed,
vault write-back). Alongside the slimming, Codex lands as the second
supported harness.

### Changed

- **Devloop carve-out** (#151, S1–S4; boundary spec #93/#94): the
  `/issue-loop` deterministic rail moved to funloops. ThinkWeave keeps
  `docs/agents/loop.toml`, the byte-compatible `scripts/issue_loop.py` shim,
  and the vault↔issue contract; the cross-repo seam (pin, shim, indexer
  schema) is enforced by `tests/test_devloop_boundaries.py`. Along the way
  the loop gained native GitHub issue dependencies as its only DAG
  representation (#95), schema-enforced gate outputs (#99), v2 trajectory
  notes with linked insights (#98), prime v3 FTS+concept fusion (#100), the
  ponytail dispatch persona (#89) and stack-tip simplify pass (#90) — all
  now living upstream.
- **Semantic retrieval fails loud** (#134, PR #167): `mode='similar'` and
  the semantic leg of `hybrid` raise `SemanticSearchUnavailable` with an
  actionable remedy instead of silently degrading to FTS-only results. TLS
  trust failures get their own diagnostic (`EmbeddingCertificateError`).
  An empty result now unambiguously means "ran and found nothing."
- **Prompt register: hooks capture, never judge** (#101, PR #163): the
  inline feedback lexicon and probe heuristic are deleted. Raw prompts are
  the only hook-time substrate; classification is one LLM pass at `/wrap`
  (`weave wrap-finalize --verdicts`), producing grounded feedback/probe
  events in the frozen RLVR schema. Read seams collapse multi-registration
  echoes.
- **Steering reads three observable signals** (#96): the hub-pressure
  signal is gone; behavioral evidence only.

### Added

- **Codex as a second harness** (epic #103): the `HarnessProfile` seam with
  Codex install + hooks adapters — MCP registration, passive session
  capture, AGENTS.md guidance block, headless argv (#104/#106/#107, PR
  #146); `weave import codex` for `~/.codex/sessions` rollout JSONL (#108);
  the full skill surface projected from canonical commands — 28 skills with
  explicitly declared worker contracts and metadata sidecars, drift-tested
  against their sources (#111, PRs #159/#168); Codex onboarding requires
  working semantic retrieval (#134).
- **Native-Windows support**: install hardening (session paths, console
  encoding, headless cron; PR #8), runtime launchers that never sync the
  live venv (PR #156), correct user-scope config escaping (PR #157), a
  fully green test suite on Windows (PR #158), and a sandbox-safe Codex
  bootstrap — `weave.cmd` launcher, persisted+broadcast user PATH, doctor
  checks with PATH-free remedies (#164, PR #165).
- **Prompt-time retrieval (R2)** actually fires on `UserPromptSubmit` —
  bounded, deduped, deadline-capped context injection (PR #10).
- **Importer commons** (#27, PR #42): shared `build_source_frontmatter`,
  tolerant/atomic `ImportManifest`, one indexing policy across importers.
- **Operational hardening** (epic #53, PR #73; PRs #48/#68): cron rails
  survive headless-mode failures loudly — expanded PATH, canonical flock,
  judge-worker CLI fallback; `weave doctor --mcp` grew checks for plugin
  manifests (PR #155) and dangling command symlinks (#172, PR #173).

### Fixed

- Hook lifecycle is single-owner and replay-safe (#161, PR #166):
  multi-registration (plugin + stale settings entries) wrote every buffer
  event 2–3×. The plugin now owns registration and the installer sweeps
  stale entries across scopes; buffer archiving is idempotent and preserves
  the buffer on failure; session resume/compact always re-inject context.
- `batch_completions` bounds task *admission*, not just active calls — a
  512-prompt batch at concurrency 20 holds 20 tasks, not 512 (#176, the
  first externally-reported bug; PR #178).
- `/discover`'s focus_research strategy no longer orphans items in a dead
  research queue (PR #49); `Queue.items_since()` closes the archive leak
  (#26, PR #38).
- Worktree-prefixed project names (`mp-`/`qdi-`) normalize onto the shared
  project instead of minting duplicates (PR #154).
- Theme-merge stale-read bug caught by the CLI vertical-slice coverage
  (PR #9); config-scoped ontology reads in concept promotion (PR #78).

### Internal

- CI: ruff pinned (0.15.10) and required; perf-marked tests deselected on
  shared runners; `vault_factory` builder fixture; package-edge contract
  test with a shrink-only baseline (PRs #40/#41/#69/#74/#75/#76).

## [0.1.1] — 2026-06-21

Native-Windows install fixes (session-dir `:` paths, cp1252 console
encoding, onboarding uv/XDG paths), PyPI publication metadata and wheel
packaging fix, docs for the CLI install route (PRs #6/#7/#8).

## [0.1.0] — 2026-06-18

First shipped release: the Obsidian-native memory layer — markdown as
source of truth over a derived SQLite index, 17 `weave_*` MCP tools, the
session/wrap lifecycle, concept ontology with strict gating, themes and
concept hubs, the acquisition spine (discover→drain), and the nightly
`/dream` orchestrator. Distributed via marketplace self-host + community
plugin.
