"""The hook-envelope normaliser and the task-lifecycle event writers.

One vocabulary, per-profile name maps.

Claude Code's event vocabulary is the canonical one (dec-5a076384): E3 shims
for other harnesses are *translators* onto it, allowed to adapt protocol but
never vault semantics. This module is the Python side of that seam (cf. #25):
``HarnessProfile.hook_events`` declares each harness's canonical→native name
map, and the two functions here swap ``hook_event_name`` between the two
vocabularies without touching any other field. Claude Code and Codex speak
the canonical names natively (measured — docs/HARNESSES.md §"Event names"),
so for them both directions are the identity.

The *handler* deliberately does not call this: an installed hook command
carries no ``$THINKWEAVE_HARNESS``, so it reads its own argv instead
(docs/HARNESSES.md §"Why the handler reads argv, not the profile"). The
consumers are shims and the conformance suite.

The task writers below put ``task_open`` / ``task_close`` rows into the
existing per-session events register (``<weave_dir>/buffer/<key>.jsonl``) —
the ledger is a projection of that register, never a separate file, and a
task's open and close correlate by the vault-minted task id alone. Harness
identifiers ride only as a qualified ``{harness, kind, value}`` triple under
``session_ref``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from thinkweave.core.events import iter_jsonl
from thinkweave.core.harness import CANONICAL_EVENTS as CANONICAL_EVENTS

if TYPE_CHECKING:
    from thinkweave.core.harness import HarnessProfile

TASK_OPEN = "task_open"
TASK_CLOSE = "task_close"
TASK_EVENT_TYPES = (TASK_OPEN, TASK_CLOSE)


def task_open_event(
    task_id: str,
    ts: str,
    *,
    session_id: str,
    grain: str,
    session_ref: dict | None = None,
) -> dict:
    """One ``task_open`` register row — minted at the dispatch seam."""
    event = {
        "ts": ts,
        "type": TASK_OPEN,
        "task_id": task_id,
        "session_id": session_id,
        "grain": grain,
    }
    if session_ref:
        event["session_ref"] = session_ref
    return event


def task_close_event(
    task_id: str,
    ts: str,
    *,
    session_id: str,
    orphan: bool = False,
    session_ref: dict | None = None,
) -> dict:
    """One ``task_close`` register row. ``orphan`` marks a boundary close
    that could not be paired to an open — recorded loudly, never dropped."""
    event = {
        "ts": ts,
        "type": TASK_CLOSE,
        "task_id": task_id,
        "session_id": session_id,
    }
    if orphan:
        event["orphan"] = True
    if session_ref:
        event["session_ref"] = session_ref
    return event


def register_path(weave_dir: Path, session_key: str) -> Path:
    """The events-register stream one session key appends to."""
    return weave_dir / "buffer" / f"{session_key}.jsonl"


def append_task_event(weave_dir: Path, session_key: str, event: dict) -> Path:
    """Append one lifecycle row to the session's events register."""
    path = register_path(weave_dir, session_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return path


def task_rows(path: Path) -> list[dict]:
    """The task-lifecycle rows of one register stream, in file order.

    Same tolerant read as :func:`thinkweave.core.events.feedback_events`:
    works on a live buffer or an archived ``events.jsonl``, skips malformed
    lines, returns ``[]`` for an absent file.
    """
    return [r for r in iter_jsonl(path) if r.get("type") in TASK_EVENT_TYPES]


class UnknownHookEvent(ValueError):
    """The envelope names an event the profile declares no mapping for.

    Refusing beats guessing: claude-mem's OpenCode plugin subscribed to bus
    events that never fire and captured nothing, silently, until a user filed
    a bug (#2462). An unmapped name is surfaced, never passed through.
    """


def to_native(profile: HarnessProfile, envelope: dict) -> dict:
    """Rewrite a canonical envelope's event name into the profile's native one."""
    event = envelope.get("hook_event_name", "")
    native = profile.hook_events.get(event)
    if not native:
        raise UnknownHookEvent(
            f"{profile.id} declares no native event for canonical {event!r}"
        )
    return {**envelope, "hook_event_name": native}


def to_canonical(profile: HarnessProfile, envelope: dict) -> dict:
    """Rewrite a native envelope's event name into the canonical vocabulary."""
    native = envelope.get("hook_event_name", "")
    for event, mapped in profile.hook_events.items():
        if mapped == native:
            return {**envelope, "hook_event_name": event}
    raise UnknownHookEvent(
        f"{profile.id} maps no canonical event onto native {native!r}"
    )


# ---------------------------------------------------------------------------
# Bash command classification — shared by the hook capture and the child digest


_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+")


def command_head(segment: str) -> str:
    """A shell segment normalised for prefix classification.

    Leading ``VAR=value`` assignments are dropped and the executable is
    reduced to its basename, so ``PYTHONPATH=src /repo/.venv/bin/pytest -q``
    classifies as ``pytest -q``. That is the shape Codex's code-mode
    ``exec_command`` calls took on 2026-09-05 — every one of the session's
    pytest runs was kept only because ``"pythonpath…".startswith("python")``
    and none was recognised as a test run. Lower-cased; classifiers compare
    against lower-case prefixes.
    """
    seg = segment.strip()
    while True:
        stripped = _ENV_ASSIGNMENT_RE.sub("", seg, count=1)
        if stripped == seg:
            break
        seg = stripped
    if not seg:
        return ""
    head, sep, rest = seg.partition(" ")
    head = head.rsplit("/", 1)[-1]
    return (head + sep + rest).lower()


def is_git_commit(command: str) -> bool:
    """Check if a bash command is a git commit."""
    cmd = command_head(command)
    return cmd.startswith("git commit") and "--amend" not in cmd


def parse_commit_from_output(command: str, output: str) -> dict | None:
    """Extract commit info from git commit output.

    Git commit output looks like:
      [branch abc1234] Commit message
       N files changed, M insertions(+), K deletions(-)
    """
    if not output:
        return None

    info: dict = {}

    # Extract hash from [branch hash] pattern
    m = re.search(r"\[[\w/.-]+\s+([0-9a-f]{7,})\]", output)
    if m:
        info["hash"] = m.group(1)

    # Extract message from -m flag or from output
    m_flag = re.search(r'-m\s+["\'](.+?)["\']', command)
    if m_flag:
        info["message"] = m_flag.group(1)[:120]
    else:
        # Message is after the hash bracket
        m_msg = re.search(r"\[[^\]]+\]\s+(.+)", output)
        if m_msg:
            info["message"] = m_msg.group(1).strip()[:120]

    # Extract files from "N file(s) changed" line
    m_files = re.search(r"(\d+)\s+files?\s+changed", output)
    if m_files:
        info["files_changed"] = int(m_files.group(1))

    return info if info else None
