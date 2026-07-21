"""Vertical-slice regression tests for prompt-time retrieval (R2).

Guards the failure class found 2026-07-05: R2 shipped 2026-06-13 and never
injected a single note in production — every layer was individually
"working" (and unit-tested), but the composed path silently produced nothing,
and no telemetry existed to notice. The defects were only visible end-to-end:

- the similarity arm blew its wall-clock deadline on every real prompt
  (pure-Python cosine scan + slow-storage I/O), was abandoned, and the
  designed FTS fallback AND-matched full prose so it always returned 0 hits;
- misses were silent and un-counted, so the ~4s cost was re-paid every turn;
- the session-note fallback rglob'd the entire vault on every prompt of a
  fresh session (16s over WSL2 9P).

These tests therefore run the REAL composed path — ``VaultManager`` →
``Indexer`` → ``EmbeddingSearch`` over a real SQLite embeddings.db →
``Search.similar`` in the real deadlined daemon thread → the real
``_handle_user_prompt_submit`` handler → stdout emit + buffer write-back →
indexer ``context_served`` projection. The ONLY fake is the embedding
provider (the network boundary): a deterministic bag-of-words embedder, so
"domain prompts score above the cosine floor, unrelated notes don't" holds
without an API key.

Unit-level behavior (caps, gates, dedup arithmetic) lives in
``test_prompt_time_retrieval.py``; this file is deliberately about the seams
between the layers.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import struct
import time
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager
from thinkweave.operations.prompt_time_retrieval import (
    PROMPT_TIME_MISS,
    PROMPT_TIME_TOOL,
    _consecutive_trailing_misses,
    _served_ids_and_stats,
)
from thinkweave.surfaces.hooks import handler as hooks_handler

# A prompt shaped like the ones R2 was built for (and structurally like the
# prose that made the old FTS arm a guaranteed no-op): natural language,
# no exact keyword phrase, on-topic for the seeded notes below.
PROMPT = (
    "I'd like to scan the codebase for deepening opportunities around deep "
    "modules with clean interfaces and vertical slice testing"
)

ON_TOPIC = [
    (
        "Deep modules need narrow interfaces",
        "A deep module hides complexity behind a clean narrow interface. "
        "Deepening opportunities come from moving logic below the interface "
        "so vertical slice testing can exercise the whole module.",
    ),
    (
        "Vertical slice testing strategy",
        "Vertical slice testing drives a module through its public interface "
        "end to end. Deep modules with clean interfaces make slice tests "
        "cheap; shallow modules force mock-heavy unit testing.",
    ),
]

OFF_TOPIC = [
    (
        "Tomato watering schedule",
        "Water the tomato plants every second morning and check the garden "
        "soil for dryness before the summer heat arrives.",
    ),
]


class _BagOfWordsProvider:
    """Deterministic offline embedder: hashed bag-of-words, L2-normalized.

    Texts sharing vocabulary get high cosine; disjoint texts get ~0. That
    reproduces the property the real provider gives R2's cosine floor
    (domain prompts ≥ floor, unrelated notes below it) with zero network.
    ``embed_delay`` simulates a slow endpoint for the deadline-miss tests.
    ``calls`` counts embed invocations so tests can assert the adaptive
    skip stops paying for new attempts.
    """

    model = "fake-bow-v1"
    _DIM = 64

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.embed_delay = 0.0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.embed_delay:
            time.sleep(self.embed_delay)
        out = []
        for text in texts:
            vec = [0.0] * self._DIM
            for word in re.findall(r"[a-z]+", text.lower()):
                slot = int(hashlib.md5(word.encode()).hexdigest(), 16)
                vec[slot % self._DIM] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


@pytest.fixture()
def r2_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real vault with indexed + embedded notes and a faked provider.

    Everything downstream of the provider is the production code path.
    Returns ``(cfg, provider)``.
    """
    from thinkweave.core import config as config_mod
    from thinkweave.core.embeddings import EmbeddingSearch

    cfg = Config(vault_root=tmp_path / "vault")
    provider = _BagOfWordsProvider()
    monkeypatch.setattr(EmbeddingSearch, "_provider", lambda self: provider)
    # The handler (and its _log_info/_log_error helpers) resolve config via
    # load_config(); route every resolution to this test vault.
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)

    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    for title, body in ON_TOPIC + OFF_TOPIC:
        vm.create_note(NoteType.NOTE, title, body=body, project="t")
    idx = Indexer(config=cfg)
    idx.rebuild(full=True)
    idx.close()
    es = EmbeddingSearch(config=cfg)
    stats = es.compute_all()
    es.close()
    assert stats["computed"] == len(ON_TOPIC + OFF_TOPIC)
    return cfg, provider


