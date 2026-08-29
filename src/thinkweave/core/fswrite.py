"""Write-safety primitives for harness-owned and user-owned files (#191 AC5).

Every thinkweave writer that touches a harness config, user-scope config, or
docs file goes through here. (Vault-internal state writers — queues, session
buffers — keep their own idioms; their failure domain is thinkweave's, not a
user's hand-maintained file.) The doctrine, in one place:

*Ownership decides the strategy.* A file thinkweave generates outright is
regenerated whole (behind a sentinel/fingerprint gate — :func:`replace_between`
with ``on_missing="error"`` refuses to clobber an unmarked file). A file the
user or another tool also writes is never regenerated: our span is spliced in
and every byte outside it survives (the sentinel blocks here, the key-scoped
TOML/JSON splice in ``core.mcp_config``, the hook-entry merge in
``surfaces.hooks.install``).

*Prefer the harness's native CLI shape.* Where a harness ships its own
registration command (``HarnessProfile.mcp_via_cli`` — ``claude mcp add``,
``codex mcp add``), our writers reproduce its output byte-for-byte instead of
inventing a shape (pinned in ``tests/test_codex_install.py``).
ponytail: we splice rather than shell out to the CLI itself — one less child
process to sandbox and mock; the upgrade path, if a harness's CLI output ever
drifts from our writer, is to invoke ``mcp_via_cli`` directly.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, backup: bool = False) -> None:
    """Replace ``path``'s content via a unique tempfile + ``os.replace``.

    The original survives untouched until the atomic rename — that is the
    rollback guarantee, and the temp name is unique (``mkstemp`` beside the
    target, same filesystem) so two concurrent writers cannot interleave on
    one temp path; last rename wins whole. Durability boundary: atomic
    against process death, but nothing is fsynced — a machine crash inside
    the OS flush window can surface the previous content. A failed write
    removes its own temp file.

    The replacement inherits the original's permissions (Codex creates
    ``config.toml`` 0600 and it can carry env secrets; the umask default
    would quietly widen it). ``backup=True`` additionally keeps the previous
    file at ``<path>.bak`` — copied as bytes, so a restore returns exactly
    what was there, CRLF endings and non-UTF-8 content included.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if path.exists():
            if backup:
                shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
            os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def replace_between(
    text: str,
    start: str,
    end: str,
    block: str,
    *,
    on_missing: str = "error",
) -> str:
    """Replace the ``start``…``end`` sentinel span with ``block``, leaving
    every byte outside it alone.

    ``on_missing`` says what a document without both sentinels gets:
    ``"append"`` adds a fresh block at the end (the instructions-file nudge),
    ``"error"`` refuses (a generated file must never overwrite unmarked
    prose). ``block`` carries its own sentinels.
    """
    lo = text.find(start)
    hi = text.find(end, max(lo, 0))
    if lo == -1 or hi == -1:
        if on_missing != "append":
            raise ValueError(
                f"sentinel block ({start!r}…{end!r}) not found; refusing to "
                "overwrite a file that does not mark our span"
            )
        sep = "" if text == "" or text.endswith("\n") else "\n"
        return f"{text}{sep}\n{block}\n"
    return text[:lo] + block + text[hi + len(end) :]
