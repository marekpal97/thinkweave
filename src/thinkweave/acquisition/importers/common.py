"""Shared helpers for the bulk importers (chatgpt, claude_history, messenger,
transcript).

Two concerns live here so the importers don't each hand-roll them:

1. :class:`ImportManifest` — the idempotency ledger. Every importer keeps a
   JSON file under ``weave_dir`` mapping already-imported source keys to the
   vault note id/filename they produced, so re-running an import skips what's
   already in the vault. The three importers previously defined structurally
   identical ``_load_manifest``/``_save_manifest`` pairs; the only real
   difference was the map's field name (``imported_ids`` for chatgpt /
   claude_history, ``imported_urls`` for messenger), which is now a
   constructor argument.

2. :func:`index_imported_notes` — the single indexing policy (see its
   docstring for the decision + rationale).
"""

from __future__ import annotations

import json
from pathlib import Path

from thinkweave.core.config import Config


class ImportManifest:
    """Idempotency ledger persisted as JSON under ``weave_dir``.

    The ledger is a ``{"version": 1, <id_field>: {key: value}}`` dict where
    ``<id_field>`` is the per-importer map name (``imported_ids`` by default;
    messenger uses ``imported_urls``). Importers test membership before
    creating a note and record the produced note id/filename after.
    """

    def __init__(self, path: Path, id_field: str = "imported_ids", data: dict | None = None):
        self.path = path
        self.id_field = id_field
        self.data: dict = data if data is not None else {"version": 1, id_field: {}}
        # Guarantee the id map exists even if a hand-edited manifest dropped it.
        self.data.setdefault("version", 1)
        self.data.setdefault(id_field, {})

    @classmethod
    def load(
        cls, weave_dir: Path, filename: str, id_field: str = "imported_ids"
    ) -> ImportManifest:
        """Load the manifest at ``weave_dir/filename`` (empty if absent)."""
        path = weave_dir / filename
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = {"version": 1, id_field: {}}
        return cls(path, id_field=id_field, data=data)

    @property
    def ids(self) -> dict:
        """The mutable ``{key: value}`` map of already-imported items."""
        return self.data.setdefault(self.id_field, {})

    def is_imported(self, key: str) -> bool:
        return key in self.ids

    def mark(self, key: str, value: str) -> None:
        """Record ``key`` as imported, pointing at the produced ``value``."""
        self.ids[key] = value

    def set_meta(self, **kwargs: object) -> None:
        """Attach top-level metadata (``completed_at``, ``source_file``, …)."""
        self.data.update(kwargs)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")


def index_imported_notes(config: Config, paths: list[Path]) -> dict:
    """Index the notes an import just wrote — the one importer indexing policy.

    **Decision: end-of-run bulk indexing over the exact set of written paths,
    via** :meth:`Indexer.index_paths`, **for every importer.**

    Why this over the two prior divergent behaviours:

    - *Per-file* ``index_file`` right after each ``create_note`` (what
      chatgpt / messenger / transcript did) constructs a fresh ``Indexer``
      (new SQLite connection + schema init + migrations) *and* calls
      ``_rebuild_fts()`` — a full rebuild of the FTS table — on **every**
      note. That is O(N) FTS rebuilds for an N-note import; claude_history
      imports can be thousands of notes, making this quadratic-feeling in
      practice.
    - *Full* ``rebuild(full=True)`` at end (what claude_history did) tears
      down and recomputes every edge and re-reads the *entire* vault, not
      just the imported notes — wasteful when appending into an established
      vault.

    ``index_paths`` is the targeted middle ground: it skips the vault-wide
    ``rglob``, touches only the supplied paths, and rebuilds edges
    (incrementally) and FTS exactly **once** at the end. Cost is
    O(imported notes), independent of vault size.

    Trade-off accepted: notes are not searchable mid-run. Imports are
    batch/offline CLI operations with no concurrent reader, so incremental
    mid-run searchability has no consumer — the per-file FTS-rebuild cost it
    would buy is pure waste. Sidecar projections that only a *full* rebuild
    refreshes (context_served, prompts, co_served) are irrelevant to imported
    historical content (which has no retrieval_log/events sidecars) and are
    healed at cadence by the nightly ``/dream`` full rebuild / ``weave index
    --full``.

    Returns the indexer stats dict (``indexed``/``skipped``/``removed``/
    ``edges``). A no-op when ``paths`` is empty (e.g. a fully idempotent
    re-run).
    """
    # Imported lazily so importing this module stays cheap for callers that
    # only need ImportManifest.
    from thinkweave.core.indexer import Indexer

    if not paths:
        return {"indexed": 0, "skipped": 0, "removed": 0, "edges": 0}

    idx = Indexer(config=config)
    try:
        return idx.index_paths(paths)
    finally:
        idx.close()
