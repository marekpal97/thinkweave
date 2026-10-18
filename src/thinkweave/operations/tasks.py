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
import logging
import re
import subprocess
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from thinkweave.core.buffer import append_event, archived_events_path, buffer_path
from thinkweave.core.config import tracker_project
from thinkweave.core.events import feedback_events, iter_jsonl
from thinkweave.core.harness import lookup
from thinkweave.core.harness import active as active_harness
from thinkweave.core.schemas import NoteType
from thinkweave.core.task_contract import (
    TASK_ID_RE,
    TASK_KIND,
    GithubIssue,
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

if TYPE_CHECKING:
    from thinkweave.operations.trajectory_outcome import Feedback

log = logging.getLogger(__name__)

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
        """Close the task, clearing any orphan flag a missing close earned;
        False when it was already closed."""
        self.frontmatter.pop("orphan", None)
        if self.closed:
            return False
        self.frontmatter["status"] = "closed"
        return True

    def reopen(self) -> bool:
        """Reopen a closed task; False when it was already open."""
        if not self.closed:
            return False
        self.frontmatter["status"] = "open"
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
    """Index one just-written note, edges included, so id lookups and graph
    walks resolve without a walk of the vault.

    A locked or missing index never fails the write: the markdown is the
    truth, and a note whose indexing failed has no stored hash, so the next
    ``weave index`` pass picks it up. The failure is logged, not hidden.
    """
    try:
        from thinkweave.core.indexer import Indexer

        idx = Indexer(config=vm.config)
        try:
            idx.index_file(path)
        finally:
            idx.close()
    except Exception:
        log.warning("task note %s not indexed; the next weave index pass will", path,
                    exc_info=True)


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

    def open_touching(self, paths: list[str]) -> list[Task]:
        """Open tasks with a round whose ``did.paths`` names one of
        ``paths``; absolute and repo-relative forms of a file match."""
        wanted = [PurePosixPath(p).parts for p in paths if p]
        found: list[Task] = []
        for path in self._filed("tsk-*.md"):
            fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            if fm.get("kind") != TASK_KIND or fm.get("status") == "closed":
                continue
            touched = [
                PurePosixPath(p).parts
                for r in fm.get("rounds") or []
                for p in (r.get("did") or {}).get("paths") or []
            ]
            if any(_same_file(a, b) for a in wanted for b in touched):
                found.append(Task(fm, path))
        return found

    def for_trajectory(self, note_id: str) -> Task | None:
        """The task, open or closed, holding the devloop round whose session
        ref names this trajectory note."""
        return next(iter(self.holding({SessionRef.note("devloop", note_id)})), None)

    def holding(self, refs: set[SessionRef]) -> list[Task]:
        """Tasks, open or closed, holding a round under one of ``refs``."""
        wanted = [ref.to_dict() for ref in refs]
        found: list[Task] = []
        for path in self._filed("tsk-*.md"):
            fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            if any(r.get("session_ref") in wanted for r in fm.get("rounds") or []):
                found.append(Task(fm, path))
        return found

    def file_feedback(self, task: Task, feedback: Feedback) -> str:
        """Write ``feedback`` as a note beside ``task``, edged
        ``feedback_for`` it; returns the note id, or ``""`` when the note
        already exists."""
        if (task.path.parent / f"{feedback.note_id}.md").exists():
            return ""
        vm = VaultManager(config=self.cfg)
        path = vm.create_note(
            NoteType.NOTE,
            title=feedback.note_id,
            body=feedback.body,
            project=str(task.frontmatter.get("project", "")),
            extra_frontmatter={
                "source": feedback.source,
                "ref": feedback.ref,
                "files": list(feedback.files),
                "feedback_for": [task.id],
            },
            output_dir=task.path.parent,
            note_id=feedback.note_id,
        )
        _index_now(vm, path)
        return feedback.note_id

    def close_tracked(self, issue_state=None) -> TrackerClosures:
        """Close every open work-grain task whose ``asked`` is a GitHub issue
        that is now closed. A child keyed to that issue stays open: its own
        ``close_child`` closes it. ``issue_state(repo, number)`` returns the
        issue's state; a failed lookup leaves its task open and is listed
        under ``errors``."""
        issue_state = issue_state or _gh_issue_state
        vm = VaultManager(config=self.cfg)
        closed: list[str] = []
        errors: dict[str, str] = {}
        for path in self._filed("tsk-*.md"):
            fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            issue = GithubIssue.parse(str(fm.get("asked", "")))
            if (
                not issue
                or fm.get("kind") != TASK_KIND
                or fm.get("grain") != "work"
                or fm.get("status") != "open"
            ):
                continue
            task_id = str(fm.get("id") or path.stem)
            try:
                task = Task(fm, path)
                if issue_state(issue.repo, issue.number).lower() != "closed":
                    continue
                task.close()
                task.save(vm)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                reason = (getattr(exc, "stderr", "") or str(exc)).strip()
                errors[task_id] = f"issue {fm['asked']} not checked: {reason}"
            else:
                closed.append(task_id)
        return TrackerClosures(closed, errors)

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


@dataclass
class RefEvidence:
    """The session's tracker-ref evidence, served to the model before it
    writes the task declaration; it informs ``asked``, never sets it."""

    commit_closes: dict[str, list[str]] = field(default_factory=dict)
    prs: dict[str, list[str]] = field(default_factory=dict)
    tasks: dict[str, str] = field(default_factory=dict)

    @classmethod
    def gather(cls, cfg, session_fm: dict, warnings: list[str]) -> RefEvidence:
        """Closing refs in the session's commits, the PRs of its pushed
        branch, and open tasks touching its files. A failed lookup is
        appended to ``warnings``, never dropped."""
        evidence = cls()
        commits = session_fm.get("commits") or []
        for commit in commits:
            for ref in _closing_refs(str(commit.get("message", ""))):
                evidence.commit_closes.setdefault(ref, []).append(
                    str(commit.get("hash", ""))
                )
        branch = session_fm.get("git_branch")
        if branch:
            try:
                evidence.prs = _gh_branch_prs(branch)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                reason = (getattr(exc, "stderr", "") or str(exc)).strip()
                warnings.append(f"PR lookup for branch {branch} failed: {reason}")
        paths = list(session_fm.get("files_touched") or [])
        paths += [f for c in commits for f in c.get("files") or []]
        if paths:
            try:
                evidence.tasks = {
                    t.id: t.title for t in TaskStore(cfg).open_touching(paths)
                }
            except (OSError, ValueError) as exc:
                warnings.append(f"open-task lookup by touched file failed: {exc}")
        return evidence

    def lines(self) -> list[str]:
        """The report block; empty when there is no evidence."""
        out = [
            f"  {ref} closed by commit {', '.join(hashes)}"
            for ref, hashes in self.commit_closes.items()
        ]
        out += [
            f"  PR {url}" + (f" closes {', '.join(refs)}" if refs else "")
            for url, refs in self.prs.items()
        ]
        out += [f"  open task {tid} ({title}) touched the same files"
                for tid, title in self.tasks.items()]
        return ["Ref evidence for the task declaration:", *out] if out else []

    def closing_refs(self, repo: str) -> set[str]:
        """Every ref the session's commits and PRs close, normalised
        against ``repo``."""
        pr_refs = [ref for refs in self.prs.values() for ref in refs]
        return {normalize_tracker_ref(ref, repo) for ref in [*self.commit_closes, *pr_refs]}


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

    def open(self, task: Task, ref: SessionRef | None = None, base: str = "") -> None:
        """Record the open; ``base`` is the repo HEAD the work starts from."""
        extra = {"grain": task.grain}
        if base:
            extra["base"] = base
        self._append(TASK_OPEN, task.id, ref, **extra)

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

    def wrap_closes(self) -> set[str]:
        """Ids a wrap pass closed here; hook-route closes carry a ref."""
        return {
            row["task_id"] for row in self.rows()
            if row["type"] == TASK_CLOSE and not row.get("session_ref")
        }

    def unclosed(self) -> list[str]:
        """Ids opened here with no close here."""
        return [t for t, pair in self._pairs().items() if pair["open"] and not pair["close"]]

    def opened_base(self, task_id: str) -> str:
        """The repo HEAD this task's ``task_open`` row recorded; ``""`` when
        it recorded none (a dispatch made outside a repo, or an open row
        written before the base was kept)."""
        opened = self._pairs().get(task_id, {}).get("open") or {}
        return str(opened.get("base", ""))

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
        append_event(self._target, row, harness=ref.harness if ref else "")


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
    def bound(cls, cfg, task_id: str) -> TranscriptSource:
        """The slice a prompt bound to ``task_id``; none when nothing bound
        it, and a gap when its binding cannot be read."""
        path = _binding_path(cfg, task_id)
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            transcript = str(data["transcript_path"])
            return cls(
                Path(transcript) if transcript else None,
                str(data["since"]),
                SessionRef.from_dict(data["session_ref"]),
                tuple(map(str, data.get("gaps", ()))),
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return cls(gaps=(f"binding {path.name} unreadable ({type(exc).__name__}: {exc})",))

    def bind(self, cfg, task_id: str) -> bool:
        """Record this slice — or the gaps that left it without a
        transcript — as ``task_id``'s. A real binding stands; a gap-only one
        yields to a later real slice. Returns whether this call bound it."""
        if self.session_ref is None:
            return False
        path = _binding_path(cfg, task_id)
        since = self.since
        if path.exists():
            prior = TranscriptSource.bound(cfg, task_id)
            if prior.path or self.path is None:
                return False
            if prior.session_ref == self.session_ref:
                # The session's first prompt naming the task opens its slice.
                since = prior.since
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "transcript_path": str(self.path or ""),
                "since": since,
                "session_ref": self.session_ref.to_dict(),
                "gaps": list(self.gaps),
            }),
            encoding="utf-8",
        )
        return True


