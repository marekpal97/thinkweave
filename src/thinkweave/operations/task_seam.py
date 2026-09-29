"""The dispatch seam — live task-boundary capture at real task edges.

Every execution route mints its task stub at its existing choke point (the
Claude Code SubagentStart hook, ``weave task open`` for headless dispatch)
and records the close at the matching boundary. Boundaries captured here are
ground truth; retroactive inference over transcripts is banned. Lifecycle
rows land only in the per-session events register (``operations.hook_events``
owns the writers); the ledger view is :func:`task_ledger`, a projection —
open and close pair by the vault-minted task id with no ordering or timing
dependence. The task id is minted at open, rides the dispatch descriptor
(:class:`TaskDispatch`), and names the envelope return file the performer
appends to; at close those rows compile into one entry of the stub note's
``rounds[]`` ledger.

:func:`reconcile_tasks` is the other half of the same system: the wrap
task pass, run by ``weave wrap-finalize`` (and by the dream-wrap catch-up
through the same verb) over the declaration file the wrap LLM composed.
Wrap reconciles only what the seam cannot see — declaration is not
inference.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from thinkweave.core.task_contract import (
    TASK_KIND,
    envelope_return_name,
    validate_envelope,
    validate_task_note,
)
from thinkweave.operations import hook_events


@dataclass(frozen=True)
class TaskDispatch:
    """The dispatch descriptor — what the seam hands the performer."""

    task_id: str
    grain: str
    envelope_return: str
    note: str
    title: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TaskClose:
    """What one boundary close recorded and compiled."""

    task_id: str
    note: str
    envelopes: int
    errors: tuple[str, ...]


def mint_task_id() -> str:
    """A vault-minted task id in portable charset — never a harness id."""
    return f"tsk-{uuid.uuid4().hex[:8]}"


def envelope_path(cfg, task_id: str) -> Path:
    return cfg.weave_dir / "tasks" / envelope_return_name(task_id)


def agent_ref(harness: str, agent_id: str) -> dict:
    """A harness agent id as its qualified session_ref triple."""
    return {"harness": harness, "kind": "agent_id", "value": agent_id}


def _mint_stub(
    cfg,
    *,
    session_key: str,
    project: str,
    title: str,
    grain: str,
    role: str = "",
    harness: str = "",
) -> tuple[str, Path, str]:
    """Mint one conforming stub note; returns (task_id, path, title)."""
    from thinkweave.core.schemas import NoteType
    from thinkweave.core.vault import VaultManager, parse_frontmatter

    task_id = mint_task_id()
    fm: dict = {
        "kind": TASK_KIND,
        "status": "open",
        "grain": grain,
        "rounds": [],
        "title": title or f"Task {task_id}",
    }
    if role:
        fm["role"] = role
    if harness:
        fm["harness"] = harness

    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    # The note is filed by the task id itself, so no harness value can ever
    # reach a filename; the human title lives in frontmatter.
    note_path = vm.create_note(
        NoteType.NOTE,
        title=task_id,
        project=project,
        extra_frontmatter=fm,
        session_id=session_key,
        note_id=task_id,
    )
    written, _ = parse_frontmatter(note_path.read_text(encoding="utf-8"))
    errors = validate_task_note(written)
    if errors:
        raise ValueError(f"task stub does not conform: {errors}")
    return task_id, note_path, fm["title"]


def open_task(
    cfg,
    *,
    session_key: str,
    project: str = "",
    title: str = "",
    grain: str = "per-dispatch",
    role: str = "",
    harness: str = "",
    session_ref: dict | None = None,
) -> TaskDispatch:
    """Mint the task at the dispatch boundary: stub note + register row."""
    task_id, note_path, title = _mint_stub(
        cfg,
        session_key=session_key,
        project=project,
        title=title,
        grain=grain,
        role=role,
        harness=harness,
    )
    # The return path is handed out here, so the directory it names must
    # exist here — a performer's first append never creates directories.
    envelope_path(cfg, task_id).parent.mkdir(parents=True, exist_ok=True)

    hook_events.append_task_event(
        cfg.weave_dir,
        session_key,
        hook_events.task_open_event(
            task_id,
            _now(),
            session_id=session_key,
            grain=grain,
            session_ref=session_ref,
        ),
    )
    return TaskDispatch(
        task_id=task_id,
        grain=grain,
        envelope_return=str(envelope_path(cfg, task_id)),
        note=str(note_path),
        title=title,
    )


def close_task(
    cfg,
    task_id: str,
    *,
    session_key: str,
    session_ref: dict | None = None,
) -> TaskClose:
    """Record the boundary close and compile the round.

    The performer's envelope rows (from the return file the task id names)
    become one ``rounds[]`` entry on the stub; invalid rows are reported in
    ``errors``, never silently dropped, and the close row is recorded either
    way — boundary truth does not depend on the performer's output shape.
    """
    from thinkweave.core.vault import VaultManager, parse_frontmatter

    stub = find_stub(cfg, task_id)
    if stub is None:
        raise ValueError(f"no task stub for {task_id}")

    envelopes, errors = _read_envelopes(envelope_path(cfg, task_id), task_id)

    fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
    round_entry: dict = {"envelopes": envelopes}
    if session_ref:
        round_entry["session_ref"] = session_ref
    rounds = list(fm.get("rounds") or []) + [round_entry]

    vm = VaultManager(config=cfg)
    vm.update_note(
        stub, frontmatter_updates={"status": "closed", "rounds": rounds}
    )

    hook_events.append_task_event(
        cfg.weave_dir,
        session_key,
        hook_events.task_close_event(
            task_id, _now(), session_id=session_key, session_ref=session_ref
        ),
    )
    return TaskClose(
        task_id=task_id,
        note=str(stub),
        envelopes=len(envelopes),
        errors=tuple(errors),
    )


def record_orphan_stop(cfg, *, session_key: str, session_ref: dict | None) -> None:
    """A boundary close that pairs with no open — flagged, never dropped."""
    hook_events.append_task_event(
        cfg.weave_dir,
        session_key,
        hook_events.task_close_event(
            "", _now(), session_id=session_key, orphan=True,
            session_ref=session_ref,
        ),
    )


def render_descriptor(cfg, task_id: str) -> TaskDispatch:
    """Re-render an existing task's dispatch descriptor from its stub."""
    from thinkweave.core.vault import parse_frontmatter

    stub = find_stub(cfg, task_id)
    if stub is None:
        raise ValueError(f"no task stub for {task_id}")
    fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
    return TaskDispatch(
        task_id=task_id,
        grain=str(fm.get("grain", "per-dispatch")),
        envelope_return=str(envelope_path(cfg, task_id)),
        note=str(stub),
        title=str(fm.get("title", "")),
    )


