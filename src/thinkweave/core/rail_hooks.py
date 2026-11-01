"""The rsi-rail hook registration set: ``<rsi_rail>/hooks/hooks.toml``.

Layers 2 and 3 of dec-4f158b58: which canonical hook events are registered,
which matchers ``PostToolUse`` registers under, and which foreign hook
scripts (scripts beside the file, under the rail's ``hooks/`` folder) run on
which event. The file is harness-neutral (dec-6d5f1913); the installer
renders it per profile and the doctor compares what it rendered.

The file is sparse like the rest of the rail: absent, or every line
commented, means the canonical registrations of ``hooks/hooks.json`` and no
foreign hook, which is exactly today's behaviour. Unknown event names and
unknown keys are refused by name, so a typo never reads as a default.

:func:`load` takes the repo and the canonical definitions and returns a
:class:`Registration`; :func:`read` adds the canonical file. Lives in
``core`` because the round manifest (``core.manifest``) records the set in
force, and core never imports the surfaces that render it.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from thinkweave.core.harness import CANONICAL_EVENTS, HarnessProfile
from thinkweave.core.plugin_route import plugin_namespace

RAIL_HOOKS_DIR = "hooks"
"""The rsi-rail folder thinkweave's installer reads: ``hooks.toml`` and the
foreign hook scripts it names."""

RAIL_HOOKS_FILE = "hooks.toml"

_EVENT_KEYS = ("enabled", "matchers")
_FOREIGN_KEYS = ("name", "event", "matcher", "script")
_FOREIGN_REQUIRED = ("name", "event", "script")


@dataclass(frozen=True)
class ForeignHook:
    """One ``[[foreign]]`` row: a script of the user's own that runs on
    ``event`` (and, for a tool event, on tools matching ``matcher``)."""

    name: str
    event: str
    matcher: str
    script: Path
    """Absolute path of the script, inside the rail's ``hooks/`` folder."""

    @property
    def command(self) -> str:
        """The command a settings-file harness registers: the quoted path."""
        return f'"{self.script.as_posix()}"'


@dataclass(frozen=True)
class Registration:
    """The registration set a rail file declares. The default instance is
    the canonical set: every event enabled, canonical matchers, no foreign
    hook."""

    disabled: frozenset[str] = frozenset()
    matchers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Event → matchers, only for events whose matchers the file sets."""
    foreign: tuple[ForeignHook, ...] = ()
    hooks_dir: Path | None = None
    """The rail's ``hooks/`` folder, when a rail is configured: what marks a
    settings-file entry as a rendered foreign hook."""

    @property
    def is_default(self) -> bool:
        return not (self.disabled or self.matchers or self.foreign)


DEFAULT = Registration()


def canonical_hooks_path() -> Path:
    """Locate the canonical ``hooks/hooks.json`` shipped at the repo root.

    Walks up from this module's file — the package is installed editable
    into the repo venv by ``uv run --project`` on every supported route
    (there is no PyPI wheel), so the repo layout is always present. Fails
    loud otherwise: an installer that cannot see its source of truth must
    not fall back to a guess.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "hooks" / "hooks.json"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "hooks/hooks.json not found above "
        f"{Path(__file__).resolve()} — `weave hooks install` derives the "
        "hook definitions from the canonical file and must run from a "
        "thinkweave repo checkout (plugin or dev clone)."
    )


def load_canonical_hooks(hooks_json: str | Path = "") -> dict:
    """Parse the canonical hook definitions: ``{event: [entries…]}``.

    ``hooks_json`` overrides the content source (contract tests drive the
    installer with a mutated temp copy); the default is the repo's own
    ``hooks/hooks.json``.
    """
    path = Path(hooks_json) if hooks_json else canonical_hooks_path()
    return json.loads(path.read_text(encoding="utf-8"))["hooks"]


def read(repo: Path | None, hooks_json: str | Path = "") -> Registration:
    """:func:`load` against the canonical definitions."""
    return load(repo, load_canonical_hooks(hooks_json))