@dataclass(frozen=True)
class TaskDispatch:
    """The dispatch descriptor — what the seam hands the performer."""

    task_id: str
    grain: str
    envelope_return: str
    note: str
    title: str
    instructions: str = ""
    """What the performer owes the close: trailer-tagged commits, and one
    envelope row with its outputs and a summary ``outcome``."""

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
class IssueNode:
    """One GitHub issue's place in its sub-issue tree: how many sub-issues
    it has, and its parent's tracker ref (``""`` for none)."""

    sub_issues: int = 0
    parent: str = ""

    @classmethod
    def from_api(cls, fields: dict) -> IssueNode:
        """The node ``gh api``'s issue fields describe."""
        parent = str(fields.get("parent_issue_url") or "")
        return cls(
            sub_issues=int((fields.get("sub_issues_summary") or {}).get("total") or 0),
            parent=normalize_tracker_ref(parent.replace("api.github.com/repos/", "github.com/")),
        )

    def epic(self, ref: str) -> str:
        """The nearest epic of the issue ``ref`` names: ``ref`` itself when
        it has sub-issues, else its parent, else ``ref``."""
        return ref if self.sub_issues else self.parent or ref


@dataclass(frozen=True)
class TrackerClosures:
    """Which tasks one tracker sweep closed, and why it skipped others
    (``{task id: reason}``)."""

    closed: list[str]
    errors: dict[str, str]


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


