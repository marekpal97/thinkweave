"""The hook-envelope normaliser — one vocabulary, per-profile name maps.

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
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from thinkweave.core.harness import CANONICAL_EVENTS as CANONICAL_EVENTS

if TYPE_CHECKING:
    from thinkweave.core.harness import HarnessProfile


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
