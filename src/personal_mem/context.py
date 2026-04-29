"""Project context payload builder.

Assembles a structured context summary for a project — used by both the
SessionStart hook (to wake Claude Code up oriented) and by the
``mem_project_snapshot`` MCP tool (for mid-session re-fetching).

The payload is built from the index and vault, with zero LLM calls. Each
section is assembled independently and labelled by a ``## Heading`` so
Claude can skim or deep-read. Sections are budgeted as whole units —
when the total exceeds the caller's budget, whole sections are dropped
(never items chopped mid-line).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from personal_mem.config import Config, load_config
from personal_mem.schemas import NoteType

# Rough approximation: 1 token ≈ 4 characters for English markdown. Used
# to convert caller-facing token budgets into char budgets.
CHARS_PER_TOKEN = 4

# Section identifiers — stable keys for the ``sections`` override.
SECTIONS = (
    "header",
    "tools",
    "sessions",
    "state",
    "backlog",
    "decisions",
    "probes",
    "themes",
    "concepts",
    "sources",
    "footer",
)

# Drop order when over budget. Header/tools/state/sessions are load-bearing
# and dropped last; decorative sections go first.
_DROP_ORDER = ("sources", "themes", "concepts", "probes", "decisions", "backlog")


@dataclass
class Section:
    """One addressable chunk of the payload."""

    key: str
    title: str
    body: str
    # Soft per-section budget in characters. Used only as a hint when the
    # section itself decides whether to inline more or truncate.
    soft_budget_chars: int = 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_project_context(
    cfg: Config | None = None,
    project: str = "",
    *,
    sections: list[str] | None = None,
    budget_tokens: int = 10000,
) -> str:
    """Assemble the structured context payload for a project.

    Args:
        cfg: Optional pre-loaded Config. If None, loads from env/config.toml.
        project: Project slug. If empty, uses cfg.default_project.
        sections: Optional subset of SECTIONS to include. Default: all.
        budget_tokens: Soft cap on the total payload size in tokens.

    Returns:
        A markdown string with ``## Heading`` sections. If the vault is
        missing or empty, returns a minimal header noting that state.
        Never raises — each section is isolated in its own try/except so
        a single failure cannot corrupt the whole payload.
    """
    cfg = cfg or load_config()
    if not project:
        project = cfg.default_project or ""

    wanted = list(sections) if sections else list(SECTIONS)

    # Build each requested section. Errors in one section become a visible
    # ``_(section failed: <reason>)_`` line so partial failures are loud
    # but non-fatal.
    collected: dict[str, Section] = {}
    for key in wanted:
        try:
            section = _build_section(key, cfg, project)
        except Exception as e:  # pragma: no cover — defensive
            section = Section(
                key=key,
                title=_default_title(key),
                body=f"_(section failed: {type(e).__name__}: {e})_",
                soft_budget_chars=200,
            )
        if section is not None:
            collected[key] = section

    max_chars = budget_tokens * CHARS_PER_TOKEN
    return _assemble(collected, wanted, max_chars)


# ---------------------------------------------------------------------------
# Section dispatch
# ---------------------------------------------------------------------------


def _build_section(key: str, cfg: Config, project: str) -> Section | None:
    if key == "header":
        return _build_header(cfg, project)
    if key == "tools":
        return _build_tools_manifest()
    if key == "sessions":
        return _build_recent_sessions(cfg, project, n=5)
    if key == "state":
        return _build_state_excerpt(cfg, project, max_chars=12000)
    if key == "backlog":
        return _build_backlog(cfg, project)
    if key == "decisions":
        return _build_recent_decisions(cfg, project, n=10)
    if key == "probes":
        return _build_open_probes(cfg, project, n=20)
    if key == "concepts":
        return _build_concept_histogram(cfg, project, n=20)
    if key == "sources":
        return _build_recent_sources(cfg, project, n=5)
    if key == "themes":
        return _build_active_themes(cfg, project, n=10)
    if key == "footer":
        return _build_footer()
    return None


def _default_title(key: str) -> str:
    return {
        "header": "Header",
        "tools": "Available MCP Tools",
        "sessions": "Recent Wrapped Sessions",
        "state": "State of Play",
        "backlog": "Backlog (Open Items)",
        "decisions": "Recent Decisions",
        "probes": "Open Probes",
        "themes": "Active Themes",
        "concepts": "Concept Histogram",
        "sources": "Recent Sources",
        "footer": "Retrieval Hints",
    }.get(key, key.title())


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_header(cfg: Config, project: str) -> Section:
    """Vault/project identity plus per-type note counts."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"- **Project**: `{project or '(unset)'}`",
        f"- **Vault**: `{cfg.vault_root}`",
        f"- **Today**: {now}",
    ]

    counts = _index_counts(cfg, project)
    if counts:
        total = sum(counts.values())
        by_type = ", ".join(f"{t}={n}" for t, n in sorted(counts.items()))
        lines.append(f"- **Vault stats** (this project): {total} notes ({by_type})")
    elif _index_missing(cfg):
        lines.append("- **Vault stats**: index not built (run `mem index`)")

    return Section(
        key="header",
        title=_default_title("header"),
        body="\n".join(lines),
        soft_budget_chars=600,
    )