@dataclass(frozen=True)
class EntryLanding:
    """Where one declared entry landed: its task, the ref it declared, and
    how it matched — ``continuing``, ``ref``, ``epic`` or ``mint``."""

    task_id: str
    asked: str
    match: str


@dataclass
class TaskPassResult:
    """What one wrap task pass reconciled — folded into the wrap report."""

    landed: list[EntryLanding] = field(default_factory=list)
    minted: list[str] = field(default_factory=list)
    appended: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)
    attached: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)
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
    cwd: str = "",
) -> TaskDispatch:
    """Mint a child task at its dispatch boundary: the note, then its
    ``task_open`` row. The descriptor names the envelope return file the
    performer appends to, whose directory exists from here on. ``cwd`` is
    the worktree the child starts in (default: the working directory); its
    HEAD is the round's base commit."""
    if asked:
        asked = normalize_tracker_ref(asked, current_repo())
    task = TaskStore(cfg).mint(
        grain, title, project, asked=asked, role=role, harness=harness, session_key=session_key
    )
    task.save(VaultManager(config=cfg))
    Register(cfg, session_key).open(
        task,
        SessionRef.agent(harness, agent_id) if agent_id else None,
        base=head_sha(cwd),
    )
    _envelope_path(cfg, task.id).parent.mkdir(parents=True, exist_ok=True)
    return _descriptor(cfg, task)


