<!--
  VENDORED DEV TOOLING — do not edit the body to diverge from upstream.

  Source:  DietrichGebert/ponytail  (GitHub)  — skills/ponytail-audit/SKILL.md
  Pinned:  16f29800fd2681bdf24f3eb4ccffe38be3baec6b
  Fetched: 2026-07-18

  License: MIT. Vendored verbatim as a pinned dev-tooling dependency. Upstream
  notice, retained per the MIT terms:

      Copyright (c) 2026 DietrichGebert

      Permission is hereby granted, free of charge, to any person obtaining a
      copy of this software and associated documentation files, to deal in the
      software without restriction, including the rights to use, copy, modify,
      merge, publish, distribute, sublicense, and/or sell copies, subject to
      the above copyright notice and this permission notice being included.

  WHY VENDORED, NOT INSTALLED: ponytail ships a plugin whose installer wires a
  `UserPromptSubmit` hook. Thinkweave already installs its own `UserPromptSubmit`
  hook (hooks/hooks.json → weave-hook user_prompt_submit); the two would collide.
  So we vendor the SKILL TEXT ONLY and never run `ponytail` / `plugin add` /
  any ponytail installer. No ponytail hook is ever registered.

  WIRING (machine-local, NOT committed): slash-command discovery reads
  `.claude/commands/`. Those entries are symlinks back into a committed source
  file and are machine-local — `git ls-files .claude/commands/` is empty, so no
  symlink is tracked in the repo. This is the repo's convention (issue-loop is
  wired the same way, by convention rather than by any committed symlink or
  script). To expose this skill as /ponytail-audit on a machine, create the
  symlink once:

      ln -s ../../docs/agents/ponytail-audit.command.md \
            .claude/commands/ponytail-audit.md

  (run from the repo root). The arch-proposal orchestrator (issue #61) invokes
  this skill's text directly in a fresh audit subagent, so the symlink is only
  needed for interactive `/ponytail-audit` use, not for the slow loop's
  simplification axis.

  COMPANION: ponytail-review (diff variant) lives at
  docs/agents/ponytail-review.command.md — vendored at the same pinned sha for
  issue #58's simplify gate.

  UPDATING: re-fetch upstream, re-pin the sha + fetch date above, and re-vendor
  the body verbatim. Do not hand-edit the body.
-->
---
name: ponytail-audit
description: >
  Whole-repo audit for over-engineering. Like ponytail-review, but scans the
  entire codebase instead of a diff: a ranked list of what to delete, simplify,
  or replace with stdlib/native equivalents. Use when the user says "audit this
  codebase", "audit for over-engineering", "what can I delete from this repo",
  "find bloat", "ponytail-audit", or "/ponytail-audit". One-shot report, does
  not apply fixes.
---

ponytail-review, repo-wide. Scan the whole tree instead of a diff. Rank
findings biggest cut first.

## Tags

Same as ponytail-review:

- `delete:` dead code, unused flexibility, speculative feature. Replacement: nothing.
- `stdlib:` hand-rolled thing the standard library ships. Name the function.
- `native:` dependency or code doing what the platform already does. Name the feature.
- `yagni:` abstraction with one implementation, config nobody sets, layer with one caller.
- `shrink:` same logic, fewer lines. Show the shorter form.

## Hunt

Deps the stdlib or platform already ships, single-implementation interfaces,
factories with one product, wrappers that only delegate, files exporting one
thing, dead flags and config, hand-rolled stdlib.

## Output

One line per finding, ranked: `<tag> <what to cut>. <replacement>. [path]`.
End with `net: -<N> lines, -<M> deps possible.` Nothing to cut: `Lean already. Ship.`

## Boundaries

Scope: over-engineering and complexity only. Correctness bugs, security holes,
and performance are explicitly out of scope. Route them to a normal review
pass. Lists findings, applies nothing. One-shot.
"stop ponytail-audit" or "normal mode" to revert.
