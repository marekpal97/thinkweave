---
description: Inline ChatGPT-export import — walk conversations.json, summarize each thread via the running model, and mem_create one note per conversation. The `mem import chatgpt --via inline` path; pairs with `--via batch` which fans out via the API wrapper instead.
allowed-tools: Read, Bash, mcp__personal-mem__mem_create, mcp__personal-mem__mem_concepts
---

# /import-chatgpt — Inline ChatGPT export import

Import a ChatGPT data export (`conversations.json`) without burning an
OpenAI API key. Walks the same JSON the `--via batch` path would parse,
but produces summaries via the running session's model instead of
`agent_client.batch_completions_sync`.

The user invokes this from `mem import chatgpt --via inline <path>` (the
CLI prints a hint pointing here) or directly as `/import-chatgpt <path>`.

## Steps

1. **Read the export.** `Read` the path passed in the prompt body — it's a
   JSON array. Top-level keys per conversation include `id`, `title`,
   `create_time`, and `mapping` (a tree of messages). For batches >50
   conversations, prefer `--via batch` instead.

2. **Walk conversations.** For each conversation:
   - Flatten the `mapping` tree into a chronological transcript
     (user → assistant → user → …). The same logic lives in
     `importers/chatgpt.parse_thread` if you want to shell out via
     `python -c "from personal_mem.importers.chatgpt import parse_thread; …"`.
   - Compose a summary inline — 4 sections:
     - `## Summary` (3–5 sentences)
     - `## Key questions` (bullets — user-driven curiosity)
     - `## Key insights` (bullets — concrete takeaways)
     - `## Concepts` (kebab-case terms — 3–6, the ontology gate routes
       non-canonical to `proposed_concepts:`)
   - Call `mcp__personal-mem__mem_create(note_type='source', title=<conversation title>, body=<summary markdown>, concepts=<list>, extra_frontmatter={"source_type": "chatgpt", "chatgpt_id": <id>, "created": <iso date>})`.

3. **Report.** One-line summary: `Imported N conversation(s); K skipped (already imported); M errors.`

## Notes

- Dedup: the importer's batch path keys on `(conversation_id, title)`. Before
  `mem_create`, call `mcp__personal-mem__mem_search(query=<title>, type='source')` to
  catch the already-imported case.
- Don't pull message bodies into `mem_create`'s `body` — only the structured
  summary. The raw transcript is too noisy to live in the vault.
- The `concepts` you assign go through the strict ontology gate. Use
  `mcp__personal-mem__mem_concepts(action='list', limit=200)` once at the
  start to load the vocabulary.
- This skill produces the same note shape as the batch path — there's
  no fork in downstream behaviour, just in which model writes the summary.
