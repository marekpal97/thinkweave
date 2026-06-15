"""Session-extraction operation — the heavy lift behind ``weave_extract``.

Walks a session note, materializes insights as derived notes, materializes
decisions (with auto-flip of superseded decisions), runs the auto-todo pass,
emits dedup suggestions, and finally archives the JSONL event buffer into
the session folder. Pure business logic — returns a structured result; the
MCP / CLI surfaces format the human-readable report.

Imports `core/`, `retrieval/`, `synthesis/`, `sources/`, and the
``thinkweave.core.events`` module — never `surfaces/`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from thinkweave.core.buffer import archive_buffer
from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteMeta, NoteType
from thinkweave.core.vault import (
    VaultManager,
    parse_frontmatter,
    render_frontmatter,
    strip_section,
)
from thinkweave.retrieval.search import Search
from thinkweave.synthesis.concepts import (
    build_keep_set,
    load_ontology,
    split_concepts_by_ontology,
)
from thinkweave.operations import rejudge_queue
from thinkweave.synthesis.prediction import append_verdict


@dataclass
class ExtractOutcome:
    """Structured result from :func:`extract_session`.

    ``session_id`` is whatever the caller passed in — Claude Code UUID,
    minted ses-id, slug, anything. ``session_note_id`` is the canonical
    thinkweave id stamped on the session note's frontmatter (always
    ``ses-XXXXXXXX``). They diverge when the caller passed a non-ses-id
    value and ``weave_extract`` auto-minted a fresh note.

    **Pass ``session_id`` (not ``session_note_id``) to** ``weave wrap-finalize``
    — decisions are stamped with ``source_session = session_id`` (the input
    form), and the judge matches on that field. The format report flags
    this explicitly so callers don't pick the wrong identifier.
    """

    session_id: str
    session_note_id: str = ""
    summary: str = ""
    created_notes: list[NoteMeta] = field(default_factory=list)
    created_decisions: list[NoteMeta] = field(default_factory=list)
    created_todos: list[NoteMeta] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    processed_at: str = ""
    error: str = ""
    skipped_reason: str = ""

    @property
    def all_created(self) -> list[NoteMeta]:
        return self.created_notes + self.created_decisions + self.created_todos


# ---------------------------------------------------------------------------
# Insight parsing helpers (kept here, not in core/, because they speak the
# session note's "## Candidate Insights" markdown convention)
# ---------------------------------------------------------------------------


def parse_candidate_insights(body: str) -> list[dict]:
    """Parse ``## Candidate Insights`` blocks into ``{title, body}`` dicts."""
    insights: list[dict] = []
    in_section = False
    current_block: list[str] = []
    for line in body.split("\n"):
        if line.strip().startswith("## Candidate Insights"):
            in_section = True
            continue
        if in_section and line.strip().startswith("## "):
            break
        if in_section:
            stripped = line.strip()
            if not stripped and current_block:
                _flush_insight(current_block, insights)
                current_block = []
            elif stripped:
                current_block.append(line)
    if current_block:
        _flush_insight(current_block, insights)
    return insights


def _flush_insight(lines: list[str], insights: list[dict]) -> None:
    if not lines:
        return
    title_line = lines[0].strip().lstrip("-#*").strip()
    title_line = re.sub(r"^★\s*Insight[─ ]*", "", title_line).strip()
    if not title_line or all(c in "─-=" for c in title_line):
        for line in lines[1:]:
            candidate = line.strip().lstrip("-#*").strip()
            candidate = re.sub(r"^★\s*Insight[─ ]*", "", candidate).strip()
            if candidate and not all(c in "─-=" for c in candidate):
                title_line = candidate
                break
    if not title_line:
        return
    body = "\n".join(lines[1:]).strip() if len(lines) > 1 else title_line
    body = re.sub(r"\n[─-]{3,}\s*$", "", body).strip()
    insights.append({"title": title_line, "body": body or title_line})


def _append_to_section(path: Path, section_header: str, content: str) -> None:
    text = path.read_text(encoding="utf-8")
    if section_header in text:
        idx = text.index(section_header) + len(section_header)
        nl = text.index("\n", idx)
        text = text[: nl + 1] + content + "\n" + text[nl + 1 :]
    else:
        text = text.rstrip() + f"\n\n{section_header}\n{content}\n"
    path.write_text(text, encoding="utf-8")


