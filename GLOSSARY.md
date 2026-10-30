# Thinkweave

The language of thinkweave's self-improvement rail: how a session's settings are named, held, recorded, compared and changed. Vault-wide concepts stay in the ontology; this file holds the repo's own terms and joins each to the ontology on its `_Concepts_` line.

## Language

**Knob**:
One named value a session or run reads at start, such as a context budget, a gate switch or a model for a role. A thinkweave knob is policy only when it is read on the serving chain (SessionStart, UserPromptSubmit, a `weave_*` call, `/wrap`); vault maintenance and machine keys stay in the vault config.
_Avoid_: lever, setting, parameter, tunable
_Concepts_: agent-harness, context-engineering

**Policy**:
Everything the user tunes that shapes how a session or run behaves, held in one private git repo, rsi-rail (`~/python_projects/rsi-rail`), one folder per reader. It holds the serving-chain knobs in `thinkweave/config.toml` and the user-scope texts under `rules/`, `skills/` and, after #346, `devloop/`. The policy in force is the repo's commit; a framework carries no policy of its own. Claude Code's `settings.json` is witnessed, not held.
_Avoid_: posture, overlay, harness-policy, profile, assigned half, committed half
_Concepts_: agent-harness, version-control

**Seam**:
A code path in thinkweave or devloop that reads a policy value or text from the rsi-rail repo instead of carrying a constant. A framework change may add a seam with its behaviour default off; it never changes what another user gets.
_Avoid_: hook point, override, plugin point, adapter
_Concepts_: agent-harness, harness-adapter

**Round**:
One stint of work on a task: a wrapped session, a child dispatch, or a devloop run. The record that carries the manifest, the events of the work and the outcomes.
_Avoid_: attempt, iteration, cycle
_Concepts_: task-trajectory

**Witness**:
The act of recording on a round which policy revision and which harness settings it ran under. "In force" is a commit comparison against the witnessed commit, never a flag.
_Avoid_: stamp, snapshot, fingerprint, audit
_Concepts_: task-trajectory, observability

**Manifest**:
The witness record on a task round: `{harness, readers, rsi_rail: {commit, dirty}, harness_settings}`. `readers` names each reader's version (thinkweave commit, funloops pin, plugin versions); `harness_settings` carries the `settings.json` values the repo cannot hold.
_Avoid_: lockfile, fingerprint, stamp, environment
_Concepts_: task-trajectory, experiment-management

**Resolved policy**:
What a round ran under: the rsi-rail commit its manifest names, read at that commit, plus the reader versions beside it. Derived from the manifest, never stored as separate knob values.
_Avoid_: lockfile, fingerprint, snapshot, environment
_Concepts_: agent-harness, experiment-management

**Rail**:
A deterministic code path with a CLI verb and no model turns. The self-improvement rail is the closed path event, door, proposal, commit, manifest, judge, supersede, built from existing primitives.
_Avoid_: pipeline, framework, flywheel
_Concepts_: recursive-self-improvement, agent-harness

**Proposal**:
A decision whose `file_paths` name its target in the rsi-rail repo and whose predicted outcome is the test, applied as one repo commit that cites it. Approval is the user's yes in session, nothing else.
_Avoid_: hardening issue, promotion, suggestion, tracker issue
_Concepts_: recursive-self-improvement, human-in-the-loop

**Applied**:
The key on a proposal decision recording `{repo, commit}` of the policy commit it produced; the only enforced join between a tweak and its commit. It points and marks; it triggers nothing. The `Decision:` trailer `/tweak` writes in the commit message is a readability convention, not enforced.
_Avoid_: commit-msg hook, trailer join, applied flag
_Concepts_: version-control, architectural-decisions

**Apply mode**:
The field on a proposal that says whether approval flips the default at once (`now`, the default) or alternates it against baseline first (`trial`). Trial is for a value that never ran live.
_Avoid_: trial flag, rollout mode
_Concepts_: experiment-management, human-in-the-loop

**Tweak**:
A proposal applied: one commit in the rsi-rail repo changing a knob or a text, after which the decision gains `applied` and moves to accepted. Judged later by the prediction judge against its predicted outcome; a later decision that supersedes it is the revert.
_Avoid_: experiment, update, hotfix, adjustment
_Note_: every applied tweak is one rsi-rail commit; a framework PR only ever adds a seam
_Concepts_: recursive-self-improvement, human-in-the-loop

**Skill**:
A SKILL.md or command file a harness loads; thinkweave's live under `commands/`, personal ones under `skills/` in the rsi-rail repo, linked into each harness profile. A personal skill is policy and changes by a tweak. Devloop's `skills[]` field is its stage log, not a skill.
_Avoid_: stage, skill knob, prompt (for a skill file)
_Concepts_: claude-code-skills, prompt-engineering

**Ledger**:
The per-skill outcome record derived from rounds: invocations, fix rounds attributed by the devloop stage log, corrections in the same session. Read by the diagnosis body behind `/tweak` and `/retro` and by the prediction judge, never maintained by hand.
_Avoid_: scoreboard, skill metrics
_Concepts_: task-trajectory, model-evaluation