def _build_tools_manifest() -> Section:
    """Static manifest of MCP tools so Claude knows what's available.

    Keep this list in sync with ``mcp/server.py``. Grouped by purpose.
    """
    body = (
        "Personal-mem exposes these tools via MCP. Prefer them over shelling out:\n"
        "\n"
        "**Search & retrieve**\n"
        "- `mem_search(query, mode='fts'|'similar'|'hybrid', project, type, tags, limit)` — FTS / semantic / hybrid (RRF) search. `type` accepts str or list. Empty query returns date-sorted recent notes.\n"
        "- `mem_concept_search(concepts, match_mode='any'|'all', project, type, min_matches, limit)` — find notes by one or more concepts, intersection or union. Cross-project when `project` omitted.\n"
        "- `mem_context(query, project, type, concepts, limit)` — 3-layer retrieval (FTS → concept expansion → recency) with type filter.\n"
        "- `mem_timeline(project, days)` — chronological view of sessions + decisions.\n"
        "- `mem_read(id)` — fetch a single note by ID.\n"
        "- `mem_graph(id, depth)` — walk the typed-edge graph outward.\n"
        "- `mem_source_lens(source_id)` — walk out from a source: decisions, sessions, inbound notes, concept reach.\n"
        "- `mem_decisions_for_file(file_path, project)` — every decision ever made touching a file (indexed JOIN).\n"
        "- `mem_project_snapshot(project, sections, budget_tokens)` — on-demand project overview (same payload as this one).\n"
        "\n"
        "**Create & link**\n"
        "- `mem_create(note_type, title, body, tags, concepts, project, session_id)` — create a note.\n"
        "- `mem_update(note_id, frontmatter_updates, body_append)` — update a note.\n"
        "- `mem_link(source_id, target_id, edge_type)` — add a typed edge.\n"
        "- `mem_unlink(source_id, target_id, edge_type)` — remove an edge.\n"
        "\n"
        "**Extract & judge** (session end)\n"
        "- `mem_extract(session_id, summary, insights, decisions, projects)` — enrich a session with insights + decisions.\n"
        "- `mem_judge(session_id)` — evaluate decisions against git evidence.\n"
        "\n"
        "**Landing & concepts**\n"
        "- `mem_landing(project, doc='decisions'|'backlog'|'state'|'all')` — regenerate landing docs.\n"
        "- `mem_concepts(project, prefix, min_count)` — list concepts with counts.\n"
        "- `mem_concepts_tighten()` — find near-duplicate concepts.\n"
        "- `mem_concepts_merge(from_concept, to_concept)` — rename across the vault.\n"
        "- `mem_concepts_drift(project)` — advisory drift report (near-dupes, ontology candidates, staleness)."
    )
    return Section(
        key="tools",
        title=_default_title("tools"),
        body=body,
        soft_budget_chars=2400,
    )


