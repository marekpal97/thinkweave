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
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
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
        title=fm["title"],
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
