# Domain Docs

How the engineering skills should consume this repo's domain documentation and decision history when exploring the codebase.

**Layout: single-context.** thinkweave is one cohesive Python package — there is no per-subsystem `CONTEXT.md` split.

## Glossary — before exploring, read these

This repo already maintains rich domain docs. Treat them as the glossary — do **not** create a separate `CONTEXT.md` that restates them:

- **`ARCHITECTURE.md`** at the repo root — the narrative domain reference and authoritative source of thinkweave's ubiquitous language: source primitive, capability lanes, acquisition spine, dream orchestrator, memory seam, ontology as joint vocabulary, themes vs concept hubs, queue primitive, surface contract, etc.
- **`CLAUDE.md`** at the repo root — the operating guide; also names core terms (retrieval modalities, concepts/tags/themes distinction) and points at the deeper reference docs (`docs/LIFECYCLES.md`, `docs/CLI-AND-MCP.md`, `docs/SCHEMA.md`).

When your output names a domain concept (issue title, refactor proposal, hypothesis, test name), use the term as these docs define it. Don't drift to synonyms — say "the acquisition spine" or "concept hub," not a re-invented name. If a genuinely new term crystallizes during a session, `/domain-modeling` may seed a root `CONTEXT.md` for it lazily rather than bloating `ARCHITECTURE.md`.

## ADRs — decisions live in the vault, not `docs/adr/`

**This repo has no `docs/adr/` folder and should not grow one.** Architectural decisions — including code-level ones — are first-class notes in the thinkweave vault (`type: decision`, `dec-XXXX`). This is the ADR store, and it is *richer* than a flat markdown ADR folder:

- Decisions link to the code they touch via `file_path`, queryable as a graph.
- Each carries `predicted_outcome` → `prediction_match` → `prediction_history` (the decision-context / RLVR substrate) — a decision records what it expected to happen and is later judged against reality.
- Decisions have 4-state, evidence-gated supersession (see `docs/LIFECYCLES.md`), so "this was later reversed because…" is captured structurally, not by editing prose.

### Reading decisions (do this instead of reading `docs/adr/`)

- **Every decision touching a file you're about to change**: `weave_graph(id=<file_path>, filter='decisions_for_file')`. Run this before refactoring — it surfaces the prior reasoning and predictions for that exact code path.
- **Decisions on a topic / subsystem**: `weave_search(query='…', mode='hybrid', type=['decision'])`, optionally `concepts=[…]`.
- **State of a project's decisions over time**: `weave_timeline(project, days)`.

Treat a decision's stated reasoning and `predicted_outcome` the way you'd treat an ADR: **do not re-litigate a settled decision.** If a candidate refactor contradicts one, surface it explicitly rather than silently overriding:

> _Contradicts dec-XXXX (predicted X, matched) — but worth reopening because…_

### Recording decisions (do this instead of writing a `docs/adr/*.md` file)

When the user rejects a candidate with a load-bearing reason a future explorer would need — or a real architectural decision crystallizes — record it as a **vault decision**, not a filesystem ADR:

- `weave_create` with `type: decision`, ≥2 ontology concepts, the code `file_path`(s) it touches, and a `predicted_outcome` where the decision makes a falsifiable bet. The dream/judge loop scores that prediction later.

Only record when the reasoning is genuinely load-bearing; skip ephemeral ("not worth it right now") and self-evident reasons.

If the thinkweave MCP tools are unavailable in a given session, fall back to `weave` CLI equivalents (see `docs/CLI-AND-MCP.md`); do **not** substitute a `docs/adr/` markdown file.