def plugin_owned(profile: HarnessProfile) -> bool:
    """True when an active plugin owns this profile's registration (Claude
    Code's plugin route): its hooks.json is then the registration, and the
    settings files carry only what the plugin does not own."""
    return profile.hook_mechanism == "plugin" and bool(
        plugin_namespace(manifest=profile.installed_plugins, dev_link=profile.dev_link)
    )


def load(repo: Path | None, canonical: Mapping[str, list]) -> Registration:
    """Read ``<repo>/hooks/hooks.toml`` against the canonical definitions
    (``{event: [entries…]}`` from ``hooks/hooks.json``). No repo or no file
    → :data:`DEFAULT` (with ``hooks_dir`` set when a repo is). Raises
    ``ValueError`` naming the offending event, key or script."""
    if repo is None:
        return DEFAULT
    hooks_dir = Path(repo) / RAIL_HOOKS_DIR
    path = hooks_dir / RAIL_HOOKS_FILE
    if not path.is_file():
        return Registration(hooks_dir=hooks_dir)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: not valid TOML: {exc}") from exc

    disabled: set[str] = set()
    matchers: dict[str, tuple[str, ...]] = {}
    foreign: list[ForeignHook] = []
    for key, value in data.items():
        if key == "foreign":
            foreign = _foreign_rows(path, hooks_dir, value)
            continue
        if key not in CANONICAL_EVENTS:
            raise ValueError(
                f"{path}: unknown event {key!r}; canonical events are {', '.join(CANONICAL_EVENTS)}"
            )
        if not isinstance(value, dict):
            raise ValueError(f"{path}: [{key}] must be a table")
        for sub, setting in value.items():
            if sub not in _EVENT_KEYS:
                raise ValueError(
                    f"{path}: unknown key {key}.{sub}; allowed: {', '.join(_EVENT_KEYS)}"
                )
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"{path}: {key}.enabled must be true or false")
        if not enabled:
            disabled.add(key)
        if "matchers" in value:
            matchers[key] = _matchers(path, key, value["matchers"], canonical)
    return Registration(
        disabled=frozenset(disabled),
        matchers=matchers,
        foreign=tuple(foreign),
        hooks_dir=hooks_dir,
    )


def _matchers(
    path: Path, event: str, value: object, canonical: Mapping[str, list]
) -> tuple[str, ...]:
    """Validate ``[event].matchers``: only an event whose canonical entries
    carry a matcher (``PostToolUse`` today) takes one, as a non-empty list of
    non-empty strings."""
    if not any(e.get("matcher") for e in canonical.get(event, [])):
        raise ValueError(
            f"{path}: {event}.matchers is not allowed; {event} registers no tool matcher"
        )
    if not isinstance(value, list) or not value or not all(isinstance(m, str) and m for m in value):
        raise ValueError(
            f"{path}: {event}.matchers must be a non-empty list of matcher "
            "strings (set enabled = false to drop the event)"
        )
    return tuple(value)