def _build_recent_sessions(cfg: Config, project: str, n: int = 5) -> Section:
    """Last N wrapped sessions with summaries + top insights + derived IDs."""
    if _index_missing(cfg):
        return Section(
            key="sessions",
            title=_default_title("sessions"),
            body="_(index not built — no session history available)_",
            soft_budget_chars=200,
        )

    import sqlite3

    db = sqlite3.connect(str(cfg.index_db))
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            """
            SELECT id, title, date, path, frontmatter, body_text
            FROM notes
            WHERE type = 'session'
              AND (? = '' OR project = ?)
            ORDER BY date DESC
            LIMIT 100
            """,
            (project, project),
        ).fetchall()
    finally:
        db.close()

    # Filter to wrapped sessions and sort by processed_at (falling back to date)
    # in Python — avoids reliance on SQLite JSON1 extension.
    wrapped: list[tuple] = []
    for row in rows:
        try:
            fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
        except json.JSONDecodeError:
            fm = {}
        if not fm.get("processed"):
            continue
        sort_key = str(fm.get("processed_at") or row["date"] or "")
        wrapped.append((sort_key, row, fm))

    wrapped.sort(key=lambda t: t[0], reverse=True)
    wrapped = wrapped[:n]

    if not wrapped:
        return Section(
            key="sessions",
            title=_default_title("sessions"),
            body="_(no wrapped sessions found — run `/mem-wrap` at session end)_",
            soft_budget_chars=200,
        )

    # Collect session IDs to batch-query derived notes.
    session_ids = [row["id"] for _sk, row, _fm in wrapped]
    derived_map = _get_derived_artifacts(cfg, session_ids)

    chunks = []
    for _sort_key, row, fm in wrapped:
        ses_id = row["id"]
        title = row["title"] or fm.get("id", "session")
        date = fm.get("processed_at") or row["date"] or ""
        summary = _extract_summary(row["body_text"] or "")
        insight_titles = _extract_insight_titles(row["body_text"] or "")

        chunk = f"### {title} (`{ses_id}`)\n_{date}_\n\n{summary}"
        if insight_titles:
            ins_lines = "\n".join(f"- {t}" for t in insight_titles[:3])
            chunk += f"\n\n**Top insights:**\n{ins_lines}"

        artifacts = derived_map.get(ses_id, [])
        if artifacts:
            art_lines = ", ".join(
                f"`{a_id}` ({a_type})" for a_id, a_type, _title in artifacts
            )
            chunk += f"\n**Derived:** {art_lines}"

        chunks.append(chunk)

    return Section(
        key="sessions",
        title=_default_title("sessions"),
        body="\n\n".join(chunks),
        soft_budget_chars=9000,
    )


def _get_derived_artifacts(
    cfg: Config, session_ids: list[str]
) -> dict[str, list[tuple[str, str, str]]]:
    """Return {session_id: [(note_id, type, title), ...]} for derived notes/decisions.

    Uses the ``source_session`` frontmatter field that ``mem_extract`` sets on
    every derived artifact.
    """
    if not session_ids or _index_missing(cfg):
        return {}

    import sqlite3

    placeholders = ",".join("?" for _ in session_ids)
    db = sqlite3.connect(str(cfg.index_db))
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            f"""
            SELECT id, type, title, frontmatter
            FROM notes
            WHERE type IN ('decision', 'note')
            ORDER BY date DESC
            LIMIT 500
            """,
        ).fetchall()
    finally:
        db.close()

    id_set = set(session_ids)
    result: dict[str, list[tuple[str, str, str]]] = {}
    for row in rows:
        try:
            fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
        except json.JSONDecodeError:
            continue
        src_ses = fm.get("source_session", "")
        if src_ses in id_set:
            result.setdefault(src_ses, []).append(
                (row["id"], row["type"], row["title"] or "")
            )
    return result


def _build_state_excerpt(cfg: Config, project: str, max_chars: int = 12000) -> Section:
    """Read STATE.md verbatim (up to max_chars)."""
    if not project:
        return Section("state", _default_title("state"), "_(no project set)_", 100)

    state_path = cfg.vault_root / "projects" / project / "STATE.md"
    if not state_path.exists():
        return Section(
            key="state",
            title=_default_title("state"),
            body="_(STATE.md not found — run `mem landing --doc state`)_",
            soft_budget_chars=200,
        )

    text = state_path.read_text(encoding="utf-8")
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n_... (STATE.md truncated — read full file for more)_"

    return Section(
        key="state",
        title=_default_title("state"),
        body=text,
        soft_budget_chars=max_chars,
    )