def _submit(session_id: str, capsys, prompt: str = PROMPT) -> dict:
    """Drive the real UserPromptSubmit handler; return its parsed stdout."""
    hooks_handler._handle_user_prompt_submit(
        {"session_id": session_id, "prompt": prompt, "cwd": "/tmp/x"}
    )
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


def _buffer_events(cfg: Config, session_id: str) -> list[dict]:
    buf = cfg.weave_dir / "buffer" / f"{session_id}.jsonl"
    if not buf.exists():
        return []
    return [
        json.loads(line)
        for line in buf.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _note_ids_by_title(cfg: Config) -> dict[str, str]:
    with sqlite3.connect(str(cfg.index_db)) as db:
        return {t: i for i, t in db.execute("SELECT id, title FROM notes")}


def _archive_and_project(cfg: Config, cc_session_id: str) -> str:
    """Run the real Stop-side tail: archive the live buffer into the session
    folder, reindex, and return the session NOTE id the projection keys on."""
    from thinkweave.core.buffer import archive_buffer

    vm = VaultManager(config=cfg)
    session_path = hooks_handler._find_session_note(vm, cc_session_id)
    assert session_path is not None, "prompt hook must have created a session"
    archive_buffer(cfg.weave_dir, cc_session_id, session_path.parent)
    idx = Indexer(config=cfg)
    idx.rebuild(full=True)
    idx.close()
    with sqlite3.connect(str(cfg.index_db)) as db:
        row = db.execute(
            "SELECT id FROM notes WHERE type='session' AND path LIKE ?",
            (f"%{session_path.parent.name}%",),
        ).fetchone()
    assert row is not None
    return row[0]


class TestHealthyPathEndToEnd:
    """The 'shipped but unwired' guard: a real prose prompt must actually
    inject — through the daemon thread, the cosine floor, the render, the
    stdout emit, the buffer write-back, and the indexer projection."""

    def test_prose_prompt_injects_relevant_notes(self, r2_vault, capsys):
        cfg, provider = r2_vault
        result = _submit("ses-vert-1", capsys)

        ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "Possibly relevant" in ctx, (
            "R2 produced no injection for a healthy on-topic prompt — this "
            "is exactly the silent-never-fires regression this file guards"
        )
        ids = _note_ids_by_title(cfg)
        for title, _ in ON_TOPIC:
            assert ids[title] in ctx
        for title, _ in OFF_TOPIC:
            assert ids[title] not in ctx, "cosine floor must drop off-topic"

        # Write-back: a retrieval event tagged with the R2 sentinel …
        fired = [
            e
            for e in _buffer_events(cfg, "ses-vert-1")
            if e.get("tool") == PROMPT_TIME_TOOL
        ]
        assert len(fired) == 1
        assert set(fired[0]["returned_ids"]) == {ids[t] for t, _ in ON_TOPIC}

        # … which, after the real Stop-side archive, the indexer projects to
        # context_served as 'prompttime'. This is the assertion that was
        # false in production for three weeks: the ledger stayed empty.
        note_sess_id = _archive_and_project(cfg, "ses-vert-1")
        with sqlite3.connect(str(cfg.index_db)) as db:
            rows = db.execute(
                "SELECT note_id, source FROM context_served "
                "WHERE session_id = ?",
                (note_sess_id,),
            ).fetchall()
        assert {(ids[t], "prompttime") for t, _ in ON_TOPIC} <= set(rows)

    def test_second_turn_dedups_served_ids(self, r2_vault, capsys):
        cfg, provider = r2_vault
        first = _submit("ses-vert-2", capsys)
        assert "hookSpecificOutput" in first
        second = _submit("ses-vert-2", capsys)
        assert "hookSpecificOutput" not in second, (
            "same ids re-served on the next turn — buffer dedup broken"
        )


class TestDeadlineMissTelemetryAndSkip:
    """The 'silent 100% failure' guard: a deadline miss must be recorded,
    must never count as a firing, and after `deadline_miss_limit`
    consecutive misses R2 must stop paying the embedding cost."""

    @pytest.fixture()
    def slow_vault(self, r2_vault):
        cfg, provider = r2_vault
        cfg.retrieval_prompt_time.embed_deadline_seconds = 0.05
        provider.embed_delay = 0.5
        return cfg, provider

    def test_miss_writes_telemetry_not_firing(self, slow_vault, capsys):
        cfg, provider = slow_vault
        result = _submit("ses-miss-1", capsys)
        assert "hookSpecificOutput" not in result

        events = _buffer_events(cfg, "ses-miss-1")
        misses = [e for e in events if e.get("type") == PROMPT_TIME_MISS]
        assert len(misses) == 1, "deadline miss must not be silent"

        _, firings, _ = _served_ids_and_stats(cfg, "ses-miss-1")
        assert firings == 0, "a miss must never count as a firing"

        log = (cfg.weave_dir / "hooks.log").read_text(encoding="utf-8")
        assert "deadline miss" in log

    def test_adaptive_skip_stops_paying_after_limit(self, slow_vault, capsys):
        cfg, provider = slow_vault
        limit = cfg.retrieval_prompt_time.deadline_miss_limit
        for _ in range(limit):
            _submit("ses-miss-2", capsys)
        assert _consecutive_trailing_misses(cfg, "ses-miss-2") == limit

        calls_before = len(provider.calls)
        result = _submit("ses-miss-2", capsys)
        assert "hookSpecificOutput" not in result
        assert len(provider.calls) == calls_before, (
            "adaptive skip must not attempt (and pay for) a new embedding"
        )
        # The skip itself is not a miss — the streak must not keep growing.
        assert _consecutive_trailing_misses(cfg, "ses-miss-2") == limit

    def test_success_resets_the_streak(self, slow_vault, capsys):
        cfg, provider = slow_vault
        for _ in range(cfg.retrieval_prompt_time.deadline_miss_limit - 1):
            _submit("ses-miss-3", capsys)
        # Endpoint recovers below the limit → next attempt runs and fires.
        provider.embed_delay = 0.0
        result = _submit("ses-miss-3", capsys)
        assert "hookSpecificOutput" in result
        assert _consecutive_trailing_misses(cfg, "ses-miss-3") == 0

    def test_miss_events_invisible_to_context_served(self, slow_vault, capsys):
        cfg, provider = slow_vault
        _submit("ses-miss-4", capsys)
        note_sess_id = _archive_and_project(cfg, "ses-miss-4")
        with sqlite3.connect(str(cfg.index_db)) as db:
            n = db.execute(
                "SELECT COUNT(*) FROM context_served WHERE session_id = ?",
                (note_sess_id,),
            ).fetchone()[0]
        assert n == 0, "miss telemetry leaked into the RLVR serving ledger"


class _FixedVectorProvider:
    """Provider stub returning one fixed vector; model matches seeded rows."""

    model = "fake-bow-v1"

    def __init__(self, vec: list[float]) -> None:
        self._vec = vec

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self._vec) for _ in texts]


