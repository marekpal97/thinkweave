"""Reading and writing one MCP-server registration, in whichever file format
the active harness keeps its config in.

:class:`~thinkweave.core.harness.HarnessProfile` says *where* the config lives
and deliberately says nothing about its format — that is this module's job.
Claude Code keeps JSON (``~/.claude.json``, servers under ``mcpServers``);
Codex keeps TOML (``$CODEX_HOME/config.toml``, servers under ``mcp_servers``).
The key name follows from the format rather than needing its own profile field:
each harness names the table the way its format's conventions do.

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
import os
import re
import stat
import tomllib
from pathlib import Path
from typing import Any

TOML_SERVERS_KEY = "mcp_servers"
JSON_SERVERS_KEY = "mcpServers"


class MalformedConfig(ValueError):
    """The config file exists but could not be parsed — the caller should
    refuse rather than overwrite a file it does not understand."""


def _is_toml(path: Path) -> bool:
    return path.suffix == ".toml"


def canonical(path: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """The entry as this format stores it.

    Callers build one entry shape and pass it through here so that what they
    later compare against :func:`read_entry` is like-for-like — otherwise every
    ``weave install`` on a TOML harness would report phantom drift.

    Codex infers the transport from the presence of ``command`` vs ``url``, has
    no ``type`` key, and ``codex exec --strict-config`` errors out on the
    unknown field. An empty ``env`` is dropped for the same reason ``codex mcp
    add`` omits it — it is noise, and its absence is what a re-read returns.
    """
    if not _is_toml(path):
        return entry
    return {k: v for k, v in entry.items() if k != "type" and v != {}}


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


def _servers_key(path: Path) -> str:
    return TOML_SERVERS_KEY if _is_toml(path) else JSON_SERVERS_KEY


def read_entry(path: Path, name: str) -> dict[str, Any] | None:
    """The named server's block, or None when the file or the entry is absent.

    Raises :class:`MalformedConfig` on an unparseable file.
    """
    doc = _load(path)
    if doc is None:
        return None
    return doc.get(_servers_key(path), {}).get(name)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def write_entry(path: Path, name: str, entry: dict[str, Any]) -> None:
    """Register (or converge) the named server, leaving the rest of the file
    alone. Creates the file when it does not exist."""
    if _is_toml(path):
        _toml_write(path, name, entry)
    else:
        _json_write(path, name, entry)


def remove_entry(path: Path, name: str) -> bool:
    """Drop the named server. Returns False when there was nothing to remove."""
    if read_entry(path, name) is None:
        return False
    if _is_toml(path):
        _toml_write(path, name, None)
    else:
        _json_write(path, name, None)
    return True


def _atomic_write(path: Path, text: str) -> None:
    """tempfile + os.replace, so an interrupted write cannot leave the user's
    harness config truncated.

    The replacement inherits the original's permissions: Codex creates
    ``config.toml`` 0600 and it can carry env secrets, so falling back to the
    umask default would quietly widen it.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
    os.replace(tmp, path)


def _json_write(path: Path, name: str, entry: dict[str, Any] | None) -> None:
    doc = _load(path) or {}
    servers = doc.setdefault(JSON_SERVERS_KEY, {})
    if entry is None:
        servers.pop(name, None)
    else:
        servers[name] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(doc, indent=2) + "\n")


# ---------------------------------------------------------------------------
# the TOML half
# ---------------------------------------------------------------------------

# A table header on its own line. TOML requires headers to start a line, so
# this finds every one of them — including the array-of-tables `[[…]]` form,
# which must terminate our block just as a plain header does.
_HEADER = re.compile(r"^[ \t]*\[\[?(?P<key>[^\]]*)\]\]?[ \t]*(?:#.*)?$")


def _owns(header_key: str, name: str) -> bool:
    """True for our table and any of its sub-tables (``…thinkweave.env``)."""
    parts = [p.strip().strip("\"'") for p in header_key.split(".")]
    return parts[:2] == [TOML_SERVERS_KEY, name]


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


def _toml_table(name: str, entry: dict[str, Any]) -> str:
    """Our table, rendered as one contiguous block (``env`` inlined, so the
    whole entry is a single splice unit)."""
    lines = [f"[{TOML_SERVERS_KEY}.{name}]"]
    lines += [f"{k} = {_toml_scalar(v)}" for k, v in entry.items()]
    return "\n".join(lines) + "\n"


def _splice(text: str, name: str, block: str | None) -> str:
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
            was_ours, ours = ours, _owns(header.group("key"), name)
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


def _toml_write(path: Path, name: str, entry: dict[str, Any] | None) -> None:
    before = _load(path) or {}
    text = path.read_text(encoding="utf-8") if path.exists() else ""

    after = _splice(text, name, _toml_table(name, entry) if entry is not None else None)

    # Independent check that the splice did what it claimed: re-parse and
    # compare against the document we meant to produce. A line-oriented cut
    # cannot see into a multi-line string, so this is what stops a config with
    # an exotic value from being silently mangled.
    #
    # ponytail: the check is exact but the *repair* is not attempted — an
    # unsplice-able config is refused and the user edits it by hand. Upgrade
    # path is a real TOML round-tripper (tomlkit), a dependency this project
    # does not otherwise need.
    servers = {k: v for k, v in before.get(TOML_SERVERS_KEY, {}).items() if k != name}
    if entry is not None:
        servers[name] = entry
    expected = {**before}
    if servers:
        expected[TOML_SERVERS_KEY] = servers
    else:
        expected.pop(TOML_SERVERS_KEY, None)

    try:
        got = tomllib.loads(after)
    except tomllib.TOMLDecodeError as exc:
        raise MalformedConfig(
            f"editing {path} would have produced invalid TOML ({exc}); "
            f"left untouched — edit the [{TOML_SERVERS_KEY}.{name}] table by hand"
        ) from exc
    if got != expected:
        raise MalformedConfig(
            f"could not edit {path} without disturbing the rest of the file; "
            f"left untouched — edit the [{TOML_SERVERS_KEY}.{name}] table by hand"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, after)
