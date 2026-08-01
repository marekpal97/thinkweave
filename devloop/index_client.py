"""The package's single seam into the derived SQLite index.

Stdlib only, strictly read-only, never imports ``thinkweave`` (the rail may
run where the package is not installed). #100 completes the seam by moving
the remaining SQL (``trajectory.prime``'s two queries) in here.
"""

from __future__ import annotations

import os
import sqlite3
import tomllib
from pathlib import Path

# The seam's error type, re-exported so callers can degrade on an index
# problem without importing sqlite3 themselves (see the importer-allowlist
# test in tests/test_devloop_boundaries.py).
Error = sqlite3.Error


def open_ro(db_path: str) -> sqlite3.Connection:
    """Open the derived index strictly read-only (never mutate derived state)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _read_weave_dir_override(vault_root: Path) -> Path | None:
    """Honor a top-level ``weave_dir`` in the vault's config.toml.

    PR #10 relocates derived state (index.db, embeddings.db, buffer/) off the
    vault path — on 9P-mounted vaults the live index is ``<weave_dir>/index.db``,
    NOT ``<vault>/.weave/index.db``. Mirror ``core.config``'s resolution: ``~``
    expands, a relative value anchors at ``vault_root``, absolute passes
    through. Read ``config/config.toml`` first, then the legacy
    ``.weave/config.toml``. Malformed/unreadable config or an absent key →
    ``None`` (fall back to the legacy layout; never crash).
    """
    for rel in ("config/config.toml", ".weave/config.toml"):
        path = vault_root / rel
        if not path.exists():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        value = data.get("weave_dir")
        if isinstance(value, str) and value.strip():
            resolved = Path(value).expanduser()
            return resolved if resolved.is_absolute() else vault_root / resolved
    return None


def resolve_db_path(db: str | None, vault: str | None) -> str | None:
    """Resolve the read-only index db path without importing thinkweave.

    ``--db`` wins; else derive from ``--vault``: ``<weave_dir>/index.db`` when
    the vault's config.toml overrides ``weave_dir`` (PR #10), otherwise the
    legacy ``<vault>/.weave/index.db``; else ``THINKWEAVE_INDEX_DB``. Returns
    None when nothing resolves — the prime then serves an empty (unprimed)
    block rather than guessing a path (never touch an ambient real vault).
    """
    if db:
        return db
    if vault:
        vault_root = Path(vault)
        weave_dir = _read_weave_dir_override(vault_root) or (vault_root / ".weave")
        return str(weave_dir / "index.db")
    return os.environ.get("THINKWEAVE_INDEX_DB") or None