def close_child(
    cfg, task_id: str, *, session_key: str, source: TranscriptSource | None = None
) -> TaskClose:
    """Close a child at its boundary. Its envelope rows and its transcript
    digest — ``source``, else the slice a prompt bound to it — become the
    task's round, and every gap in locating, binding or reading the
    transcript lands on it. Invalid envelope rows are reported, never
    dropped silently, and the close row is recorded either way."""
    task = TaskStore(cfg).get(task_id)
    if task is None:
        raise ValueError(f"no task note for {task_id}")
    if not task.accepts_round("dispatch"):
        raise ValueError(f"{task_id} {task.refusal('dispatch')}")
    source = source or TranscriptSource.bound(cfg, task_id)
    register = Register(cfg, session_key)
    errors: list[str] = []
    envelopes = _read_envelopes(_envelope_path(cfg, task_id), task_id, errors)
    digest = ChildDigest.read(source, until=_now()).with_trailer_commits(task_id)
    data = {**digest.round_fields(envelopes), "envelopes": envelopes + digest.claims(task_id)}
    if base := register.opened_base(task_id):
        data["base"] = base
    task.frontmatter.update(digest.note_fields(task.frontmatter))
    task.put_round(replace(Round.from_dict(data, grain=task.grain), session_ref=source.session_ref))
    task.close()
    task.save(VaultManager(config=cfg))
    register.close(task, source.session_ref)
    return TaskClose(
        task_id=task_id,
        note=str(task.path),
        envelopes=len(envelopes),
        errors=tuple(errors),
        gaps=digest.gaps,
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
    prompt names; returns the ids this call bound. A harness whose profile
    cannot bind a worker binds the session with a gap instead."""
    ref = SessionRef.session(harness, session_key)
    profile = lookup(harness)
    if profile is None or not profile.binds_workers:
        gap = f"{harness} declares no worker binding for a dispatched session"
    elif not transcript_path:
        gap = "the prompt hook carried no transcript path"
    else:
        gap = ""
    source = TranscriptSource(
        None if gap else Path(transcript_path), since, ref, (gap,) if gap else ()
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

    An invalid declaration aborts with no writes. A GitHub sub-issue
    ``asked`` resolves to its epic's ref, and the round names the sub-issue.
    Each entry continues its ``continuing`` task, else the open task
    carrying its ``asked`` ref, else a task this session already minted
    under its ``title``, else mints one;
    its ``round`` lands as a ``route: session`` round under the session
    note's ref, replacing this session's earlier round, so a re-wrap
    re-applies rather than duplicates. A re-wrap that lands a round moves
    this session's earlier round off every task its entries no longer name,
    and reopens such a task when this session's own ``done`` closed it;
    rounds other sessions wrote stay put. ``done`` closes the task only when
    the declared ref is the task's own ``asked`` — the only closure wrap
    performs; on an epic's task for a sub-issue the round records it. A
    closed task's entry is skipped with a warning. Each landed entry is
    recorded with how it matched; a ref-less mint while an open task
    touched the session's files, and a declared ref the session's closing
    refs do not name, are warned, never changed. ``children`` gain this task as parent, and the decisions
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
    failed = False
    for entry in entries:
        try:
            wrap.apply(entry, solo=len(entries) == 1)
        except ValueError as exc:
            failed = True
            wrap.result.errors.append(f"declared: {exc}")
    wrap.move_rounds(blocked=failed)
    if declaration.get("sparsity", "boundary") == "boundary":
        wrap.flag_orphans()
    return wrap.result


#: The feedback event keys a round copies; an absent or empty one stays off
#: the round, so a stream written before a key existed compiles unchanged.
_FEEDBACK_KEYS = ("register", "prompt_ref", "ts", "text")


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
        # Tasks this pass's entries resolved to, and those it put a round on.
        self.named: set[str] = set()
        self.rounded: set[str] = set()
        self.repo = current_repo()
        filed = list(_folder_notes(folders))
        notes = [fm for _path, fm in filed]
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
        self.decisions = {
            str(fm["id"]): path for path, fm in filed
            if fm.get("type") == "decision" and fm.get("id")
            and anchors & set(fm.get("derived_from") or [])
        }
        # The ref evidence weave_extract served the declaring model.
        self.evidence = RefEvidence.gather(
            cfg, session_notes[0] if session_notes else {}, self.result.warnings
        )
        self.closing_refs = self.evidence.closing_refs(self.repo)
        self.verdicts = [
            {k: str(row[k]) for k in _FEEDBACK_KEYS if row.get(k)}
            for stream in streams
            for row in feedback_events(stream)
        ]
        # The repo HEAD the session's first prompt saw, as its hook recorded
        # it. The chain's earliest record wins, so a resumed session names
        # the commit it began from; "" when no stream records one.
        bases = [
            (str(row.get("ts", "")), str(row.get("hash", "")))
            for stream in streams
            for row in iter_jsonl(stream)
            if row.get("type") == "base" and row.get("hash")
        ]
        self.base = min(bases)[1] if bases else ""
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
        issue = _tracker_ref(str(entry.get("asked") or ""), self.repo, self.result.warnings)
        asked = _epic_ref(issue, self.result.warnings)
        task = self._task_for(entry, asked)
        if task is None:
            return
        self.named.add(task.id)
        self._land(entry, task, asked, issue)
        if asked:
            task.frontmatter["asked"] = asked
        if entry.get("consumes"):
            consumed = [*(task.frontmatter.get("consumes") or []), *entry["consumes"]]
            task.frontmatter["consumes"] = list(dict.fromkeys(consumed))
        minted: list[str] = []
        if "round" in entry:
            new = self._round(entry, task, solo, issue)
            minted = (new.decisions or {}).get("minted") or []
            task.put_round(new, replaces=(self.wrap_ref,))
            self.rounded.add(task.id)
        # A sub-issue's done stays on its round; the epic's own close (or
        # the tracker sweep) closes the epic's task.
        own = not issue or issue == task.frontmatter.get("asked")
        closing = bool(entry.get("done")) and own and task.close()
        minting = task.path is None
        self.result.warnings += task.save(self.vm)
        if minting:
            self.register.open(task)
            self.result.minted.append(task.id)
        if closing:
            self.register.close(task)
            self.result.closed.append(task.id)
        self._stamp_decisions(minted, task)
        self._attach_children(entry, task)

    def _land(self, entry: dict, task: Task, asked: str, issue: str) -> None:
        """Record how the entry matched its task, and warn on the choices
        the session's ref evidence puts in doubt."""
        if entry.get("continuing"):
            match = "continuing"
        elif asked and task.path and task.frontmatter.get("asked") == asked:
            match = "ref" if asked == issue else "epic"
        else:
            match = "mint"
        self.result.landed.append(EntryLanding(task.id, issue, match))
        touching = sorted(set(self.evidence.tasks) - {task.id})
        if match == "mint" and not issue and touching:
            self.result.warnings.append(
                f"{task.id} minted with no ref while open task(s) "
                f"{', '.join(touching)} touched the same files — declare "
                "continuing or asked if it is that work"
            )
        if issue and self.closing_refs and not {issue, asked} & self.closing_refs:
            self.result.warnings.append(
                f"{task.id} declared {issue}, but the session's commits and PRs "
                f"close {', '.join(sorted(self.closing_refs))}"
            )

    def move_rounds(self, *, blocked: bool) -> None:
        """Take the session round this session's wraps wrote off each task no
        entry named, once this pass landed a round elsewhere; reopen one whose
        close this session's own ``done`` recorded in the register.
        ``blocked`` — an entry failed — moves nothing, so the failed entry
        never loses its earlier round, and says so."""
        if not self.rounded:
            return
        if blocked:
            self.result.warnings.append(
                "this session's earlier rounds were not moved: a declared entry "
                "failed, so its task is unknown — fix it and re-wrap"
            )
            return
        refs = {self.session_ref, self.wrap_ref}
        closed_here = self.register.wrap_closes()
        for task in self.store.holding(refs):
            if task.id in self.named:
                continue
            left = [r for r in task.rounds if r.session_ref in refs and r.route == "session"]
            if not left:
                continue
            task.rounds = [r for r in task.rounds if r not in left]
            if task.id in closed_here and any(r.done for r in left) and task.reopen():
                self.result.reopened.append(task.id)
            self.result.warnings += task.save(self.vm)
            self.result.moved.append(task.id)

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
        """The entry's task; ``None`` when it is closed, so the entry is
        skipped with a warning. Any other task that cannot take a session
        round is refused."""
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
            project = tracker_project(asked, self.vm.config.vault_root) or self.project
            return self.store.mint(grain, title, project, session_key=self.session_key)
        if refusal := task.refusal("session"):
            if task.closed:
                self.named.add(task.id)
                self.result.warnings.append(f"{task.id} {refusal} — entry skipped")
                return None
            raise ValueError(f"{task.id} {refusal}")
        if (continuing or by_ref) and "round" in entry:
            self.result.appended.append(task.id)
        return task

    def _round(self, entry: dict, task: Task, solo: bool, issue: str) -> Round:
        """The entry's round under this session's ref, naming the ``issue``
        it worked. A single-task session credits it with every insight and
        verdict the session recorded; with several tasks only what each
        entry declares is attributed. Decisions credit the same way."""
        data = {"route": "session", "session_ref": self.session_ref.to_dict(), **entry["round"]}
        if issue:
            data.setdefault("asked", issue)
        if self.base:
            data.setdefault("base", self.base)
        if entry.get("done"):
            data["done"] = True
        if solo:
            for key, found in (("notes", self.insights), ("feedback", self.verdicts)):
                if found:
                    data.setdefault(key, found)
            if self.decisions:
                data["decisions"] = {"minted": list(self.decisions), **(data.get("decisions") or {})}
        if entry.get("children"):
            data.setdefault("children", list(entry["children"]))
        return Round.from_dict(data, grain=task.grain)

    def _stamp_decisions(self, minted: list[str], task: Task) -> None:
        """Stamp each decision the round lists minted with the task id,
        in the session folder or wherever the index has it filed."""
        for dec_id in minted:
            path = self.decisions.get(dec_id) or indexed_note_path(self.vm.config, dec_id)
            if path is None:
                self.result.warnings.append(
                    f"decision {dec_id} is not in the index — task_id stamp skipped"
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
    of its issue's nearest epic — the payload's ``epic_url`` only when that
    lookup fails — minting a work-grain task, titled as its epic else as
    the trajectory, when none is open; a re-record of the same trajectory
    replaces its round. A run never closes its task, and nothing closes it when its PR
    merges: its tracker issue closing does (``TaskStore.close_tracked``).
    With no ``session_key`` no register row is written: no session owns
    the run.
    A payload outside the contract raises ``ValueError`` before anything is
    written."""
    warnings: list[str] = []
    repo = current_repo()
    issue = _tracker_ref(devloop_ask(payload), repo, warnings)
    assert isinstance(payload, dict)
    epic_url = normalize_tracker_ref(str(payload["frontmatter"].get("epic_url") or ""), repo)
    asked = _epic_ref(issue, warnings, fallback=epic_url)
    store = TaskStore(cfg)
    title = payload["frontmatter"].get("epic_title") or payload.get("title", "")
    task = store.open_by_ref(asked) or store.mint(
        "work",
        str(title),
        tracker_project(asked, cfg.vault_root) or project,
        asked=asked,
        session_key=session_key,
    )
    task.put_round(Round.from_devloop(
        payload,
        task_id=task.id,
        trajectory=trajectory,
        base=_merge_base(str(payload["frontmatter"].get("branch") or ""), warnings),
    ))
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
    if entry.asked:
        parts.append(f"asked {entry.asked}")
    if entry.done:
        parts.append("done")
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


_NO_TRANSCRIPT = "no transcript bound to the task"


def current_repo() -> str:
    """``owner/name`` of the working directory's GitHub ``origin``, or ``""``."""
    url = _git("remote", "get-url", "origin")
    found = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url)
    return found[1] if found else ""


def head_sha(cwd: str = "") -> str:
    """The full sha of ``cwd``'s (default: the working directory's) repo
    HEAD; ``""`` outside a repo, where no base commit exists to record."""
    return _git("rev-parse", "HEAD", cwd=cwd)


def _merge_base(branch: str, warnings: list[str]) -> str:
    """The commit ``branch`` forked from — its merge base with the trunk.
    ``""`` outside any repo, where no base commit exists to record; inside
    one, a branch or trunk git cannot resolve leaves the round unanchored,
    and that is announced."""
    if not branch:
        return ""
    if not _git("rev-parse", "--git-dir"):
        return ""
    for trunk in ("origin/HEAD", "origin/main", "origin/master"):
        if sha := _git("merge-base", trunk, branch):
            return sha
    warnings.append(
        f"no merge base for branch {branch!r} — the round carries no base "
        "commit, so a replay has no git anchor to start from"
    )
    return ""


def _git(*args: str, cwd: str = "") -> str:
    """One git call's stdout, stripped; ``""`` when git refuses or cannot
    run."""
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=5,
            check=True, cwd=cwd or None,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


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


def _epic_ref(ref: str, warnings: list[str], fallback: str = "") -> str:
    """The tracker ref of ``ref``'s nearest epic: ``ref`` itself when it has
    sub-issues, else its sub-issue parent, else ``ref``. A ref no lookup can
    take keeps ``fallback`` or ``ref``; so does a failed lookup, announced."""
    issue = GithubIssue.parse(ref)
    if not issue:
        return fallback or ref
    try:
        node = _gh_issue(issue.repo, issue.number)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        kept = fallback or ref
        reason = (getattr(exc, "stderr", "") or str(exc)).strip()
        warnings.append(
            f"epic lookup for {ref} failed ({reason}) — kept {kept}, so the "
            "round may miss its nearest epic's task"
        )
        return kept
    return node.epic(ref)


def _gh_issue(repo: str, number: str) -> IssueNode:
    """``repo#number``'s place in its sub-issue tree, read through ``gh api``."""
    return IssueNode.from_api(json.loads(subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{number}",
         "-q", "{sub_issues_summary, parent_issue_url}"],
        capture_output=True, text=True, timeout=15, check=True,
    ).stdout))