def _build_backlog(cfg: Config, project: str) -> Section:
    """Read BACKLOG.md's 'Open' section verbatim."""
    if not project:
        return Section("backlog", _default_title("backlog"), "_(no project set)_", 100)

    backlog_path = cfg.vault_root / "projects" / project / "BACKLOG.md"
    if not backlog_path.exists():
        return Section(
            key="backlog",
            title=_default_title("backlog"),
            body="_(BACKLOG.md not found — run `mem landing --doc backlog`)_",
            soft_budget_chars=200,
        )

    text = backlog_path.read_text(encoding="utf-8")
    open_section = _slice_markdown_section(text, "Open")
    if not open_section:
        return Section(
            key="backlog",
            title=_default_title("backlog"),
            body="_(no open backlog items)_",
            soft_budget_chars=200,
        )

    return Section(
        key="backlog",
        title=_default_title("backlog"),
        body=open_section,
        soft_budget_chars=3200,
    )


def _build_recent_decisions(cfg: Config, project: str, n: int = 10) -> Section:
    """Last N decisions (proposed or accepted) with title, status, rationale."""
    if _index_missing(cfg):
        return Section(
            key="decisions",
            title=_default_title("decisions"),
            body="_(index not built)_",
            soft_budget_chars=200,
        )

    import sqlite3

    db = sqlite3.connect(str(cfg.index_db))
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            """
            SELECT id, title, date, frontmatter, body_text
            FROM notes
            WHERE type = 'decision'
              AND (? = '' OR project = ?)
            ORDER BY date DESC
            LIMIT ?
            """,
            (project, project, n),
        ).fetchall()
    finally:
        db.close()

    if not rows:
        return Section(
            key="decisions",
            title=_default_title("decisions"),
            body="_(no decisions yet)_",
            soft_budget_chars=200,
        )

    lines = []
    for row in rows:
        try:
            fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
        except json.JSONDecodeError:
            fm = {}
        status = fm.get("status", "proposed")
        summary = fm.get("summary") or _extract_summary(row["body_text"] or "")
        note_id = row["id"]
        title = row["title"] or note_id
        date = row["date"] or ""
        lines.append(
            f"- **[{note_id}] {title}** ({status}, {date})\n  {summary[:200]}"
        )

    return Section(
        key="decisions",
        title=_default_title("decisions"),
        body="\n".join(lines),
        soft_budget_chars=4800,
    )


def _build_open_probes(cfg: Config, project: str, n: int = 20) -> Section:
    """Notes tagged ``probe`` from the last 30 days."""
    if _index_missing(cfg):
        return Section(
            key="probes",
            title=_default_title("probes"),
            body="_(index not built)_",
            soft_budget_chars=200,
        )

    import sqlite3

    db = sqlite3.connect(str(cfg.index_db))
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            """
            SELECT id, title, date, tags
            FROM notes
            WHERE type = 'note'
              AND tags LIKE '%probe%'
              AND (? = '' OR project = ?)
            ORDER BY date DESC
            LIMIT ?
            """,
            (project, project, n * 2),  # overfetch to filter non-matching tags
        ).fetchall()
    finally:
        db.close()

    filtered = []
    for row in rows:
        try:
            tag_list = json.loads(row["tags"]) if row["tags"] else []
        except json.JSONDecodeError:
            tag_list = []
        if "probe" not in tag_list:
            continue
        filtered.append(row)
        if len(filtered) >= n:
            break

    if not filtered:
        return Section(
            key="probes",
            title=_default_title("probes"),
            body="_(no open probes)_",
            soft_budget_chars=200,
        )

    lines = [
        f"- **[{row['id']}]** {row['title']} _({row['date'] or 'no date'})_"
        for row in filtered
    ]
    return Section(
        key="probes",
        title=_default_title("probes"),
        body="\n".join(lines),
        soft_budget_chars=1600,
    )


