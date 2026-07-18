---
name: plan-distill
description: "End-of-grill fork distillation: walk the grill session's decision-half, mint one plan-time decision per real design fork (alternatives-considered + falsifiable predicted_outcome + plan_ref), and skip every clarifying answer. Write surface is weave_create/weave add decisions only — no code edits, no PRs. Headless-safe."
argument-hint: "nothing — reads the just-finished grill transcript, mints plan-time decisions (no code changes, no PRs)"
disable-model-invocation: true
---

# Plan-Distill — mint plan-time decisions from grill forks

Run this at the **end of a grill session** (`/grill-me`, `/grill-with-docs`, or
the installed `grilling` skill), after the skill's confirmation gate has settled
the plan. It reads the grill transcript and turns the **forks** — the places
where a concrete alternative was considered and rejected — into **plan-time
decisions** in the vault.

Plan-time predictions are the best RLVR rows the system can mint: made *before*
implementation, so the later `/judge-prediction` scores a genuine forecast, not
a post-hoc rationalization (which wrap-time decisions partly are). Decisions
already carry `plan_ref` (`operations/extract.py:371`) — the linkage field
exists; this command fills it.

**This runs outside the loop.** `/plan-distill` is human-invoked at grill/plan
time, not part of the `/issue-loop` fast loop. That keeps the
`vault-issue-contract.md` ownership partition intact: the loop still mints **no**
decisions, and `/wrap` is still the sole decision owner *within a work session*.
Plan-distill is a distinct, deliberately-invoked front door for the plan-time
forecast — a different surface, a different moment, not a second writer racing
the loop for the same record.

**This command rides the installed `grilling` skill; it never edits or forks
it.** The Pocock skills v1.1.0 `grilling` skill already splits **facts** (explore
the codebase) from **decisions** (require a human answer) and adds a confirmation
gate before enacting the plan. This command distills from the **decision half
only** — no new classification machinery. The installed
`~/.claude/skills/grilling` / `grill-me` artifacts are machine-global; **do not
edit them, do not fork them** — this repo-side companion command is the entire
delta.

## Contract in one line

**Walk the grill's decision-half → keep only real forks (a considered-and-rejected
alternative AND a falsifiable predicted_outcome — BOTH required) → mint one
`weave_create(type=decision)` per fork with an `## Alternatives considered`
section, `predicted_outcome`, and `plan_ref`.** Clarifying answers mint nothing.
No code is edited; no PR is opened.

## 1. The fork-gate (forks, not questions)

A heavy grill surfaces 30–40 questions, but **most are *clarifying*** (eliciting
a fact the agent could have explored) — only some are *forking* (a real
alternative was on the table and got rejected). Decisions belong at the forks.

Walk the transcript's **decision-half** and, for each candidate answer, apply
the two-condition **fork-gate**. Mint a decision **only when BOTH** hold:

- **(a) a concrete alternative was considered and rejected** — there was a real
  branch in the design (option X vs option Y), and one was chosen over the other
  for a stated reason. Not "what does this field mean?" — that is clarifying.
- **(b) the choice has a falsifiable `predicted_outcome`** — a claim you could
  later check against the vault or filesystem (a query, a metric, an observable
  state). "It'll be cleaner" is not falsifiable; "the phase-1 fan-out drops from
  N ontology reads to 1, verifiable in `dream.py`" is.

**If either condition is missing, skip the candidate** — a **clarifying answer
never mints** a decision. This gate *replaces any count cap*: there is **no count
cap**. Decision count scales with real contention, not question count — a
40-question grill with 3 genuine forks yields ~3 decisions; a heavy design grill
lands ~3–6; a **purely clarifying grill yields zero** (and that is a correct,
complete run, not a failure). Question count **does not drive** decision count.

## 2. Mint one decision per fork

Load the existing concept labels first (`weave_concepts` — the strict ontology
gate applies; unrecognised terms are auto-routed to `proposed_concepts:`), then
mint one decision per surviving fork.

**All three load-bearing fields go in `frontmatter=`, not as top-level kwargs.**
The MCP `weave_create` schema (`surfaces/mcp/tools/notes.py`) accepts only
`type` / `title` / `body` / `project` / `tags` / `frontmatter` / `session_id` —
any other top-level kwarg is **silently dropped**. A `concepts=…` /
`predicted_outcome=…` / `plan_ref=…` passed at the top level vanishes, and the
decision mints with none of them (the ontology gate never runs — it keys off
`frontmatter["concepts"]`). Nest them:

```
weave_create(
  type="decision",
  title="<verb> <what> over <rejected alternative>",
  body="<Context / Decision / Consequences + ## Alternatives considered>",
  frontmatter={
    "concepts": [...],                  # ≥2 ontology-aligned; weave_concepts first
    "predicted_outcome": "<falsifiable claim + where/when/what query verifies it>",
    "plan_ref": "[pending]",            # §3 convention — a scalar string
  },
)
```

