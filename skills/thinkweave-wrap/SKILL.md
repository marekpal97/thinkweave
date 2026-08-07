---
name: thinkweave-wrap
description: Persist the current Codex session's durable insights, consequential decisions, and explicit follow-ups into ThinkWeave, then run the deterministic wrap finalizer. Use when the user asks to wrap, save the session, persist what was learned, or before ending a substantive session. Run headlessly without asking what to capture.
---

# ThinkWeave Wrap

Perform one curated extraction pass, then the deterministic finalizer. Never ask the user what to capture. Honor any capture guidance already given in the session.

## Extract

1. Determine the project from the repository or explicit user context.
2. Call `weave_concepts(action="list", min_count=5)` and reuse existing vocabulary.
3. Review the conversation, changed files, test results, and abandoned approaches. Select:
   - at most three non-obvious, reusable insights;
   - every consequential decision, including meaningful abandoned choices;
   - only user-requested future work as insights tagged `todo`.
4. Reuse an existing current session note if visible in startup context or retrieval. Otherwise mint a stable ID such as `codex-<project>-<YYYYMMDD-HHMMSS>`.
5. Call `weave_extract` once with:
   - a two- or three-sentence summary under roughly 400 characters;
   - insights with title, body, and at least two concepts;
   - decisions with title, Context/Decision/Consequences rationale, outcome, relevant file paths, and at least two concepts;
   - `force=true` only when enriching an already processed or auto-extracted session.

Keep insights around 1,000 characters and decision rationales around 1,500 characters. Record experiential gotchas and reasoning, not textbook facts or a changelog.

## Finalize

Run `weave wrap-finalize <session-id> --project <project>` after extraction. If `weave` is not on `PATH`, run `codex mcp get thinkweave`, take the path following `--project`, and execute:

```text
uv run --no-sync --project <thinkweave-root> python -m thinkweave wrap-finalize <session-id> --project <project>
```

Do not run a dependency sync from this workflow. Refresh the state landing document only when the session materially changed the project's big picture.

## Finish

Do not restate successful tool output. Report only an error, a manual action, or a material state-document change.