def _build_concept_histogram(cfg: Config, project: str, n: int = 20) -> Section:
    """Top N concepts by count within the project."""
    if _index_missing(cfg) or not project:
        return Section(
            key="concepts",
            title=_default_title("concepts"),
            body="_(no concept data available)_",
            soft_budget_chars=200,
        )

    import sqlite3

    db = sqlite3.connect(str(cfg.index_db))
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            """
            SELECT nc.concept, COUNT(*) as cnt
            FROM note_concepts nc
            JOIN notes n ON n.id = nc.note_id
            WHERE n.project = ?
            GROUP BY nc.concept
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (project, n),
        ).fetchall()
    finally:
        db.close()

    if not rows:
        return Section(
            key="concepts",
            title=_default_title("concepts"),
            body="_(no concepts indexed yet)_",
            soft_budget_chars=200,
        )

    pieces = [f"`{row['concept']}` ({row['cnt']})" for row in rows]
    return Section(
        key="concepts",
        title=_default_title("concepts"),
        body=", ".join(pieces),
        soft_budget_chars=1200,
    )


def _build_recent_sources(cfg: Config, project: str, n: int = 5) -> Section:
    """Last N source notes added (cross-project — sources are global)."""
    if _index_missing(cfg):
        return Section(
            key="sources",
            title=_default_title("sources"),
            body="_(index not built)_",
            soft_budget_chars=200,
        )

    import sqlite3

    db = sqlite3.connect(str(cfg.index_db))
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            """
            SELECT id, title, date, frontmatter
            FROM notes
            WHERE type = 'source'
            ORDER BY date DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
    finally:
        db.close()

    if not rows:
        return Section(
            key="sources",
            title=_default_title("sources"),
            body="_(no sources yet)_",
            soft_budget_chars=200,
        )

    lines = []
    for row in rows:
        try:
            fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
        except json.JSONDecodeError:
            fm = {}
        source_type = fm.get("source_type", "")
        label = f" [{source_type}]" if source_type else ""
        lines.append(
            f"- `{row['id']}`{label} {row['title'][:100]} _({row['date'] or 'no date'})_"
        )

    return Section(
        key="sources",
        title=_default_title("sources"),
        body="\n".join(lines),
        soft_budget_chars=1600,
    )


def _build_footer() -> Section:
    body = (
        "Retrieval is three modalities. Pick by what you have:\n"
        "- **FTS** — keyword/text. `mem_search(query, mode='fts')`. Empty query = list mode.\n"
        "- **Similarity** — semantic. `mem_search(query, mode='similar')`. "
        "`mode='hybrid'` fuses FTS + similarity (RRF) when uncertain.\n"
        "- **Graph** — structural. `mem_graph(id, depth)`. Specialisations: "
        "`mem_source_lens` (walk out from a source), `mem_decisions_for_file` "
        "(file → decisions), `mem_concept_search` (set ops over concept edges).\n"
        "\n"
        "Compositions:\n"
        "- `mem_context(query, type=['note','decision','theme'])` — FTS → similarity-via-"
        "concept → recency, deduped. Use when you want a budgeted blob.\n"
        "- `mem_project_snapshot(project)` — re-fetch this payload on demand.\n"
        "\n"
        "All filtering primitives accept `since` / `until` (ISO dates), and "
        "`mem_search` accepts `concepts=[...]` for combined text+concept queries.\n"
        "\n"
        "Run `/mem-wrap` before `/clear` or `/exit` to preserve this session's insights."
    )
    return Section(
        key="footer",
        title=_default_title("footer"),
        body=body,
        soft_budget_chars=1200,
    )


def _build_active_themes(cfg: Config, project: str, n: int = 10) -> Section:
    """Active themes — global, optionally filtered by primary stake project.

    Themes live at vault/themes/ regardless of project, so the section is
    cross-project by design but biases toward the current project's
    stake when known.
    """
    if not cfg.index_db.exists():
        return Section(
            key="themes",
            title=_default_title("themes"),
            body="_Index not built._",
            soft_budget_chars=600,
        )

    import sqlite3

    db = sqlite3.connect(str(cfg.index_db))
    db.row_factory = sqlite3.Row
    try:
        # Project-stake themes first, then any other active themes.
        rows = db.execute(
            "SELECT id, title, project, frontmatter FROM notes "
            "WHERE type = 'theme' "
            "ORDER BY (project = ?) DESC, date DESC LIMIT ?",
            (project, n),
        ).fetchall()
    finally:
        db.close()

    if not rows:
        return Section(
            key="themes",
            title=_default_title("themes"),
            body="_No themes recorded yet._",
            soft_budget_chars=600,
        )

    import json as _json

    lines: list[str] = []
    for r in rows:
        fm = _json.loads(r["frontmatter"]) if r["frontmatter"] else {}
        if str(fm.get("status", "active")) != "active":
            continue
        proj = r["project"] or fm.get("project", "—")
        lines.append(f"- `{r['id']}` **{r['title']}** ({proj})")

    body = "\n".join(lines) if lines else "_No active themes._"
    return Section(
        key="themes",
        title=_default_title("themes"),
        body=body,
        soft_budget_chars=1200,
    )


