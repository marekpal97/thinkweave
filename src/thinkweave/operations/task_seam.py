"""The dispatch seam — live task-boundary capture at real task edges.

Every execution route mints its task stub at its existing choke point (the
Claude Code SubagentStart hook, ``weave task open`` for headless dispatch)
and records the close at the matching boundary. Boundaries captured here are
ground truth; a transcript never places a boundary. At a child's close its
transcript slice is read once (:class:`ChildDigest`) for what the child was
asked, did and produced. Lifecycle
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
import re
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from thinkweave.core.task_contract import (
    TASK_KIND,
    envelope_return_name,
    normalize_devloop_run,
    normalize_tracker_ref,
    validate_envelope,
    validate_task_note,
)
from thinkweave.core import harness
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
    gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChildDigest:
    """What a child's transcript slice says it was asked, did, and produced.

    Read once at the close boundary; the boundaries themselves stay
    seam-given. Every transcript field is optional — whatever the read
    could not recover is named in ``gaps``.
    """

    version: str = ""
    asked: str = ""
    description: str = ""
    model: str = ""
    role: str = ""
    paths: tuple[str, ...] = ()
    commits: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    tools: dict = field(default_factory=dict)
    tool_errors: int = 0
    duration: float | None = None
    success: bool | None = None
    gaps: tuple[str, ...] = ()

    @classmethod
    def read(cls, transcript: Path, *, since: str = "", until: str = "") -> "ChildDigest":
        """Digest one transcript (its slice from the prompt nearest ``since``
        to ``until``, when given); never raises."""
        try:
            return _digest(transcript, since, until)
        except Exception as exc:  # noqa: BLE001 — a close must never fail on a read
            return cls(gaps=(f"digest aborted: {type(exc).__name__}: {exc}",))

    def note_fields(self, fm: dict) -> dict:
        """Stub fields the digest fills; a value already on the stub stands."""
        fill = {
            key: value
            for key, value in (
                ("asked", self.asked), ("model", self.model), ("role", self.role)
            )
            if value and not fm.get(key)
        }
        # The stub's minted placeholder title yields to the dispatch description.
        if self.description and fm.get("title") == f"Task {fm.get('id')}":
            fill["title"] = self.description
        return fill

    def round_fields(self, envelopes: list[dict]) -> dict:
        """The round entry's digest fields; ``envelopes`` contribute outputs."""
        outputs = (
            [{"kind": "file", "ref": p} for p in self.paths]
            + [{"kind": "commit", "ref": c} for c in self.commits]
            + [{"kind": "note", "ref": n} for n in self.notes]
            + [_output_ref(o) for row in envelopes for o in row.get("outputs") or []]
        )
        fields: dict = {
            "tools": dict(self.tools),
            "tool_errors": self.tool_errors,
            "digest": {"version": self.version, "gaps": list(self.gaps)},
        }
        did = {"paths": list(self.paths), "commits": list(self.commits)}
        if any(did.values()):
            fields["did"] = {k: v for k, v in did.items() if v}
        if outputs:
            fields["outputs"] = list({(o["kind"], o["ref"]): o for o in outputs}.values())
        if self.duration is not None:
            fields["cost"] = {"duration": self.duration}
        return fields

    def envelope(self, task_id: str) -> dict | None:
        """The child's own handback claim as an envelope row, when it made one."""
        if self.success is None:
            return None
        row = {"task_id": task_id, "outcome": "success" if self.success else "failure"}
        if self.model:
            row["model"] = self.model
        return row


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
    asked: str = "",
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
    if asked:
        fm["asked"] = asked

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
    asked: str = "",
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
        asked=normalize_tracker_ref(asked, current_repo()) if asked else "",
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
    transcript: Path | None = None,
) -> TaskClose:
    """Record the boundary close and compile the round.

    The performer's envelope rows (from the return file the task id names)
    become one ``rounds[]`` entry on the stub; invalid rows are reported in
    ``errors``, never silently dropped, and the close row is recorded either
    way — boundary truth does not depend on the performer's output shape.
    The child's transcript — ``transcript``, else the session a prompt
    bound to this task — is digested into the stub and the round.
    """
    from thinkweave.core.vault import VaultManager, parse_frontmatter

    stub = find_stub(cfg, task_id)
    if stub is None:
        raise ValueError(f"no task stub for {task_id}")

    now = _now()
    envelopes, errors = _read_envelopes(envelope_path(cfg, task_id), task_id)
    since = ""
    if transcript is None:
        bound = _read_binding(cfg, task_id)
        if bound:
            transcript = Path(bound["transcript_path"])
            since = bound["since"]
            session_ref = session_ref or bound["session_ref"]
    digest = (
        ChildDigest.read(transcript, since=since, until=now) if transcript else None
    )

    fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
    updates: dict = {"status": "closed"}
    round_entry: dict = {"envelopes": envelopes}
    if digest:
        claim = digest.envelope(task_id)
        round_entry = {
            "envelopes": envelopes + ([claim] if claim else []),
            **digest.round_fields(envelopes),
        }
        updates.update(digest.note_fields(fm))
    if session_ref:
        round_entry["session_ref"] = session_ref
    updates["rounds"] = list(fm.get("rounds") or []) + [round_entry]

    vm = VaultManager(config=cfg)
    vm.update_note(stub, frontmatter_updates=updates)

    hook_events.append_task_event(
        cfg.weave_dir,
        session_key,
        hook_events.task_close_event(
            task_id, now, session_id=session_key, session_ref=session_ref
        ),
    )
    return TaskClose(
        task_id=task_id,
        note=str(stub),
        envelopes=len(envelopes),
        errors=tuple(errors),
        gaps=digest.gaps if digest else ("no transcript bound; the round carries no digest",),
    )


