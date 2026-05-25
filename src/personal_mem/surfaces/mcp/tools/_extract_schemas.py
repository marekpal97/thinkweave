"""Tool schemas for the extract / judge / landing / enrich quartet.

Pulled out of ``extract.py`` so the handler module stays focused. Schemas
are ~220 lines of nested dicts; isolating them here keeps the surface
modules under 600 LOC each.
"""

from __future__ import annotations


def tool_schemas() -> list:
    from mcp.types import Tool

    return [
        Tool(
            name="mem_extract",
            description=(
                "Extract structured knowledge and decisions from a session.\n\n"
                "Creates knowledge notes and decision notes inside the session folder "
                "with derived_from links, writes a summary, strips raw event logs, "
                "archives buffer as events.jsonl, and marks the session processed.\n\n"
                "Call at the end of a productive work session. Provide curated "
                "insights and decisions for best results.\n\n"
                "QUALITY GUIDANCE:\n"
                "- Insights should capture personal experience and context, not "
                "restate textbook facts. Include what surprised you, what went wrong, "
                "and non-obvious implications.\n"
                "- Decisions need substantive Context sections explaining the problem "
                "and alternatives considered, not just the conclusion.\n"
                "- Both successful AND abandoned approaches should be recorded "
                "(no survivorship bias).\n\n"
                "If the session has auto_extracted=true (from Stop hook), use "
                "force=true to enrich it with LLM-generated insights and decisions.\n\n"
                "IMPORTANT: Every insight and decision MUST include concepts (min 2). "
                "Notes with <2 concepts cannot auto-link in the knowledge graph. "
                "Call mem_concepts first to load existing labels and reuse them."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Session note ID (e.g. 'ses-a1b2c3d4') or CLAUDE_SESSION_ID. "
                            "If no matching session note exists, one is auto-created."
                        ),
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "2-3 sentence summary of what was accomplished. "
                            "If omitted, auto-generated from extracted notes."
                        ),
                    },
                    "insights": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "body": {"type": "string", "description": "Markdown body for the note."},
                                "tags": {"type": "array", "items": {"type": "string"}},
                                "concepts": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "REQUIRED. Domain-specific technical terms for graph linking "
                                        "(e.g. write-ahead-log, recursive-cte). Minimum 2 concepts "
                                        "per insight — notes with <2 concepts cannot auto-link in "
                                        "the knowledge graph. Call mem_concepts first to reuse "
                                        "existing labels."
                                    ),
                                },
                            },
                            "required": ["title", "body", "concepts"],
                        },
                        "description": (
                            "Knowledge to extract as notes. Max 3 — quality over quantity. "
                            "Each becomes a note with derived_from link to this session. "
                            "Every insight MUST include concepts (min 2) for graph connectivity. "
                            "If omitted, parses ## Candidate Insights from the session body."
                        ),
                    },
                    "decisions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "rationale": {"type": "string", "description": "WHY this change was made — the reasoning behind the decision."},
                                "file_paths": {"type": "array", "items": {"type": "string"}, "description": "Files affected by this decision."},
                                "outcome": {"type": "string", "enum": ["committed", "abandoned", "partial"], "description": "Was this change committed, abandoned, or partially done?"},
                                "tags": {"type": "array", "items": {"type": "string"}, "description": "Broad categories (e.g. refactor, bugfix, performance)."},
                                "summary": {"type": "string", "description": "One-sentence summary of the decision. Used in the per-project decisions landing page. Keep concise."},
                                "concepts": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "REQUIRED. Domain-specific technical terms for graph linking "
                                        "(e.g. write-ahead-log, recursive-cte). Minimum 2 concepts "
                                        "per decision — decisions with <2 concepts cluster separately "
                                        "from the knowledge graph. Call mem_concepts first to reuse "
                                        "existing labels."
                                    ),
                                },
                                "supersedes": {"type": "string", "description": "ID of decision this replaces."},
                                "cites": {"type": "array", "items": {"type": "string"}, "description": "Source note IDs that informed this decision."},
                                "plan_ref": {"type": "string", "description": "Which plan item this decision implements (e.g. 'Step 3: Replace auth middleware')."},
                                "predicted_outcome": {
                                    "type": "string",
                                    "description": (
                                        "OPTIONAL forward-looking prediction. Prose string carrying "
                                        "the claim plus a manifestation pointer — where/when/what "
                                        "query verifies it (e.g. 'next CI run on this branch will "
                                        "show all judge tests green', 'this lands in a single commit "
                                        "touching only synthesis/judge.py'). The decision file is "
                                        "seeded with prediction_match=pending; later the "
                                        "/judge-prediction skill maps it against evidence to "
                                        "confirmed/contradicted/unevaluable/stale. Feeds the RLVR "
                                        "export. Leave empty when the session has no clear "
                                        "prediction; never invent one."
                                    ),
                                },
                            },
                            "required": ["title", "rationale", "outcome", "concepts"],
                        },
                        "description": (
                            "Significant decisions from this session — both successful and "
                            "abandoned. Include rationale (WHY), affected files, and outcome. "
                            "Focus on decisions that matter for project evolution. "
                            "Typical sessions have 2-5 decisions, but include all that are significant."
                        ),
                    },
                    "project": {
                        "type": "string",
                        "description": (
                            "Project name. Required when no session note exists yet "
                            "(e.g. non-code conversations). Ignored if session already exists."
                        ),
                    },
                    "plan_path": {"type": "string", "description": "File path of the plan used during this session. Stored in session context.plan for traceability."},
                    "plan_summary": {"type": "string", "description": "Brief summary of the plan's main tasks/items (2-5 lines). Stored alongside plan_path in session context.plan."},
                    "force": {"type": "boolean", "description": "Re-extract even if session is already marked processed."},
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="mem_judge",
            description=(
                "Evaluate decision notes based on downstream evidence.\n\n"
                "Updates verdict (kept/superseded/reverted/unknown) and confidence "
                "score on decision frontmatter. No LLM — pure graph traversal and "
                "git state checks. STRUCTURAL VERDICTS ONLY: prediction verdicts "
                "(confirmed/contradicted/unevaluable/stale on predicted_outcome) "
                "are emitted by the /judge-prediction skill, not this tool.\n\n"
                "Use after extraction to assess which decisions held up, or any time "
                "later to reconcile with post-session events (commits that happened "
                "after the session, files that were reverted, etc.).\n\n"
                "Evaluation logic:\n"
                "- committed + tests pass → kept (0.9)\n"
                "- re-edited by later decision → superseded (0.7)\n"
                "- committed, files deleted → reverted (0.6)\n"
                "- committed, not tested → kept (0.6)\n"
                "- not committed → unknown (0.0)"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Evaluate all decisions derived from this session."},
                    "decision_id": {"type": "string", "description": "Evaluate a single decision by ID."},
                    "project": {"type": "string", "description": "Evaluate all decisions in a project."},
                },
            },
        ),
        Tool(
            name="mem_landing",
            description=(
                "Generate landing documents. Filenames resolve from "
                "vault/.mem/sources.yaml: landing_files: (defaults documented "
                "in ARCHITECTURE.md §User configuration).\n\n"
                "decisions (per-project): Decision ledger with table + Mermaid DAG.\n"
                "backlog (per-project): Open items (todo), stalled proposals, parked.\n"
                "state (per-project): Data-driven skeleton — for best results, read "
                "the generated file and enhance with your own judgment. Use "
                "state_context=true to get raw data for a richer narrative.\n"
                "themes (global, vault root): Global theme ledger — active, dormant, "
                "resolved themes with project / last-catalyst / # decisions columns. "
                "Themes are global so this doc is too — pass doc='themes' and the "
                "project argument is ignored.\n\n"
                "Documents are excluded from the vault index (they're views, not source).\n"
                "Run after extraction to refresh the decisions + backlog ledgers. "
                "Only refresh state-of-play if the session genuinely changed the "
                "project's big picture."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": (
                            "Project name. Required for project-scoped docs "
                            "(decisions, backlog, state). Ignored for 'themes'."
                        ),
                    },
                    "doc": {
                        "type": "string",
                        "enum": ["all", "decisions", "backlog", "state", "themes"],
                        "default": "all",
                        "description": "Which document(s) to generate.",
                    },
                    "state_context": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, returns structured context data for the "
                            "state-of-play landing doc instead of writing the "
                            "file. Use this to write a richer narrative with "
                            "your own judgment."
                        ),
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="mem_enrich",
            description=(
                "LLM-assisted concept assignment for vault notes missing concepts.\n\n"
                "Sends batches of notes to gpt-5-mini with the full ontology as context. "
                "Writes assigned concepts to markdown frontmatter (permanent, Obsidian-visible). "
                "After enrichment, automatically rebuilds the index and re-runs mem_connect "
                "to materialize new edges as wikilinks.\n\n"
                "Run this to fix sessions (0% concept coverage), decisions (60% missing), "
                "and any imported notes (claude-mem, ChatGPT) that lack concepts.\n\n"
                "Requires OPENAI_API_KEY in environment."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Scope to one project. Empty = all projects."},
                    "note_types": {"type": "array", "items": {"type": "string"}, "description": "Types to enrich. Default: [session, note, decision, source]."},
                    "limit": {"type": "integer", "default": 0, "description": "Max notes to process. 0 = no limit."},
                    "force": {"type": "boolean", "default": False, "description": "Re-enrich notes that already have concepts."},
                    "dry_run": {"type": "boolean", "default": False, "description": "Show what would be done without writing."},
                },
                "required": [],
            },
        ),
    ]