def _gh_issue_state(repo: str, number: str) -> str:
    """``repo#number``'s state, ``open`` or ``closed``."""
    return subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{number}", "-q", ".state"],
        capture_output=True, text=True, timeout=15, check=True,
    ).stdout.strip()


_CLOSING_REF_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+((?:[\w.-]+/[\w.-]+)?#\d+)",
    re.IGNORECASE,
)


def _closing_refs(message: str) -> list[str]:
    """The refs a commit message closes with GitHub's closing keywords."""
    return _CLOSING_REF_RE.findall(message)


def _gh_branch_prs(branch: str) -> dict[str, list[str]]:
    """The URL of each PR whose head is ``branch``, with the issue refs it closes."""
    out = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "all",
         "--json", "url,closingIssuesReferences"],
        capture_output=True, text=True, timeout=15, check=True,
    ).stdout
    return {
        pr["url"]: [f"#{i['number']}" for i in pr.get("closingIssuesReferences") or []]
        for pr in json.loads(out or "[]")
    }


_DISPATCH_INSTRUCTIONS = (
    "End every commit message with the trailer line `Task: {task_id}`. "
    "Before you finish, append one JSON line to {envelope_return}: "
    '{{"task_id": "{task_id}", "outcome": "<a short summary of what you did, '
    'or why you wrote nothing>", "outputs": [<each file you wrote and each '
    'note id you created>]}}.'
)


