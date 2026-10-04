"""Tasks — one durable note per unit of work, written by every execution route.

A task note is a ledger: its rounds reference the sessions, notes and
outputs other surfaces own, and its body is rendered from them. Three routes
write rounds, and each runs the same five steps:

1. get or mint the :class:`Task` from the :class:`TaskStore`;
2. build a :class:`Round` from the route's own evidence;
3. ``task.put_round(round)``;
4. ``task.close()``, only on a route that may close;
5. ``task.save(vm)``, then the boundary row in the session's :class:`Register`.

The routes are a child dispatch (:func:`open_child` / :func:`close_child`,
from the subagent hooks or ``weave task open|close``), the wrap declaration
(:func:`apply_declaration`) and a devloop run (:func:`record_run`). The
register's boundaries are ground truth; a transcript never places one.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from thinkweave.core.buffer import archived_events_path, buffer_path
from thinkweave.core.events import feedback_events, iter_jsonl
from thinkweave.core.harness import active as active_harness
from thinkweave.core.schemas import NoteType
from thinkweave.core.task_contract import (
    TASK_ID_RE,
    TASK_KIND,
    Round,
    SessionRef,
    accepts_round,
    devloop_ask,
    envelope_return_name,
    normalize_tracker_ref,
    round_refusal,
    validate_envelope,
    validate_task_note,
    validate_wrap_declaration,
)
from thinkweave.core.vault import (
    VaultManager,
    find_session_note_by_source,
    indexed_note_path,
    parse_frontmatter,
    render_frontmatter,
)
from thinkweave.operations import hook_events

TASK_OPEN = "task_open"
TASK_CLOSE = "task_close"


# ---------------------------------------------------------------------------
# The objects


class Task:
    """One task note: its frontmatter and its rounds, loaded once and saved
    whole. Whether it takes a round is the contract's ``round_refusal``."""

    def __init__(
        self,
        frontmatter: dict,
        path: Path | None = None,
        *,
        project: str = "",
        session_key: str = "",
    ) -> None:
        self.frontmatter = {k: v for k, v in frontmatter.items() if k != "rounds"}
        where = f"{path.name} round" if path else "round"
        self.rounds = [
            Round.from_dict(r, grain=self.grain, where=where)
            for r in frontmatter.get("rounds") or []
        ]
        self.path = path
        # Where an unsaved mint is filed on its first save.
        self._project = project
        self._session_key = session_key

    @classmethod
    def load(cls, path: Path) -> Task:
        return cls(parse_frontmatter(path.read_text(encoding="utf-8"))[0], path)

    @property
    def id(self) -> str:
        return str(self.frontmatter.get("id", ""))

    @property
    def title(self) -> str:
        return str(self.frontmatter.get("title", ""))

    @property
    def grain(self) -> str:
        return str(self.frontmatter.get("grain", ""))

    @property
    def closed(self) -> bool:
        return self.frontmatter.get("status") == "closed"

    def accepts_round(self, route: str) -> bool:
        return accepts_round(self.frontmatter, route)

    def refusal(self, route: str) -> str:
        """Why this task takes no ``route`` round, or ``""`` when it does."""
        return round_refusal(self.frontmatter, route)

    def put_round(self, new: Round, *, replaces: tuple[SessionRef, ...] = ()) -> None:
        """Append ``new``, dropping any round under its session ref (or one
        of ``replaces``) — a re-run of the same work replaces its round."""
        same = {new.session_ref, *replaces} - {None}
        self.rounds = [r for r in self.rounds if r.session_ref not in same] + [new]

    def close(self) -> bool:
        """Close the task; False when it was already closed."""
        if self.closed:
            return False
        self.frontmatter["status"] = "closed"
        return True

    def attach_child(self, child: Task) -> bool:
        """Become ``child``'s parent; False when it already is. Raises
        ``ValueError`` for a refused edge: no task parents itself, work-grain
        tasks are peers, and a child attached elsewhere is never moved."""
        if child.id == self.id:
            raise ValueError(f"{child.id} cannot be its own parent")
        if child.grain == "work":
            raise ValueError(
                f"{child.id} is work grain — work-grain tasks are peers, never children"
            )
        parent = str(child.frontmatter.get("parent") or "")
        if parent == self.id:
            return False
        if parent:
            raise ValueError(f"{child.id} is already attached to {parent}")
        child.frontmatter["parent"] = self.id
        return True

    def flag_orphan(self) -> bool:
        """Flag an open dispatch whose close the register lacks; False when
        nothing changed. Work-grain tasks stay open by design."""
        if self.closed or self.grain == "work" or self.frontmatter.get("orphan"):
            return False
        self.frontmatter["orphan"] = True
        return True

    def to_frontmatter(self) -> dict:
        return {**self.frontmatter, "rounds": [r.to_dict() for r in self.rounds]}

    def save(self, vm: VaultManager) -> list[str]:
        """Validate, render the ledger body and write the note; returns what
        the render could not resolve. A note that would not conform raises
        ``ValueError`` and nothing is written."""
        fm = self.to_frontmatter()
        errors = validate_task_note(fm)
        if errors:
            raise ValueError(f"{self.id} does not conform: " + "; ".join(errors))
        if self.path is None:
            vm.ensure_dirs()
            # Filed by the task id itself, so no harness value ever reaches
            # a filename; the human title lives in frontmatter.
            self.path = vm.create_note(
                NoteType.NOTE,
                title=self.id,
                project=self._project,
                extra_frontmatter=fm,
                session_id=self._session_key,
                note_id=self.id,
            )
            self.frontmatter = Task.load(self.path).frontmatter
            fm = self.to_frontmatter()
        warnings: list[str] = []
        body = _ledger_body(vm.config, self.rounds, warnings)
        self.path.write_text(
            render_frontmatter(fm) + "\n\n" + body, encoding="utf-8", newline="\n"
        )
        _index_now(vm, self.path)
        return warnings


