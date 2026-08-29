"""Reading and writing one MCP-server registration, in whichever file format
the active harness keeps its config in.

:class:`~thinkweave.core.harness.HarnessProfile` says *where* the config lives
and deliberately says nothing about its format — that is this module's job.
Claude Code keeps JSON (``~/.claude.json``, servers under ``mcpServers``);
Codex keeps TOML (``$CODEX_HOME/config.toml``, servers under ``mcp_servers``).
The servers key usually follows the format's convention, but it is ultimately
the profile's fact (``mcp_servers_key``): OpenCode keeps a JSON file whose key
is ``mcp``, so every production caller passes the declared key through the
``servers_key=`` parameter and the suffix-derived default is only a fallback.

Two rules shape everything here:

*Key-scoped, never sentinel-scoped.* The entry is found by its name under the
servers table, whoever wrote it. ChatGPT desktop's Settings→Import can
pre-create a ``thinkweave`` entry carrying no marker of ours, and a second
registration beside it would make the harness spawn the server twice — so
adoption has to work on identity, not provenance (#106).

*Never rewrite bytes we do not own.* A harness config is a user's file: their
model choice, their other servers, their comments. The TOML writer splices our
table in textually and leaves every other line exactly as it found it, then
re-parses the result and refuses to save if anything outside our key moved.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from thinkweave.core.fswrite import atomic_write_text

TOML_SERVERS_KEY = "mcp_servers"
JSON_SERVERS_KEY = "mcpServers"

#: The documented entry bodies :func:`canonical` can render — the vocabulary a
#: profile's ``mcp_entry_shape`` must come from (pinned by the conformance
#: suite, so a row cannot declare a tag no interpreter branch dispatches on).
ENTRY_SHAPES = ("command-args", "argv-array")


class MalformedConfig(ValueError):
    """The config file exists but could not be parsed — the caller should
    refuse rather than overwrite a file it does not understand."""


def _is_toml(path: Path) -> bool:
    return path.suffix == ".toml"


def canonical(
    path: Path, entry: dict[str, Any], *, shape: str = "command-args"
) -> dict[str, Any]:
    """The entry as this harness's documented schema stores it.

    Callers build one entry shape (Claude Code's split ``command``/``args``/
    ``env``) and pass it through here so that what they later compare against
    :func:`read_entry` is like-for-like — otherwise every repeat ``weave
    install`` would report phantom drift.

    ``shape`` is the profile's ``mcp_entry_shape``: a harness's own published
    schema is a truth source for declared profile data (dec-2fa074a0, owner
    override 2026-08-29), so a differing documented body is rendered here —
    keyed off profile data, never a per-harness writer fork. Whether the
    written entry parses on a live install remains #114/#195's verification.

    ``argv-array`` (OpenCode, blueprint n-767d66b4 §4): ``type: local``,
    launcher and argv merged into ONE ``command`` array, and an
    ``environment`` map — omitted when empty, as the docs mark it optional.

    ``command-args`` keeps the split shape and then normalises by file
    format: Codex infers the transport from the presence of ``command`` vs
    ``url``, has no ``type`` key, and ``codex exec --strict-config`` errors
    out on the unknown field. An empty ``env`` is dropped for the same reason
    ``codex mcp add`` omits it — it is noise, and its absence is what a
    re-read returns.
    """
    if shape not in ENTRY_SHAPES:
        raise ValueError(
            f"unknown mcp_entry_shape {shape!r}; expected one of {ENTRY_SHAPES}"
        )
    if shape == "argv-array":
        merged: dict[str, Any] = {
            "type": "local",
            "command": [entry["command"], *entry.get("args", [])],
        }
        if entry.get("env"):
            merged["environment"] = dict(entry["env"])
        # `enabled` is optional in the docs, whose own example sets it true
        # (opencode.ai/docs/mcp-servers/ via n-767d66b4 §4). Written
        # explicitly because a wrong guess about its default is the silent
        # registered-but-never-started failure (review r3 advisory).
        merged["enabled"] = True
        # ponytail: the shape dispatch returns here, BEFORE the file-format
        # trims below — shape and format are ordered, not composed. Safe
        # while the only argv-array row writes JSON; a TOML harness
        # declaring argv-array would need the trims moved after the
        # dispatch.
        return merged
    if not _is_toml(path):
        return entry
    return {k: v for k, v in entry.items() if k != "type" and v != {}}


def invocation(entry: dict[str, Any]) -> tuple[str, list[Any]]:
    """The ``(command, args)`` pair an entry launches, whichever shape holds it.

    The ``argv-array`` body carries one merged ``command`` array and no
    ``args`` key; every other shape splits a launcher string from an ``args``
    list. Readers — the doctor's fingerprint and its launch probe — come
    through here so both shapes compare, and run, as the same invocation.
    """
    cmd = entry.get("command", "")
    if isinstance(cmd, list):
        return (cmd[0] if cmd else "", list(cmd[1:]))
    return (cmd, list(entry.get("args", [])))


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def _load(path: Path) -> dict[str, Any] | None:
    """Parse the whole config, or None when the file does not exist."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        return tomllib.loads(text) if _is_toml(path) else json.loads(text)
    except (OSError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise MalformedConfig(f"{path} is not valid {path.suffix.lstrip('.') or 'JSON'}: {exc}") from exc


def _servers_key(path: Path, override: str = "") -> str:
    """The key server entries nest under — the format's convention, unless
    the harness profile declares otherwise (``mcp_servers_key``: OpenCode is
    a JSON file whose key is ``mcp``, not ``mcpServers``)."""
    return override or (TOML_SERVERS_KEY if _is_toml(path) else JSON_SERVERS_KEY)


def read_entry(
    path: Path, name: str, *, servers_key: str = ""
) -> dict[str, Any] | None:
    """The named server's block, or None when the file or the entry is absent.

    Raises :class:`MalformedConfig` on an unparseable file.
    """
    doc = _load(path)
    if doc is None:
        return None
    return doc.get(_servers_key(path, servers_key), {}).get(name)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def write_entry(
    path: Path, name: str, entry: dict[str, Any], *, servers_key: str = ""
) -> None:
    """Register (or converge) the named server, leaving the rest of the file
    alone. Creates the file when it does not exist."""
    if _is_toml(path):
        _toml_write(path, name, entry, servers_key)
    else:
        _json_write(path, name, entry, servers_key)


def remove_entry(path: Path, name: str, *, servers_key: str = "") -> bool:
    """Drop the named server. Returns False when there was nothing to remove."""
    if read_entry(path, name, servers_key=servers_key) is None:
        return False
    if _is_toml(path):
        _toml_write(path, name, None, servers_key)
    else:
        _json_write(path, name, None, servers_key)
    return True


def _json_write(
    path: Path, name: str, entry: dict[str, Any] | None, key: str = ""
) -> None:
    doc = _load(path) or {}
    servers = doc.setdefault(_servers_key(path, key), {})
    if entry is None:
        servers.pop(name, None)
    else:
        servers[name] = entry
    atomic_write_text(path, json.dumps(doc, indent=2) + "\n")


# ---------------------------------------------------------------------------
# the TOML half
# ---------------------------------------------------------------------------

# A table header on its own line. TOML requires headers to start a line, so
# this finds every one of them — including the array-of-tables `[[…]]` form,
# which must terminate our block just as a plain header does.
_HEADER = re.compile(r"^[ \t]*\[\[?(?P<key>[^\]]*)\]\]?[ \t]*(?:#.*)?$")


def _owns(header_key: str, name: str, key: str) -> bool:
    """True for our table and any of its sub-tables (``…thinkweave.env``)."""
    parts = [p.strip().strip("\"'") for p in header_key.split(".")]
    return parts[:2] == [key, name]


def _toml_scalar(value: Any) -> str:
    """Render one TOML value.

    ``json.dumps`` handles strings: JSON's escape set is a subset of TOML's
    basic-string escapes, so a Windows path's backslashes come out correct.
    ``ensure_ascii=False`` is required, not cosmetic — the default escapes a
    non-BMP character (an emoji in a vault path) as a surrogate pair, which
    TOML rejects as "not a Unicode scalar value".
    """
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(v) for v in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(
            f"{_toml_scalar(k)} = {_toml_scalar(v)}" for k, v in value.items()
        )
        return "{ " + inner + " }"
    return json.dumps(value, ensure_ascii=False)


def _toml_table(name: str, entry: dict[str, Any], key: str) -> str:
    """Our table, rendered as one contiguous block (``env`` inlined, so the
    whole entry is a single splice unit)."""
    lines = [f"[{key}.{name}]"]
    lines += [f"{k} = {_toml_scalar(v)}" for k, v in entry.items()]
    return "\n".join(lines) + "\n"


def _splice(text: str, name: str, block: str | None, key: str) -> str:
    """Replace our table (and its sub-tables) with ``block``, or drop it when
    ``block`` is None. Appends when the table is absent.

    Sections are cut at header lines, so every byte of every *other* section —
    comments, spacing, key order — survives untouched.
    """
    out: list[str] = []
    tail: list[str] = []  # trailing blank lines of the section before ours

    def keep(line: str) -> None:
        """Emit a line we do not own, holding blanks back: on a removal they
        belong to the gap we are closing, not to the section above."""
        nonlocal tail
        if line.strip() == "":
            # A blank with nothing above it separates nothing, which happens
            # only once our table has gone from the very top of the file. A
            # user's own leading whitespace survives: `replaced` is still False
            # until we reach our table.
            if out or not replaced:
                tail.append(line)
            return
        out.extend(tail)
        tail = []
        out.append(line)

    ours = False
    replaced = False
    trailing: list[str] = []  # blank/comment run at the end of our table
    for line in text.splitlines(keepends=True):
        header = _HEADER.match(line)
        if header:
            was_ours, ours = ours, _owns(header.group("key"), name, key)
            if was_ours and not ours:
                # A comment sitting directly above a header documents *that*
                # header, so the run trailing our table is not ours to delete.
                # Replaying it through `keep` rather than appending it verbatim
                # is what collapses the gap correctly on a removal.
                for held in trailing:
                    keep(held)
            trailing = []
            if ours and not replaced:
                # Our table's turn: emit the replacement once, here, so the
                # entry keeps its position in the user's file.
                if block is not None:
                    out.extend(tail)
                    out.append(block)
                tail = []
                replaced = True
                continue
        if ours:
            # A comment followed by one of our own keys documents our table and
            # leaves with it; only an unbroken run up to the next header is held.
            trailing = (
                [*trailing, line]
                if line.strip() == "" or line.lstrip().startswith("#")
                else []
            )
            continue
        keep(line)
    for held in trailing:
        keep(held)
    out.extend(tail)

    if not replaced and block is not None:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        if out and out[-1].strip() != "":
            out.append("\n")
        out.append(block)
    return "".join(out)


def _toml_write(
    path: Path, name: str, entry: dict[str, Any] | None, key: str = ""
) -> None:
    key = _servers_key(path, key)
    before = _load(path) or {}
    text = path.read_text(encoding="utf-8") if path.exists() else ""

    after = _splice(
        text, name, _toml_table(name, entry, key) if entry is not None else None, key
    )

    # Independent check that the splice did what it claimed: re-parse and
    # compare against the document we meant to produce. A line-oriented cut
    # cannot see into a multi-line string, so this is what stops a config with
    # an exotic value from being silently mangled.
    #
    # ponytail: the check is exact but the *repair* is not attempted — an
    # unsplice-able config is refused and the user edits it by hand. Upgrade
    # path is a real TOML round-tripper (tomlkit), a dependency this project
    # does not otherwise need.
    servers = {k: v for k, v in before.get(key, {}).items() if k != name}
    if entry is not None:
        servers[name] = entry
    expected = {**before}
    if servers:
        expected[key] = servers
    else:
        expected.pop(key, None)

    try:
        got = tomllib.loads(after)
    except tomllib.TOMLDecodeError as exc:
        raise MalformedConfig(
            f"editing {path} would have produced invalid TOML ({exc}); "
            f"left untouched — edit the [{key}.{name}] table by hand"
        ) from exc
    if got != expected:
        raise MalformedConfig(
            f"could not edit {path} without disturbing the rest of the file; "
            f"left untouched — edit the [{key}.{name}] table by hand"
        )

    atomic_write_text(path, after)
