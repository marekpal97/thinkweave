"""``weave policy inventory`` — the knob inventory, rendered from the Config
field metadata (dec-db6baaab: the registry is derived, not hand-written).

- ``--toml`` prints the rsi-rail repo's ``thinkweave/config.toml`` template:
  every rail-eligible address as a commented line at its default, grouped by
  TOML table, with the events that read it. The rail file is regenerated
  from this output, never hand-edited.
- ``--json`` prints every knob, rail-eligible or not, as one JSON list.
- ``--check PATH`` exits 0 when the file at PATH is the rendering, its live
  (uncommented) lines aside, else prints the first differing line and
  exits 1.

Rail-eligible means one of the field's ``read_at`` events is on the serving
chain (``core.config.SERVING_CHAIN``); the same derivation is the allowlist
``load_config`` enforces on the rail file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from thinkweave.core.config import SERVING_CHAIN, READ_AT, KnobSpec, knob_specs, resolve_ref

_HEADER = """\
# rsi-rail: thinkweave's serving-chain knobs. Rendered by
# `weave policy inventory --toml` from the knob metadata on thinkweave's
# Config fields (dec-db6baaab); regenerate it, never hand-edit a commented
# line. Read by load_config as the tier after the vault's config.toml
# (thinkweave#359): a live key here wins over the vault file; a commented key
# keeps the vault value or the code default.
#
# Sparse by rule (dec-dc79eeeb): every address is listed as a commented line
# carrying its default and the events that read it. Uncomment a key, and its
# table header, only when its value differs from that default, so a live line
# is always a deliberate tweak and its commit carries a
# `Decision: dec-xxxxxxxx` trailer.
#
# Which keys belong here is derived: a key is listed iff one of the events
# that read it is on the serving chain:
#   {chain}.
# Any other key fails load_config with a ValueError naming it; knobs read
# only at {rest} time stay in the vault's config.toml."""

# Rows wider than this are not padded to align their notes.
_ALIGN_MAX = 48


def _toml_value(value) -> str:
    if value is None:
        return "<unset>"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (tuple, list)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"no TOML rendering for {type(value).__name__}")


def _row(spec: KnobSpec, name: str | None = None) -> tuple[str, str]:
    """``(value, note)`` for one address. The value is the default the reader
    applies when the key is unset: the field's own, else the reader's
    declaration ``default_ref`` names. A knob whose unset state is not a
    value (``unset`` in its metadata) renders ``<unset>`` and says what
    unset means."""
    note = "read at: " + ", ".join(spec.read_at)
    default = spec.default
    ref = spec.metadata.get("default_ref")
    if default is None and ref:
        default = resolve_ref(ref)
        if name is not None:
            default = default.get(name)
    unset = spec.metadata.get("unset")
    if unset:
        return "<unset>", note + "; unset = " + unset.format(default=default)
    return _toml_value(default), note


def _rail_rows() -> dict[str, list[tuple[str, str, str]]]:
    """``{table: [(key, value, note)]}`` in field order, keyed tables
    expanded over their allowed names."""
    tables: dict[str, list[tuple[str, str, str]]] = {}
    for spec in knob_specs():
        if not spec.rail or spec.address is None:
            continue
        table, _, key = spec.address.rpartition(".")
        if spec.keyed is None:
            tables.setdefault(table, []).append((key, *_row(spec)))
            continue
        placeholder = f"<{spec.keyed.name}>"
        for name in resolve_ref(spec.keyed.metadata["choices_ref"]):
            tables.setdefault(table.replace(placeholder, name), []).append((key, *_row(spec, name)))
    return tables


def render_toml() -> str:
    """The commented sparse template; deterministic (field order)."""
    chain = [e for e in READ_AT if e in SERVING_CHAIN]
    rest = [e for e in READ_AT if e not in SERVING_CHAIN]
    parts = [_HEADER.format(chain=", ".join(chain), rest=", ".join(rest))]
    for table, rows in _rail_rows().items():
        widths = [len(f"{k} = {v}") for k, v, _ in rows]
        width = max([w for w in widths if w <= _ALIGN_MAX], default=0)
        lines = [f"# [{table}]"]
        for key, value, note in rows:
            lines.append(f"# {f'{key} = {value}':<{width}}   # {note}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts) + "\n"


def _jsonable(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return list(value)
    return value


def render_json() -> str:
    rows = [
        {
            "field": f"{spec.owner.__name__}.{spec.name}",
            "address": spec.address,
            "read_at": list(spec.read_at),
            "rail": spec.rail,
            "default": _jsonable(spec.default),
        }
        for spec in knob_specs()
    ]
    return json.dumps(rows, indent=2)


_HEADER_LINE = re.compile(r"^#?\s*\[([^\]]+)\]\s*$")
_KEY_LINE = re.compile(r"^#?\s*([A-Za-z0-9_]+)\s*=")


def check(text: str, rendered: str | None = None) -> str | None:
    """``None`` when ``text`` is the rendering apart from its live lines, else
    the first differing line. A live key or table header stands in for its
    commented twin, so the twin is not expected."""
    live: set[tuple[str, str]] = set()
    kept: list[str] = []
    table = ""
    for line in text.splitlines():
        stripped = line.strip()
        header = _HEADER_LINE.match(stripped)
        if header:
            table = header.group(1).strip()
        if stripped and not stripped.startswith("#"):
            key = _KEY_LINE.match(stripped)
            live.add(("[", table) if header else (table, key.group(1) if key else stripped))
            continue
        kept.append(line)

    expected: list[str] = []
    table = ""
    for line in (rendered if rendered is not None else render_toml()).splitlines():
        header = _HEADER_LINE.match(line) if line.startswith("# [") else None
        if header:
            table = header.group(1).strip()
            if ("[", table) in live:
                continue
        key = _KEY_LINE.match(line) if line.startswith("# ") and not header else None
        if key and (table, key.group(1)) in live:
            continue
        expected.append(line)

    for i, (want, got) in enumerate(zip(expected, kept), start=1):
        if want != got:
            return f"line {i}: expected {want!r}, found {got!r}"
    if len(expected) != len(kept):
        i = min(len(expected), len(kept)) + 1
        if len(expected) > len(kept):
            return f"line {i}: expected {expected[i - 1]!r}, found end of file"
        return f"line {i}: expected end of file, found {kept[i - 1]!r}"
    return None


def cmd_policy(args: argparse.Namespace) -> None:
    """Dispatch ``weave policy inventory``."""
    if getattr(args, "policy_action", None) != "inventory":
        print("usage: weave policy inventory {--toml | --json | --check PATH}", file=sys.stderr)
        sys.exit(2)
    if args.check:
        diff = check(Path(args.check).read_text(encoding="utf-8"))
        if diff:
            print(diff)
            sys.exit(1)
        return
    print(render_json() if args.json else render_toml(), end="" if args.toml else "\n")