def _index_now(vm: VaultManager, path: Path) -> None:
    """Index one just-written note so id lookups resolve without a walk.

    Best-effort: a locked or missing index never fails the write — the
    markdown is the truth, the index is derived and the next ``weave index``
    pass catches up.
    """
    try:
        from thinkweave.core.indexer import Indexer

        idx = Indexer(config=vm.config)
        try:
            idx.index_file(path)
        finally:
            idx.close()
    except Exception:
        pass


class TaskStore:
    """Task notes by id, by tracker ref, and freshly minted."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def get(self, task_id: str) -> Task | None:
        """The task note filed under this id.

        Index first: ``Task.save`` indexes at write, so ``notes.id`` resolves
        the path in one query. The fallback is a glob bounded to the folders
        task notes are ever filed in (``projects/*/sessions/*/``), never a
        vault-wide ``rglob`` — a miss on that walk measured 29s on a DrvFs
        vault (2026-10-03), which is the UserPromptSubmit hook timeout when a
        dispatched prompt names an id.
        """
        if not task_id:
            return None
        path = indexed_note_path(self.cfg, task_id) or next(
            self._filed(f"{task_id}.md"), None
        )
        return Task.load(path) if path else None

    def _filed(self, pattern: str):
        """Task-note files matching ``pattern`` in every filing folder."""
        return self.cfg.vault_root.glob(f"projects/*/sessions/*/{pattern}")

    def open_by_ref(self, asked: str) -> Task | None:
        """The open work-grain task whose ``asked`` is this tracker ref."""
        for path in self._filed("tsk-*.md"):
            fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            if fm.get("asked") == asked and accepts_round(fm, "session"):
                return Task(fm, path)
        return None

    def mint(
        self,
        grain: str,
        title: str,
        project: str,
        *,
        asked: str = "",
        role: str = "",
        harness: str = "",
        session_key: str = "",
    ) -> Task:
        """A fresh open task with a vault-minted id, unsaved: its first save
        files it, in the session's folder when ``session_key`` names one."""
        task_id = f"tsk-{uuid.uuid4().hex[:8]}"
        fm = {
            "type": "note",
            "kind": TASK_KIND,
            "id": task_id,
            "status": "open",
            "grain": grain,
            "title": title or f"Task {task_id}",
            "rounds": [],
        }
        fm.update((k, v) for k, v in (("role", role), ("harness", harness), ("asked", asked)) if v)
        return Task(fm, project=project, session_key=session_key)


