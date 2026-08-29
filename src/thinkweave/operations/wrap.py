"""End-of-session wrap finalization — the deterministic tail of ``/wrap``.

``/wrap`` has two phases. The first is an LLM phase: distil a session
digest from the conversation, then write the session's insights and decisions
via ``weave_extract``. The second is *this* — purely deterministic plumbing:
prune stub session folders, (re)index, judge the freshly written decisions
against git evidence, and refresh the DECISIONS / BACKLOG landing docs, plus a
read-only concept-drift advisory.

Bundling that chain into one in-process call is the whole point: it used to be
~5 separate MCP round-trips, each costing a full model turn on whatever model
the wrap session was running. Here it's one ``weave wrap-finalize`` Bash call
with **zero** model turns.

Pure orchestration over existing operations — returns a structured result; the
CLI surface (``surfaces/cli/wrap.py``) formats the human-readable report.
Imports ``core/`` / ``operations/`` / ``synthesis/`` only — never ``surfaces/``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from thinkweave.core.config import Config


@dataclass
class WrapFinalizeResult:
    """Structured outcome of :func:`finalize_wrap`."""

    session_id: str
    project: str = ""
    orphans_pruned: int = 0
    orphans_freed_bytes: int = 0
    indexed: int = 0
    removed: int = 0
    edges: int = 0
    decisions_judged: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)  # verdict -> count
    landing_written: list[str] = field(default_factory=list)
    drift_text: str = ""
    verdicts_written: int = 0
    verdicts_skipped: int = 0
    verdicts_unmatched: int = 0
    prompts_reprojected: int = 0
    errors: list[str] = field(default_factory=list)
    # Per-step wall time (seconds) — keys: verdicts, prune, index, judge,
    # landing, drift. Populated even when a step errors, so a slow failure
    # is visible.
    timings: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "project": self.project,
            "orphans_pruned": self.orphans_pruned,
            "orphans_freed_bytes": self.orphans_freed_bytes,
            "indexed": self.indexed,
            "removed": self.removed,
            "edges": self.edges,
            "decisions_judged": self.decisions_judged,
            "verdicts": self.verdicts,
            "landing_written": self.landing_written,
            "drift_text": self.drift_text,
            "verdicts_written": self.verdicts_written,
            "verdicts_skipped": self.verdicts_skipped,
            "verdicts_unmatched": self.verdicts_unmatched,
            "errors": self.errors,
            "timings": self.timings,
        }


# The registers the prompt-verdict rail accepts. ``correction`` /
# ``confirmation`` persist as frozen-shape ``feedback`` events (the human
# reward channel); ``probe`` persists as a sibling ``probe`` event that
# ``extract_prompts`` joins back onto the prompt as
# ``classification="probe"`` (probe pressure, /discover, the prompts
# projection). ``neutral`` is not an event — the wrap LLM only lists the
# non-neutral verdicts.
_VERDICT_REGISTERS = frozenset({"correction", "confirmation", "probe"})


def _resolve_events_file(cfg: Config, session_id: str, project: str) -> Path | None:
    """Locate the session's event stream without creating anything.

    Primary: the archived ``events.jsonl`` in a session folder matched by
    folder-name prefix or frontmatter — ``source_session`` or ``id``, the
    live folder being named after the source UUID with the minted ses- id
    inside (#181). Among matches the most recently written events file
    wins (folder name breaks exact mtime ties): a forced re-extract mints
    a second folder claiming the same source UUID, and the freshest
    archive is the one the extract just wrote.

    Last resort: the live buffer — keyed by the source UUID, so a ses- id
    also probes via its ``source_session``. Hooks recreate the buffer the
    moment any event fires after ``archive_buffer`` ran, so a buffer
    alongside an archive is post-archive noise; it is the events file
    only when no archive exists (archive failed, or never ran).
    """
    if project:
        sessions_dir = cfg.vault_root / "projects" / project / "sessions"
        if sessions_dir.exists():
            from thinkweave.core.vault import parse_frontmatter

            best: tuple[float, str, Path] | None = None
            for d in sessions_dir.iterdir():
                if not d.is_dir() or d.name == "misc":
                    continue
                if not d.name.startswith(session_id):
                    sm = d / "session.md"
                    if not sm.exists():
                        continue
                    try:
                        fm, _ = parse_frontmatter(
                            sm.read_text(encoding="utf-8")
                        )
                    except Exception:  # noqa: BLE001
                        continue
                    if session_id not in (
                        fm.get("source_session"),
                        fm.get("id"),
                    ):
                        continue
                events_file = d / "events.jsonl"
                if not events_file.exists():
                    continue
                rank = (events_file.stat().st_mtime, d.name)
                if best is None or rank > best[:2]:
                    best = (*rank, events_file)
            if best:
                return best[2]
    buf = cfg.weave_dir / "buffer" / f"{session_id}.jsonl"
    if buf.exists():
        return buf
    if session_id.startswith("ses-"):
        src = _source_session_of(cfg, session_id)
        if src:
            src_buf = cfg.weave_dir / "buffer" / f"{src}.jsonl"
            if src_buf.exists():
                return src_buf
    return None


def _session_note_id(cfg: Config, session_id: str, project: str) -> str:
    """Map a wrap's session id (Claude UUID or ``ses-…``) to the session
    note id the index keys prompts by. Falls back to the input unchanged."""
    if session_id.startswith("ses-"):
        return session_id
    events = _resolve_events_file(cfg, session_id, project)
    if events is None:
        return session_id
    sm = events.parent / "session.md"
    if not sm.exists():
        return session_id
    from thinkweave.core.vault import parse_frontmatter

    try:
        fm, _ = parse_frontmatter(sm.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return session_id
    return str(fm.get("id") or session_id)


def _source_session_of(cfg: Config, session_note_id: str) -> str:
    """Inverse of :func:`_session_note_id`: map a ``ses-…`` note id to the
    ``source_session`` its decisions are stamped with (the extract input).
    Empty string when unknown — including when the index doesn't exist yet
    (the verdicts step runs before reindex)."""
    from thinkweave.core.vault import VaultManager
    from thinkweave.retrieval.search import Search

    try:
        s = Search(config=cfg)
        try:
            row = s.get_note_by_id(session_note_id)
        finally:
            s.close()
    except (FileNotFoundError, sqlite3.Error):
        return ""
    if not row:
        return ""
    vm = VaultManager(config=cfg)
    try:
        note = vm.read_note(vm.root / row["path"])
    except (OSError, ValueError, KeyError):
        return ""
    return str(note.frontmatter.get("source_session") or "")


def _append_verdict_events(
    cfg: Config,
    session_id: str,
    project: str,
    verdicts: list[dict],
    result: WrapFinalizeResult,
) -> None:
    """Deterministic half of the async prompt labeler (#101).

    The wrap LLM composes ``[{"prompt": <text-or-prefix>, "register":
    "correction"|"confirmation"|"probe", "about": <referent clause>}, ...]``;
    this matches each verdict against the session's captured prompt events
    (echo-collapsed) and appends ONE event for the single best-matching
    prompt (#181 — a prefix matching several rows must not fan out),
    reusing the prompt event's own ``ts`` so the join back onto the prompt
    is exact.
    ``correction`` / ``confirmation`` write ``feedback`` events (the base
    schema predates #101 and is frozen; ``about`` is an additive optional
    key); ``probe`` writes a sibling ``probe`` event that
    ``extract_prompts`` folds into ``Prompt.classification``.

    ``about`` is the grounding clause — WHAT the feedback was about / what
    the probe sought, composed from full session context — so downstream
    consumers (RLVR export, probe distillation) get an explicit referent
    instead of re-inferring one from a 120-char prompt excerpt. The skill
    rules require grounding-or-discard (a verdict whose referent can't be
    named should not be emitted at all), so an absent ``about`` is legal
    but expected to be rare.

    Idempotent: a verdict whose matched prompt already carries the register
    from a previous wrap is skipped outright, so re-wraps never
    double-write nor spill onto sibling prompts.
    """
    from thinkweave.core.events import Prompt, extract_prompts, feedback_events

    events_file = _resolve_events_file(cfg, session_id, project)
    if events_file is None:
        result.verdicts_unmatched = len(verdicts)
        result.errors.append(
            f"verdicts: no events file found for session {session_id}"
        )
        return

    prompts = extract_prompts(events_file)
    # Two sets with different roles (#181 review): ``preexisting`` holds
    # labels read from the file — a verdict whose candidate already carries
    # the register from a PREVIOUS wrap is a re-wrap duplicate and is
    # skipped outright (falling through would label a prompt the user never
    # judged). ``batch`` holds labels written in THIS call and drives the
    # fall-through so repeated same-prefix verdicts in one batch distribute
    # over distinct prompts.
    preexisting = {
        (r.get("ts", ""), r.get("register", ""))
        for r in feedback_events(events_file)
    }
    preexisting.update(
        (p.ts.isoformat(), "probe")
        for p in prompts
        if p.classification == "probe"
    )
    batch: set[tuple[str, str]] = set()

    lines: list[str] = []
    # Longest needle first (#181 review): the most specific verdict claims
    # its prompt before a broader prefix can take it — in-batch assignment
    # stops depending on the wrap LLM's verdict order.
    for v in sorted(
        verdicts, key=lambda v: -len(str(v.get("prompt", "")).strip())
    ):
        register = str(v.get("register", "")).strip().lower()
        needle = str(v.get("prompt", "")).strip().lower()
        about = str(v.get("about", "")).strip()
        if register not in _VERDICT_REGISTERS:
            result.errors.append(f"verdicts: invalid register {register!r}")
            continue
        if not needle:
            result.errors.append("verdicts: verdict with empty prompt ref")
            continue
        matched = [
            p for p in prompts if p.text.strip().lower().startswith(needle)
        ]
        if not matched:
            result.verdicts_unmatched += 1
            continue
        # #181: one verdict labels one prompt. Duplicate rows of the same
        # text can survive the echo-collapse window (old multi-registration
        # capture, #161) — keep the first row per text. A previously-written
        # label on ANY candidate means this verdict is a re-wrap duplicate:
        # skip it. Otherwise label the first candidate not taken by this
        # batch, so repeated verdicts on DISTINCT same-prefix prompts
        # distribute (identical-text repeats collapsed above count as
        # skipped).
        first_by_text: dict[tuple[str, str], Prompt] = {}
        for p in matched:
            first_by_text.setdefault((p.session_id, p.text), p)
        if any(
            (c.ts.isoformat(), register) in preexisting
            for c in first_by_text.values()
        ):
            result.verdicts_skipped += 1
            continue
        p = next(
            (
                c
                for c in first_by_text.values()
                if (c.ts.isoformat(), register) not in batch
            ),
            None,
        )
        if p is None:
            # Every candidate was taken by this batch — the label is
            # dropped, which is an anomaly worth surfacing, unlike the
            # silent re-wrap skip above.
            result.verdicts_skipped += 1
            result.errors.append(
                f"verdicts: no unlabeled prompt left for {needle!r}"
                f" ({register})"
            )
            continue
        ts_iso = p.ts.isoformat()
        batch.add((ts_iso, register))
        event = {
            "ts": ts_iso,
            "type": "probe" if register == "probe" else "feedback",
            "session_id": p.session_id,
            "prompt_ref": p.text[:120],
        }
        if register != "probe":
            event["register"] = register
        if about:
            event["about"] = about[:300]
        lines.append(json.dumps(event, ensure_ascii=False))

    if lines:
        with events_file.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        result.verdicts_written += len(lines)


def finalize_wrap(
    cfg: Config,
    *,
    session_id: str,
    project: str = "",
    prune: bool = True,
    verdicts: list[dict] | None = None,
) -> WrapFinalizeResult:
    """Run the deterministic post-extraction chain in one process.

    Order:

    0. **verdicts** — append the wrap LLM's prompt verdicts (feedback
       registers + probe labels) as events (:func:`_append_verdict_events`).
       Runs first so the events land before any archival/indexing the
       later steps trigger.
    1. **prune** orphan session folders (conservative GC; ``session_id`` is
       protected). Done first so the reindex in step 2 also drops their rows.
    2. **index** — incremental rebuild. Picks up the notes ``weave_extract`` just
       wrote and removes any pruned folders' rows in the same pass.
    3. **judge** — ``judge_and_writeback(session_id=...)``: verdict + status
       onto each new decision, batched, re-indexing touched files.
    4. **landing** — regenerate DECISIONS.md + BACKLOG.md (cheap; always done).
       STATE.md is *not* touched — refreshing it is an LLM judgment call the
       wrap skill makes, not this deterministic tail.
    5. **drift** — read-only concept-drift advisory; surfaced in the result,
       never acted on here.

    Every step is wrapped: a failure in one is recorded in ``errors`` and the
    rest still run. Returns a :class:`WrapFinalizeResult`.
    """
    result = WrapFinalizeResult(session_id=session_id, project=project)

    # 0. prompt verdicts → events (#101) -----------------------------------
    if verdicts:
        _t = time.perf_counter()
        try:
            _append_verdict_events(cfg, session_id, project, verdicts, result)
        except Exception as e:  # noqa: BLE001 — best-effort labeler
            result.errors.append(f"verdicts: {e}")
        finally:
            result.timings["verdicts"] = time.perf_counter() - _t

    # 1. prune orphan session folders -------------------------------------
    if prune:
        _t = time.perf_counter()
        try:
            from thinkweave.operations.prune import find_orphans, prune_orphans

            orphans = find_orphans(
                cfg, project=project, current_session_id=session_id
            )
            if orphans:
                pr = prune_orphans(orphans, dry_run=False)
                result.orphans_pruned = pr.deleted
                result.orphans_freed_bytes = pr.freed_bytes
        except Exception as e:  # noqa: BLE001 — best-effort GC
            result.errors.append(f"prune: {e}")
        finally:
            result.timings["prune"] = time.perf_counter() - _t

    # 2. reindex ----------------------------------------------------------
    _t = time.perf_counter()
    try:
        from thinkweave.core.indexer import Indexer
        from thinkweave.core.vault import VaultManager

        VaultManager(config=cfg).ensure_dirs()
        idx = Indexer(config=cfg)
        try:
            stats = idx.rebuild(full=False)
        finally:
            idx.close()
        result.indexed = stats.get("indexed", 0)
        result.removed = stats.get("removed", 0)
        result.edges = stats.get("edges", 0)
        # Verdicts landed in events.jsonl, not session.md — the incremental
        # rebuild above won't re-project this session's prompts on its own.
        if verdicts:
            idx = Indexer(config=cfg)
            try:
                result.prompts_reprojected = idx.reproject_session_prompts(
                    _session_note_id(cfg, session_id, project)
                )
            finally:
                idx.close()
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"index: {e}")
    finally:
        result.timings["index"] = time.perf_counter() - _t

    # 3. judge extracted decisions + write back verdict/status ------------
    _t = time.perf_counter()
    try:
        from thinkweave.operations.decisions import (
            judge_and_writeback,
            rejudge_supersession_predecessors,
        )

        judged = judge_and_writeback(cfg, session_id=session_id)
        if not judged and session_id.startswith("ses-"):
            # #181: the extract report hints the minted ses- id, but
            # decisions are stamped with the extract *input* (often a
            # Claude Code UUID). Fall back to that source id so the judge
            # still finds them.
            src = _source_session_of(cfg, session_id)
            if src and src != session_id:
                judged = judge_and_writeback(cfg, session_id=src)
        result.decisions_judged = len(judged)
        for _dec, res in judged:
            verdict = res.get("verdict", "unknown")
            result.verdicts[verdict] = result.verdicts.get(verdict, 0) + 1

        # Evidence-gated supersession flip. weave_extract only *enqueues* a
        # predecessor when a new decision declares ``supersedes: [dec-X]`` —
        # it never flips status. The wrap worker holds this session's commits,
        # so re-judge every such predecessor now: blame survival decides
        # whether the predecessor's lines were actually replaced (→
        # ``superseded``) or still co-contribute (→ ``kept``). Predecessors
        # whose successor isn't committed yet stay put and wait for a later
        # cycle (dream apply drains the headless/deferred backlog).
        pred_ids: list[str] = []
        for _dec, _res in judged:
            sup = _dec.frontmatter.get("supersedes") or []
            if isinstance(sup, str):
                sup = [sup]
            pred_ids.extend(str(s) for s in sup if s)
        if pred_ids:
            pred_judged = rejudge_supersession_predecessors(cfg, pred_ids)
            result.decisions_judged += len(pred_judged)
            for _dec, res in pred_judged:
                verdict = res.get("verdict", "unknown")
                result.verdicts[verdict] = result.verdicts.get(verdict, 0) + 1
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"judge: {e}")
    finally:
        result.timings["judge"] = time.perf_counter() - _t

    # 4. refresh DECISIONS + BACKLOG landing docs -------------------------
    # Two cheap SQL renders, not collapsible: ``write_landing_docs`` has no
    # project-scoped ``all`` value — ``docs="all"`` would wrongly regenerate
    # STATE.md (LLM-owned) and THEMES.md (global). Timings confirm sub-second.
    _t = time.perf_counter()
    if project:
        try:
            from thinkweave.synthesis.landing import write_landing_docs

            for doc in ("decisions", "backlog"):
                written = write_landing_docs(cfg, project, docs=doc)
                result.landing_written.extend(sorted(written.keys()))
        except Exception as e:  # noqa: BLE001
            result.errors.append(f"landing: {e}")
    else:
        result.errors.append("landing: skipped (no project)")
    result.timings["landing"] = time.perf_counter() - _t

    # 5. concept-drift advisory (read-only) -------------------------------
    _t = time.perf_counter()
    try:
        from thinkweave.operations.concepts import drift as concept_drift

        d = concept_drift(cfg, project=project)
        result.drift_text = (d.get("text") or "").strip()
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"drift: {e}")
    finally:
        result.timings["drift"] = time.perf_counter() - _t

    return result