class TestScanStaysVectorized:
    """The 'linear-CPU creep' guard: the pure-Python cosine loop cost ~1s at
    7k vectors and grew with the vault until it silently crossed the
    deadline. With numpy installed the stdlib loop must not run at all, and
    a full-scale scan must finish far inside the 4s deadline."""

    pytestmark = pytest.mark.skipif(
        pytest.importorskip("numpy", reason="embeddings extra") is None,
        reason="numpy required",
    )

    def _seed_at_scale(self, cfg: Config, n: int = 7000, dim: int = 1536):
        import numpy as np

        rng = np.random.default_rng(42)
        cfg.weave_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(cfg.embeddings_db)) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS embeddings ("
                "note_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, "
                "embedding BLOB NOT NULL, model TEXT NOT NULL, "
                "created_at TEXT NOT NULL)"
            )
            rows = [
                (
                    f"n-scale-{i:05d}",
                    "h",
                    struct.pack(f"{dim}f", *rng.random(dim, dtype=np.float32)),
                    "fake-bow-v1",
                    "2026-07-05T00:00:00+00:00",
                )
                for i in range(n)
            ]
            db.executemany("INSERT INTO embeddings VALUES (?,?,?,?,?)", rows)

    def test_stdlib_cosine_not_called_when_numpy_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from thinkweave.core import embeddings as emb_mod

        cfg = Config(vault_root=tmp_path / "vault")
        self._seed_at_scale(cfg, n=50, dim=8)
        # Stub the provider itself, not just _call_api: search() compares
        # rows against provider.model, so the stub must carry the same model
        # string the seeded rows were stamped with.
        stub = _FixedVectorProvider([1.0] + [0.0] * 7)
        monkeypatch.setattr(
            emb_mod.EmbeddingSearch, "_provider", lambda self: stub
        )

        def _tripwire(a, b):  # pragma: no cover - the assertion IS the test
            raise AssertionError(
                "stdlib cosine_similarity ran with numpy installed — "
                "the O(n·dim) interpreted scan is back"
            )

        monkeypatch.setattr(emb_mod, "cosine_similarity", _tripwire)
        es = emb_mod.EmbeddingSearch(config=cfg)
        hits = es.search("q", limit=10)
        es.close()
        assert len(hits) == 10

    def test_full_scale_scan_fits_deadline_with_room(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import numpy as np

        from thinkweave.core import embeddings as emb_mod

        cfg = Config(vault_root=tmp_path / "vault")
        self._seed_at_scale(cfg)
        query = np.random.default_rng(7).random(1536, dtype=np.float32)
        stub = _FixedVectorProvider([float(x) for x in query])
        monkeypatch.setattr(
            emb_mod.EmbeddingSearch, "_provider", lambda self: stub
        )
        es = emb_mod.EmbeddingSearch(config=cfg)
        t0 = time.monotonic()
        hits = es.search("q", limit=20)
        elapsed = time.monotonic() - t0
        es.close()
        assert len(hits) == 20
        # Generous CI headroom: the vectorized scan measures ~0.1s where the
        # stdlib loop measured ~0.9s (and grew linearly until it crossed the
        # 4s deadline). 2s is far above vectorized noise, far below relapse.
        assert elapsed < 2.0, (
            f"7k×1536 scan took {elapsed:.2f}s — deadline headroom is gone"
        )


class TestSessionNoteFallbackBounded:
    """The '16s rglob on every prompt' guard: when the index misses, the
    fallback must check newest-first and read only a capped handful of
    session notes — never walk the vault."""

    def test_unindexed_newest_session_found_in_few_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        cfg = Config(vault_root=tmp_path / "vault")
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        for i in range(12):
            vm.create_note(
                NoteType.SESSION,
                f"Old session {i}",
                project="t",
                extra_frontmatter={"source_session": f"ses-old-{i}"},
            )
        target = vm.create_note(
            NoteType.SESSION,
            "Just created, not yet indexed",
            project="t",
            extra_frontmatter={"source_session": "ses-fresh-42"},
        )
        # Make "newest" unambiguous even on coarse-mtime filesystems.
        future = time.time() + 60
        (target).touch()
        import os

        os.utime(target, (future, future))
        assert not cfg.index_db.exists()  # forces the fallback path

        reads = []
        real_read = VaultManager.read_note
        monkeypatch.setattr(
            VaultManager,
            "read_note",
            lambda self, p: (reads.append(p), real_read(self, p))[1],
        )
        found = hooks_handler._find_session_note(vm, "ses-fresh-42")
        assert found == target
        assert len(reads) <= 3, (
            f"fallback read {len(reads)} session notes — newest-first "
            "ordering or the cap has regressed"
        )

    def test_capped_miss_returns_none_instead_of_scanning(
        self, tmp_path: Path
    ):
        cfg = Config(vault_root=tmp_path / "vault")
        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        for i in range(20):
            vm.create_note(
                NoteType.SESSION,
                f"Session {i}",
                project="t",
                extra_frontmatter={"source_session": f"ses-{i}"},
            )
        # No index, and the wanted id doesn't exist: bounded miss, no error.
        assert hooks_handler._find_session_note(vm, "ses-nope") is None