def bind_session(
    cfg, task_id: str, *, transcript_path: str, since: str, session_ref: dict
) -> bool:
    """Bind a dispatched session's transcript to an open per-dispatch task.

    A dispatcher binds a session by putting the task id in its prompt; the
    first binding stands. Returns whether this call bound the task.
    """
    from thinkweave.core.vault import parse_frontmatter

    path = _binding_path(cfg, task_id)
    stub = find_stub(cfg, task_id)
    if path.exists() or stub is None:
        return False
    fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
    if fm.get("status") != "open" or fm.get("grain") != "per-dispatch":
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "transcript_path": transcript_path,
            "since": since,
            "session_ref": session_ref,
        }),
        encoding="utf-8",
    )
    return True


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


def session_task_rows(cfg, session_key: str) -> list[dict]:
    """Every task row one session has recorded: archived stream plus live buffer.

    The first Stop of a session archives the live buffer into the session
    folder, and a background subagent outlives that turn — so the live
    register alone misses opens recorded before the archive.
    """
    from thinkweave.core.vault import VaultManager, find_session_note_by_source

    rows: list[dict] = []
    note = find_session_note_by_source(VaultManager(config=cfg), session_key)
    if note is not None:
        rows = hook_events.task_rows(note.parent / "events.jsonl")
    return rows + hook_events.task_rows(
        hook_events.register_path(cfg.weave_dir, session_key)
    )


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
    attached: list[str] = field(default_factory=list)
    stamped: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "minted": self.minted,
            "appended": self.appended,
            "closed": self.closed,
            "orphaned": self.orphaned,
            "attached": self.attached,
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
    """Apply the wrap declaration: the model judged, this pass writes.

    The declaration is the model's judgment about the session's work,
    composed during the ``/wrap`` turn. This pass makes no decisions of
    its own — it validates the declaration
    (``validate_wrap_declaration``; an invalid file aborts with no
    writes), applies each entry, and enforces the contract's invariants.

    For each declared entry:

    - ``continuing: tsk-…`` — the work continues that task. Its
      ``round`` appends to the existing open work-grain note. Never a
      second note; a closed task cannot be reopened.
    - ``asked`` — normalized to its tracker ref (a bare ``#n`` resolves
      against the current repo). Without ``continuing``, the open
      work-grain task carrying that ref is the one the round appends to,
      so every route that works one ticket builds one task.
    - neither — new work. A stub is minted (``title`` required) and the
      ``round``, if present, is its first entry.
    - ``done: true`` — the user said the task is finished, so the note
      closes. This is the only closure wrap performs. ``outcome`` is
      never written — the dream judge owns it.
    - ``children: [tsk-…]`` — the per-dispatch seam tasks this task's
      work dispatched. Attachment is declared, never inferred:
      timestamps cannot attribute a dispatch when independent tasks run
      concurrently, so only the model that dispatched can say which
      task a child served.
    - ``round.decisions.minted`` — those decisions are stamped with the
      task id.
    - ``round`` lands as a ``route: session`` round whose session ref is
      the session note. In a single-task session the round's ``notes``
      (the session's insights) and ``feedback`` (its verdicts) default to
      everything the session recorded; with several tasks only what each
      entry declares is attributed.

    Re-running the pass for the same session re-applies rather than
    duplicates: each round carries the wrapping session's ref and replaces
    that session's earlier round, a mint whose title this session already
    minted reuses that note, and a repeated ``done`` is a no-op.

    After the entries land, one mechanical check runs: orphan flags at
    ``boundary`` sparsity (a per-dispatch open with no id-matched close).
    At ``task-id-only`` sparsity — a catch-up declarer that was not
    present — an absent close is not evidence, so nothing is flagged.
    Every touched note's body is re-rendered from its rounds and the note
    is re-validated against the contract before the pass returns.
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
    touched: list[Path] = []
    now = _now()
    wrap_ref = {
        "harness": harness.active().id,
        "kind": "session_id",
        "value": session_key,
    }
    minted_here = _minted_by_this_session(cfg, streams, session_key)
    session = _SessionRecord.read(folders or [], streams)
    session_ref = session.ref(wrap_ref["harness"]) or wrap_ref
    solo = len(declaration["declared"]) == 1
    repo = current_repo()
    for entry in declaration["declared"]:
        continuing = str(entry.get("continuing") or "")
        asked = normalize_tracker_ref(str(entry.get("asked") or ""), repo)
        if asked.startswith("#"):
            result.warnings.append(
                f"declared: no GitHub remote to resolve {asked} against — "
                "kept bare, so it matches no other route's task"
            )
        resumed = continuing or (asked and _open_task_by_ref(cfg, asked)) or ""
        existing = resumed or minted_here.get(str(entry.get("title", "")), "")
        if existing:
            stub = find_stub(cfg, existing)
            if stub is None:
                result.errors.append(f"declared: no task stub for {existing}")
                continue
            fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
            if fm.get("status") != "open":
                if entry.get("done"):
                    continue
                result.errors.append(
                    f"declared: {existing} is closed — a continuation "
                    "cannot reopen it"
                )
                continue
            if fm.get("grain") != "work" and continuing:
                result.errors.append(
                    f"declared: {continuing} is not work grain — rounds "
                    "accrete on work-grain notes only"
                )
                continue
            task_id = existing
        else:
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
        if asked:
            updates["asked"] = asked
        if entry.get("consumes"):
            updates["consumes"] = list(entry["consumes"])
        if "round" in entry:
            fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
            round_entry = {
                "route": "session", "session_ref": session_ref, **entry["round"],
            }
            if solo:
                for key, found in (
                    ("notes", session.insights), ("feedback", session.feedback),
                ):
                    if found:
                        round_entry.setdefault(key, found)
            if entry.get("children"):
                round_entry.setdefault("children", list(entry["children"]))
            kept = [
                r for r in fm.get("rounds") or []
                if r.get("session_ref") not in (wrap_ref, session_ref)
            ]
            updates["rounds"] = kept + [round_entry]
            if resumed:
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
        touched.append(stub)
        _stamp_decisions(vm, entry, task_id, folders or [], result)
        _attach_children(cfg, vm, entry, task_id, result, touched)

    if sparsity == "boundary":
        _flag_orphans(cfg, vm, streams, result, touched)

    for stub in dict.fromkeys(touched):
        _render_body(cfg, vm, stub)
        fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
        result.errors.extend(
            f"{stub.name}: {e}" for e in validate_task_note(fm)
        )
    return result


def record_devloop_run(
    cfg,
    payload: object,
    *,
    project: str,
    trajectory: str = "",
    session_key: str = "",
) -> str:
    """Land one devloop run as a ``route: devloop`` round; returns the task id.

    The run resolves its task by the tracker ref its issue normalizes to:
    the open work-grain task carrying that ref gains the round (a re-record
    of the same trajectory replaces it), otherwise a work-grain task is
    opened for it. A payload that does not fit the contract raises
    ``ValueError`` before anything is written.
    """
    from thinkweave.core.vault import VaultManager, parse_frontmatter

    probe = normalize_devloop_run(payload, task_id=mint_task_id(), trajectory=trajectory)
    asked = normalize_tracker_ref(probe["asked"], current_repo())
    found = _open_task_by_ref(cfg, asked)
    stub = find_stub(cfg, found) if found else None
    if stub is None:
        stub = Path(
            open_task(
                cfg,
                session_key=session_key or "devloop",
                project=project,
                title=probe["title"],
                grain="work",
            ).note
        )
    fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
    run = normalize_devloop_run(payload, task_id=str(fm["id"]), trajectory=trajectory)
    entry = run["rounds"][0]
    kept = [
        r for r in fm.get("rounds") or []
        if not entry.get("session_ref")
        or r.get("session_ref") != entry["session_ref"]
    ]
    vm = VaultManager(config=cfg)
    vm.update_note(stub, frontmatter_updates={"asked": asked, "rounds": kept + [entry]})
    _render_body(cfg, vm, stub)
    errors = validate_task_note(
        parse_frontmatter(stub.read_text(encoding="utf-8"))[0]
    )
    if errors:
        raise ValueError(f"{stub.name}: {errors}")
    return str(fm["id"])


def render_ledger_body(cfg, fm: dict) -> str:
    """The task body: one wikilink line per round, in ledger order.

    Derived from the rounds alone and never hand-edited. Its wikilinks are
    also the task's graph edges (the indexer types a link to a session as
    ``derived_from``, any other as ``relates_to``). A deliverable whose ref
    equals a child task's output ref names that child.
    """
    from thinkweave.synthesis.concept_hub import safe_hub_maps
    from thinkweave.synthesis.hub import reflink

    idmap, title_map, _ = safe_hub_maps(cfg)

    def link(note_id: str) -> str:
        return reflink(note_id, idmap, title_map)

    lines = []
    for entry in fm.get("rounds") or []:
        credit = _child_credit(cfg, entry.get("children") or [])
        lines.append("- " + " · ".join(_round_parts(entry, link, credit)))
    return "## Rounds\n\n" + ("\n".join(lines) or "_No rounds yet._") + "\n"


def current_repo() -> str:
    """``owner/name`` of the working directory's GitHub ``origin``, or ``""``."""
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    found = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url)
    return found[1] if found else ""


# ---------------------------------------------------------------------------
# Wrap-pass plumbing


@dataclass(frozen=True)
class _SessionRecord:
    """What the wrapped session itself recorded: its session note, the
    insight notes derived from it, and its prompt verdicts."""

    note_id: str = ""
    insights: tuple[str, ...] = ()
    feedback: tuple[dict, ...] = ()

    @classmethod
    def read(cls, folders: list[Path], streams: list[Path]) -> "_SessionRecord":
        from thinkweave.core.events import feedback_events
        from thinkweave.core.vault import parse_frontmatter

        notes = [
            parse_frontmatter(p.read_text(encoding="utf-8"))[0]
            for p in _folder_notes(folders)
        ]
        sessions = [str(fm.get("id")) for fm in notes if fm.get("type") == "session"]
        insights = [
            str(fm["id"]) for fm in notes
            if fm.get("type") == "note" and fm.get("id")
            and not fm.get("kind") and not fm.get("auto_extracted")
            and set(sessions) & set(fm.get("derived_from") or [])
        ]
        verdicts = [
            {k: str(row.get(k, "")) for k in ("register", "prompt_ref", "ts")}
            for stream in streams
            for row in feedback_events(stream)
        ]
        return cls(
            note_id=sessions[0] if sessions else "",
            insights=tuple(insights),
            feedback=tuple(verdicts),
        )

    def ref(self, harness_id: str) -> dict | None:
        if not self.note_id:
            return None
        return {"harness": harness_id, "kind": "note", "value": self.note_id}


def _open_task_by_ref(cfg, asked: str) -> str:
    """The id of the open work-grain task whose ``asked`` is this tracker
    ref, or ``""``.

    ponytail: parses every task stub per lookup, O(task notes); the upgrade
    path is the SQLite index once stubs are indexed at write.
    """
    from thinkweave.core.vault import parse_frontmatter

    for path in cfg.vault_root.rglob("tsk-*.md"):
        fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if (
            fm.get("kind") == TASK_KIND and fm.get("asked") == asked
            and fm.get("status") == "open" and fm.get("grain") == "work"
        ):
            return str(fm.get("id", ""))
    return ""


def _render_body(cfg, vm, stub: Path) -> None:
    from thinkweave.core.vault import parse_frontmatter

    fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
    vm.update_note(stub, body=render_ledger_body(cfg, fm))


def _round_parts(entry: dict, link, credit: dict[str, str]) -> list[str]:
    ref = entry.get("session_ref") or {}
    head = (
        link(ref["value"]) if ref.get("kind") == "note"
        else f"`{ref.get('harness', '')} {ref.get('value', '?')}`"
    )
    parts = [f"{entry.get('route', 'dispatch')} {head}"]
    if entry.get("notes"):
        parts.append("notes " + ", ".join(link(n) for n in entry["notes"]))
    decisions = entry.get("decisions") or {}
    for key in ("minted", "re_served", "reverted"):
        if decisions.get(key):
            label = key.replace("_", "-")
            parts.append(f"decisions {label} " + ", ".join(link(d) for d in decisions[key]))
    if entry.get("feedback"):
        parts.append("feedback " + ", ".join(f.get("register", "?") for f in entry["feedback"]))
    for out in entry.get("outputs") or []:
        target = link(out["ref"]) if out.get("kind") == "note" else out.get("ref", "")
        text = f"{out.get('role', 'output')} {out.get('kind', '')} {target}"
        if out.get("role") == "deliverable" and out.get("ref") in credit:
            text += f" by {link(credit[out['ref']])}"
        parts.append(text)
    commits = (entry.get("did") or {}).get("commits")
    if commits:
        parts.append("commits " + ", ".join(commits))
    return parts


