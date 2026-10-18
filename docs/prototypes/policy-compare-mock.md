# Mock: `weave policy compare` read-out on real rounds (#339)

**Prototype (HITL): a mock for reaction, not a landing.** This file mocks the two modes of the future `weave policy compare` read-out (dec-87662eda, amendment 1): **before/after** (`--knob <address>`, rounds sliced by the knob's resolved value as recorded on the round) and **paired** (`A B` over alternated rounds, only for proposals with `apply: trial`, dec-436d4ced). Data window: every round in the vault's derived index, 2026-09-23 to 2026-10-18 (task notes begin 2026-09-23; nothing older exists). Rounds found by grain: **67 interactive-session rounds** across 47 sessions (work-grain task notes, `route: session`), **1,460 child-dispatch rounds** (per-dispatch task notes; 1,203 carry `tool_errors` and `cost.duration`), **24 devloop-run rounds** (`route: devloop`) joined to **52 devloop trajectory notes** carrying PR verdicts. Two gaps the tables cannot hide: (1) **no round records a resolved knob value yet** (#344 lands that), so the before/after slice uses a stand-in, the round date split at the `v0.3.0` tag (2026-10-11), i.e. pre-v0.3.0 vs v0.3.0+; (2) **no round carries full feedback `text`** (#297 landed 2026-10-18, backfill #324 open): all 58 feedback entries (35 corrections, 19 confirmations, 4 probes) have a 120-character `prompt_ref` and no `text`, so every correction quote below is the truncated `prompt_ref`. Count with full text: 0. Count with truncated text: 58. The knob under compare, `hooks.stop.unverified_claim_nudge`, does not exist in code yet (#345), so **every number is the no-treatment baseline**; the arm labels in mode 2 are a sham assignment, which is what makes the paired table useful here: it shows the noise floor.

## 1. Pairing rule

- **Interactive sessions**: within a project, order sessions by SessionStart time; odd sessions take arm A, even sessions arm B. A session with several tasks (ses-be8bd6ea holds four) is one unit. Child dispatches inherit the parent session's arm.
- **Devloop runs**: order issues by first run time; odd issues take A, even B. A re-run of the same issue inherits its arm (four issues below ran twice).
- The pair is the consecutive (A, B) within the same project or issue stream; a trailing unpaired unit counts in the per-arm mean but not in the paired difference.

Resulting assignment on the real rounds from 2026-10-10 onward (earlier rounds omitted for length; the rule is the same). Same-day order used the task-note timestamp, a stand-in for SessionStart time. `baseline` = A, `nudge-on` = B.

| Round (session / issue) | Grain | Project or issue | Date | Arm |
|---|---|---|---|---|
| ses-bfdce624 (tsk-4091b425 +4) | interactive session | funloops | 10-10 | baseline |
| ses-c46c5bae (tsk-507e92e9) | interactive session | python_projects | 10-10 | baseline |
| ses-36aed053 (tsk-48f239f6) | interactive session | thinkweave | 10-10 | baseline |
| ses-0b564ee3 (tsk-48f239f6, tsk-5749620a, tsk-bc5d642b) | interactive session | thinkweave | 10-10 | nudge-on |
| ses-f6508c1f (tsk-5749620a) | interactive session | claude_mods | 10-10 | baseline |
| ses-1a671b06 (tsk-8abd7e46) | interactive session | thinkweave | 10-11 | baseline |
| ses-e7a7e9c7 (tsk-8abd7e46) | interactive session | thinkweave | 10-11 | nudge-on |
| ses-86b6fe8f (tsk-6ca406db, tsk-cae80ee3) | interactive session | thinkweave | 10-11 | baseline |
| ses-ce31c053 (tsk-0ef95611) | interactive session | thinkweave | 10-13 | nudge-on |
| ses-be8bd6ea (tsk-c8ab7afd +3) | interactive session | thinkweave | 10-13 | baseline |
| ses-703e8079 (tsk-c8ab7afd) | interactive session | thinkweave | 10-13 | nudge-on |
| ses-946bd115 (tsk-c8ab7afd) | interactive session | thinkweave | 10-13 | baseline |
| ses-58e8b70c (tsk-590cf517) | interactive session | thinkweave | 10-13 | nudge-on |
| ses-b171c2af (tsk-590cf517, tsk-83e526cf) | interactive session | thinkweave | 10-13 | baseline |
| ses-7466c56b (tsk-dbb5426c) | interactive session | research | 10-15 | baseline |
| ses-843b1c96 (tsk-373f9266) | interactive session | python_projects | 10-15 | nudge-on |
| ses-747dd66b (tsk-0b6a03e7) | interactive session | claude_mods | 10-15 | nudge-on |
| funloops#109 (tsk-0ab32168, n-f71d19ad) | devloop run | funloops#109 | 10-10 | baseline |
| thinkweave#273 (tsk-4091b425, n-92ea1cb8) | devloop run | thinkweave#273 | 10-10 | nudge-on |
| thinkweave#228 (tsk-4091b425, n-3803c545) | devloop run | thinkweave#228 | 10-10 | baseline |
| thinkweave#274 (tsk-4091b425, n-9776dafa) | devloop run | thinkweave#274 | 10-10 | nudge-on |
| thinkweave#204 (tsk-4091b425, n-7da193c6) | devloop run | thinkweave#204 | 10-10 | baseline |
| thinkweave#174 (tsk-4091b425, n-9cd5db4c) | devloop run | thinkweave#174 | 10-10 | nudge-on |
| thinkweave#298 (tsk-8abd7e46, n-39f34c1e) | devloop run | thinkweave#298 | 10-11 | baseline |
| thinkweave#316 (tsk-e514c9f5, n-c430fe0b) | devloop run | thinkweave#316 | 10-15 | nudge-on |
| thinkweave#280 (tsk-6ca406db, n-e8b09659) | devloop run | thinkweave#280 | 10-15 | baseline |
| thinkweave#223 (tsk-208ee481, 2 runs) | devloop run | thinkweave#223 | 10-15 | nudge-on |
| thinkweave#224 (tsk-eb5f2e55, 2 runs) | devloop run | thinkweave#224 | 10-15 | baseline |
| thinkweave#307 (tsk-6c78000f, 2 runs) | devloop run | thinkweave#307 | 10-15 | nudge-on |
| thinkweave#317 (tsk-5ed11790, 2 runs) | devloop run | thinkweave#317 | 10-15 | baseline |
| thinkweave#297 (tsk-f26fbe0b, n-2707f1af) | devloop run | thinkweave#297 | 10-15 | nudge-on |

17 interactive sessions (10 A, 7 B, 7 within-project pairs); 14 devloop issues (7 A, 7 B, 7 pairs).

## 2. Mode 1, before/after: `weave policy compare --knob hooks.stop.unverified_claim_nudge`

Slice stand-in (the knob is unrecorded until #344, and unbuilt until #345): round date `< 2026-10-11` (pre-v0.3.0) vs `>= 2026-10-11` (v0.3.0+). A live run would print the recorded value (`off` / `on`) in the slice header instead of a date. Difference = after minus before; SE = root of the sum of squared slice SEs. All numbers real; nothing in this table is treated, so the differences are drift plus a harness change (11 of 19 implementer stages on 10-15 ran on `pi` + `openrouter/qwen/qwen3.8-omni-flash`, the rest on `claude-code`).

| Outcome | n (before / after) | Mean before | Mean after | Diff | SE | Correction texts, before (`prompt_ref`, truncated) | Correction texts, after |
|---|---|---|---|---|---|---|---|
| Corrections per session | 35 / 12 sessions | 0.60 | 1.17 | +0.57 | 0.41 | tsk-e1b440af: "dude this makes 0 sense. Demo doesnt need to write an issue…"; tsk-1eb4a03d: "this is absolutely ridiculous and inexcusable. I left this run to deliberately be autonomous overnight."; tsk-2844d05e: "No, that's wrong — the default should come from the USER environment variable…" (21 total) | tsk-590cf517: "Can we take a pause - how is it possible you're at 110k tokens already?"; tsk-c8ab7afd: "Having read the plan I take 2 issues - 1. We're overemphasizing A vs B readouts…"; tsk-0b6a03e7: "context tab is dead" (14 total) |
| Tool errors (per child dispatch) | 630 / 573 dispatches | 0.343 | 0.353 | +0.010 | 0.064 | n/a (dispatch rounds carry no feedback) | n/a |
| Tokens | 0 / 0 | **no source yet** | | | | `cost` on a round holds only `duration`; devloop stage log holds `duration_sec`. No ticket records usage; #344's record stage covers policy, version, harness, model, not tokens. Proxy available now, dispatch duration (s): 164.6 ± 21.5 before, 91.1 ± 9.8 after. | |
| Fix rounds, interactive (`did.attempts` per session) | 35 / 12 sessions | 1.26 | 1.75 | +0.49 | 0.44 | as row 1 | as row 1 |
| Fix rounds, devloop (`fix_rounds` per run) | 40 / 12 runs | 0.12 | 0.42 | +0.29 | 0.20 | PR review feedback notes: 4 exist (n-58388da1 on PR #240 x3, n-85dd0133 on tw_dogfood3), all before | none filed after (phase-1 judge has not run on the 10-15 PRs) |
| PR verdict (devloop, phase 1) | 40 / 12 runs | merged-clean 29, reworked 3, routed-to-human 2, no PR 5, pending 1 | merged-clean 1, routed-to-human 5, merged but phase-1 pending 6 | n/a (categorical; a live run prints the share merged-clean: 0.73 vs 0.08 judged so far) | | | |

Reading: nothing here is an effect of the knob; the after slice coincides with a harness swap and with the wayfinder grill sessions, which are correction-heavy by nature (ses-58e8b70c, ses-946bd115). This is exactly the confound the resolved policy manifest (#344, amendment 2) is meant to make visible: a live slice would print harness and model beside the date so the judge can split further.

## 3. Mode 2, paired: `weave policy compare baseline nudge-on`

Arms are the **sham assignment** from section 1 (mock: the labels; real: every number). Because the nudge never ran, the paired differences below are the noise floor a real trial would have to beat. Paired diff = mean over consecutive within-stream pairs of (nudge-on minus baseline); SE = sd of the pair differences over root n pairs.

| Outcome | n (baseline / nudge-on) | Mean baseline | Mean nudge-on | Paired diff (n pairs) | SE | Correction texts, baseline | Correction texts, nudge-on |
|---|---|---|---|---|---|---|---|
| Corrections per session | 10 / 7 sessions | 1.10 | 1.14 | -0.29 (7) | 0.61 | tsk-48f239f6 ses-36aed053: "this looks good but nothing shows up on clicking?"; tsk-8abd7e46 ses-1a671b06: "i actually think we should decouple vs learn…"; tsk-c8ab7afd ses-946bd115: "we should definitely make sure skill invocations become an event we can read…" (11 total) | tsk-590cf517 ses-58e8b70c: "i want to focus on this specific ones - the one around cutting feedback and prompts (unforgivable)…"; tsk-373f9266 ses-843b1c96: "wait what? that hook looks completely unncecessary…"; tsk-0b6a03e7 ses-747dd66b: "but i dont se the pane now" (8 total) |
| Tool errors (child dispatches summed per session) | 5 / 3 sessions with children | 5.40 | 3.00 | not estimable (1 pair) | | n/a | n/a |
| Tokens | 0 / 0 | **no source yet** (see mode 1) | | | | | |
| Fix rounds, interactive (`did.attempts`) | 10 / 7 sessions | 1.80 | 2.00 | +0.14 (7) | 0.77 | as row 1 | as row 1 |
| Fix rounds, devloop (`fix_rounds` per issue, re-runs summed) | 7 / 7 issues | 0.71 | 0.14 | -0.57 (7) | 0.30 | PR review feedback notes: none in window | none |
| PR verdict (devloop, phase 1) | 7 / 7 issues | merged-clean 4, routed-to-human 1, pending 2 | merged-clean 3, pending 4 | n/a (categorical) | | | |

Reading: a sham assignment produced a fix-rounds "effect" of -0.57 ± 0.30, close to two SEs, because thinkweave#317 (3 fix rounds, qwen implementer) fell in the baseline arm. That is the size of swing a real trial at this volume will see from nothing.

## 4. Verdict on paired vs before/after

On the primed question the before/after slice is empty by construction: `primed: true` on all 52 devloop trajectories since 2026-09-22, so there is no "before" value to slice on, and the paired table is the only instrument that can ever answer it. That is the one thing it says that the slice cannot. Everything else the real rounds show argues against running it now. Devloop volume is about 14 issues in the last eight days and 52 runs in five weeks, so a trial alternating per issue reaches the ten rounds per arm named in dec-87662eda only after two to four weeks of steady loop use, and the sham pairs above show the noise at seven pairs: fix rounds swing -0.57 ± 0.30 and corrections -0.29 ± 0.61 with no treatment at all. Session-to-session variance dominates both grains; correction counts per session range 0 to 5 with a mean near 1, and a single qwen-harness issue moved the devloop arm mean by half a fix round. The primed effect would have to be at least one fix round per issue, or a change in merged-clean share of 30 points or more, to clear that floor at n = 7 pairs; nothing in the five weeks of all-primed runs (fix rounds 0.12 per run, 73 percent merged-clean) suggests the unprimed arm would move that far. The harness and model also vary within the window, which the per-issue pairing rule does not control; until #344 records the resolved policy manifest, a pair can straddle two harnesses and the read-out cannot say so. The correction-text column is the most useful part of either table and today it is 120-character stubs; #324's backfill and #297's live text would make the paired texts readable, but they do not change the counts. **Recommendation: do not file a live paired trial task yet.** Land #344 (resolved policy on the round, harness and model included), let the before/after slice run on the first tweak (#345), and revisit the primed trial when devloop has about twenty issues on one harness with the manifest recorded, or when a `/retro` names a concrete unprimed failure that priming would have caught.

## 5. Open choices for the user

1. **Pairing unit for interactive sessions**: session as fixed here (a four-task session like ses-be8bd6ea counts once), or task, which would pair ses-be8bd6ea's four tasks separately and bring the pair count up at the cost of within-session correlation.
2. **Stand-in before #344**: run the before/after slice on round date and thinkweave tag as mocked, or print nothing until the resolved value is on the round, so the first live read-out is never a date proxy.
3. **Harness in the pairing rule**: require both members of a pair to share harness and model (drops cross-harness pairs, fewer pairs), or keep the plain alternation and let the manifest columns do the splitting at read time.
4. **Tokens row**: add a ticket to capture usage on the round (Stop-hook read of the transcript's usage, and the devloop stage `tokens` that `_stage_envelope` already accepts but no emitter fills), or drop the row and keep duration as the cost proxy.
5. **Minimum n before a difference prints**: refuse to print a diff and SE below a floor (say five per arm, three pairs) and show only the counts and texts, or always print and trust the judge to read the n.