def task_ledger(rows: list[dict]) -> dict[str, dict]:
    """Project register rows into ``{task_id: {"open": row, "close": row}}``.

    Pure pairing by task id — row order and timing carry no meaning. Orphan
    rows (no task id) stay out; they are read straight off the register.
    """
    ledger: dict[str, dict] = {}
    for row in rows:
        task_id = row.get("task_id", "")
        if not task_id:
            continue
        entry = ledger.setdefault(task_id, {"open": None, "close": None})
        if row.get("type") == hook_events.TASK_OPEN:
            entry["open"] = row
        elif row.get("type") == hook_events.TASK_CLOSE:
            entry["close"] = row
    return ledger


def pending_open(rows: list[dict], session_ref: dict) -> str:
    """The task id of the unclosed open annotated with this ref, or ``""``."""
    for task_id, entry in task_ledger(rows).items():
        opened = entry["open"]
        if opened and not entry["close"]:
            if opened.get("session_ref") == session_ref:
                return task_id
    return ""


def closed_task(rows: list[dict], session_ref: dict) -> str:
    """The task id of the already-closed open annotated with this ref, or ``""``.

    Claude Code delivers SubagentStop twice per subagent (observed live
    2026-09-28: a second stop with the same agent_id ~9 s after the close).
    A stop whose ref resolves here is that duplicate — the boundary is
    already recorded, so the handler skips it instead of writing a spurious
    orphan row. A stop matching neither an open nor a closed task still
    lands as an orphan.
    """
    for task_id, entry in task_ledger(rows).items():
        opened = entry["open"]
        if opened and entry["close"]:
            if opened.get("session_ref") == session_ref:
                return task_id
    return ""


@dataclass
class TaskPassResult:
    """What one wrap task pass reconciled — folded into the wrap report."""

    minted: list[str] = field(default_factory=list)
    appended: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)
    reparented: list[str] = field(default_factory=list)
    proposals: list[dict] = field(default_factory=list)
    stamped: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "minted": self.minted,
            "appended": self.appended,
            "closed": self.closed,
            "orphaned": self.orphaned,
            "reparented": self.reparented,
            "proposals": self.proposals,
            "stamped": self.stamped,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def reconcile_tasks(
    cfg,
    declaration: object,
    *,
    session_key: str,
    project: str,
    streams: list[Path],
    folders: list[Path] | None = None,
) -> TaskPassResult:
    """The wrap task pass — reconcile what the seam cannot see.

    Driven entirely by the declaration file the wrap LLM composed
    (``validate_wrap_declaration`` is the gate; an invalid file aborts the
    pass with no writes). Declaration by the model that was present is
    permitted; retroactive inference over exhaust stays banned — every
    other duty here reads only the register and the stubs.

    Duties, in order: solo-lane boundary declaration (mint or one appended
    round on the open work-grain note — never a second note), consumes
    edges, continuation proposals from consumes overlap (proposed in the
    result, never merged silently), explicit-done closure (the only
    closure wrap may perform; ``outcome`` is never written — the dream
    judge owns it), decision ``task_id`` stamps, orphan flags at boundary
    sparsity (an open with no id-matched close; at task-id-only sparsity
    an absent close is not evidence), and root-task re-parenting of seam
    children by interval containment over the session chain's streams
    (a child's open inside a segment's activity span — first non-task row
    to last row — re-parents under the declared root).
    """
    from thinkweave.core.task_contract import validate_wrap_declaration
    from thinkweave.core.vault import VaultManager, parse_frontmatter

    result = TaskPassResult()
    errors = validate_wrap_declaration(declaration)
    if errors:
        result.errors.extend(errors)
        return result
    assert isinstance(declaration, dict)
    sparsity = declaration.get("sparsity", "boundary")
    primary = streams[0] if streams else hook_events.register_path(
        cfg.weave_dir, session_key
    )

    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    open_stubs = [
        (p, fm) for p, fm in _all_stubs(cfg) if fm.get("status") == "open"
    ]
    root_id = ""
    touched: list[Path] = []
    now = _now()
    for entry in declaration["declared"]:
        continuing = str(entry.get("continuing") or "")
        if continuing:
            stub = find_stub(cfg, continuing)
            if stub is None:
                result.errors.append(f"declared: no task stub for {continuing}")
                continue
            fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
            if fm.get("status") != "open":
                result.errors.append(
                    f"declared: {continuing} is closed — a continuation "
                    "cannot reopen it"
                )
                continue
            if fm.get("grain") != "work":
                result.errors.append(
                    f"declared: {continuing} is not work grain — rounds "
                    "accrete on work-grain notes only"
                )
                continue
            task_id = continuing
        else:
            _propose_continuations(entry, open_stubs, result)
            task_id, stub, _title = _mint_stub(
                cfg,
                session_key=session_key,
                project=project,
                title=str(entry.get("title", "")),
                grain=str(entry.get("grain", "work")),
            )
            _append_rows(
                primary,
                [
                    hook_events.task_open_event(
                        task_id,
                        now,
                        session_id=session_key,
                        grain=str(entry.get("grain", "work")),
                    )
                ],
            )
            result.minted.append(task_id)

        updates: dict = {}
        if entry.get("asked"):
            updates["asked"] = entry["asked"]
        if entry.get("consumes"):
            updates["consumes"] = list(entry["consumes"])
        if "round" in entry:
            fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
            updates["rounds"] = list(fm.get("rounds") or []) + [entry["round"]]
            if continuing:
                result.appended.append(task_id)
        if entry.get("done"):
            updates["status"] = "closed"
            _append_rows(
                primary,
                [hook_events.task_close_event(task_id, now, session_id=session_key)],
            )
            result.closed.append(task_id)
        if updates:
            vm.update_note(stub, frontmatter_updates=updates)
        if entry.get("root"):
            root_id = task_id
        touched.append(stub)
        _stamp_decisions(vm, entry, task_id, folders or [], result)

    if sparsity == "boundary":
        _flag_orphans(cfg, vm, streams, result, touched)
    if root_id:
        _reparent_children(cfg, vm, streams, root_id, result, touched)

    for stub in dict.fromkeys(touched):
        fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
        result.errors.extend(
            f"{stub.name}: {e}" for e in validate_task_note(fm)
        )
    return result


# ---------------------------------------------------------------------------
# Wrap-pass plumbing


def _propose_continuations(
    entry: dict, open_stubs: list[tuple[Path, dict]], result: TaskPassResult
) -> None:
    """Consumes overlap with an open task is a continuation *proposal* —
    surfaced for the user, never an implicit merge."""
    declared = set(entry.get("consumes") or [])
    for _path, fm in open_stubs:
        overlap = sorted(declared & set(fm.get("consumes") or []))
        if overlap:
            result.proposals.append(
                {
                    "task_id": str(fm.get("id", "")),
                    "overlap": overlap,
                    "declared_title": str(entry.get("title", "")),
                }
            )


def _stamp_decisions(
    vm, entry: dict, task_id: str, folders: list[Path], result: TaskPassResult
) -> None:
    """Stamp each decision the round declares minted with the task id."""
    from thinkweave.core.vault import parse_frontmatter

    minted = ((entry.get("round") or {}).get("decisions") or {}).get("minted")
    for dec_id in minted or []:
        for path in _folder_notes(folders):
            fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            if fm.get("id") == dec_id:
                vm.update_note(path, frontmatter_updates={"task_id": task_id})
                result.stamped += 1
                break
        else:
            result.warnings.append(
                f"decision {dec_id} not found in the session chain — "
                "task_id stamp skipped"
            )