def _descriptor(cfg, task: Task) -> TaskDispatch:
    envelope_return = str(_envelope_path(cfg, task.id))
    return TaskDispatch(
        task_id=task.id,
        grain=task.grain,
        envelope_return=envelope_return,
        note=str(task.path),
        title=task.title,
        instructions=_DISPATCH_INSTRUCTIONS.format(
            task_id=task.id, envelope_return=envelope_return
        ),
    )


def _folder_notes(folders: list[Path]):
    """(path, frontmatter) for every note in the wrapped session's folders."""
    for folder in folders:
        for path in folder.rglob("*.md"):
            yield path, parse_frontmatter(path.read_text(encoding="utf-8"))[0]


def _same_file(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Whether two path-part tuples name one file: equal, or one is a
    trailing suffix of the other (a repo-relative path under an absolute)."""
    n = min(len(a), len(b))
    return n > 0 and a[-n:] == b[-n:]


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
# The child digest — each harness profile names its format's reader
# (HarnessProfile.digest_reader). transcript_parser keeps only text turns,
# while the digest needs tool calls, their results and a timestamp slice.


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
    tools: dict | None = None
    """Tool-call counts, or None when no transcript was read."""
    tool_errors: int = 0
    duration: float | None = None
    success: bool | None = None
    gaps: tuple[str, ...] = ()

    @classmethod
    def read(cls, source: TranscriptSource, *, until: str = "") -> ChildDigest:
        """Digest the source's transcript (from the prompt nearest its
        ``since`` up to ``until``, when given) with the reader its session
        ref's harness profile declares; never raises. The source's own gaps
        lead the digest's."""
        harness = source.session_ref.harness if source.session_ref else ""
        profile = lookup(harness)
        reader = profile.load_digest_reader() if profile else None
        missing = [_NO_TRANSCRIPT] if source.path is None else []
        if harness and not reader:
            missing.append(f"{harness} declares no digest reader for its transcripts")
        elif not harness and source.path is not None:
            missing.append("no session ref names the transcript's harness")
        if missing:
            return cls(gaps=source.gaps + tuple(missing))
        try:
            read = reader(source.path, source.since, until)
        except Exception as exc:  # noqa: BLE001 — a close must never fail on a read
            read = cls(gaps=(f"digest aborted: {type(exc).__name__}: {exc}",))
        return replace(read, gaps=source.gaps + read.gaps)

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
        fields: dict = {"digest": {"version": self.version, "gaps": list(self.gaps)}}
        if self.tools is not None:
            fields.update(tools=dict(self.tools), tool_errors=self.tool_errors)
        did = {"paths": list(self.paths), "commits": list(self.commits)}
        if any(did.values()):
            fields["did"] = {k: v for k, v in did.items() if v}
        if outputs:
            fields["outputs"] = list({(o["kind"], o["ref"]): o for o in outputs}.values())
        if self.duration is not None:
            fields["cost"] = {"duration": self.duration}
        return fields

    def with_trailer_commits(self, task_id: str) -> ChildDigest:
        """The digest plus every commit, on any ref of the working
        directory's repo, whose ``Task:`` trailer names ``task_id``; a
        failed search is a gap."""
        try:
            found = subprocess.run(
                ["git", "log", "--all", f"--grep=^Task: {task_id}$", "--format=%H"],
                capture_output=True, text=True, timeout=15, check=True,
            ).stdout.split()
        except (OSError, subprocess.SubprocessError) as exc:
            reason = (getattr(exc, "stderr", "") or str(exc)).strip()
            return replace(self, gaps=self.gaps + (f"Task trailer search failed: {reason}",))
        # A transcript's short sha of the same commit yields to the full one.
        kept = tuple(c for c in self.commits if not any(f.startswith(c) for f in found))
        return replace(self, commits=kept + tuple(found))

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