**Body shape** (Context / Decision / Consequences, then the counterfactual):

```markdown
## Context
<the design pressure the fork sat under — one or two sentences>

## Decision
<what was chosen>

## Consequences
<what this commits us to / what it rules out>

## Alternatives considered
<the rejected branch: what it was, and the stated reason it lost>
```

**Body budget: ~1K chars.** The wrap-body doctrine (~1,000 chars per body, not
3K) is settled — over-writing dominates small-batch cost. Keep each decision
tight: the fork, the choice, the counterfactual, the prediction. Do not pad.

The `## Alternatives considered` section is **mandatory** — it *is* the
counterfactual, and a fork without one failed condition (a) and should not have
reached this step.

## 3. The `plan_ref` convention

`plan_ref` is a **scalar string** — always, no exceptions. The field's contract
is a string (`surfaces/mcp/tools/_extract_schemas.py:108`; consumed as a string
in `synthesis/judge.py:138`), so **never write a YAML flow list** (`[a, b]`) into
it. A flow list is both wrong-typed and, with issue refs, literally unparseable:
a bracketed value carrying a `#`-prefixed issue number is a YAML syntax error,
because `#` starts a comment.

- **When `/to-spec` → `/to-tickets` have already run** and produced spec/ticket
  refs, set `plan_ref` to a **single comma-joined string** of those refs — e.g.
  `plan_ref: "spec-4c1, 91, 92"` (drop the `#`; the digits identify the issues).
  One string, not a list.
- **When they have not run yet** (distillation happens at the end of the grill,
  often before ticketing), set the documented placeholder **`plan_ref:
  "[pending]"`**. The `/to-tickets` step rewrites `[pending]` in place — to the
  comma-joined ref string above — once it mints the tickets, closing the linkage.
  `[pending]` is the settled placeholder convention: always this exact token, so
  `/to-tickets` can find and replace it.

## 4. MCP-absent fallback (headless degrade)

When the `thinkweave` MCP server is unavailable, mint the same decision through
the CLI — the flag shape is verified against `weave add`'s argparse
(`surfaces/cli/_parser_basics.py`: `--type`, repeatable `-f key=value`):

```bash
weave add "<verb> <what> over <rejected alternative>" \
  --type decision \
  -f concepts=<c1>,<c2> \
  -f predicted_outcome="<falsifiable claim + verifying query — no commas>" \
  -f plan_ref=[pending] \
  --body "$(cat <<'EOF'
## Context
...
## Decision
...
## Consequences
...
## Alternatives considered
...
EOF
)"
```

**Comma caveat on `-f`.** `_parse_fm_token` (`surfaces/cli/notes.py`)
**comma-splits** any `-f key=value` whose value contains a comma into a *list*.
That is correct for `concepts=a,b` (a genuine list) but wrong for a prose
`predicted_outcome`: a comma in the prediction would silently turn the string
into a list. So on the CLI path, phrase `predicted_outcome` **comma-free**, or —
better for prose predictions — use the MCP `frontmatter=` path (§2), where the
value is a literal string. `plan_ref=[pending]` is safe on `-f`: the leading `[`
makes `_parse_fm_token` JSON-probe it, the probe fails, and it falls through to
the scalar-string branch — so it round-trips as the string `"[pending]"`, not a
list. (A comma-joined multi-ref `plan_ref` would split, so set that one via the
MCP path or let `/to-tickets` write it.)

`weave add` runs the incoming `concepts=` through the same strict ontology gate
as `weave_create`, so non-canonical terms land in `proposed_concepts:`
automatically — no pre-canonicalisation needed. Note-and-continue if MCP read
tools (e.g. `weave_graph`) are unavailable; they are not required to mint.

## 5. Write-surface enumeration

The command's **entire write surface** is `weave_create` (MCP) / `weave add`
(CLI fallback) **decisions** — nothing else. **No code edits, no `git`, no `gh`,
no PRs, no labels.** If you ever find yourself about to edit a file, stage a
commit, or open a PR, stop — that is not this command's job. Distillation writes
decisions to the vault and returns; enacting the plan is the fast loop's work,
reached separately.

## Headless posture & the skill-resolution gotcha

This command is headless-safe: it never prompts, and it degrades to the §4 CLI
fallback when MCP is unavailable. But there is a resolution gotcha — **headless
`claude -p "/plan-distill"` only resolves slash commands from `.claude/commands/`
symlinks, not from the skills catalog.** So either the invoking context
inline-references this file path (`docs/agents/plan-distill.command.md`), **or**
the machine-local symlink is created once (mirrors how `issue-loop`,
`arch-proposal`, and the vendored ponytail skills are wired — the symlink is
**NOT committed**; `git ls-files .claude/commands/` is empty):

```bash
# from the repo root, once per machine
ln -s ../../docs/agents/plan-distill.command.md .claude/commands/plan-distill.md
```