def _flag_orphans(
    cfg, vm, streams: list[Path], result: TaskPassResult, touched: list[Path]
) -> None:
    """An open with no id-matched close in the register is an orphan.

    Work-grain notes stay open across sessions by design and are never
    orphans; ids this pass just touched carry their own boundary truth.
    """
    from thinkweave.core.vault import parse_frontmatter

    rows: list[dict] = []
    for stream in streams:
        rows.extend(hook_events.task_rows(stream))
    skip = set(result.minted + result.appended + result.closed)
    for task_id, entry in task_ledger(rows).items():
        opened = entry["open"]
        if entry["close"] or opened is None or task_id in skip:
            continue
        if opened.get("grain") == "work":
            continue
        stub = find_stub(cfg, task_id)
        if stub is None:
            result.warnings.append(f"orphan open {task_id} has no stub")
            continue
        fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
        if fm.get("status") == "open" and not fm.get("orphan"):
            vm.update_note(stub, frontmatter_updates={"orphan": True})
            result.orphaned.append(task_id)
            touched.append(stub)


def _reparent_children(
    cfg, vm, streams: list[Path], root_id: str,
    result: TaskPassResult, touched: list[Path],
) -> None:
    """Compile the tree: a seam child whose open falls inside a segment's
    activity span re-parents under the declared root task. Work-grain rows
    are peers, never children; a row outside the span (stale buffer
    carry-over) stays where it is."""
    from thinkweave.core.vault import parse_frontmatter

    for stream in streams:
        rows = _stream_rows(stream)
        span = _activity_span(rows)
        if span is None:
            continue
        start, end = span
        for row in rows:
            if row.get("type") != hook_events.TASK_OPEN:
                continue
            task_id = str(row.get("task_id") or "")
            if not task_id or task_id == root_id or row.get("grain") == "work":
                continue
            ts = _parse_ts(row.get("ts"))
            if ts is None or not (start <= ts <= end):
                continue
            stub = find_stub(cfg, task_id)
            if stub is None:
                result.warnings.append(f"child {task_id} has no stub")
                continue
            fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
            if fm.get("parent") != root_id:
                vm.update_note(stub, frontmatter_updates={"parent": root_id})
                result.reparented.append(task_id)
                touched.append(stub)


def _folder_notes(folders: list[Path]):
    for folder in folders:
        yield from folder.rglob("*.md")


def _all_stubs(cfg) -> list[tuple[Path, dict]]:
    """Every task stub in the vault with its frontmatter.

    ponytail: one rglob over the vault per pass, O(vault files) — same
    ceiling and upgrade path (the SQLite index) as :func:`find_stub`.
    """
    from thinkweave.core.vault import parse_frontmatter

    out: list[tuple[Path, dict]] = []
    for path in cfg.vault_root.rglob("tsk-*.md"):
        fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if fm.get("kind") == TASK_KIND:
            out.append((path, fm))
    return out


def _append_rows(path: Path, rows: list[dict]) -> None:
    """Append lifecycle rows to the stream that holds this session's
    register — the resolved chain file, which may be an archived
    ``events.jsonl`` rather than the key-named live buffer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _stream_rows(path: Path) -> list[dict]:
    """Every row of one register stream, tolerant of malformed lines."""
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _activity_span(rows: list[dict]):
    """(start, end) of one segment's activity: first non-task row to last
    row of the stream. ``None`` for an empty or timestamp-less stream."""
    all_ts = [t for t in (_parse_ts(r.get("ts")) for r in rows) if t]
    activity = [
        t
        for r in rows
        if (t := _parse_ts(r.get("ts")))
        and r.get("type") not in hook_events.TASK_EVENT_TYPES
    ]
    if not all_ts:
        return None
    return min(activity or all_ts), max(all_ts)


def _parse_ts(value) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def find_stub(cfg, task_id: str) -> Path | None:
    """Locate a task stub by its id — the filename the id itself names.

    ponytail: one exact-name rglob over the vault per lookup, O(vault
    files); closes are rare next to retrieval traffic. The upgrade path is
    the SQLite index once stubs are indexed at open.
    """
    return next(cfg.vault_root.rglob(f"{task_id}.md"), None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_envelopes(path: Path, task_id: str) -> tuple[list[dict], list[str]]:
    """Parse and validate the performer's return file: (valid rows, errors)."""
    if not path.exists():
        return [], []
    valid: list[dict] = []
    errors: list[str] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"{path.name}[{i}]: not JSON")
            continue
        row_errors = validate_envelope(row, f"{path.name}[{i}]")
        if not row_errors and row.get("task_id") != task_id:
            row_errors = [f"{path.name}[{i}]: task_id does not match {task_id}"]
        if row_errors:
            errors.extend(row_errors)
        else:
            valid.append(row)
    return valid, errors
