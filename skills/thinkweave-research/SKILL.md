---
name: thinkweave-research
description: Research a URL or external source and preserve a concise, sourced result in ThinkWeave. Use when the user asks to ingest, research-and-save, archive, or add a paper, repository, article, news item, video, podcast, or other URL to durable memory. Do not write to the vault for a general web-research request unless persistence is requested.
---

# ThinkWeave Research

Use web access for the source and `weave_*` MCP tools for durable storage. Prefer primary sources and respect source and copyright limits: store a synthesis, not a copied article.

## Research and preserve

1. Normalize the input to a URL. Resolve common identifiers such as `arxiv:`, `doi:`, and `gh:owner/repo` first.
2. Call `weave_sources_config` when available and classify the configured `source_type`. Otherwise use the narrowest honest type: `paper`, `repo`, `article`, `news`, `youtube-events`, `youtube-concepts`, `podcast-events`, or `podcast-concepts`.
3. Search ThinkWeave by URL, title, and one distinctive identifier before fetching. Reuse an existing `src-*` unless the user explicitly wants a refreshed entry.
4. Fetch authoritative content. For papers prefer the paper or abstract; for repositories prefer the repository and its documentation; for articles use the original publisher.
5. Produce a compact body containing:
   - what the source is and its central claim;
   - the most useful evidence or mechanics;
   - limitations, uncertainty, or relevance to the user's purpose;
   - a Markdown link to the original source.
6. Call `weave_concepts(action="list", min_count=2)` and select at least two fitting concepts. Put new terms in `proposed_concepts`.
7. Call `weave_create(type="source", ...)` with frontmatter containing `source_type`, `url`, authors when known, concepts, and proposed concepts. Keep sources global unless project scope is explicitly useful.

## Report

Return the `src-*` ID, title, source type, concepts, and original URL. List a skip or failure reason explicitly; never report an ingest that did not complete.