def _child_credit(cfg, children: list[str]) -> dict[str, str]:
    """{output ref: child task id} over the children's recorded outputs."""
    from thinkweave.core.vault import parse_frontmatter

    credit: dict[str, str] = {}
    for child_id in children:
        stub = find_stub(cfg, child_id)
        if stub is None:
            continue
        fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
        for entry in fm.get("rounds") or []:
            for out in entry.get("outputs") or []:
                credit.setdefault(str(out.get("ref", "")), child_id)
    return credit


def _attach_children(
    cfg, vm, entry: dict, task_id: str, result: TaskPassResult,
    touched: list[Path],
) -> None:
    """Write the declared child → parent edges.

    A child is a per-dispatch task the hook seam minted mechanically;
    which declared task it served is the model's call. Work-grain notes
    are peers, never children. A child already attached to a different
    task is an error, not an overwrite — two declarers must not fight
    over one child silently.
    """
    from thinkweave.core.vault import parse_frontmatter

    for child_id in entry.get("children") or []:
        if child_id == task_id:
            result.errors.append(f"children: {child_id} cannot be its own parent")
            continue
        stub = find_stub(cfg, child_id)
        if stub is None:
            result.errors.append(f"children: no task stub for {child_id}")
            continue
        fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
        if fm.get("grain") == "work":
            result.errors.append(
                f"children: {child_id} is work grain — work-grain tasks are "
                "peers, never children"
            )
            continue
        parent = str(fm.get("parent") or "")
        if parent == task_id:
            continue
        if parent:
            result.errors.append(
                f"children: {child_id} is already attached to {parent}"
            )
            continue
        vm.update_note(stub, frontmatter_updates={"parent": task_id})
        result.attached.append(child_id)
        touched.append(stub)


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