class Register:
    """One session's task rows in its shared event log: the live buffer, plus
    the session folder's archived events once a Stop filed them. Never a
    separate file, and the only writer of task rows. An open and its close
    pair by task id alone, never by order or timing."""

    def __init__(self, cfg, session_key: str, streams: list[Path] | None = None) -> None:
        """``streams`` pins the files read — the wrap pass's resolved session
        chain — and rows are then written to the first of them."""
        self.cfg = cfg
        self.session_key = session_key
        self._streams = streams
        self._target = streams[0] if streams else buffer_path(cfg.weave_dir, session_key)

    def rows(self) -> list[dict]:
        return [
            row
            for stream in self._read_streams()
            for row in iter_jsonl(stream)
            if row.get("type") in (TASK_OPEN, TASK_CLOSE)
        ]

    def open(self, task: Task, ref: SessionRef | None = None) -> None:
        self._append(TASK_OPEN, task.id, ref, grain=task.grain)

    def close(
        self, task: Task | None, ref: SessionRef | None = None, *, orphan: bool = False
    ) -> None:
        """Record a close; ``orphan`` marks a stop that paired with no open —
        recorded loudly, never dropped."""
        extra = {"orphan": True} if orphan else {}
        self._append(TASK_CLOSE, task.id if task else "", ref, **extra)

    def resolve_stop(self, ref: SessionRef | None) -> ChildStop:
        """Pair a stop with the open annotated with its ref: ``pending`` when
        that open is unclosed, ``duplicate`` when its close is already here
        (a harness may deliver one stop twice), else ``orphan``."""
        if ref is None:
            return ChildStop("orphan")
        duplicate = ""
        for task_id, pair in self._pairs().items():
            opened = pair["open"]
            if opened and opened.get("session_ref") == ref.to_dict():
                if not pair["close"]:
                    return ChildStop("pending", task_id)
                duplicate = duplicate or task_id
        return ChildStop("duplicate", duplicate) if duplicate else ChildStop("orphan")

    def unclosed(self) -> list[str]:
        """Ids opened here with no close here."""
        return [t for t, pair in self._pairs().items() if pair["open"] and not pair["close"]]

    def listing(self) -> list[LedgerEntry]:
        """One entry per task this session opened or closed, with its note's
        title, status and parent when the note exists."""
        store = TaskStore(self.cfg)
        entries = []
        for task_id, pair in self._pairs().items():
            opened = pair["open"] or {}
            entry = LedgerEntry(
                task_id=task_id,
                grain=str(opened.get("grain", "")),
                opened=str(opened.get("ts", "")),
                closed=bool(pair["close"]),
            )
            if task := store.get(task_id):
                entry = replace(
                    entry,
                    title=task.title,
                    status=str(task.frontmatter.get("status", "")),
                    parent=str(task.frontmatter.get("parent") or "") or None,
                )
            entries.append(entry)
        return entries

    def _pairs(self) -> dict[str, dict]:
        pairs: dict[str, dict] = {}
        for row in self.rows():
            if row.get("task_id"):
                pair = pairs.setdefault(row["task_id"], {"open": None, "close": None})
                pair["open" if row["type"] == TASK_OPEN else "close"] = row
        return pairs

    def _read_streams(self) -> list[Path]:
        if self._streams is not None:
            return self._streams
        # A background subagent can outlive the turn whose Stop archived the
        # live buffer, so its open may sit in the archive.
        note = find_session_note_by_source(VaultManager(config=self.cfg), self.session_key)
        archive = [archived_events_path(note.parent)] if note else []
        return archive + [buffer_path(self.cfg.weave_dir, self.session_key)]

    def _append(self, kind: str, task_id: str, ref: SessionRef | None, **extra) -> None:
        row = {
            "ts": _now(), "type": kind, "task_id": task_id,
            "session_id": self.session_key, **extra,
        }
        if ref:
            row["session_ref"] = ref.to_dict()
        self._target.parent.mkdir(parents=True, exist_ok=True)
        with self._target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


