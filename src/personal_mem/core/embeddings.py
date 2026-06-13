"""API-based embeddings with SQLite cache.

Embeddings are computed via the OpenAI API and cached in a separate
SQLite database (.mem/embeddings.db). Never called during hooks.

Requires: pip install personal-mem[embeddings]  (httpx)
"""

from __future__ import annotations

import math
import sqlite3
import struct
from datetime import datetime, timezone

from personal_mem.core.config import Config, load_config

EMBEDDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    note_id      TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    embedding    BLOB NOT NULL,
    model        TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""


def _pack_embedding(vec: list[float]) -> bytes:
    """Pack a float list into bytes for SQLite BLOB storage."""
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_embedding(blob: bytes) -> list[float]:
    """Unpack bytes back to float list."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity using stdlib math only."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class EmbeddingSearch:
    """Compute, cache, and search embeddings via external API."""

    def __init__(self, config: Config | None = None):
        self.config = config or load_config()
        self._db: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self.config.mem_dir.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.config.embeddings_db))
            self._db.row_factory = sqlite3.Row
            self._db.executescript(EMBEDDINGS_SCHEMA)
        return self._db

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    def clear(self) -> int:
        """Delete every cached embedding; return the row count removed.

        Backs ``mem index --embed --reset`` — the escape hatch for a
        provider/model switch, where cached vectors live in the wrong
        space (and usually the wrong dimensionality). Schema is kept;
        only rows are dropped, so the following ``compute_all`` re-embeds
        the whole vault from scratch.
        """
        removed = int(self.db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])
        self.db.execute("DELETE FROM embeddings")
        self.db.commit()
        return removed

    def latest_embedded_at(self) -> str | None:
        """Return the ISO timestamp of the most-recently-cached embedding.

        Returns ``None`` when the embeddings table is empty. Used by
        ``compute_all(only_new=True)`` and ``mem doctor`` to drive the
        keep-warm story — a cheap signal that the embed cron hasn't run.
        """
        row = self.db.execute("SELECT MAX(created_at) FROM embeddings").fetchone()
        if row is None:
            return None
        # sqlite returns a Row; index by 0 to get the scalar.
        return row[0]

    def compute_all(self, *, only_new: bool = False, since: str = "") -> dict:
        """Compute embeddings for notes in the index.

        Skips notes whose content_hash hasn't changed (so re-runs are
        idempotent even over the full set). The ``only_new`` /  ``since``
        knobs let cron drive a cheap nightly refresh that doesn't pull
        every note's body into Python.

        Args:
            only_new: When True, restrict the candidate set to notes
                whose ``updated_at`` is strictly greater than the most
                recent ``embeddings.created_at`` (i.e. the
                "everything embedded before the last embed run is
                trusted"  contract). On an empty embeddings table this
                degrades to a full scan.
            since: ISO timestamp; alternative cutoff for the
                ``updated_at`` filter. Overrides ``only_new``'s
                derived cutoff when both are passed. Pass to backfill
                a known window (e.g. ``--since 2026-05-01``).

        Returns stats dict ``{computed, skipped, errors, scanned, cutoff}``.
        ``scanned`` is the number of rows pulled from the index;
        ``cutoff`` is the ISO timestamp actually applied (or "" when
        none).
        """
        # Decide the cutoff. Explicit `since` wins; otherwise derive
        # from the embeddings table when only_new is set.
        cutoff = since or ""
        if not cutoff and only_new:
            cutoff = self.latest_embedded_at() or ""

        # Read candidate notes from the main index, restricted by cutoff
        # when one is set.
        index_db = sqlite3.connect(str(self.config.index_db))
        index_db.row_factory = sqlite3.Row
        if cutoff:
            notes = index_db.execute(
                "SELECT id, title, body_text, content_hash FROM notes "
                "WHERE updated_at > ?",
                (cutoff,),
            ).fetchall()
        else:
            notes = index_db.execute(
                "SELECT id, title, body_text, content_hash FROM notes"
            ).fetchall()
        index_db.close()

        # Get existing cached hashes
        cached: dict[str, str] = {}
        for row in self.db.execute("SELECT note_id, content_hash FROM embeddings"):
            cached[row["note_id"]] = row["content_hash"]

        stats = {
            "computed": 0,
            "skipped": 0,
            "errors": 0,
            "scanned": len(notes),
            "cutoff": cutoff,
        }
        batch_texts: list[tuple[str, str, str]] = []  # (note_id, text, content_hash)

        for note in notes:
            if cached.get(note["id"]) == note["content_hash"]:
                stats["skipped"] += 1
                continue
            text = f"{note['title']}\n\n{note['body_text'] or ''}"
            batch_texts.append((note["id"], text, note["content_hash"]))

        # Stamp the *provider's* actual model into each row — not
        # ``config.embedding_model`` (a legacy default that can disagree
        # with ``api.yaml::embeddings.model``). ``search()`` filters the
        # cache on this column, so it must name the space the vectors
        # actually live in.
        model_name = self._provider().model

        # Compute in batches
        batch_size = 20
        for i in range(0, len(batch_texts), batch_size):
            batch = batch_texts[i : i + batch_size]
            texts = [t[1] for t in batch]
            try:
                embeddings = self._call_api(texts)
                now = datetime.now(timezone.utc).isoformat()
                for (note_id, _, chash), emb in zip(batch, embeddings):
                    self.db.execute(
                        """INSERT OR REPLACE INTO embeddings
                           (note_id, content_hash, embedding, model, created_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (note_id, chash, _pack_embedding(emb), model_name, now),
                    )
                    stats["computed"] += 1
                self.db.commit()
            except Exception as e:
                stats["errors"] += len(batch)
                print(f"Embedding API error: {e}")

        return stats

    def search(
        self,
        query: str,
        limit: int = 5,
        *,
        project: str = "",
        note_type: str | list[str] = "",
    ) -> list[tuple[str, float]]:
        """Semantic search: embed query, then cosine similarity over cached embeddings.

        Args:
            query: Natural-language query to embed.
            limit: Max results.
            project: Optional project filter — only notes from this project.
            note_type: Optional type filter — string or list of types.

        Returns list of (note_id, similarity_score) sorted by score descending.
        Filtered by joining against the main index; requires ``index.db`` to exist.
        """
        query_emb = self._call_api([query])[0]
        # The query was embedded by the configured provider, so it lives
        # in that model's vector space. Only compare against cache rows
        # from the *same* model — cosine across models (even at equal
        # dim, e.g. ada-002 vs 3-small) is meaningless, and across dims
        # ``cosine_similarity``'s ``zip`` would silently truncate. This
        # makes a mixed cache (mid-migration, or pre-``--reset``) degrade
        # to "fewer results" rather than "plausible-but-wrong scores".
        query_model = self._provider().model
        query_dim = len(query_emb)

        # Build the set of allowed note_ids if filters are requested
        allowed: set[str] | None = None
        if project or note_type:
            import sqlite3

            type_list: list[str]
            if isinstance(note_type, str):
                type_list = [note_type] if note_type else []
            else:
                type_list = [t for t in note_type if t]

            conds: list[str] = []
            params: list = []
            if project:
                conds.append("project = ?")
                params.append(project)
            if type_list:
                placeholders = ",".join("?" for _ in type_list)
                conds.append(f"type IN ({placeholders})")
                params.extend(type_list)
            where = " AND ".join(conds)

            try:
                idx_db = sqlite3.connect(str(self.config.index_db))
                allowed = {
                    row[0]
                    for row in idx_db.execute(f"SELECT id FROM notes WHERE {where}", params)
                }
                idx_db.close()
            except sqlite3.Error:
                allowed = set()  # filter couldn't be applied — return empty

            if not allowed:
                return []

        results: list[tuple[str, float]] = []
        for row in self.db.execute(
            "SELECT note_id, embedding FROM embeddings WHERE model = ?",
            (query_model,),
        ):
            if allowed is not None and row["note_id"] not in allowed:
                continue
            cached_emb = _unpack_embedding(row["embedding"])
            if len(cached_emb) != query_dim:  # belt-and-suspenders vs a mislabelled row
                continue
            score = cosine_similarity(query_emb, cached_emb)
            results.append((row["note_id"], score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]

    def _call_api(self, texts: list[str]) -> list[list[float]]:
        """Delegate to the configured :class:`EmbeddingProvider`.

        Provider selection reads ``vault/config/api.yaml::embeddings``
        via :func:`personal_mem.core.embedding_provider.build_from_vault`.
        """
        if not texts:
            return []
        provider = self._provider()
        return provider.embed(texts)

    def _provider(self):
        """Cached :class:`EmbeddingProvider` for this ``EmbeddingSearch``.

        Reading the config + instantiating the backend on every call
        would re-import the SDK and re-warm the SentenceTransformer
        model — neither cheap. Memoize per-instance.
        """
        cached = getattr(self, "_cached_provider", None)
        if cached is not None:
            return cached
        from personal_mem.core.embedding_provider import build_from_vault
        cached = build_from_vault(self.config.vault_root)
        self._cached_provider = cached  # type: ignore[attr-defined]
        return cached
