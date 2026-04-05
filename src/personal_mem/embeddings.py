"""API-based embeddings with SQLite cache.

Embeddings are computed via external API (OpenAI/Anthropic) and cached
in a separate SQLite database (.mem/embeddings.db). Never called during hooks.

Requires: pip install personal-mem[embeddings]  (httpx)
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path

from personal_mem.config import Config, load_config

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

    def compute_all(self) -> dict:
        """Compute embeddings for all notes in the index.

        Skips notes whose content_hash hasn't changed.
        Returns stats dict.
        """
        # Read all notes from the main index
        index_db = sqlite3.connect(str(self.config.index_db))
        index_db.row_factory = sqlite3.Row
        notes = index_db.execute(
            "SELECT id, title, body_text, content_hash FROM notes"
        ).fetchall()
        index_db.close()

        # Get existing cached hashes
        cached: dict[str, str] = {}
        for row in self.db.execute("SELECT note_id, content_hash FROM embeddings"):
            cached[row["note_id"]] = row["content_hash"]

        stats = {"computed": 0, "skipped": 0, "errors": 0}
        batch_texts: list[tuple[str, str, str]] = []  # (note_id, text, content_hash)

        for note in notes:
            if cached.get(note["id"]) == note["content_hash"]:
                stats["skipped"] += 1
                continue
            text = f"{note['title']}\n\n{note['body_text'] or ''}"
            batch_texts.append((note["id"], text, note["content_hash"]))

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
                        (note_id, chash, _pack_embedding(emb), self.config.embedding_model, now),
                    )
                    stats["computed"] += 1
                self.db.commit()
            except Exception as e:
                stats["errors"] += len(batch)
                print(f"Embedding API error: {e}")

        return stats

    def search(self, query: str, limit: int = 5) -> list[tuple[str, float]]:
        """Semantic search: embed query, then cosine similarity over cached embeddings.

        Returns list of (note_id, similarity_score) sorted by score descending.
        """
        query_emb = self._call_api([query])[0]

        results: list[tuple[str, float]] = []
        for row in self.db.execute("SELECT note_id, embedding FROM embeddings"):
            cached_emb = _unpack_embedding(row["embedding"])
            score = cosine_similarity(query_emb, cached_emb)
            results.append((row["note_id"], score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]

    def _call_api(self, texts: list[str]) -> list[list[float]]:
        """Call the embedding API. Requires httpx."""
        try:
            import httpx
        except ImportError:
            raise ImportError(
                "Embeddings require httpx. Install with: pip install personal-mem[embeddings]"
            )

        api_key = os.environ.get(self.config.embedding_api_key_env, "")
        if not api_key:
            raise ValueError(
                f"API key not found. Set {self.config.embedding_api_key_env} environment variable."
            )

        response = httpx.post(
            self.config.embedding_api_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.config.embedding_model,
                "input": texts,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()

        # OpenAI-compatible response format
        return [item["embedding"] for item in data["data"]]