@dataclass(frozen=True)
class TranscriptSource:
    """Where a child task's transcript comes from, and the session ref its
    round closes under; ``gaps`` names what locating it could not recover."""

    path: Path | None = None
    since: str = ""
    session_ref: SessionRef | None = None
    gaps: tuple[str, ...] = ()

    @classmethod
    def agent_file(
        cls, harness: str, agent_id: str, *, agent_transcript: str = "", parent_transcript: str = ""
    ) -> TranscriptSource:
        """A built-in subagent's own transcript: the path its stop names,
        else Claude Code's ``<parent>/subagents/agent-<id>.jsonl``."""
        path = None
        if agent_transcript:
            path = Path(agent_transcript)
        elif parent_transcript and agent_id:
            path = Path(parent_transcript).with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
        return cls(path, session_ref=SessionRef.agent(harness, agent_id) if agent_id else None)

    @classmethod
    def bound_slice(cls, path: str, since: str, session_ref: SessionRef) -> TranscriptSource:
        """A dispatched session's transcript from the prompt that bound it."""
        return cls(Path(path), since, session_ref)

    @classmethod
    def bound(cls, cfg, task_id: str) -> TranscriptSource:
        """The slice a prompt bound to ``task_id``; none when nothing bound
        it, and a gap when its binding cannot be read."""
        path = _binding_path(cfg, task_id)
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ref = SessionRef.from_dict(data["session_ref"])
            return cls.bound_slice(str(data["transcript_path"]), str(data["since"]), ref)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return cls(gaps=(f"binding {path.name} unreadable ({type(exc).__name__}: {exc})",))

    def bind(self, cfg, task_id: str) -> bool:
        """Record this slice as ``task_id``'s transcript; the first binding
        stands. Returns whether this call bound it."""
        path = _binding_path(cfg, task_id)
        if path.exists() or self.path is None or self.session_ref is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "transcript_path": str(self.path),
                "since": self.since,
                "session_ref": self.session_ref.to_dict(),
            }),
            encoding="utf-8",
        )
        return True

    def digest(self, until: str) -> ChildDigest | None:
        return ChildDigest.read(self, until=until) if self.path else None


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
class ChildStop:
    """How one stop paired with the register (see ``Register.resolve_stop``);
    ``closed`` is what the close compiled when the stop was ``pending``."""

    kind: str
    task_id: str = ""
    closed: TaskClose | None = None


@dataclass(frozen=True)
class RunLanded:
    """Which task one devloop run landed on, and what it could not resolve."""

    task_id: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class LedgerEntry:
    """One row of ``weave task ledger``; note fields are absent when the
    task's note is missing."""

    task_id: str
    grain: str
    opened: str
    closed: bool
    title: str | None = None
    status: str | None = None
    parent: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


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
        return asdict(self)


# ---------------------------------------------------------------------------
# Route: a child dispatch — subagent hooks, or `weave task open|close`


def open_child(
    cfg,
    *,
    session_key: str,
    project: str = "",
    title: str = "",
    grain: str = "per-dispatch",
    role: str = "",
    harness: str = "",
    agent_id: str = "",
    asked: str = "",
) -> TaskDispatch:
    """Mint a child task at its dispatch boundary: the note, then its
    ``task_open`` row. The descriptor names the envelope return file the
    performer appends to, whose directory exists from here on."""
    if asked:
        asked = normalize_tracker_ref(asked, current_repo())
    task = TaskStore(cfg).mint(
        grain, title, project, asked=asked, role=role, harness=harness, session_key=session_key
    )
    task.save(VaultManager(config=cfg))
    Register(cfg, session_key).open(task, SessionRef.agent(harness, agent_id) if agent_id else None)
    _envelope_path(cfg, task.id).parent.mkdir(parents=True, exist_ok=True)
    return _descriptor(cfg, task)


def close_child(
    cfg, task_id: str, *, session_key: str, source: TranscriptSource | None = None
) -> TaskClose:
    """Close a child at its boundary. Its envelope rows and its transcript
    digest — ``source``, else the slice a prompt bound to it — become the
    task's round. Invalid envelope rows are reported, never dropped
    silently, and the close row is recorded either way."""
    task = TaskStore(cfg).get(task_id)
    if task is None:
        raise ValueError(f"no task note for {task_id}")
    if not task.accepts_round("dispatch"):
        raise ValueError(f"{task_id} {task.refusal('dispatch')}")
    source = source or TranscriptSource.bound(cfg, task_id)
    errors: list[str] = []
    envelopes = _read_envelopes(_envelope_path(cfg, task_id), task_id, errors)
    digest = source.digest(until=_now())
    data: dict = {"envelopes": envelopes}
    if digest:
        data = {**digest.round_fields(envelopes), "envelopes": envelopes + digest.claims(task_id)}
        task.frontmatter.update(digest.note_fields(task.frontmatter))
    task.put_round(replace(Round.from_dict(data, grain=task.grain), session_ref=source.session_ref))
    task.close()
    task.save(VaultManager(config=cfg))
    Register(cfg, session_key).close(task, source.session_ref)
    return TaskClose(
        task_id=task_id,
        note=str(task.path),
        envelopes=len(envelopes),
        errors=tuple(errors),
        gaps=source.gaps + (digest.gaps if digest else (_NO_TRANSCRIPT,)),
    )