def _foreign_rows(path: Path, hooks_dir: Path, value: object) -> list[ForeignHook]:
    if not isinstance(value, list):
        raise ValueError(f"{path}: foreign must be an array of tables ([[foreign]])")
    rows: list[ForeignHook] = []
    names: set[str] = set()
    root = hooks_dir.resolve()
    for i, row in enumerate(value):
        where = f"{path}: foreign[{i}]"
        if not isinstance(row, dict):
            raise ValueError(f"{where} must be a table")
        for key in row:
            if key not in _FOREIGN_KEYS:
                raise ValueError(
                    f"{where}: unknown key {key!r}; allowed: {', '.join(_FOREIGN_KEYS)}"
                )
        for key in _FOREIGN_REQUIRED:
            if not isinstance(row.get(key), str) or not row[key]:
                raise ValueError(f"{where}: {key} is required (a string)")
        name, event, script = row["name"], row["event"], row["script"]
        matcher = row.get("matcher", "")
        if not isinstance(matcher, str):
            raise ValueError(f"{where} ({name}): matcher must be a string")
        if name in names:
            raise ValueError(f"{where}: duplicate foreign hook name {name!r}")
        names.add(name)
        if event not in CANONICAL_EVENTS:
            raise ValueError(
                f"{where} ({name}): unknown event {event!r}; canonical events "
                f"are {', '.join(CANONICAL_EVENTS)}"
            )
        target = (hooks_dir / script).resolve()
        if Path(script).is_absolute() or not target.is_relative_to(root):
            raise ValueError(
                f"{where} ({name}): script {script!r} must be a path inside {hooks_dir}"
            )
        if not target.is_file():
            raise ValueError(f"{where} ({name}): script {target} does not exist")
        if os.name != "nt" and not os.access(target, os.X_OK):
            raise ValueError(f"{where} ({name}): script {target} is not executable")
        rows.append(ForeignHook(name, event, matcher, hooks_dir / script))
    return rows


# --------------------------------------------------------------------------- #
# What a profile can carry
# --------------------------------------------------------------------------- #


def _native(profile: HarnessProfile, event: str) -> bool:
    return bool(profile.hook_events.get(event))


def unrenderable(profile: HarnessProfile, reg: Registration, *, plugin_owned: bool) -> list[str]:
    """What of ``reg`` this profile cannot carry, one phrase each.

    A foreign hook on an event the profile has no native equivalent for
    cannot fire there. When an active plugin owns registration (Claude
    Code's plugin route), changed matchers cannot be expressed: the
    plugin's hooks.json registers its own and Claude Code offers no
    per-event suppression of a plugin hook (settings hooks merge with plugin
    hooks; ``disableAllHooks`` is all-or-nothing). A disabled event is
    carried there all the same: the hook handler reads this file and exits
    before any work on an event it disables."""
    out = [
        f"foreign hook {f.name!r} on {f.event} (no native {f.event} on {profile.id})"
        for f in reg.foreign
        if not _native(profile, f.event)
    ]
    if plugin_owned:
        out += [
            f"{e} matchers (the active plugin registers its own)"
            for e in CANONICAL_EVENTS
            if e in reg.matchers and _native(profile, e)
        ]
    return out


def events_in_force(profile: HarnessProfile, reg: Registration, *, plugin_owned: bool) -> list[str]:
    """The canonical events in force on this profile once ``reg`` is
    rendered, sorted: natively mapped, minus disabled ones. On the plugin
    route a disabled event stays registered but the handler drops it, so it
    is not in force either (``plugin_owned`` is kept for the callers)."""
    return sorted(
        e for e in CANONICAL_EVENTS if _native(profile, e) and e not in reg.disabled
    )


def foreign_in_force(profile: HarnessProfile, reg: Registration) -> list[ForeignHook]:
    """The foreign hooks this profile carries."""
    return [f for f in reg.foreign if _native(profile, f.event)]


def stub_payload(profile: HarnessProfile, reg: Registration) -> dict | None:
    """The registration set an extension-mechanism stub hands its shim, or
    None when ``reg`` changes nothing there (the stub stays today's one-line
    re-export). ``events``: the enabled canonical events; ``matchers``: only
    events whose matchers the rail sets; ``foreign``: rows the profile can
    carry, with the script's absolute path."""
    foreign = foreign_in_force(profile, reg)
    if not (reg.disabled or reg.matchers or foreign):
        return None
    payload: dict = {"events": [e for e in CANONICAL_EVENTS if e not in reg.disabled]}
    if reg.matchers:
        payload["matchers"] = {e: list(m) for e, m in reg.matchers.items()}
    payload["foreign"] = [
        {
            "name": f.name,
            "event": f.event,
            "matcher": f.matcher,
            "script": f.script.as_posix(),
        }
        for f in foreign
    ]
    return payload