def _build_decision_body(rationale: str, title: str, outcome: str) -> str:
    rationale = (rationale or "").lstrip()
    rationale = re.sub(
        r"^##\s+Context\s*\n+", "", rationale, count=1, flags=re.IGNORECASE
    )
    has_decision_hdr = bool(
        re.search(r"^##\s+Decision\b", rationale, flags=re.MULTILINE | re.IGNORECASE)
    )
    has_consequences_hdr = bool(
        re.search(
            r"^##\s+Consequences\b", rationale, flags=re.MULTILINE | re.IGNORECASE
        )
    )
    if has_decision_hdr:
        body = f"## Context\n\n{rationale}"
    else:
        body = f"## Context\n\n{rationale}\n\n## Decision\n\n{title}"
    if outcome == "abandoned" and not has_consequences_hdr:
        body += "\n\n## Consequences\n\nApproach was abandoned."
    return body


def _match_session_commits(dec_note: NoteMeta, session_commits: list) -> list[str]:
    """Commit hashes whose touched files intersect a decision's ``file_paths``.

    Basename-level intersection (paths may be repo-relative on one side and
    vault-relative on the other). Returns ``[]`` when the decision declares no
    files or no session commit touches them — i.e. there is no git evidence
    that this decision actually landed. Shared by the up-flip-to-`accepted`
    pass and the supersession evidence gate so both judge landing identically.
    """
    dec_files = dec_note.frontmatter.get("file_paths", [])
    if not dec_files:
        return []
    dec_basenames = {Path(fp).name for fp in dec_files}
    matched: list[str] = []
    for commit in session_commits:
        commit_files = commit.get("files", [])
        commit_hash = commit.get("hash", "")
        if not commit_hash or not commit_files:
            continue
        if dec_basenames & {Path(f).name for f in commit_files}:
            matched.append(commit_hash)
    return matched


# ---------------------------------------------------------------------------
# Main operation
# ---------------------------------------------------------------------------