def stop_child(cfg, *, session_key: str, source: TranscriptSource) -> ChildStop:
    """A subagent's stop boundary: close the pending open it ends, skip a
    repeated stop for a task already closed, and record a stop that pairs
    with nothing as an orphan row."""
    register = Register(cfg, session_key)
    stop = register.resolve_stop(source.session_ref)
    if stop.kind == "pending":
        closed = close_child(cfg, stop.task_id, session_key=session_key, source=source)
        return replace(stop, closed=closed)
    if stop.kind == "orphan":
        register.close(None, source.session_ref, orphan=True)
    return stop


def bind_session(
    cfg, prompt: str, *, harness: str, session_key: str, transcript_path: str, since: str
) -> list[str]:
    """Bind a dispatched session's transcript to each open child task its
    prompt names; returns the ids this call bound."""
    source = TranscriptSource.bound_slice(
        transcript_path, since, SessionRef.session(harness, session_key)
    )
    store = TaskStore(cfg)
    return [
        task_id
        for task_id in dict.fromkeys(TASK_ID_RE.findall(prompt))
        if (task := store.get(task_id))
        and task.accepts_round("dispatch")
        and source.bind(cfg, task_id)
    ]


def dispatch_descriptor(cfg, task_id: str) -> TaskDispatch:
    """Re-render an existing task's dispatch descriptor."""
    task = TaskStore(cfg).get(task_id)
    if task is None:
        raise ValueError(f"no task note for {task_id}")
    return _descriptor(cfg, task)


# ---------------------------------------------------------------------------
# Route: the wrap declaration — `weave wrap-finalize --tasks`


def apply_declaration(
    cfg,
    declaration: object,
    *,
    session_key: str,
    project: str,
    streams: list[Path],
    folders: list[Path] | None = None,
) -> TaskPassResult:
    """Apply the wrap declaration: the model judged, this pass writes.

    An invalid declaration aborts with no writes. Each entry continues its
    ``continuing`` task, else the open task carrying its ``asked`` ref, else
    a task this session already minted under its ``title``, else mints one;
    its ``round`` lands as a ``route: session`` round under the session
    note's ref, replacing this session's earlier round, so a re-wrap
    re-applies rather than duplicates. ``done`` closes — the only closure
    wrap performs. ``children`` gain this task as parent, and the decisions
    the round minted gain its id. At ``boundary`` sparsity an open
    dispatch with no close is flagged orphan; a ``task-id-only`` declarer
    was not present, so an absent close is no evidence.
    """
    errors = validate_wrap_declaration(declaration)
    if errors:
        return TaskPassResult(errors=errors)
    assert isinstance(declaration, dict)
    wrap = _DeclarationPass(
        cfg, session_key=session_key, project=project, streams=streams, folders=folders or []
    )
    entries = declaration["declared"]
    for entry in entries:
        try:
            wrap.apply(entry, solo=len(entries) == 1)
        except ValueError as exc:
            wrap.result.errors.append(f"declared: {exc}")
    if declaration.get("sparsity", "boundary") == "boundary":
        wrap.flag_orphans()
    return wrap.result


