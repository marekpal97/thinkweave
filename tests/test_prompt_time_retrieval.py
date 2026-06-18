"""Tests for ``operations/prompt_time_retrieval.py`` — R2 enrichment logic.

Two layers:

- Pure orchestration (``build_enrichment``) with the retrieval arm stubbed —
  triviality gate, dedup vs the live buffer, and the hard caps.
- The retrieval guard (``_retrieve``) with a fake Search, proving the
  similarity arm is deadlined + cosine-floored and FTS is fused.

There are no stopword/keyword heuristics to test — relevance is the cosine
floor (covered in ``_retrieve``), not a wordlist.
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
        lambda *a, **k: [_FakeResult("n-aaaaaa01"), _FakeResult("n-bbbbbb02")],
    )
    block, ids = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    assert block is not None
    assert block.startswith(ptr._HEADER)
    assert ids == ["n-aaaaaa01", "n-bbbbbb02"]
    assert "n-aaaaaa01" in block


def test_disabled_is_noop(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.retrieval_prompt_time.enabled = False
    called = {"n": 0}
    monkeypatch.setattr(
        ptr, "_retrieve",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or [],
    )
    block, ids = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    assert (block, ids) == (None, [])
    assert called["n"] == 0  # never even searched


@pytest.mark.parametrize("prompt", ["ok", "yes", "/clear and go", "   ", "hi"])
def test_triviality_gate_skips_short_and_slash(tmp_path: Path, monkeypatch, prompt):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(ptr, "_retrieve", lambda *a, **k: [_FakeResult("n-zzzzzz99")])
    block, ids = ptr.build_enrichment(cfg, "ses-1", prompt)
    assert (block, ids) == (None, [])


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
        lambda *a, **k: [
            _FakeResult("n-served01"), _FakeResult("n-served02"),
            _FakeResult("n-fresh0003"),
        ],
    )
    block, ids = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    assert ids == ["n-fresh0003"]


def test_all_results_already_served_is_noop(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    _write_buffer(cfg, "ses-1", [
        {"type": "startup", "returned_ids": ["n-served01"], "token_est": 10},
    ])
    monkeypatch.setattr(ptr, "_retrieve", lambda *a, **k: [_FakeResult("n-served01")])
    block, ids = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    assert (block, ids) == (None, [])


def test_max_pieces_per_turn_cap(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.retrieval_prompt_time.max_pieces_per_turn = 2
    monkeypatch.setattr(
        ptr, "_retrieve",
        lambda *a, **k: [_FakeResult(f"n-id{i:06d}") for i in range(5)],
    )
    block, ids = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    assert len(ids) == 2


def test_max_firings_per_session_cap(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.retrieval_prompt_time.max_firings_per_session = 2
    _write_buffer(cfg, "ses-1", [
        {"type": "retrieval", "tool": ptr.PROMPT_TIME_TOOL,
         "returned_ids": ["n-old00001"], "chars": 40},
        {"type": "retrieval", "tool": ptr.PROMPT_TIME_TOOL,
         "returned_ids": ["n-old00002"], "chars": 40},
    ])
    monkeypatch.setattr(ptr, "_retrieve", lambda *a, **k: [_FakeResult("n-fresh999")])
    block, ids = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    assert (block, ids) == (None, [])


def test_session_char_ceiling_cap(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.retrieval_prompt_time.max_injected_chars_per_session = 100
    _write_buffer(cfg, "ses-1", [
        {"type": "retrieval", "tool": ptr.PROMPT_TIME_TOOL,
         "returned_ids": ["n-old00001"], "chars": 95},
    ])
    monkeypatch.setattr(ptr, "_retrieve", lambda *a, **k: [_FakeResult("n-fresh999")])
    block, ids = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    # Only 5 chars of headroom left (< _MIN_PIECE_CHARS) → no-op.
    assert (block, ids) == (None, [])


def test_per_turn_char_cap_truncates(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.retrieval_prompt_time.max_pieces_per_turn = 10
    cfg.retrieval_prompt_time.max_injected_chars_per_turn = 140
    monkeypatch.setattr(
        ptr, "_retrieve",
        lambda *a, **k: [
            _FakeResult(f"n-id{i:06d}", title="A reasonably long title here")
            for i in range(10)
        ],
    )
    block, ids = ptr.build_enrichment(cfg, "ses-1", REAL_PROMPT)
    assert block is not None
    assert len(block) <= 140
    assert 0 < len(ids) < 10


# ---------------------------------------------------------------------------
# _retrieve — deadline + cosine floor + RRF fusion
# ---------------------------------------------------------------------------


class _FakeSearch:
    def __init__(self, fts, sem, sem_delay=0.0):
        self._fts, self._sem, self._delay = fts, sem, sem_delay

    def search(self, q, note_type="", limit=10):
        return list(self._fts)

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
        fts=[],
        sem=[_FakeResult("n-high00001", rank=0.45), _FakeResult("n-low000002", rank=0.30)],
    )
    _patch_search(monkeypatch, fake)
    out = ptr._retrieve(cfg, "q", ["note"], limit=5, deadline=2.0, min_similarity=0.38)
    assert [r.id for r in out] == ["n-high00001"]  # 0.30 floored out


def test_deadline_falls_back_to_fts(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    fake = _FakeSearch(
        fts=[_FakeResult("n-fts00001"), _FakeResult("n-fts00002")],
        sem=[_FakeResult("n-sem00003", rank=0.9)],
        sem_delay=2.0,
    )
    _patch_search(monkeypatch, fake)
    t0 = time.monotonic()
    out = ptr._retrieve(cfg, "q", ["note"], limit=5, deadline=0.2, min_similarity=0.38)
    elapsed = time.monotonic() - t0
    assert [r.id for r in out] == ["n-fts00001", "n-fts00002"]  # sem abandoned
    assert elapsed < 1.5  # didn't wait the full 2s


def test_fast_similarity_is_fused(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    fake = _FakeSearch(
        fts=[_FakeResult("n-fts00001")],
        sem=[_FakeResult("n-sem00003", rank=0.9)],
        sem_delay=0.0,
    )
    _patch_search(monkeypatch, fake)
    out = ptr._retrieve(cfg, "q", ["note"], limit=5, deadline=2.0, min_similarity=0.38)
    assert {r.id for r in out} == {"n-fts00001", "n-sem00003"}  # both arms fused
