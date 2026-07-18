<!--
  VENDORED DEV TOOLING — do not edit the body to diverge from upstream.

  Source:  DietrichGebert/ponytail  (GitHub)  — skills/ponytail-review/SKILL.md
  Pinned:  16f29800fd2681bdf24f3eb4ccffe38be3baec6b
  Fetched: 2026-07-18
  License: upstream (see the ponytail repo); vendored verbatim as a dev-tooling
           dependency, treated as a pinned vendored dep.

  WHY VENDORED, NOT INSTALLED: ponytail ships a plugin whose installer wires a
  `UserPromptSubmit` hook. Thinkweave already installs its own `UserPromptSubmit`
  hook (hooks/hooks.json → weave-hook user_prompt_submit); the two would collide.
  So we vendor the SKILL TEXT ONLY and never run `ponytail` / `plugin add` /
  any ponytail installer. No ponytail hook is ever registered.

  WIRING (machine-local, NOT committed — mirrors issue-loop.command.md):
  slash-command discovery reads `.claude/commands/`. Those entries are symlinks
  back into this committed source and are machine-local (git ls-files
  .claude/commands/ is empty). To expose this skill as /ponytail-review on a
  machine, create the symlink once:

      ln -s ../../docs/agents/ponytail-review.command.md \
            .claude/commands/ponytail-review.md

  (run from the repo root). The issue-loop orchestrator invokes this skill's
  text directly in a fresh simplify subagent, so the symlink is only needed for
  interactive `/ponytail-review` use, not for the loop's simplify gate.

  COMPANION: ponytail-audit (whole-repo variant) lives upstream at
  skills/ponytail-audit/SKILL.md @ the same pinned sha — reserved for issue #61.

  UPDATING: re-fetch upstream, re-pin the sha + fetch date above, and re-vendor
  the body verbatim. Do not hand-edit the body.
-->
---
name: ponytail-review
description: >
  Code review focused exclusively on over-engineering. Finds what to delete:
  reinvented standard library, unneeded dependencies, speculative abstractions,
  dead flexibility. One line per finding: location, what to cut, what replaces
  it. Use when the user says "review for over-engineering", "what can we
  delete", "is this over-engineered", "simplify review", or invokes
  /ponytail-review. Complements correctness-focused review, this one only
  hunts complexity.
---

Review diffs for unnecessary complexity. One line per finding: location, what
to cut, what replaces it. The diff's best outcome is getting shorter.

## Format

`L<line>: <tag> <what>. <replacement>.`, or `<file>:L<line>: ...` for
multi-file diffs.

Tags:

- `delete:` dead code, unused flexibility, speculative feature. Replacement: nothing.
- `stdlib:` hand-rolled thing the standard library ships. Name the function.
- `native:` dependency or code doing what the platform already does. Name the feature.
- `yagni:` abstraction with one implementation, config nobody sets, layer with one caller.
- `shrink:` same logic, fewer lines. Show the shorter form.

## Examples

❌ "This EmailValidator class might be more complex than necessary, have you
considered whether all these validation rules are needed at this stage?"

✅ `L12-38: stdlib: 27-line validator class. "@" in email, 1 line, real validation is the confirmation mail.`

✅ `L4: native: moment.js imported for one format call. Intl.DateTimeFormat, 0 deps.`

✅ `repo.py:L88: yagni: AbstractRepository with one implementation. Inline it until a second one exists.`

✅ `L52-71: delete: retry wrapper around an idempotent local call. Nothing replaces it.`

✅ `L30-44: shrink: manual loop builds dict. dict(zip(keys, values)), 1 line.`

## Scoring

End with the only metric that matters: `net: -<N> lines possible.`

If there is nothing to cut, say `Lean already. Ship.` and stop.

## Boundaries

Scope: over-engineering and complexity only. Correctness bugs, security holes,
and performance are explicitly out of scope. Route them to a normal review
pass, not this one. A single smoke test or `assert`-based
self-check is the ponytail minimum, not bloat, never flag it for deletion.
Does not apply the fixes, only lists them.
"stop ponytail-review" or "normal mode": revert to verbose review style.