class _DeclarationPass:
    """One wrap's task pass: the session it wraps, and what it reconciled."""

    def __init__(
        self, cfg, *, session_key: str, project: str, streams: list[Path], folders: list[Path]
    ) -> None:
        self.vm = VaultManager(config=cfg)
        self.store = TaskStore(cfg)
        self.register = Register(cfg, session_key, streams)
        self.session_key = session_key
        self.project = project
        self.folders = folders
        self.result = TaskPassResult()
        self.repo = current_repo()
        notes = [fm for _path, fm in _folder_notes(folders)]
        session_notes = [fm for fm in notes if fm.get("type") == "session"]
        sessions = [str(fm.get("id")) for fm in session_notes]
        # weave_extract derives insights from the harness session id.
        anchors = {*sessions, *(str(fm["source_session"]) for fm in session_notes
                                if fm.get("source_session"))}
        self.wrap_ref = SessionRef.session(active_harness().id, session_key)
        self.session_ref = (
            SessionRef.note(self.wrap_ref.harness, sessions[0]) if sessions else self.wrap_ref
        )
        self.insights = [
            str(fm["id"]) for fm in notes
            if fm.get("type") == "note" and fm.get("id")
            and not fm.get("kind") and not fm.get("auto_extracted")
            and anchors & set(fm.get("derived_from") or [])
        ]
        self.verdicts = [
            {k: str(row.get(k, "")) for k in ("register", "prompt_ref", "ts")}
            for stream in streams
            for row in feedback_events(stream)
        ]
        # The pass writes its own opens without a session ref; hook-route
        # opens always carry one, so they never match a declared title.
        self.minted_titles: dict[str, str] = {}
        for row in self.register.rows():
            if row["type"] != TASK_OPEN or row.get("session_ref"):
                continue
            if row.get("session_id") == session_key:
                if task := self.store.get(str(row.get("task_id", ""))):
                    self.minted_titles.setdefault(task.title, task.id)

    def apply(self, entry: dict, *, solo: bool) -> None:
        asked = _tracker_ref(str(entry.get("asked") or ""), self.repo, self.result.warnings)
        task = self._task_for(entry, asked)
        if task is None:
            return
        if asked:
            task.frontmatter["asked"] = asked
        if entry.get("consumes"):
            consumed = [*(task.frontmatter.get("consumes") or []), *entry["consumes"]]
            task.frontmatter["consumes"] = list(dict.fromkeys(consumed))
        if "round" in entry:
            task.put_round(self._round(entry, task, solo), replaces=(self.wrap_ref,))
        closing = bool(entry.get("done")) and task.close()
        minting = task.path is None
        self.result.warnings += task.save(self.vm)
        if minting:
            self.register.open(task)
            self.result.minted.append(task.id)
        if closing:
            self.register.close(task)
            self.result.closed.append(task.id)
        self._stamp_decisions(entry, task)
        self._attach_children(entry, task)

    def flag_orphans(self) -> None:
        """Flag each open with no close in the register; ids this pass
        touched carry their own boundary truth."""
        touched = set(self.result.minted + self.result.appended + self.result.closed)
        for task_id in self.register.unclosed():
            if task_id in touched:
                continue
            task = self.store.get(task_id)
            if task is None:
                self.result.warnings.append(f"orphan open {task_id} has no task note")
            elif task.flag_orphan():
                self.result.warnings += task.save(self.vm)
                self.result.orphaned.append(task_id)

    def _task_for(self, entry: dict, asked: str) -> Task | None:
        """The entry's task; ``None`` when a repeated ``done`` makes the
        entry a no-op. A task that cannot take a session round is refused."""
        continuing = str(entry.get("continuing") or "")
        title = str(entry.get("title", ""))
        by_ref = self.store.open_by_ref(asked) if asked and not continuing else None
        if continuing:
            task = self.store.get(continuing)
            if task is None:
                raise ValueError(f"no task note for {continuing}")
        else:
            task = by_ref or self.store.get(self.minted_titles.get(title, ""))
        if task is None:
            grain = str(entry.get("grain", "work"))
            return self.store.mint(grain, title, self.project, session_key=self.session_key)
        if refusal := task.refusal("session"):
            if entry.get("done") and task.closed:
                return None
            raise ValueError(f"{task.id} {refusal}")
        if (continuing or by_ref) and "round" in entry:
            self.result.appended.append(task.id)
        return task

    def _round(self, entry: dict, task: Task, solo: bool) -> Round:
        """The entry's round under this session's ref. A single-task session
        credits it with every insight and verdict the session recorded;
        with several tasks only what each entry declares is attributed."""
        data = {"route": "session", "session_ref": self.session_ref.to_dict(), **entry["round"]}
        if solo:
            for key, found in (("notes", self.insights), ("feedback", self.verdicts)):
                if found:
                    data.setdefault(key, found)
        if entry.get("children"):
            data.setdefault("children", list(entry["children"]))
        return Round.from_dict(data, grain=task.grain)

    def _stamp_decisions(self, entry: dict, task: Task) -> None:
        """Stamp each decision the round declares minted with the task id."""
        minted = ((entry.get("round") or {}).get("decisions") or {}).get("minted")
        for dec_id in minted or []:
            notes = _folder_notes(self.folders)
            path = next((p for p, fm in notes if fm.get("id") == dec_id), None)
            if path is None:
                self.result.warnings.append(
                    f"decision {dec_id} not found in the session chain — task_id stamp skipped"
                )
                continue
            self.vm.update_note(path, frontmatter_updates={"task_id": task.id})
            self.result.stamped += 1

    def _attach_children(self, entry: dict, task: Task) -> None:
        """Write the declared child → parent edges. Which declared task a
        dispatch served is the model's call, never a timestamp's."""
        for child_id in entry.get("children") or []:
            try:
                child = self.store.get(child_id)
                if child is None:
                    raise ValueError("no task note")
                if task.attach_child(child):
                    self.result.warnings += child.save(self.vm)
                    self.result.attached.append(child_id)
            except ValueError as exc:
                self.result.errors.append(f"children: {child_id} not attached to {task.id}: {exc}")