def claude_code_digest(transcript: Path, since: str, until: str) -> ChildDigest:
    """Claude Code's digest reader: walk the transcript slice once and
    collect every digest field."""
    from collections import Counter

    rows, gaps = _transcript_rows(transcript)
    meta = _transcript_meta(transcript)
    rows = _slice(rows, since, until, _claude_code_prompt)
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
    return _assemble(
        rows,
        models,
        asked,
        gaps,
        _timing_gaps(rows),
        version=next((str(r["version"]) for r in rows if r.get("version")), ""),
        description=str(meta.get("description", "")),
        role=str(meta.get("agentType", "")),
        paths=tuple(paths),
        commits=tuple(commits),
        notes=tuple(notes),
        tools=dict(tools),
        tool_errors=errors,
        success=success,
    )


def _assemble(rows, models, asked: str, gaps: list[str], late_gaps=(), **fields) -> ChildDigest:
    """A reader's digest from its walk: the dominant model, a gap per missing
    version, prompt or model (then ``late_gaps``), and the slice's timing."""
    model = models.most_common(1)[0][0] if models else ""
    missing = (("version", fields.get("version", "")), ("prompt", asked), ("model", model))
    gaps += [f"no {what} in the transcript" for what, value in missing if not value]
    gaps += late_gaps
    stamps = [t for t in map(_ts, rows) if t]
    return ChildDigest(
        asked=asked,
        model=model,
        duration=round((max(stamps) - min(stamps)).total_seconds(), 3) if stamps else None,
        gaps=tuple(gaps),
        **fields,
    )


def pi_digest(transcript: Path, since: str, until: str) -> ChildDigest:
    """Pi's digest reader: walk the slice of the session's live branch once
    and collect every digest field its entries carry."""
    from collections import Counter

    from thinkweave.acquisition.importers.pi import live_branch

    if not transcript.is_file():
        return ChildDigest(gaps=(f"transcript not found: {transcript}",))
    header, chain = live_branch(transcript)
    cwd = str(header.get("cwd", ""))
    rows = _slice([e for e in chain if e.get("type") == "message"], since, until, _pi_prompt)
    calls: dict[str, dict] = {}
    tools: Counter = Counter()
    models: Counter = Counter()
    paths: dict[str, None] = {}
    commits: dict[str, None] = {}
    notes: dict[str, None] = {}
    asked, errors = "", 0
    for row in rows:
        message = _dict(row.get("message"))
        content = message.get("content")
        if message.get("role") == "assistant":
            if message.get("model"):
                models[str(message["model"])] += 1
            for block in map(_dict, _list(content)):
                if block.get("type") == "toolCall" and block.get("name"):
                    calls[str(block.get("id", ""))] = block
                    tools[str(block["name"])] += 1
        elif message.get("role") == "toolResult":
            if message.get("isError"):
                errors += 1
                continue
            call = calls.get(str(message.get("toolCallId", "")), {})
            name, args = str(call.get("name", "")), _dict(call.get("arguments"))
            if name in _PI_FILE_TOOLS and args.get("path"):
                paths[_relative(str(args["path"]), cwd)] = None
            elif name == "bash":
                sha = _commit_sha(str(args.get("command", "")), {"stdout": _prompt_text(content)})
                if sha:
                    commits[sha] = None
            elif name.endswith(_NOTE_TOOLS):
                notes.update(dict.fromkeys(_created_ids(content)))
        elif not asked:
            asked = _pi_prompt(row)
    # The session header carries a format version, not Pi's own: no version.
    return _assemble(
        rows,
        models,
        asked,
        [],
        paths=tuple(paths),
        commits=tuple(commits),
        notes=tuple(notes),
        tools=dict(tools),
        tool_errors=errors,
    )


_PI_FILE_TOOLS = ("edit", "write")


def opencode_digest(transcript: Path, since: str, until: str) -> ChildDigest:
    """OpenCode's digest reader. OpenCode keeps every session in one store,
    so the bound transcript is ``<store>#<session id>`` (the shim's prompt
    envelope); walk that session's part rows once."""
    from collections import Counter

    from thinkweave.acquisition.importers.opencode import store_rows

    store, _, session_id = str(transcript).rpartition("#")
    if not (store and session_id):
        return ChildDigest(gaps=(f"transcript is not <store>#<session id>: {transcript}",))
    meta, rows = store_rows(Path(store), session_id)
    if not meta:
        return ChildDigest(gaps=(f"session {session_id} not found in {store}",))
    cwd = str(meta.get("directory", ""))
    rows = _slice(rows, since, until, lambda row: str(row.get("user_text", "")))
    tools: Counter = Counter()
    models: Counter = Counter()
    paths: dict[str, None] = {}
    commits: dict[str, None] = {}
    notes: dict[str, None] = {}
    asked, errors = "", 0
    for row in rows:
        part = _dict(row.get("part"))
        if row.get("role") == "user":
            asked = asked or str(row.get("user_text", ""))
            continue
        if row.get("model"):
            models[str(row["model"])] += 1
        if part.get("type") != "tool" or not part.get("tool"):
            continue
        name, state = str(part["tool"]), _dict(part.get("state"))
        tools[name] += 1
        if state.get("status") == "error":
            errors += 1
            continue
        args = _dict(state.get("input"))
        if name in _PI_FILE_TOOLS and args.get("filePath"):
            paths[_relative(str(args["filePath"]), cwd)] = None
        elif name == "bash":
            sha = _commit_sha(str(args.get("command", "")), {"stdout": state.get("output", "")})
            if sha:
                commits[sha] = None
        elif name.endswith(_NOTE_TOOLS):
            notes.update(dict.fromkeys(_created_ids(state.get("output", ""))))
    return _assemble(
        rows,
        models,
        asked,
        [],
        version=str(meta.get("version", "")),
        paths=tuple(paths),
        commits=tuple(commits),
        notes=tuple(notes),
        tools=dict(tools),
        tool_errors=errors,
    )


