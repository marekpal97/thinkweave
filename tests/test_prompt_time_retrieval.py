"""Tests for ``operations/prompt_time_retrieval.py`` — R2 enrichment logic.

Three layers:

- Pure orchestration (``build_enrichment``) with the retrieval arm stubbed —
  triviality gate, dedup vs the live buffer, the hard caps, deadline-miss
  propagation, and the adaptive skip.
- The retrieval guard (``_retrieve``) with a fake Search, proving the
  similarity arm is deadlined and cosine-floored.
- The prompt gate: the RRF pool scored by a decision site, its cut-off, its
  verdicts, and its fallback on a backend error.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from thinkweave.core.config import Config
import thinkweave.operations.prompt_time_retrieval as ptr


class _FakeResult:
    def __init__(self, rid: str, title: str = "Title", rtype: str = "note", rank: float = 0.5):
        self.id = rid
        self.title = title
        self.type = rtype
        self.path = f"projects/x/{rid}.md"
        self.rank = rank


def _cfg(tmp_path: Path) -> Config:
    return Config(vault_root=tmp_path / "vault")


def _write_buffer(cfg: Config, session_id: str, events: list[dict]) -> None:
    buf = cfg.weave_dir / "buffer" / f"{session_id}.jsonl"
    buf.parent.mkdir(parents=True, exist_ok=True)
    buf.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


REAL_PROMPT = "why did we move theme naming out of the post-create hook into dream?"


# ---------------------------------------------------------------------------
# build_enrichment — gate / dedup / caps  (retrieval stubbed)
# ---------------------------------------------------------------------------


def test_emits_block_for_fresh_results(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(
        ptr, "_retrieve",
        lambda *a, **k: ([_FakeResult("n-aaaaaa01"), _FakeResult("n-bbbbbb02")], False),
    )
    block, ids, missed, _ = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    assert block is not None
    assert block.startswith(ptr._HEADER)
    assert ids == ["n-aaaaaa01", "n-bbbbbb02"]
    assert "n-aaaaaa01" in block
    assert missed is False


def test_disabled_is_noop(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.retrieval_prompt_time.enabled = False
    called = {"n": 0}
    monkeypatch.setattr(
        ptr, "_retrieve",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or ([], False),
    )
    block, ids, missed, _ = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    assert (block, ids, missed) == (None, [], False)
    assert called["n"] == 0  # never even searched


@pytest.mark.parametrize("prompt", ["ok", "yes", "/clear and go", "   ", "hi"])
def test_triviality_gate_skips_short_and_slash(tmp_path: Path, monkeypatch, prompt):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(ptr, "_retrieve", lambda *a, **k: ([_FakeResult("n-zzzzzz99")], False))
    block, ids, missed, _ = ptr.build_enrichment(cfg, "ses-1", prompt)
    assert (block, ids, missed) == (None, [], False)


def test_note_level_dedup_against_buffer(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    # n-served01 came in at startup, n-served02 via a prior prompt-time push.
    _write_buffer(cfg, "ses-1", [
        {"type": "startup", "returned_ids": ["n-served01"], "token_est": 10},
        {"type": "retrieval", "tool": ptr.PROMPT_TIME_TOOL,
         "returned_ids": ["n-served02"], "chars": 50},
    ])
    monkeypatch.setattr(
        ptr, "_retrieve",
        lambda *a, **k: ([
            _FakeResult("n-served01"), _FakeResult("n-served02"),
            _FakeResult("n-fresh0003"),
        ], False),
    )
    block, ids, missed, _ = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    assert ids == ["n-fresh0003"]
    assert missed is False


def test_all_results_already_served_is_noop(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    _write_buffer(cfg, "ses-1", [
        {"type": "startup", "returned_ids": ["n-served01"], "token_est": 10},
    ])
    monkeypatch.setattr(ptr, "_retrieve", lambda *a, **k: ([_FakeResult("n-served01")], False))
    block, ids, missed, _ = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    assert (block, ids, missed) == (None, [], False)


def test_max_pieces_per_turn_cap(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.retrieval_prompt_time.max_pieces_per_turn = 2
    monkeypatch.setattr(
        ptr, "_retrieve",
        lambda *a, **k: ([_FakeResult(f"n-id{i:06d}") for i in range(5)], False),
    )
    block, ids, missed, _ = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    assert len(ids) == 2
    assert missed is False


def test_max_firings_per_session_cap(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.retrieval_prompt_time.max_firings_per_session = 2
    _write_buffer(cfg, "ses-1", [
        {"type": "retrieval", "tool": ptr.PROMPT_TIME_TOOL,
         "returned_ids": ["n-old00001"], "chars": 40},
        {"type": "retrieval", "tool": ptr.PROMPT_TIME_TOOL,
         "returned_ids": ["n-old00002"], "chars": 40},
    ])
    monkeypatch.setattr(ptr, "_retrieve", lambda *a, **k: ([_FakeResult("n-fresh999")], False))
    block, ids, missed, _ = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    assert (block, ids, missed) == (None, [], False)


def test_session_char_ceiling_cap(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.retrieval_prompt_time.max_injected_chars_per_session = 100
    _write_buffer(cfg, "ses-1", [
        {"type": "retrieval", "tool": ptr.PROMPT_TIME_TOOL,
         "returned_ids": ["n-old00001"], "chars": 95},
    ])
    monkeypatch.setattr(ptr, "_retrieve", lambda *a, **k: ([_FakeResult("n-fresh999")], False))
    block, ids, missed, _ = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    # Only 5 chars of headroom left (< _MIN_PIECE_CHARS) → no-op.
    assert (block, ids, missed) == (None, [], False)


def test_per_turn_char_cap_truncates(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.retrieval_prompt_time.max_pieces_per_turn = 10
    cfg.retrieval_prompt_time.max_injected_chars_per_turn = 140
    monkeypatch.setattr(
        ptr, "_retrieve",
        lambda *a, **k: ([
            _FakeResult(f"n-id{i:06d}", title="A reasonably long title here")
            for i in range(10)
        ], False),
    )
    block, ids, missed, _ = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    assert block is not None
    assert len(block) <= 140
    assert 0 < len(ids) < 10
    assert missed is False


# ---------------------------------------------------------------------------
# build_enrichment — deadline-miss propagation + adaptive skip
# ---------------------------------------------------------------------------


def test_retrieve_miss_propagates_as_noop_with_missed_true(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(ptr, "_retrieve", lambda *a, **k: ([], True))
    block, ids, missed, _ = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    assert (block, ids) == (None, [])
    assert missed is True


def test_adaptive_skip_after_consecutive_misses(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.retrieval_prompt_time.deadline_miss_limit = 3
    _write_buffer(cfg, "ses-1", [
        {"ts": "t1", "type": "prompt"},
        {"ts": "t2", "type": ptr.PROMPT_TIME_MISS, "session_id": "ses-1"},
        {"ts": "t3", "type": "prompt"},
        {"ts": "t4", "type": ptr.PROMPT_TIME_MISS, "session_id": "ses-1"},
        {"ts": "t5", "type": "prompt"},
        {"ts": "t6", "type": ptr.PROMPT_TIME_MISS, "session_id": "ses-1"},
    ])
    called = {"n": 0}
    monkeypatch.setattr(
        ptr, "_retrieve",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or ([], False),
    )
    block, ids, missed, _ = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    # Threshold reached — the similarity arm must not even be attempted.
    assert called["n"] == 0
    assert (block, ids, missed) == (None, [], False)


def test_below_skip_threshold_still_retrieves(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.retrieval_prompt_time.deadline_miss_limit = 3
    _write_buffer(cfg, "ses-1", [
        {"ts": "t1", "type": ptr.PROMPT_TIME_MISS, "session_id": "ses-1"},
        {"ts": "t2", "type": ptr.PROMPT_TIME_MISS, "session_id": "ses-1"},
    ])
    called = {"n": 0}
    monkeypatch.setattr(
        ptr, "_retrieve",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or ([_FakeResult("n-x")], False),
    )
    block, ids, missed, _ = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    assert called["n"] == 1
    assert ids == ["n-x"]


def test_successful_firing_resets_the_miss_streak(tmp_path: Path, monkeypatch):
    """A successful firing between misses breaks the streak — only the
    misses AFTER the last firing count toward the adaptive-skip threshold."""
    cfg = _cfg(tmp_path)
    cfg.retrieval_prompt_time.deadline_miss_limit = 3
    _write_buffer(cfg, "ses-1", [
        {"ts": "t1", "type": ptr.PROMPT_TIME_MISS, "session_id": "ses-1"},
        {"ts": "t2", "type": ptr.PROMPT_TIME_MISS, "session_id": "ses-1"},
        {"ts": "t3", "type": "retrieval", "tool": ptr.PROMPT_TIME_TOOL,
         "returned_ids": ["n-fired"], "chars": 40},
        {"ts": "t4", "type": ptr.PROMPT_TIME_MISS, "session_id": "ses-1"},
    ])
    called = {"n": 0}
    monkeypatch.setattr(
        ptr, "_retrieve",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or ([_FakeResult("n-y")], False),
    )
    block, ids, missed, _ = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    # Only 1 miss since the last firing — well under the threshold of 3.
    assert called["n"] == 1
    assert ids == ["n-y"]


# ---------------------------------------------------------------------------
# _retrieve — deadline + cosine floor (similarity-only, no FTS)
# ---------------------------------------------------------------------------


class _FakeSearch:
    def __init__(self, sem, sem_delay=0.0):
        self._sem, self._delay = sem, sem_delay

    def similar(self, q, note_type="", limit=10):
        if self._delay:
            time.sleep(self._delay)
        return list(self._sem)

    def close(self):
        pass


def _patch_search(monkeypatch, fake):
    monkeypatch.setattr(
        "thinkweave.retrieval.search.Search", lambda config=None: fake
    )


def test_cosine_floor_drops_low_similarity(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    fake = _FakeSearch(
        sem=[_FakeResult("n-high00001", rank=0.45), _FakeResult("n-low000002", rank=0.30)],
    )
    _patch_search(monkeypatch, fake)
    out, missed = ptr._retrieve(cfg, "q", ["note"], limit=5, deadline=2.0, min_similarity=0.38)
    assert [r.id for r in out] == ["n-high00001"]  # 0.30 floored out
    assert missed is False


def test_deadline_exceeded_reports_missed(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    fake = _FakeSearch(
        sem=[_FakeResult("n-sem00003", rank=0.9)],
        sem_delay=2.0,
    )
    _patch_search(monkeypatch, fake)
    t0 = time.monotonic()
    out, missed = ptr._retrieve(cfg, "q", ["note"], limit=5, deadline=0.2, min_similarity=0.38)
    elapsed = time.monotonic() - t0
    assert out == []
    assert missed is True
    assert elapsed < 1.5  # didn't wait the full 2s


def test_fast_similarity_returns_results(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    fake = _FakeSearch(
        sem=[_FakeResult("n-sem00003", rank=0.9)],
        sem_delay=0.0,
    )
    _patch_search(monkeypatch, fake)
    out, missed = ptr._retrieve(cfg, "q", ["note"], limit=5, deadline=2.0, min_similarity=0.38)
    assert {r.id for r in out} == {"n-sem00003"}
    assert missed is False


def test_search_construction_failure_returns_empty_not_missed(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)

    def _boom(config=None):
        raise RuntimeError("no index")

    monkeypatch.setattr("thinkweave.retrieval.search.Search", _boom)
    out, missed = ptr._retrieve(cfg, "q", ["note"], limit=5, deadline=2.0, min_similarity=0.38)
    assert out == []
    assert missed is False


# ---------------------------------------------------------------------------
# build_enrichment — the caller's remaining hook budget caps the arm
# ---------------------------------------------------------------------------


def test_caller_deadline_caps_the_similarity_arm(tmp_path: Path, monkeypatch):
    """The arm runs for the smaller of the caller's remaining budget and the
    configured embed deadline; a generous caller budget changes nothing."""
    cfg = _cfg(tmp_path)
    cfg.retrieval_prompt_time.embed_deadline_seconds = 4.0
    seen: list[float] = []
    monkeypatch.setattr(
        ptr, "_retrieve",
        lambda *a, **k: seen.append(k["deadline"]) or ([], False),
    )
    ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT, deadline=1.0)
    ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT, deadline=10.0)
    ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    assert seen == [1.0, 4.0, 4.0]


def test_a_spent_budget_is_a_miss_without_starting_the_arm(tmp_path: Path, monkeypatch):
    """Under the floor the arm is not attempted and the turn counts as a
    miss, so a harness that kills the hook early still feeds the adaptive
    skip instead of paying the full budget on every prompt."""
    cfg = _cfg(tmp_path)
    called = {"n": 0}
    monkeypatch.setattr(
        ptr, "_retrieve",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or ([], False),
    )
    for spent in (0.1, 0.0, -2.0):
        assert ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT, deadline=spent) == (None, [], True, "")
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# The prompt gate — an RRF pool scored by a System 1 decision site
# ---------------------------------------------------------------------------


def _gate_on(cfg: Config, monkeypatch, provider) -> None:
    from thinkweave.core.config import DecisionSite

    cfg.verdicts.site["prompt_gate"] = DecisionSite(enabled=True)
    monkeypatch.setattr(
        "thinkweave.operations.verdicts.build_from_vault", lambda *a, **k: provider
    )


def _start_turn(cfg: Config, session_id: str, prompt: str) -> None:
    _write_buffer(cfg, session_id, [{"type": "prompt", "text": prompt}])


def _decision_events(cfg: Config, session_id: str) -> list[dict]:
    return [e for e in ptr.Ledger.read(cfg, session_id).events if e.get("type") == "verdict"]


class _RaisingEmbedder:
    model = "raising"

    def embed(self, texts):
        raise RuntimeError("429 Too Many Requests")


class _WordEmbedder:
    model = "word"

    def embed(self, texts):
        return [[float(w in t.lower()) for w in ("sourdough", "starter", "tomato")] + [1.0]
                for t in texts]


def test_a_raising_embedder_leaves_bm25_candidates_for_the_gate(vault_factory, monkeypatch):
    from thinkweave.core.decision_provider import FakeDecisionProvider
    from thinkweave.core.embeddings import EmbeddingSearch

    handle = vault_factory(notes=[
        {"title": "Sourdough starter care", "body": "Feed the starter flour and water daily."},
        {"title": "Tomato watering", "body": "Water tomatoes every second morning."},
    ], indexed=True)
    cfg = handle.config
    monkeypatch.setattr(EmbeddingSearch, "_provider", lambda self: _WordEmbedder())
    es = EmbeddingSearch(config=cfg)
    es.compute_all()
    es.close()
    monkeypatch.setattr(EmbeddingSearch, "_provider", lambda self: _RaisingEmbedder())

    prompt = "how often should I feed my sourdough starter"
    _start_turn(cfg, "ses-g", prompt)
    sourdough = _note_id(cfg, "Sourdough starter care")
    fake = FakeDecisionProvider(nouls={sourdough: 0.9})
    _gate_on(cfg, monkeypatch, fake)

    result = ptr.build_enrichment(cfg, "ses-g", prompt)

    scored = [q.subject for q in fake.calls[0][1]]
    assert sourdough in scored
    assert result.served_ids == [sourdough]
    assert result.verdict_miss == ""


def _note_id(cfg: Config, title: str) -> str:
    import sqlite3

    with sqlite3.connect(str(cfg.index_db)) as db:
        return db.execute("SELECT id FROM notes WHERE title = ?", (title,)).fetchone()[0]


def test_a_raising_backend_writes_a_verdict_miss_and_serves_the_rrf_top(tmp_path: Path, monkeypatch):
    from thinkweave.core.decision_provider import FakeDecisionProvider
    from thinkweave.surfaces.hooks import handler as handler_mod

    cfg = _cfg(tmp_path)
    monkeypatch.setattr("thinkweave.core.config.load_config", lambda: cfg)
    monkeypatch.setattr(handler_mod, "_ensure_session", lambda *a, **k: None)
    pool = [_FakeResult(f"n-rrf{i:05d}", title=f"Pool note {i}") for i in range(6)]
    monkeypatch.setattr(ptr, "_pool", lambda *a, **k: ptr.Pool(pool, False))
    _gate_on(cfg, monkeypatch, FakeDecisionProvider(error=RuntimeError("429 Too Many Requests")))

    handler_mod._handle_user_prompt_submit(
        {"session_id": "ses-miss", "prompt": REAL_PROMPT, "cwd": str(tmp_path)}
    )

    events = ptr.Ledger.read(cfg, "ses-miss").events
    misses = [e for e in events if e.get("type") == "verdict_miss"]
    assert len(misses) == 1
    assert misses[0]["site"] == "prompt_gate"
    assert "429" in misses[0]["reason"]
    served = [e for e in events if e.get("tool") == ptr.PROMPT_TIME_TOOL]
    assert served[0]["returned_ids"] == ["n-rrf00000", "n-rrf00001", "n-rrf00002"]
    assert _decision_events(cfg, "ses-miss") == []


def test_default_accept_rejects_069_and_serves_070_with_ex_ante_verdicts(vault_factory, monkeypatch):
    from thinkweave.core.buffer import archive_buffer
    from thinkweave.core.decision_provider import FakeDecisionProvider
    from thinkweave.core.indexer import Indexer

    from tests.conftest import write_session_md

    cfg = vault_factory().config
    monkeypatch.setattr(
        ptr, "_pool",
        lambda *a, **k: ptr.Pool([_FakeResult("n-low000069"), _FakeResult("n-acc000070")], False),
    )
    _gate_on(cfg, monkeypatch, FakeDecisionProvider(nouls={"n-low000069": 0.69, "n-acc000070": 0.70}))
    _start_turn(cfg, "ses-cut", REAL_PROMPT)

    result = ptr.build_enrichment(cfg, "ses-cut", REAL_PROMPT)

    assert result.served_ids == ["n-acc000070"]
    session_dir = write_session_md(
        cfg.vault_root / "projects" / "p" / "sessions" / "s1",
        type="session", id="ses-cutnote", project="p", date="2026-11-01",
    )
    archive_buffer(cfg.weave_dir, "ses-cut", session_dir)
    idx = Indexer(config=cfg)
    try:
        idx.rebuild(full=True)
        rows = idx.db.execute(
            "SELECT subject_id, phase, action, value FROM verdicts "
            "WHERE site = 'prompt_gate' ORDER BY subject_id"
        ).fetchall()
    finally:
        idx.close()
    assert [tuple(r) for r in rows] == [
        ("n-acc000070", "ex_ante", "served", 0.70),
        ("n-low000069", "ex_ante", "rejected", 0.69),
    ]
