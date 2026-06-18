"""``weave_extract`` / ``weave_judge`` / ``weave_landing``.

Thin MCP wrappers — each handler unpacks JSON args, calls into
``operations/``, and formats a ``TextContent`` payload. All business logic
lives one layer down.
"""

from __future__ import annotations

from thinkweave.core.config import Config

# Re-exports: business logic now lives in `thinkweave.operations.extract`,
# but tests and the legacy server module import these helpers from here.
from thinkweave.operations.extract import (  # noqa: E402, F401
    _build_decision_body,
    _flush_insight,
    parse_candidate_insights as _parse_candidate_insights,
)


def tool_schemas() -> list:
    from thinkweave.surfaces.mcp.tools._extract_schemas import (
        tool_schemas as _schemas,
    )

    return _schemas()


def _format_extract_report(out) -> str:
    # Header — distinguish the input session_id from the minted ses-id when
    # they diverge (typical when the caller passed a Claude Code UUID and
    # extract auto-minted a session note).
    if out.session_note_id and out.session_note_id != out.session_id:
        header = (
            f"Extracted from session {out.session_id} "
            f"(session note: {out.session_note_id})"
        )
    else:
        header = f"Extracted from session {out.session_id}"
    lines = [header + ":"]
    if out.summary:
        lines.append(f"Summary: {out.summary}")
    for n in out.created_notes:
        lines.append(f"  Created [{n.type.value}] {n.title} ({n.id}) derived_from={out.session_id}")
    for d in out.created_decisions:
        committed = d.frontmatter.get("committed", False)
        lines.append(
            f"  Created [{d.type.value}] {d.title} ({d.id}) "
            f"status={d.frontmatter.get('status')}, committed={committed}"
        )
    for t in out.created_todos:
        lines.append(f"  Auto-todo [{t.type.value}] {t.title} ({t.id}) tags=[todo, auto]")
    if not out.all_created:
        lines.append("  No insights or decisions extracted.")
    lines.extend(out.suggestions[:5])
    lines.append(f"Session marked processed={out.processed_at}")
    # Surface the exact finalize command — pass `session_id` (the input),
    # NOT `session_note_id`. Decisions stamp `source_session = session_id`
    # and the judge matches on that field; passing the ses-id when input
    # was a UUID silently returns 0 decisions (issue surfaced 2026-05-14).
    project = ""
    if out.created_decisions:
        project = out.created_decisions[0].frontmatter.get("project", "") or ""
    elif out.created_notes:
        project = out.created_notes[0].frontmatter.get("project", "") or ""
    finalize_arg = out.session_id  # the input — what decisions are stamped with
    if project:
        lines.append(
            f"▶ To finalize: weave wrap-finalize {finalize_arg} --project {project}"
        )
    else:
        lines.append(f"▶ To finalize: weave wrap-finalize {finalize_arg}")
    return "\n".join(lines)


def handle_extract(cfg: Config, args: dict):
    from mcp.types import TextContent

    from thinkweave.operations.extract import extract_session

    out = extract_session(
        cfg,
        session_id=args["session_id"],
        force=args.get("force", False),
        project=args.get("project", ""),
        summary=args.get("summary", ""),
        insights=args.get("insights"),
        decisions=args.get("decisions", []),
        plan_path=args.get("plan_path", ""),
        plan_summary=args.get("plan_summary", ""),
    )
    if out.error:
        return [TextContent(type="text", text=out.error)]
    if out.skipped_reason:
        return [TextContent(type="text", text=out.skipped_reason)]
    return [TextContent(type="text", text=_format_extract_report(out))]


def handle_judge(cfg: Config, args: dict):
    from mcp.types import TextContent

    from thinkweave.operations.decisions import judge_and_writeback

    results = judge_and_writeback(
        cfg,
        decision_id=args.get("decision_id", ""),
        session_id=args.get("session_id", ""),
        project=args.get("project", ""),
    )
    if not results:
        return [TextContent(type="text", text="No decisions found to evaluate.")]
    lines = [f"Evaluated {len(results)} decisions:"]
    for dec, result in results:
        lines.append(
            f"  {dec.id} ({dec.title}): {result['verdict']} "
            f"(confidence={result['confidence']}) — {result['evidence']}"
        )
    return [TextContent(type="text", text="\n".join(lines))]


def handle_landing(cfg: Config, args: dict):
    from mcp.types import TextContent

    from thinkweave.operations.landing import render_landing

    out = render_landing(
        cfg,
        project=args.get("project", ""),
        doc=args.get("doc", "all"),
        state_context=args.get("state_context", False),
    )
    if out.error:
        return [TextContent(type="text", text=out.error)]
    if out.state_context_text:
        return [TextContent(type="text", text=out.state_context_text)]
    scope = "global vault" if out.doc == "themes" else out.project
    lines = [f"Generated landing documents for {scope}:"]
    for filename, path in out.written.items():
        lines.append(f"  {filename} → {path.relative_to(cfg.vault_root)}")
    return [TextContent(type="text", text="\n".join(lines))]