# ---------------------------------------------------------------------------
# Assembly + budget
# ---------------------------------------------------------------------------


def _assemble(collected: dict[str, Section], wanted: list[str], max_chars: int) -> str:
    """Render sections in the requested order, dropping whole sections if over budget."""
    # Preserve wanted order, skipping missing sections.
    ordered = [collected[k] for k in wanted if k in collected]

    rendered = _render(ordered)
    if len(rendered) <= max_chars:
        return rendered

    # Over budget — drop sections in _DROP_ORDER until we fit. Never drop
    # header/tools/state/sessions/footer.
    kept = {s.key: s for s in ordered}
    for drop_key in _DROP_ORDER:
        if drop_key in kept:
            del kept[drop_key]
            remaining = [s for s in ordered if s.key in kept]
            rendered = _render(remaining)
            if len(rendered) <= max_chars:
                return rendered

    # Still over — truncate the sessions body, then state body, as last resort.
    # This preserves the structure but allows big STATE/history files to fit.
    remaining = [s for s in ordered if s.key in kept]
    for target_key in ("sessions", "state"):
        for s in remaining:
            if s.key == target_key and len(s.body) > 2000:
                s.body = (
                    s.body[: max(2000, max_chars // 4)]
                    + "\n\n_... (truncated to fit budget)_"
                )
        rendered = _render(remaining)
        if len(rendered) <= max_chars:
            return rendered

    # Hard cap as a last resort — guarantees we never blow the token budget.
    return rendered[: max_chars - 40] + "\n\n_... (payload capped)_"


def _render(sections: list[Section]) -> str:
    parts = []
    for s in sections:
        parts.append(f"## {s.title}\n\n{s.body}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Helpers — text parsing + index inspection
# ---------------------------------------------------------------------------


def _extract_summary(body_text: str) -> str:
    """Pull the ``## Summary`` paragraph from a note body, or fall back to first paragraph."""
    if not body_text:
        return ""
    lines = body_text.split("\n")
    in_summary = False
    collected: list[str] = []
    for line in lines:
        if line.strip().lower().startswith("## summary"):
            in_summary = True
            continue
        if in_summary:
            if line.startswith("## "):
                break
            if line.strip():
                collected.append(line.strip())
            elif collected:
                break
    if collected:
        return " ".join(collected)[:600]

    # Fallback: first non-heading paragraph.
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("---"):
            return s[:600]
    return ""


def _extract_insight_titles(body_text: str) -> list[str]:
    """Pull bolded insight titles from a ``## Candidate Insights`` section."""
    if not body_text or "Insights" not in body_text:
        return []

    titles: list[str] = []
    in_section = False
    for line in body_text.split("\n"):
        if line.strip().startswith("## ") and "Insight" in line:
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            stripped = line.strip()
            if stripped.startswith("- **") and "**" in stripped[4:]:
                # Extract text between the first pair of ** markers
                start = stripped.index("**") + 2
                end = stripped.index("**", start)
                titles.append(stripped[start:end])
    return titles


def _slice_markdown_section(text: str, heading: str) -> str:
    """Return the body of the ``## heading`` section (up to the next ``##``)."""
    lines = text.split("\n")
    start = None
    for i, line in enumerate(lines):
        if line.strip().lower().startswith(f"## {heading.lower()}"):
            start = i + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


def _index_missing(cfg: Config) -> bool:
    return not cfg.index_db.exists()


def _index_counts(cfg: Config, project: str) -> dict[str, int]:
    """Count notes by type for this project."""
    if _index_missing(cfg):
        return {}
    import sqlite3

    db = sqlite3.connect(str(cfg.index_db))
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            """
            SELECT type, COUNT(*) as cnt
            FROM notes
            WHERE (? = '' OR project = ?)
            GROUP BY type
            """,
            (project, project),
        ).fetchall()
    finally:
        db.close()
    return {row["type"]: row["cnt"] for row in rows}