# ---------------------------------------------------------------------------
# Route: a devloop run — `weave task record-run`


def record_run(
    cfg, payload: object, *, project: str, trajectory: str = "", session_key: str = ""
) -> RunLanded:
    """Land one devloop run as a ``route: devloop`` round on the open task
    its issue ref resolves to, minting a work-grain task when none is open;
    a re-record of the same trajectory replaces its round. A run never
    closes its task, and nothing closes it when its PR merges. With no
    ``session_key`` no register row is written: no session owns the run.
    A payload outside the contract raises ``ValueError`` before anything is
    written."""
    warnings: list[str] = []
    asked = _tracker_ref(devloop_ask(payload), current_repo(), warnings)
    assert isinstance(payload, dict)
    store = TaskStore(cfg)
    task = store.open_by_ref(asked) or store.mint(
        "work", str(payload.get("title", "")), project, asked=asked, session_key=session_key
    )
    task.put_round(Round.from_devloop(payload, task_id=task.id, trajectory=trajectory))
    task.frontmatter["asked"] = asked
    minting = task.path is None
    warnings += task.save(VaultManager(config=cfg))
    if minting and session_key:
        Register(cfg, session_key).open(task)
    return RunLanded(task.id, tuple(warnings))


# ---------------------------------------------------------------------------
# The ledger body


def _ledger_body(cfg, rounds: list[Round], warnings: list[str]) -> str:
    """One wikilink line per round, in ledger order; never hand-edited. Its
    wikilinks are the task's graph edges (a link to a session indexes as
    ``derived_from``, any other as ``relates_to``). A deliverable whose ref
    equals a child task's output ref names that child."""
    link = _linker(cfg)
    store = TaskStore(cfg)
    lines = [
        "- " + " · ".join(_round_parts(r, link, _child_credit(store, r.children or [], warnings)))
        for r in rounds
    ]
    return "## Rounds\n\n" + ("\n".join(lines) or "_No rounds yet._") + "\n"


def _round_parts(entry: Round, link, credit: dict[str, str]) -> list[str]:
    ref = entry.session_ref
    head = (
        link(ref.value) if ref and ref.kind == "note"
        else f"`{ref.harness if ref else ''} {ref.value if ref else '?'}`"
    )
    parts = [f"{entry.route or 'dispatch'} {head}"]
    if entry.notes:
        parts.append("notes " + ", ".join(link(n) for n in entry.notes))
    decisions = entry.decisions or {}
    for key in ("minted", "re_served", "reverted"):
        if decisions.get(key):
            label = key.replace("_", "-")
            parts.append(f"decisions {label} " + ", ".join(link(d) for d in decisions[key]))
    if entry.feedback:
        parts.append("feedback " + ", ".join(f.get("register", "?") for f in entry.feedback))
    for out in entry.outputs or []:
        target = link(out["ref"]) if out.get("kind") == "note" else out.get("ref", "")
        text = f"{out.get('role', 'output')} {out.get('kind', '')} {target}"
        if out.get("role") == "deliverable" and out.get("ref") in credit:
            text += f" by {link(credit[out['ref']])}"
        parts.append(text)
    commits = (entry.did or {}).get("commits")
    if commits:
        parts.append("commits " + ", ".join(commits))
    return parts


def _child_credit(store: TaskStore, children: list[str], warnings: list[str]) -> dict[str, str]:
    """{output ref: child task id} over the children's recorded outputs."""
    credit: dict[str, str] = {}
    for child_id in children:
        child = store.get(child_id)
        if child is None:
            warnings.append(f"child {child_id} has no task note; its outputs go uncredited")
            continue
        for entry in child.rounds:
            for out in entry.outputs or []:
                credit.setdefault(str(out.get("ref", "")), child_id)
    return credit


