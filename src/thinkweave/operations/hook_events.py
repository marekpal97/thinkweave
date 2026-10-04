"""Hook-envelope name translation and Bash command classification.

One vocabulary, per-profile name maps. Claude Code's event vocabulary is the
canonical one: shims for other harnesses are *translators* onto it, allowed
to adapt protocol but never vault semantics. ``HarnessProfile.hook_events``
declares each harness's canonical→native name map, and :func:`to_native` /
:func:`to_canonical` swap ``hook_event_name`` between the two vocabularies
without touching any other field. Claude Code and Codex speak the canonical
names natively (docs/HARNESSES.md §"Event names"), so for them both
directions are the identity.

The *handler* does not call the translators: an installed hook command
carries no ``$THINKWEAVE_HARNESS``, so it reads its own argv instead
(docs/HARNESSES.md §"Why the handler reads argv, not the profile"). The
consumers are shims and the conformance suite.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from thinkweave.core.harness import CANONICAL_EVENTS as CANONICAL_EVENTS

if TYPE_CHECKING:
    from thinkweave.core.harness import HarnessProfile


class UnknownHookEvent(ValueError):
    """The envelope names an event the profile declares no mapping for.

    Refusing beats guessing: a subscription to an event that never fires
    captures nothing, silently. An unmapped name is surfaced, never passed
    through.
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
    classifies as ``pytest -q`` — the shape Codex's code-mode
    ``exec_command`` calls take. Lower-cased; classifiers compare against
    lower-case prefixes.
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
    """Check if a bash command, or any ``&&``/``||``/``;`` segment of it, is
    a git commit."""
    return any(
        cmd.startswith("git commit") and "--amend" not in cmd
        for cmd in map(command_head, command_segments(command))
    )


def command_segments(command: str) -> list[str]:
    """The segments of a command chained by ``&&``, ``||`` or ``;``.

    Separators inside quotes stay part of their segment. A command whose
    quotes do not balance is not valid shell and comes back whole.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return [command]
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in ("&&", "||", ";"):
            segments.append([])
        else:
            segments[-1].append(token)
    return [shlex.join(seg) for seg in segments if seg]


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
