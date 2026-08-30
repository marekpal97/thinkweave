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
from datetime import datetime
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
    # Compaction-segment chain (#180): the harness session UUIDs this wrap
    # spanned, chronological. Empty for single-segment sessions.
    segments: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Benign anomalies worth surfacing without flipping the exit code
    # (e.g. two verdicts resolving to one prompt).
    warnings: list[str] = field(default_factory=list)
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
            "segments": self.segments,
            "errors": self.errors,
            "warnings": self.warnings,
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


def _resolve_session_chain(
    cfg: Config, session_id: str, project: str
) -> list[tuple[Path, Path | None]]:
    """Locate the logical session's event streams without creating anything.

    Returns ``[(events_file, session_folder-or-None), ...]``, best-ranked
    first: the head is the PRIMARY stream (#181's single-file resolution)
    and the tail is the rest of the compaction-segment chain (#180 — a
    long-running session spans several harness session UUIDs, one per
    context compaction, each buffering its prompts under its own id).

    Membership. Primary candidates match the session's identities — the id
    passed in and, for a ses- id, its ``source_session`` UUID (#181: a
    forced re-extract splits one session across sibling folders). The chain
    then admits sibling folders that share a primary candidate's
    ``logical_session`` (the cross-segment key hooks stamp from the
    transcript's ``bridgeSessionId``) or whose ``source_session`` appears
    in a primary's ``segments:`` list (the durable record a previous wrap
    wrote) — EXCEPT siblings a REAL wrap already processed (``processed``
    with ``auto_extracted`` cleared): ``bridgeSessionId`` is cloud-session
    identity and survives resumption across days, so a shared key alone
    also matches sessions separately worked and wrapped earlier, whose
    prompts that wrap already labeled. The Stop hook's thin auto-extract
    stub (``processed`` + ``auto_extracted``) never labels prompts, and
    earlier segments of a live logical session carry exactly that shape —
    they stay admitted.

    Ranking (#181, unchanged within a class): id-matched folders outrank
    chain-admitted ones; folders whose events actually contain prompt rows
    outrank promptless siblings; then the most recently written file wins,
    folder name breaking exact mtime ties. A member folder without an
    archived ``events.jsonl`` reads its live buffer keyed by its
    ``source_session`` — a buffer ALONGSIDE an archive stays post-archive
    noise (#181), never a chain member.

    Last resort (no folder yielded a file): the live buffer keyed by
    ``session_id``, then by the source UUID.
    """
    from thinkweave.core.events import extract_prompts

    ids = {session_id}
    src = _source_session_of(cfg, session_id) if session_id.startswith("ses-") else ""
    if src:
        ids.add(src)
    ranked: list[tuple[tuple, Path, Path]] = []
    if project:
        sessions_dir = cfg.vault_root / "projects" / project / "sessions"
        if sessions_dir.exists():
            from thinkweave.core.vault import parse_frontmatter

            folders: list[tuple[Path, dict | None]] = []
            for d in sessions_dir.iterdir():
                if not d.is_dir() or d.name == "misc":
                    continue
                fm: dict | None = None
                sm = d / "session.md"
                if sm.exists():
                    try:
                        fm, _ = parse_frontmatter(
                            sm.read_text(encoding="utf-8")
                        )
                    except Exception:  # noqa: BLE001
                        fm = None
                folders.append((d, fm))

            def id_matched(d: Path, fm: dict | None) -> bool:
                if any(d.name.startswith(i) for i in ids):
                    return True
                return fm is not None and (
                    fm.get("source_session") in ids or fm.get("id") in ids
                )

            keys: set[str] = set()
            segment_ids: set[str] = set()
            for d, fm in folders:
                if fm is None or not id_matched(d, fm):
                    continue
                key = str(fm.get("logical_session") or "")
                if key:
                    keys.add(key)
                segment_ids.update(
                    str(s) for s in fm.get("segments") or [] if s
                )

            for d, fm in folders:
                primary = id_matched(d, fm)
                in_chain = fm is not None and (
                    str(fm.get("logical_session") or "") in keys
                    or str(fm.get("source_session") or "") in segment_ids
                )
                if (
                    in_chain
                    and not primary
                    and fm.get("processed")
                    and not fm.get("auto_extracted")
                ):
                    # Already wrapped by a real wrap — its own labeler had
                    # the richer context; today's verdicts must not bind
                    # to its prompts (round 2 blocker).
                    in_chain = False
                if not primary and not in_chain:
                    continue
                ev = d / "events.jsonl"
                if not ev.exists() and fm is not None:
                    sid = str(fm.get("source_session") or "")
                    if sid:
                        ev = cfg.weave_dir / "buffer" / f"{sid}.jsonl"
                if not ev.exists():
                    continue
                rank = (
                    primary,
                    bool(extract_prompts(ev)),
                    ev.stat().st_mtime,
                    d.name,
                )
                ranked.append((rank, ev, d))
    ranked.sort(key=lambda t: t[0], reverse=True)
    out: list[tuple[Path, Path | None]] = []
    seen: set[Path] = set()
    for _rank, ev, d in ranked:
        if ev not in seen:
            seen.add(ev)
            out.append((ev, d))
    if out:
        return out
    buf = cfg.weave_dir / "buffer" / f"{session_id}.jsonl"
    if buf.exists():
        return [(buf, None)]
    if src:
        src_buf = cfg.weave_dir / "buffer" / f"{src}.jsonl"
        if src_buf.exists():
            return [(src_buf, None)]
    return []


def _chain_note_ids(
    chain: list[tuple[Path, Path | None]], session_id: str
) -> list[str]:
    """Note ids of every chain folder, primary first — the notes whose
    prompts need reprojection after verdicts land (#181: for a force-minted
    ses- id the primary can be the SIBLING note owning the source-UUID
    folder; #180: verdicts land across the whole segment chain). Falls back
    to the input id unchanged when nothing resolves."""
    from thinkweave.core.vault import parse_frontmatter

    out: list[str] = []
    for _ev, d in chain:
        sm = d / "session.md" if d is not None else None
        if sm is None or not sm.exists():
            continue
        try:
            fm, _ = parse_frontmatter(sm.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        nid = str(fm.get("id") or "")
        if nid and nid not in out:
            out.append(nid)
    return out or [session_id]


def _record_segments(
    chain: list[tuple[Path, Path | None]], result: WrapFinalizeResult
) -> None:
    """Durable chain record (#180): once a wrap sees more than one segment
    UUID, the primary session note gains ``segments: [uuid, ...]`` in
    chronological order — by each segment's earliest prompt timestamp, NOT
    file mtime: the verdict appends that just ran bumped the labeled
    files' mtimes to now (and mtime isn't durable across clone/restore —
    the same argument the resolver's ranking makes). Promptless segments
    sort last. Markdown-truth for later consumers — the task-trace epic
    (#184) keys subagent attribution on the same list — and honoured by
    the resolver, so the chain survives re-wraps even if a segment's
    ``logical_session`` stamp is lost."""
    if len(chain) < 2:
        return
    primary = chain[0][1]
    if primary is None or not (primary / "session.md").exists():
        return
    from thinkweave.core.events import extract_prompts
    from thinkweave.core.vault import parse_frontmatter, render_frontmatter

    stamped: list[tuple[float, str]] = []
    for ev, d in chain:
        if d is None:
            sid = ev.stem  # raw buffer files are named by their UUID
        else:
            sm = d / "session.md"
            sid = ""
            if sm.exists():
                try:
                    fm, _ = parse_frontmatter(sm.read_text(encoding="utf-8"))
                    sid = str(fm.get("source_session") or "")
                except Exception:  # noqa: BLE001
                    sid = ""
            if not sid:
                # No resolvable UUID — never let a placeholder (the
                # archive's literal "events" stem) into the durable list.
                continue
        prompt_ts = [
            p.ts for p in extract_prompts(ev) if p.ts != datetime.min
        ]
        key = min(prompt_ts).timestamp() if prompt_ts else float("inf")
        stamped.append((key, sid))
    stamped.sort()
    ordered: list[str] = []
    for _ts, sid in stamped:
        if sid not in ordered:
            ordered.append(sid)
    # Gate on distinct UUIDs, not file count: the #181 force-mint shape is
    # two folders over ONE source UUID — not a multi-segment session.
    if len(ordered) < 2:
        return
    # Direct frontmatter write, REPLACE semantics: update_note union-merges
    # list values, which would freeze a previously stored (possibly wrong)
    # order forever — this record must equal the recomputed chronology.
    sm = primary / "session.md"
    fm, body = parse_frontmatter(sm.read_text(encoding="utf-8"))
    if fm.get("segments") != ordered:
        fm["segments"] = ordered
        sm.write_text(
            render_frontmatter(fm) + "\n\n" + body,
            encoding="utf-8",
            newline="\n",
        )
    result.segments = ordered


def _source_session_of(cfg: Config, session_note_id: str) -> str:
    """Inverse of :func:`_session_note_id`: map a ``ses-…`` note id to the
    ``source_session`` its decisions are stamped with (the extract input).
    Empty string when unknown — including when the index doesn't exist yet
    (the verdicts step runs before reindex)."""
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
    try:
        fm = json.loads(row["frontmatter"]) if row.get("frontmatter") else {}
    except json.JSONDecodeError:
        return ""
    return str(fm.get("source_session") or "")


def _append_verdict_events(
    session_id: str,
    verdicts: list[dict],
    result: WrapFinalizeResult,
    chain: list[tuple[Path, Path | None]],
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

    if not chain:
        result.verdicts_unmatched = len(verdicts)
        result.errors.append(
            f"verdicts: no events file found for session {session_id}"
        )
        return

    # #180: prompts from EVERY segment of the logical session join the
    # match pool; each verdict event is appended to the file that holds its
    # prompt, so per-file consumers (probe classification in
    # ``extract_prompts``, reprojection) keep working unchanged.
    prompts: list[tuple[Prompt, Path]] = []
    # Two sets with different roles (#181 review): ``preexisting`` holds
    # labels read from the files — a verdict whose candidate already
    # carries the register from a PREVIOUS wrap is a re-wrap duplicate and
    # is skipped outright (falling through would label a prompt the user
    # never judged). ``batch`` holds the prompts labeled in THIS call —
    # keyed by (segment, timestamp, channel), where the channel folds
    # correction/confirmation into ``feedback`` and keeps ``probe``
    # separate (§C5: one prompt takes ONE feedback label per wrap, but a
    # probe label is orthogonal and may ride the same prompt) — and drives
    # the fall-through so repeated same-prefix verdicts in one batch
    # distribute over distinct prompts.
    preexisting: set[tuple[str, str, str]] = set()
    for events_file, _folder in chain:
        file_prompts = extract_prompts(events_file)
        prompts.extend((p, events_file) for p in file_prompts)
        preexisting.update(
            (r.get("session_id", ""), r.get("ts", ""), r.get("register", ""))
            for r in feedback_events(events_file)
        )
        preexisting.update(
            (p.session_id, p.ts.isoformat(), "probe")
            for p in file_prompts
            if p.classification == "probe"
        )
    # Chronological pool: a needle prefix-matching prompts in two segments
    # claims the earliest one, not whichever file ranked higher (unparsed
    # timestamps sort first — same conservative slot as datetime.min).
    prompts.sort(
        key=lambda pf: (
            pf[0].ts.timestamp()
            if pf[0].ts != datetime.min
            else float("-inf")
        )
    )
    batch: set[tuple[str, str, str]] = set()

    lines: dict[Path, list[str]] = {}
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
        channel = "probe" if register == "probe" else "feedback"
        matched = [
            (p, f)
            for p, f in prompts
            if p.text.strip().lower().startswith(needle)
        ]
        if not matched:
            result.verdicts_unmatched += 1
            continue
        # #181: one verdict labels one prompt. Duplicate rows of the same
        # text can survive the echo-collapse window (old multi-registration
        # capture, #161) — keep the first row per text. A previously-written
        # label on ANY candidate means this verdict is a re-wrap duplicate:
        # skip it. Otherwise label the first free candidate, preferring an
        # exact text match over a mere prefix match (round 4: the shorter
        # needle may BE a later prompt verbatim), so repeated verdicts on
        # DISTINCT same-prefix prompts distribute (identical-text repeats
        # collapsed above count as skipped).
        first_by_text: dict[tuple[str, str], tuple[Prompt, Path]] = {}
        for p, f in matched:
            first_by_text.setdefault((p.session_id, p.text), (p, f))
        if any(
            (c.session_id, c.ts.isoformat(), register) in preexisting
            for c, _f in first_by_text.values()
        ):
            result.verdicts_skipped += 1
            continue
        candidates = sorted(
            first_by_text.values(),
            key=lambda cf: cf[0].text.strip().lower() != needle,
        )
        p, target = next(
            (
                (c, f)
                for c, f in candidates
                if (c.session_id, c.ts.isoformat(), channel) not in batch
            ),
            (None, None),
        )
        if p is None:
            # Every candidate was taken by this batch — the label is
            # dropped. Worth surfacing, unlike the silent re-wrap skip
            # above, but benign (an LLM-output shape, not a failure): a
            # warning, so the wrap-finalize exit code stays 0.
            result.verdicts_skipped += 1
            result.warnings.append(
                f"verdicts: no unlabeled prompt left for {needle!r}"
                f" ({register})"
            )
            continue
        ts_iso = p.ts.isoformat()
        batch.add((p.session_id, ts_iso, channel))
        event = {
            "ts": ts_iso,
            "type": channel,
            "session_id": p.session_id,
            "prompt_ref": p.text[:120],
        }
        if register != "probe":
            event["register"] = register
        if about:
            event["about"] = about[:300]
        lines.setdefault(target, []).append(
            json.dumps(event, ensure_ascii=False)
        )

    for events_file, file_lines in lines.items():
        with events_file.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(file_lines) + "\n")
        result.verdicts_written += len(file_lines)


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

    # Resolve the segment chain ONCE — the verdict join, segments record,
    # prune shield and reprojection all consume the same resolution (it
    # re-reads every candidate's events per call, so 4 calls was 4 scans).
    try:
        chain = _resolve_session_chain(cfg, session_id, project)
    except Exception as e:  # noqa: BLE001
        chain = []
        result.errors.append(f"resolve: {e}")

    # 0. prompt verdicts → events (#101) -----------------------------------
    if verdicts:
        _t = time.perf_counter()
        try:
            _append_verdict_events(session_id, verdicts, result, chain)
        except Exception as e:  # noqa: BLE001 — best-effort labeler
            result.errors.append(f"verdicts: {e}")
        # ponytail: the segments: record only lands on verdict-bearing
        # wraps — a verdict-less multi-segment wrap leaves no durable
        # chain. Upgrade path: hoist _record_segments out of this
        # branch once a verdict-less consumer (task-trace) needs it.
        try:
            _record_segments(chain, result)
        except Exception as e:  # noqa: BLE001 — bookkeeping must not
            # flip the exit code after successful verdict writes
            result.warnings.append(f"segments: {e}")
        result.timings["verdicts"] = time.perf_counter() - _t

    # 1. prune orphan session folders -------------------------------------
    if prune:
        _t = time.perf_counter()
        try:
            from thinkweave.operations.prune import find_orphans, prune_orphans

            # #181/#180: shield every folder the chain resolved to —
            # verdicts were just written there, and their id fields may
            # name a sibling note (force-minted ses- id) or an earlier
            # compaction segment the caller's id doesn't match.
            chain_dirs = {d for _ev, d in chain if d is not None}
            orphans = [
                o
                for o in find_orphans(
                    cfg, project=project, current_session_id=session_id
                )
                if o not in chain_dirs
            ]
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
                result.prompts_reprojected = sum(
                    idx.reproject_session_prompts(nid)
                    for nid in _chain_note_ids(chain, session_id)
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