def _linker(cfg):
    """A note-id → wikilink renderer; the vault's id maps load on first use."""
    maps: list[dict] = []

    def link(note_id: str) -> str:
        from thinkweave.synthesis.concept_hub import safe_hub_maps
        from thinkweave.synthesis.hub import reflink

        if not maps:
            maps.extend(safe_hub_maps(cfg)[:2])
        return reflink(note_id, *maps)

    return link


# ---------------------------------------------------------------------------
# Plumbing


_NO_TRANSCRIPT = "no transcript bound; the round carries no digest"


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


def _tracker_ref(value: str, repo: str, warnings: list[str]) -> str:
    """``value`` as its tracker ref; a ``#<n>`` no remote resolves is kept
    bare, and announced."""
    ref = normalize_tracker_ref(value, repo)
    if ref.startswith("#"):
        warnings.append(
            f"no GitHub remote to resolve {ref} against — kept bare, so it "
            "matches no other route's task"
        )
    return ref


def _descriptor(cfg, task: Task) -> TaskDispatch:
    return TaskDispatch(
        task_id=task.id,
        grain=task.grain,
        envelope_return=str(_envelope_path(cfg, task.id)),
        note=str(task.path),
        title=task.title,
    )


def _folder_notes(folders: list[Path]):
    """(path, frontmatter) for every note in the wrapped session's folders."""
    for folder in folders:
        for path in folder.rglob("*.md"):
            yield path, parse_frontmatter(path.read_text(encoding="utf-8"))[0]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _envelope_path(cfg, task_id: str) -> Path:
    return cfg.weave_dir / "tasks" / envelope_return_name(task_id)


def _binding_path(cfg, task_id: str) -> Path:
    return cfg.weave_dir / "tasks" / f"{task_id}.bind.json"


def _read_envelopes(path: Path, task_id: str, errors: list[str]) -> list[dict]:
    """The performer's valid return rows; each invalid row lands in ``errors``."""
    if not path.exists():
        return []
    valid: list[dict] = []
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
    return valid


# ---------------------------------------------------------------------------
# The child digest — one reader for the Claude Code transcript format.
# HarnessProfile.transcript_parser keeps only text turns, while the digest
# needs tool calls, their results and a timestamp slice, so it reads here.


@dataclass(frozen=True)
class ChildDigest:
    """What a child's transcript slice says it was asked, did, and produced.

    Read once at the close boundary. Every transcript field is optional —
    whatever the read could not recover is named in ``gaps``.
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
    def read(cls, source: TranscriptSource, *, until: str = "") -> ChildDigest:
        """Digest the source's transcript (from the prompt nearest its
        ``since`` up to ``until``, when given); never raises."""
        try:
            return _digest(source.path or Path(), source.since, until)
        except Exception as exc:  # noqa: BLE001 — a close must never fail on a read
            return cls(gaps=(f"digest aborted: {type(exc).__name__}: {exc}",))

    def note_fields(self, fm: dict) -> dict:
        """Task-note fields the digest fills; a value already set stands."""
        fill = {
            key: value
            for key, value in (
                ("asked", self.asked), ("model", self.model), ("role", self.role)
            )
            if value and not fm.get(key)
        }
        # The minted placeholder title yields to the dispatch description.
        if self.description and fm.get("title") == f"Task {fm.get('id')}":
            fill["title"] = self.description
        return fill

    def round_fields(self, envelopes: list[dict]) -> dict:
        """The round's digest fields; ``envelopes`` contribute outputs."""
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

    def claims(self, task_id: str) -> list[dict]:
        """The child's own handback claim as an envelope row, when it made one."""
        if self.success is None:
            return []
        row = {"task_id": task_id, "outcome": "success" if self.success else "failure"}
        if self.model:
            row["model"] = self.model
        return [row]


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
    naive = sum(1 for r in rows if r.get("timestamp") and _ts(r) is None)
    if naive:
        gaps.append(f"{naive} row(s) with an unreadable or zone-less timestamp left out of timing")
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
    """The row's timestamp; ``None`` when it is missing, unreadable, or
    carries no zone (it cannot be compared with the zoned ones)."""
    try:
        stamp = datetime.fromisoformat(str(row.get("timestamp", "")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else None


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []
