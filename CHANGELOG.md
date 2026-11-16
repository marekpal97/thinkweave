# Changelog

Notable changes to ThinkWeave. Versioning is SemVer on a 0.x contract: the
`weave_*` MCP + CLI surface may still break between minors.

## [Unreleased]

## [0.4.0] — 2026-11-16

Two headlines. **Self-improvement rail:** the values and texts a person
tunes live in one policy repo (`rsi-rail`), which thinkweave reads through
seams. Every task round records which revision of that repo was in force.
Two personal skills, `/tweak` and `/debrief`, turn a correction into one
decision plus one commit in the repo that owns the text. **System 1
decisions:** a decision provider answers small typed questions with
probabilities. It gates prompt-time retrieval, judges after the fact which
served notes the work relied on, and judges duplicates in acquisition and
in the ontology gate. Every call lands in a `verdicts` ledger.

Both are early versions. Without an rsi-rail repo, and with every question
set off (the default), thinkweave behaves as in 0.3.1 except where
"Changed" says otherwise.

### Upgrade notes

- Run `weave doctor --migrate` once. It backfills `source_session` on
  decisions and the full feedback text on task rounds. The task-ledger
  step now rewrites only notes that are out of shape, so the run takes
  minutes, not hours.
- Dev-link machines: run `uv run weave dev-link` once after pulling (it is
  idempotent) and restart Claude Code. Hand-written cron lines that call a
  bare skill (`-p "/dream"`) must become `-p "/thinkweave:dream"`; the
  `weave schedule` block already renders the namespaced form.
- Settings-file hook route: rerun `weave hooks install`. It adds the
  `Skill` matcher on PostToolUse and the `PreCompact` event. The plugin
  route needs nothing.
- The OpenAI path for news triage is removed. `/drain` now calls
  `weave decide triage`. With the `news_triage` question set off, every
  item goes to the `news-triage-worker` subagent, so no key is needed.
- To use the rail, set the user-scope `rsi_rail` key or
  `THINKWEAVE_RSI_RAIL` to the repo's path, then run `weave hooks install`
  to link its skills and rules into each harness. `weave doctor --mcp`
  reports dangling links and hook drift.
- To use System 1, add a backend under `decisions.backends` in `api.yaml`
  and enable a question set under `[verdicts.<question_set>]` in the
  rail's `thinkweave/config.toml`. `weave decide smoke` makes one
  end-to-end call.

### Added

- **Config seam to the policy repo** (#359, PR #362): `load_config` merges
  `<rsi-rail>/thinkweave/config.toml` over the vault's `config.toml`. Only
  keys read on the serving chain are accepted; any other key is refused by
  name. Knowledge files (`sources.yaml`, `PRIORITIES.yaml`, `api.yaml`,
  the ontology) stay in the vault.
- **Round manifest** (#344, PRs #362, #378, #408): every task round
  records the rsi-rail commit and dirty flag, the harness, the reader
  versions, the project repo's commit, and the hook and question-set
  values in force. The event buffer gains rows for `Skill` calls,
  prompt-declared skills (Codex, Pi, OpenCode), subagent start and stop,
  and nudges.
- **Install from the policy repo** (#360, PRs #362, #367, #386):
  `weave hooks install` links the repo's skills, rules file, agents and
  output styles into every harness profile, and renders the repo's
  `hooks/hooks.toml` (enabled events, matchers, foreign hook scripts) per
  profile. `weave doctor --mcp` gains the `rsi-rail links` and
  `rsi-rail hooks` rows.
- **`weave policy inventory`** (PR #385): lists every Config knob with its
  default, its TOML address and the events that read it, as `--toml`,
  `--md` or `--json`. The rail's allowlist derives from the same
  declaration, so no hand-kept list exists.
- **Addressable SessionStart payload** (#352, PR #385): `[context.startup]`
  and `[context.startup.<section>]` set the section list, the drop order,
  per-section caps and the payload budget. One plan feeds the SessionStart
  hook, `weave_project_snapshot` and `weave project`.
- **`[hooks]` knobs and the Stop nudge** (#345, PRs #387, #393): per-event
  budgets, the inline context cap and `hooks.stop.unverified_claim_nudge`.
  The nudge (off by default) blocks a Stop whose last message claims a
  verification when no test command ran in that turn.
- **Serving-chain record** (#353, PR #387): the startup row carries
  `bytes`, the prompt-time rows carry `bytes` and `cap`, and `PreCompact`
  is a canonical event that writes a `compact` row on Claude Code.
- **`weave task feedback` and `weave task rounds`** (PRs #362, #377,
  #392): the first pins a correction or confirmation to a prompt
  mid-session, including prompts from earlier turns. The second reads
  rounds across tasks with their manifest, feedback, skills, nudges, tool
  errors and decisions, and names every join that found nothing.
- **Decision provider and the `verdicts` ledger** (#379, PR #389):
  `core/decision_provider.py` exposes `decide(state, questions)` with
  `noul` and `choice` answers. Each call is a `verdict` event that the
  indexer projects into the `verdicts` table. The rail can override a
  question set's text in `thinkweave/questions/<question_set>.toml`.
- **Prompt-time gate** (#380, PR #389): `prompt_gate` scores a pool of 30
  candidates (BM25 and similarity, fused by RRF) and serves those at or
  above 0.7. On an error, a 429 or a missed deadline it serves the ungated
  top of the pool and writes a `verdict_miss`.
- **Ex-post judge** (#381, PR #389): `wrap-finalize` asks, per closed
  turn, whether the work relied on each served note.
  `weave decide sweep` covers sessions no wrap judged, and
  `weave decide readout` reports calls, misses, latency, cost and
  calibration per question set.
- **Duplicate judgment** (#382, PR #394): `acquisition_dedup` drops a
  queued item judged a duplicate of an existing source. `concept_guard`
  rewrites a confident synonym to its canonical concept at `weave_create`,
  `weave_update` and extraction, and attaches a hint to a likely one.
- **Batch question sets** (#383, PR #394): `weave decide
  backfill-citations` appends a `cites:` line to decisions that cite no
  note. `weave decide triage` decides news items and sends only the
  uncertain band to the Haiku worker.
- **Replayable rounds** (#297, #324, #325, PRs #328, #349, #350): a round
  keeps its full feedback text and its base commit, on all three routes.
  `weave doctor --migrate` backfills both the feedback text and decision
  `source_session` for existing notes.
- **Citations at wrap** (#224, PR #347): `wrap-finalize` warns on each new
  decision whose body cites no note id, and offers the notes served to
  that session as ready `[[path|id]]` links.
- **Judge inputs** (PR #393): `weave judge` worklist items carry `applied`
  and the history of decisions on the same paths.

### Changed

- **SessionStart payload fits the harness cap** (#317, PR #333): each
  profile declares its inline context cap. The cue sections lead and are
  capped, not dropped whole. The tools manifest and the STATE excerpt
  leave the payload; `weave_project_snapshot` still serves them.
- **A declared `done` needs committed files** (#316, PR #326): the wrap
  task pass closes a task only when every file it recorded is committed.
  Otherwise the round lands, the task stays open, and the report names the
  files.
- Stop writes a `turn_end` event on every harness, and the Pi and OpenCode
  `UserPromptSubmit` budget rises from 2.5 s to 6 s (PR #389).
- **devloop texts and settings move to the policy repo** (#346, #395,
  PRs #378, #384, #408): the loop's pack texts and the person's
  `loop.toml` settings live in the rail's `devloop/`. `docs/agents/loop.toml`
  keeps repo facts only. The `ship` text (PR body, findings comment,
  report) is served by `devloop text ship` and can be overridden there.

### Fixed

- **One route per machine** (#254): on a dev machine with `weave dev-link`,
  thinkweave loaded twice as an MCP server (`mcp__thinkweave__*` from the
  committed `.mcp.json`, `mcp__plugin_thinkweave_thinkweave__*` from the
  plugin manifest) and twice as skills (bare `.claude/commands/` links beside
  the plugin's `/thinkweave:*`). `weave dev-link` now rejects the checkout's
  `.mcp.json` for Claude Code (`disabledMcpjsonServers` in
  `.claude/settings.local.json`) and removes the bare-name links;
  `weave dev-unlink` re-approves the file. `weave doctor --mcp` fails
  `registration scopes` when a plugin manifest and a bare-name scope both
  register the server, and a new `skill routes` row fails on bare-name links
  or a top-level `skills/` beside the plugin.
- **Worker tool allowlists on the plugin route** (#254): every `agents/*.md`
  now lists the plugin-scoped tool names beside the bare ones. Claude Code
  never rewrites `mcp__thinkweave__weave_*` to the plugin server's
  `mcp__plugin_thinkweave_thinkweave__weave_*`, so on a plugin-only install
  the dream and research workers launched without any `weave_*` tool.

- Prompt-time retrieval stays inside the budget the harness really waits
  (#329, PR #330). On Pi the shim killed the hook at 2.5 s, below the 4 s
  embedding deadline, so the miss was never recorded.
- A decision is stamped with the session note's own `ses-` id as
  `source_session`, not the harness uuid (#223, PR #331).
- Every hook and MCP launch runs the interpreter with `-P`, so a module in
  the project folder cannot shadow a stdlib module (#307, PR #332).
- Hook-side index writes retry on `database is locked`, and a nested list
  inside `proposed_concepts` no longer stops the indexer (#280, PR #327).
- `weave doctor --migrate` ran for over an hour on a large vault, because
  the task-ledger step saved and reindexed every task note (PR #348).
- A mid-session `/wrap` kept the round's manifest but lost its `Skill` and
  nudge counts. Rail hooks that fail now say so in `hooks.log` (PR #377).
- A failed or slow `git status` raises when the manifest is built. It
  never reads as a clean repo (PR #408).

### Internal

- The devloop pin moved four times, to the funloops commit that carries
  `devloop text` (funloops #114 to #117). The boundary tests in
  `tests/test_devloop_boundaries.py` stay green at each pin.

## [0.3.1] — 2026-10-12

OpenCode joins Claude Code, Codex and Pi as a fully supported harness:
passive capture, `/wrap`, transcript import and task binding, all
observed live on OpenCode 1.19.0. The same testing found capture gaps
that affected every harness: chained and quiet commits, `gh pr` calls,
and retrievals through Claude Code's plugin route.

### Upgrade notes

- OpenCode users: run `weave install --harness opencode`, then
  `weave hooks install --harness opencode --scope user`. Dispatch
  `opencode run` with stdin from `/dev/null`, or it hangs.
- Claude Code and Codex users on the settings-file hook route: rerun
  `weave hooks install` to pick up the new PostToolUse matcher. The old
  entry is rewritten in place. The plugin route needs nothing.

### Added

- **OpenCode at Pi parity (E3)** (#195): `shims/opencode/` maps
  OpenCode's plugin hooks onto the canonical events. It synthesises
  SessionStart, injects context through the system-prompt transform, and
  maps `chat.message`, `tool.execute.after` and `session.idle`. Its
  `shell.env` hook exports `OPENCODE_SESSION_ID` and `THINKWEAVE_HARNESS`,
  so `weave session-id` and `/wrap` resolve the live session. Worker-free
  commands link into `~/.config/opencode/commands/` (`/wrap`, `/brief`, …).
  Every mapped event was observed firing on OpenCode 1.19.0. Install with
  `weave install --harness opencode` and
  `weave hooks install --harness opencode --scope user`.
- **`weave import opencode`** reads OpenCode's SQLite store (read-only),
  and `opencode_digest` reads a bound worker session for task closes.
- `weave install --harness opencode` hides the Codex `thinkweave-*`
  skill projections through `permission.skill` in `opencode.json`.
  OpenCode scans `~/.claude/skills` recursively, so a `weave dev-link`
  checkout exposed them beside `/wrap`. A rule you set yourself is kept.

### Fixed

- `weave_create` accepts `concepts` / `proposed_concepts` as top-level
  arguments. They were dropped silently before. The reply now reports the
  concepts that landed, warns when a note has none, and names ignored
  arguments.
- MCP dispatch decodes JSON-encoded strings where a tool's schema wants
  an array or object. Some models send `concepts="[\"a\"]"`.
- The PostToolUse hook dropped every chained commit
  (`git add … && git commit …`) on every harness: the significance gate
  read only the first segment.
- A commit made with `git commit -q` or `-F -` lost its hash, because
  the parser reads the `[branch hash]` line those flags suppress. The
  parser also recorded `git push`'s `[new branch]` line as the commit
  message. The hook now falls back to HEAD when HEAD is under two minutes
  old.
- Mutating `gh pr` calls (create, merge, close, …) are captured. The
  session note gains `pull_requests` with the action, number and URL.
- Retrieval logging missed every call through Claude Code's plugin route
  (`mcp__plugin_thinkweave_thinkweave__…`). The PostToolUse matcher
  covers both names, and a reinstall rewrites the old matcher in place.

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