def _folder_notes(folders: list[Path]):
    for folder in folders:
        yield from folder.rglob("*.md")


def _minted_by_this_session(
    cfg, streams: list[Path], session_key: str
) -> dict[str, str]:
    """{title: task_id} for tasks an earlier wrap of this session minted.

    The pass writes its own opens without an agent ref; hook-seam opens
    always carry one, so they never match a declared title here.
    """
    from thinkweave.core.vault import parse_frontmatter

    out: dict[str, str] = {}
    for stream in streams:
        for row in hook_events.task_rows(stream):
            if (
                row.get("type") != hook_events.TASK_OPEN
                or row.get("session_id") != session_key
                or row.get("session_ref")
            ):
                continue
            stub = find_stub(cfg, str(row.get("task_id", "")))
            if stub is not None:
                fm, _ = parse_frontmatter(stub.read_text(encoding="utf-8"))
                out.setdefault(str(fm.get("title", "")), str(fm.get("id", "")))
    return out


def _append_rows(path: Path, rows: list[dict]) -> None:
    """Append lifecycle rows to the stream that holds this session's
    register — the resolved chain file, which may be an archived
    ``events.jsonl`` rather than the key-named live buffer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


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


def _binding_path(cfg, task_id: str) -> Path:
    return cfg.weave_dir / "tasks" / f"{task_id}.bind.json"


def _read_binding(cfg, task_id: str) -> dict | None:
    path = _binding_path(cfg, task_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Child-digest plumbing — one reader for the Claude Code transcript format


_NOTE_ID_RE = re.compile(r"\b[a-z]+-[0-9a-f]{8}\b")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_FILE_TOOLS = ("Write", "Edit")
_NOTE_TOOLS = ("weave_create", "weave_extract")


def _digest(transcript: Path, since: str, until: str) -> ChildDigest:
    """Walk the transcript slice once and collect every digest field."""
    from collections import Counter

    rows, gaps = _transcript_rows(transcript)
    meta = _transcript_meta(transcript)
    rows = _slice(rows, since, until)
    uses: dict[str, dict] = {}
    tools: Counter = Counter()
    models: Counter = Counter()
    paths: dict[str, None] = {}
    commits: dict[str, None] = {}
    notes: dict[str, None] = {}
    asked, errors, success = "", 0, None
    for row in rows:
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        blocks = [b for b in _list(message.get("content")) if isinstance(b, dict)]
        if row.get("type") == "assistant":
            if message.get("model") and message["model"] != "<synthetic>":
                models[str(message["model"])] += 1
            for block in blocks:
                if block.get("type") == "tool_use" and block.get("name"):
                    uses[str(block.get("id", ""))] = block
                    tools[str(block["name"])] += 1
                    if block["name"] == "SubagentHandback":
                        claim = _dict(block.get("input")).get("success")
                        success = claim if isinstance(claim, bool) else success
            continue
        if row.get("type") != "user":
            continue
        if not asked and not row.get("isMeta"):
            asked = _prompt_text(message.get("content"))
        for block in blocks:
            if block.get("type") != "tool_result":
                continue
            if block.get("is_error"):
                errors += 1
                continue
            use = uses.get(str(block.get("tool_use_id", "")), {})
            name, args = str(use.get("name", "")), _dict(use.get("input"))
            cwd = str(row.get("cwd", ""))
            result = row.get("toolUseResult")
            if name in _FILE_TOOLS and args.get("file_path"):
                paths[_relative(str(args["file_path"]), cwd)] = None
            elif name == "Bash":
                for changed in _list(_dict(_dict(result).get("bashEditDiff")).get("files")):
                    if _dict(changed).get("filePath"):
                        paths[_relative(str(changed["filePath"]), cwd)] = None
                sha = _commit_sha(str(args.get("command", "")), result)
                if sha:
                    commits[sha] = None
            elif name.endswith(_NOTE_TOOLS):
                notes.update(dict.fromkeys(_created_ids(block.get("content"))))
    version = next((str(r["version"]) for r in rows if r.get("version")), "")
    model = models.most_common(1)[0][0] if models else ""
    gaps += [
        f"no {what} in the transcript"
        for what, value in (("version", version), ("prompt", asked), ("model", model))
        if not value
    ]
    stamps = [t for t in map(_ts, rows) if t]
    return ChildDigest(
        version=version,
        asked=asked,
        description=str(meta.get("description", "")),
        model=model,
        role=str(meta.get("agentType", "")),
        paths=tuple(paths),
        commits=tuple(commits),
        notes=tuple(notes),
        tools=dict(tools),
        tool_errors=errors,
        duration=round((max(stamps) - min(stamps)).total_seconds(), 3) if stamps else None,
        success=success,
        gaps=tuple(gaps),
    )


def _transcript_rows(path: Path) -> tuple[list[dict], list[str]]:
    """The transcript's JSON-object rows, plus a gap per unreadable line."""
    if not path.is_file():
        return [], [f"transcript not found: {path}"]
    rows: list[dict] = []
    bad = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    return rows, [f"{bad} unreadable transcript line(s)"] if bad else []