def codex_digest(transcript: Path, since: str, until: str) -> ChildDigest:
    """Codex's digest reader: count the rollout slice's completed tool items
    (shell, patch, MCP, collab), whatever code-mode script issued them."""
    from collections import Counter

    rows, gaps = _transcript_rows(transcript)
    meta = next((_dict(r.get("payload")) for r in rows if r.get("type") == "session_meta"), {})
    # A turn's context row precedes its prompt, so models come from the whole rollout.
    models = Counter(
        str(_dict(r.get("payload")).get("model")) for r in rows
        if r.get("type") == "turn_context" and _dict(r.get("payload")).get("model")
    )
    rows = _slice(rows, since, until, _codex_prompt)
    cwd = str(meta.get("cwd", ""))
    tools: Counter = Counter()
    paths: dict[str, None] = {}
    commits: dict[str, None] = {}
    notes: dict[str, None] = {}
    asked, errors = "", 0
    for row in rows:
        payload = _dict(row.get("payload"))
        asked = asked or _codex_prompt(row)
        item = _dict(payload.get("item")) if payload.get("type") == "item_completed" else {}
        name = _codex_tool_name(item)
        if not name:
            continue
        tools[name] += 1
        if item.get("status") != "completed":
            errors += 1
        elif name == "apply_patch":
            paths.update(dict.fromkeys(_relative(str(p), cwd) for p in _dict(item.get("changes"))))
        elif name == "Bash":
            command = " ".join(str(c) for c in _list(item.get("command"))[-1:])
            sha = _commit_sha(command, {"stdout": item.get("stdout", "")})
            if sha:
                commits[sha] = None
        elif name.endswith(_NOTE_TOOLS):
            notes.update(dict.fromkeys(_created_ids(_dict(item.get("result")).get("content"))))
    calls = sum(
        1 for r in rows
        if _dict(r.get("payload")).get("type") in ("function_call", "custom_tool_call")
    )
    if calls and not tools:
        gaps.append(f"{calls} tool call(s) but no completed-item records to count them from")
    return _assemble(
        rows,
        models,
        asked,
        gaps,
        _timing_gaps(rows),
        version=str(meta.get("cli_version", "")),
        paths=tuple(paths),
        commits=tuple(commits),
        notes=tuple(notes),
        tools=dict(tools),
        tool_errors=errors,
    )


_CODEX_ITEM_TOOLS = {"CommandExecution": "Bash", "FileChange": "apply_patch"}


def _codex_tool_name(item: dict) -> str:
    """A completed rollout item's tool name in the hook vocabulary; ``""``
    for messages, reasoning and other non-tool items."""
    kind = item.get("type")
    if kind == "McpToolCall":
        return f"mcp__{item.get('server', '')}__{item.get('tool', '')}"
    if kind == "CollabAgentToolCall":
        return str(item.get("tool", ""))
    return _CODEX_ITEM_TOOLS.get(str(kind), "")


def _codex_prompt(row: dict) -> str:
    """A rollout row's user prompt text; ``""`` for every other row."""
    payload = _dict(row.get("payload"))
    item = _dict(payload.get("item"))
    if payload.get("type") != "item_completed" or item.get("type") != "UserMessage":
        return ""
    return "\n".join(
        str(_dict(b).get("text", "")) for b in _list(item.get("content"))
    ).strip()


def _timing_gaps(rows: list[dict]) -> list[str]:
    """A gap for the rows whose timestamp is unreadable or zone-less."""
    naive = sum(1 for r in rows if r.get("timestamp") and _ts(r) is None)
    return [f"{naive} row(s) with an unreadable or zone-less timestamp left out of timing"] if naive else []


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


def _slice(rows: list[dict], since: str, until: str, prompt) -> list[dict]:
    """Rows from the prompt nearest ``since`` up to ``until``; ``prompt``
    gives a row's human prompt text, ``""`` for any other row.

    The binding hook and the transcript stamp the same prompt a moment
    apart, in either order, so the slice opens at the nearest prompt row.
    """
    start, end = _ts({"timestamp": since}), _ts({"timestamp": until})
    if start:
        prompts = [i for i, row in enumerate(rows) if _ts(row) and prompt(row)]
        if prompts:
            first = min(prompts, key=lambda i: abs(_ts(rows[i]) - start))
            rows = rows[first:]
    if end:
        rows = [r for r in rows if not _ts(r) or _ts(r) <= end]
    return rows


def _claude_code_prompt(row: dict) -> str:
    if row.get("type") != "user" or row.get("isMeta"):
        return ""
    return _prompt_text(_dict(row.get("message")).get("content"))


def _pi_prompt(entry: dict) -> str:
    from thinkweave.acquisition.importers.pi import user_prompt

    return user_prompt(_dict(entry.get("message")))


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