def extract_session(
    cfg: Config,
    *,
    session_id: str,
    force: bool = False,
    project: str = "",
    summary: str = "",
    insights: list[dict] | None = None,
    decisions: list[dict] | None = None,
    plan_path: str = "",
    plan_summary: str = "",
) -> ExtractOutcome:
    """Extract insights, decisions, and todos from a session note.

    On success returns an ``ExtractOutcome`` whose lists describe what was
    created. ``error`` / ``skipped_reason`` are set in failure / no-op cases
    so the surface can format the right message.
    """
    outcome = ExtractOutcome(session_id=session_id)

    s = Search(config=cfg)
    session_row = s.get_note_by_id(session_id)
    s.close()

    if session_row and session_row["type"] != "session":
        outcome.error = (
            f"Note {session_id} is type '{session_row['type']}', not 'session'."
        )
        return outcome

    vm = VaultManager(config=cfg)

    if not session_row:
        proj = project or cfg.default_project
        title = (summary or "")[:60] or "conversation"
        session_path = vm.create_note(
            note_type=NoteType.SESSION,
            title=title,
            body="## Summary\n\n## Events\n",
            project=proj,
            extra_frontmatter={"source_session": session_id},
        )
        idx = Indexer(config=cfg)
        idx.index_file(session_path)
        idx.close()
    else:
        session_path = vm.root / session_row["path"]
        if not session_path.exists():
            outcome.error = f"Session file not found: {session_row['path']}"
            return outcome

    session_note = vm.read_note(session_path)
    # Surface the canonical (auto-minted or pre-existing) thinkweave id
    # alongside the caller's input `session_id`. The two diverge when the
    # caller passed something other than a `ses-XXX` value (e.g. a Claude
    # Code UUID). Format report uses this to flag the right wrap-finalize arg.
    outcome.session_note_id = session_note.id

    if session_note.frontmatter.get("processed") and not force:
        processed_at = session_note.frontmatter.get("processed_at", "unknown date")
        outcome.skipped_reason = (
            f"Session {session_id} already processed on {processed_at}. "
            "Use force=true to re-extract."
        )
        return outcome

    # Generation-is-synthesis for imported sessions. An imported Claude Code
    # session is materialised holding its verbatim transcript; the first time
    # it's synthesised (here, via either the batch or the inline backend —
    # both reach extract_session) that transcript is archived to a
    # `transcript.md` companion so the note body reads as the synthesis, not
    # the raw feed. Tightly gated: only fires for a not-yet-processed
    # `imported_from: claude-code` note that still carries the `## Transcript`
    # dump, so it never touches a live `/weave-wrap` session. This is the one
    # place the import body-shape transform lives — keeping both backends
    # byte-for-byte identical.
    if (
        session_note.frontmatter.get("imported_from") == "claude-code"
        and "## Transcript" in session_note.body
    ):
        from thinkweave.synthesis.session_synthesis import archive_transcript

        archive_transcript(session_path)
        session_note = vm.read_note(session_path)

    proj = session_note.project

    if plan_path or plan_summary:
        plan_ctx: dict = {}
        if plan_path:
            plan_ctx["path"] = plan_path
        if plan_summary:
            plan_ctx["summary"] = plan_summary
        vm.update_note(
            session_path,
            frontmatter_updates={"context": {"plan": plan_ctx}},
        )

    session_dir = session_path.parent
    idx = Indexer(config=cfg)
    for md_file in session_dir.glob("*.md"):
        if md_file.name == "session.md":
            continue
        try:
            fm, _ = parse_frontmatter(md_file.read_text(encoding="utf-8"))
            derived = fm.get("derived_from", [])
            if isinstance(derived, str):
                derived = [derived]
            if session_id in derived or session_note.id in derived:
                rel = str(md_file.relative_to(vm.root))
                idx._remove_by_path(rel)
                md_file.unlink()
        except Exception:
            continue

    insights_in = insights if insights else parse_candidate_insights(session_note.body)
    # Cap how many insight notes one extraction creates (config
    # ``extract.insights_cap``, default 3).
    insights_in = insights_in[: int(getattr(cfg, "extract_insights_cap", 3))]
    decisions_in = decisions or []

    # Strict creation policy: terms not in the merged ontology (seed +
    # vault override) cannot reach canonical `concepts:` from extraction.
    # They flow into `proposed_concepts:` and surface in
    # /weave-resolve-concepts at count >= DRIFT_COUNT_THRESHOLD for review.
    ontology_keep = build_keep_set(load_ontology())

    for insight in insights_in:
        title = insight["title"]
        body = insight["body"]
        tags = insight.get("tags", [])
        canonical, proposed = split_concepts_by_ontology(
            insight.get("concepts", []),
            proposed=insight.get("proposed_concepts", []),
            ontology_keep=ontology_keep,
        )
        extra_fm: dict = {"derived_from": [session_id]}
        if canonical:
            extra_fm["concepts"] = canonical
        if proposed:
            extra_fm["proposed_concepts"] = proposed
        path = vm.create_note(
            note_type=NoteType.NOTE,
            title=title,
            body=body,
            project=proj,
            tags=tags,
            extra_frontmatter=extra_fm,
            output_dir=session_path.parent,
        )
        idx.index_file(path)
        outcome.created_notes.append(vm.read_note(path))

    # Commits the session hooks captured. The single source of git evidence
    # for this wrap pass — it both up-flips committed decisions to `accepted`
    # and gates whether a `supersedes:` declaration is allowed to flip its
    # predecessors to `superseded` (see the supersession block below).
    session_commits = session_note.frontmatter.get("commits", [])

    for dec in decisions_in:
        outcome_value = dec.get("outcome", "committed")
        # Tightened semantics (B8, 2026-05-29): every decision lands as
        # `proposed` regardless of outcome. The commit_refs-matching pass
        # below flips up to `accepted` ONLY when at least one matching
        # commit hash is found on the session. This guarantees every
        # `accepted` decision carries commit evidence — the status badge
        # becomes load-bearing again. The user-asserted classification
        # remains visible via the `committed: bool` field below.
        extra_fm = {
            "status": "proposed",
            "committed": outcome_value == "committed",
            "source_session": session_id,
            "derived_from": [session_id],
        }
        if dec.get("file_paths"):
            extra_fm["file_paths"] = dec["file_paths"]
        dec_canonical, dec_proposed = split_concepts_by_ontology(
            dec.get("concepts", []),
            proposed=dec.get("proposed_concepts", []),
            ontology_keep=ontology_keep,
        )
        if dec_canonical:
            extra_fm["concepts"] = dec_canonical
        if dec_proposed:
            extra_fm["proposed_concepts"] = dec_proposed
        if dec.get("supersedes"):
            extra_fm["supersedes"] = dec["supersedes"]
        if dec.get("cites"):
            extra_fm["cites"] = dec["cites"]
        if dec.get("plan_ref"):
            extra_fm["plan_ref"] = dec["plan_ref"]
        if dec.get("summary"):
            extra_fm["summary"] = dec["summary"]
        if dec.get("predicted_outcome"):
            # Optional forward-looking text the wrap composer (or caller)
            # writes when recording the decision. Judged later by the
            # ``/judge-prediction`` skill — feeds the `prediction.match`
            # column in the RLVR export.
            #
            # Canonical shape: a prose string carrying claim + manifestation
            # pointer (where/when/what query verifies it). Structured-dict
            # callers (the legacy ``{family, text, polarity}`` form) still
            # roundtrip through the passthrough below for back-compat, but
            # will be deprecated in a future phase — new callers should
            # always pass a string.
            extra_fm["predicted_outcome"] = dec["predicted_outcome"]
            # Seed an initial ``pending`` history entry so the cron drain
            # has something to find. Only when no prior history exists —
            # if the caller passed their own ``prediction_history``,
            # respect it.
            if not extra_fm.get("prediction_history"):
                extra_fm.update(
                    append_verdict({}, match="pending", reason="awaiting evidence")
                )
        dec_body = _build_decision_body(
            dec.get("rationale", ""), dec["title"], outcome_value
        )
        path = vm.create_note(
            note_type=NoteType.DECISION,
            title=dec["title"],
            body=dec_body,
            project=proj,
            tags=dec.get("tags", []),
            extra_frontmatter=extra_fm,
            output_dir=session_path.parent,
        )
        idx.index_file(path)
        outcome.created_decisions.append(vm.read_note(path))

        # New decision's canonical id (from the freshly-indexed note frontmatter).
        # Used both for the status writeback below and for the rejudge-queue
        # entry — the judge skill threads ``successor_decision_id`` through to
        # the LLM via the worklist payload.
        new_decision_id = outcome.created_decisions[-1].id

        supersedes_raw = dec.get("supersedes") or []
        if isinstance(supersedes_raw, str):
            supersedes_raw = [supersedes_raw]

        # Passive supersession. A bare `supersedes:` declaration is a
        # re-judge *trigger*, not proof the replacement landed — so we only
        # enqueue the predecessor here and never flip its `status` from this
        # turn (symmetric with the headless ``operations/notes.py`` path; the
        # old eager assertion-based flip is gone). The evidence-bearing
        # structural judge owns the flip: ``wrap-finalize`` re-judges these
        # predecessors with this session's commits in hand, and ``dream
        # apply`` does the same for the headless/deferred backlog. Either way
        # the ``superseded`` badge only lands when git-blame survival shows
        # the predecessor's committed lines were actually replaced — keeping
        # it as load-bearing as the B8-gated ``accepted`` badge.
        for target_id in supersedes_raw:
            # Idempotent on decision_id, so re-extracts of the same session
            # don't pile up entries.
            try:
                rejudge_queue.enqueue(
                    cfg,
                    decision_id=target_id,
                    reason=f"superseded by {new_decision_id}",
                    source="supersession",
                )
            except Exception:
                pass

    if outcome.created_decisions and session_commits:
        for dec_note in outcome.created_decisions:
            matched_hashes = _match_session_commits(dec_note, session_commits)
            if matched_hashes:
                # Tightened semantics (B8): the up-flip to `accepted`
                # happens here, gated on real commit evidence. Decisions
                # whose outcome was `committed` but whose file_paths
                # don't intersect any session commit stay `proposed`.
                fm_update: dict = {"commit_refs": matched_hashes}
                if dec_note.frontmatter.get("committed"):
                    fm_update["status"] = "accepted"
                vm.update_note(
                    vm.root / dec_note.path,
                    frontmatter_updates=fm_update,
                )

    try:
        if outcome.created_decisions:
            from thinkweave.synthesis.concepts import (
                get_all_concepts,
                suggest_similar,
            )

            all_concepts = get_all_concepts(idx.db)
            existing_list = list(all_concepts.keys())
            s2 = Search(config=cfg)
            for dec_note in outcome.created_decisions:
                dec_concepts = dec_note.frontmatter.get("concepts", [])
                if dec_concepts:
                    for concept in dec_concepts:
                        similar = suggest_similar(concept, existing_list)
                        for sim in similar:
                            if sim != concept.lower():
                                outcome.suggestions.append(
                                    f"  ⚠ Concept '{concept}' is similar to existing "
                                    f"'{sim}' ({all_concepts.get(sim, 0)} notes). "
                                    f"Consider weave_concepts_merge if they mean the same thing."
                                )
                    for concept in dec_concepts[:3]:
                        results = s2.search(query=concept, note_type="source", limit=3)
                        for r in results:
                            outcome.suggestions.append(
                                f"  Tip: {dec_note.id} shares concept '{concept}' with "
                                f"source {r.title} ({r.id}). Consider weave_link with cites."
                            )
            s2.close()
    except Exception:
        pass

    try:
        from thinkweave.acquisition.sources import load_user_config

        cfg_merged = load_user_config(cfg.vault_root)
        if cfg_merged.get("auto_todo_extraction", True):
            from thinkweave.core.events import extract_todos

            # Scan ONLY the raw session narrative — never the freshly-composed
            # insight bodies / decision rationales. Those are curated prose, and
            # the intended channel for a deliberate todo there is the explicit
            # `todo` tag, not regex-mining. Feeding rationale prose to the
            # extractor produced garbage todos whenever the prose merely
            # discussed the word "todo" (e.g. "legacy todo-tag queue → …").
            todos = extract_todos(session_note.body, source_session_id=session_id)

            seen_titles: set[str] = set()
            for todo in todos:
                title = todo.text.strip()
                if not title or title.lower() in seen_titles:
                    continue
                seen_titles.add(title.lower())
                extra_fm = {
                    "derived_from": [session_id],
                    "auto_extracted": True,
                }
                path = vm.create_note(
                    note_type=NoteType.NOTE,
                    title=title[:80],
                    body=f"Auto-extracted TODO: {title}",
                    project=proj,
                    tags=["todo", "auto"],
                    extra_frontmatter=extra_fm,
                    output_dir=session_path.parent,
                )
                idx.index_file(path)
                outcome.created_todos.append(vm.read_note(path))
    except Exception:
        pass

    summary_text = summary
    if not summary_text and outcome.all_created:
        note_titles = ", ".join(n.title for n in outcome.created_notes)
        dec_titles = ", ".join(n.title for n in outcome.created_decisions)
        parts = []
        if note_titles:
            parts.append(f"{len(outcome.created_notes)} notes: {note_titles}")
        if dec_titles:
            parts.append(f"{len(outcome.created_decisions)} decisions: {dec_titles}")
        summary_text = f"Extracted {'; '.join(parts)}."
    outcome.summary = summary_text

    if summary_text:
        if force and "## Summary" in session_note.body:
            cur_text = session_path.read_text(encoding="utf-8")
            cur_fm, cur_body = parse_frontmatter(cur_text)
            cur_body = strip_section(cur_body, "## Summary")
            cur_body = cur_body.rstrip() + f"\n\n## Summary\n{summary_text}\n"
            session_path.write_text(
                render_frontmatter(cur_fm) + "\n\n" + cur_body, encoding="utf-8"
            )
        elif "## Summary" in session_note.body:
            _append_to_section(session_path, "## Summary", summary_text)
        else:
            vm.update_note(session_path, body_append=f"## Summary\n{summary_text}")

    today = date.today().isoformat()
    outcome.processed_at = today
    fm_updates: dict = {"processed": True, "processed_at": today}
    if session_note.frontmatter.get("auto_extracted"):
        fm_updates["auto_extracted"] = False
    vm.update_note(
        session_path,
        frontmatter_updates=fm_updates,
    )

    session_text = session_path.read_text(encoding="utf-8")
    fm_part, body_part = parse_frontmatter(session_text)
    cleaned_body = strip_section(body_part, "## Events")
    cleaned_body = strip_section(cleaned_body, "## Candidate Insights")
    if cleaned_body != body_part:
        content = render_frontmatter(fm_part) + "\n\n" + cleaned_body
        session_path.write_text(content, encoding="utf-8")

    idx.index_file(session_path)
    idx.close()

    try:
        source_session = session_note.frontmatter.get("source_session", session_id)
        archive_buffer(cfg.weave_dir, source_session, session_path.parent)
    except Exception:
        pass

    return outcome