def _transcript_meta(path: Path) -> dict:
    """A subagent transcript's ``agent-<id>.meta.json`` sidecar, or ``{}``."""
    meta = path.with_name(path.stem + ".meta.json")
    try:
        return _dict(json.loads(meta.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}


def _slice(rows: list[dict], since: str, until: str) -> list[dict]:
    """Rows from the prompt nearest ``since`` up to ``until``.

    The binding hook and the transcript stamp the same prompt a moment
    apart, in either order, so the slice opens at the nearest prompt row.
    """
    start, end = _ts({"timestamp": since}), _ts({"timestamp": until})
    if start:
        prompts = [
            i for i, row in enumerate(rows)
            if row.get("type") == "user" and not row.get("isMeta") and _ts(row)
            and _prompt_text(_dict(row.get("message")).get("content"))
        ]
        if prompts:
            first = min(prompts, key=lambda i: abs(_ts(rows[i]) - start))
            rows = rows[first:]
    if end:
        rows = [r for r in rows if not _ts(r) or _ts(r) <= end]
    return rows


def _prompt_text(content) -> str:
    """A human prompt's text; ``""`` for tool results and non-text rows."""
    if isinstance(content, str):
        return content.strip()
    blocks = [b for b in _list(content) if isinstance(b, dict)]
    if any(b.get("type") == "tool_result" for b in blocks):
        return ""
    return "\n".join(
        str(b.get("text", "")) for b in blocks if b.get("type") == "text"
    ).strip()


def _commit_sha(command: str, result) -> str:
    """The commit a Bash call made: the recorded git operation, else the
    hook capture's commit parser over its stdout."""
    sha = _dict(_dict(_dict(result).get("gitOperation")).get("commit")).get("sha")
    if sha:
        return str(sha)
    if hook_events.is_git_commit(command):
        parsed = hook_events.parse_commit_from_output(command, str(_dict(result).get("stdout", "")))
        return str((parsed or {}).get("hash", ""))
    return ""


def _created_ids(content) -> list[str]:
    """Note ids on the ``Created …`` lines of a weave_create/extract result."""
    text = content if isinstance(content, str) else "\n".join(
        str(_dict(b).get("text", "")) for b in _list(content)
    )
    return [
        found
        for line in text.splitlines()
        if line.strip().startswith("Created")
        for found in _NOTE_ID_RE.findall(line)
    ]


def _output_ref(ref: str) -> dict:
    """An envelope's bare output ref as a ``{kind, ref}`` output."""
    if _NOTE_ID_RE.fullmatch(ref):
        kind = "note"
    elif _SHA_RE.match(ref):
        kind = "commit"
    elif ref.startswith(("http://", "https://")):
        kind = "url"
    else:
        kind = "file"
    return {"kind": kind, "ref": ref}


def _relative(path: str, cwd: str) -> str:
    """``path`` relative to the session's working directory when under it."""
    try:
        return Path(path).relative_to(cwd).as_posix() if cwd else path
    except ValueError:
        return path


def _ts(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(row.get("timestamp", "")).replace("Z", "+00:00"))
    except ValueError:
        return None


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []
