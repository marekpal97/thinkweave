"""The ex-post judge: one pass over a session's ledger, from wrap or a sweep."""

from __future__ import annotations

import json

import pytest

from thinkweave.core.buffer import append_event, archive_buffer, buffer_path
from thinkweave.core.config import DecisionSite
from thinkweave.core.decision_provider import FakeDecisionProvider
from thinkweave.core.indexer import Indexer
from thinkweave.operations import decisions

from tests.conftest import write_session_md


def _gate(ts: str, *answers: tuple[str, float, str]) -> dict:
    return {
        "ts": ts, "type": "decision", "site": "prompt_gate", "turn": 1,
        "subject_id": "", "phase": "ex_ante", "action": "none",
        "source": "prompttime", "model": "fake", "backend": "fake",
        "latency_ms": 40, "cost": 0.001,
        "answers": [
            {"question": "helps", "kind": "noul", "value": v, "probs": {"noul": v},
             "subject_id": sid, "action": action}
            for sid, v, action in answers
        ],
    }


#: Two prompts and a startup row, as the hooks buffer them.
_REPLAY = [
    {"ts": "2026-11-01T10:00:00+00:00", "type": "startup",
     "returned_ids": ["n-start01", "tsk-open01"], "token_est": 10},
    {"ts": "2026-11-01T10:01:00+00:00", "type": "prompt", "text": "fix the parser"},
    _gate("2026-11-01T10:01:01+00:00",
          ("n-gate0001", 0.9, "served"), ("n-gate0002", 0.2, "rejected")),
    {"ts": "2026-11-01T10:01:02+00:00", "type": "retrieval",
     "tool": "prompt_time_retrieval", "returned_ids": ["n-gate0001"], "chars": 60},
    {"ts": "2026-11-01T10:01:30+00:00", "type": "retrieval",
     "tool": "mcp__thinkweave__weave_search", "returned_ids": ["n-fly00001", "tsk-x0001"]},
    {"ts": "2026-11-01T10:01:40+00:00", "tool": "Edit", "file": "src/parser.py",
     "context": ""},
    {"ts": "2026-11-01T10:02:00+00:00", "type": "turn_end", "turn": 1,
     "message": "Fixed the parser.", "files": ["src/parser.py"]},
    {"ts": "2026-11-01T10:03:00+00:00", "type": "prompt", "text": "now the docs"},
    {"ts": "2026-11-01T10:03:30+00:00", "type": "retrieval",
     "tool": "mcp__thinkweave__weave_read", "returned_ids": ["n-fly00002"]},
    {"ts": "2026-11-01T10:04:00+00:00", "type": "turn_end", "turn": 2,
     "message": "Docs updated.", "files": []},
]


@pytest.fixture
def replayed(vault_factory, monkeypatch):
    """A session ``ses-ex`` (harness ``h-1``) whose live buffer replays ``_REPLAY``,
    with the ex-post site on and a fake backend behind it."""
    cfg = vault_factory(indexed=True).config
    cfg.decision_sites["ex_post"] = DecisionSite(enabled=True)
    folder = write_session_md(
        cfg.vault_root / "projects" / "p" / "sessions" / "s1",
        type="session", id="ses-ex", project="p", date="2026-11-01",
        source_session="h-1",
    )
    for event in _REPLAY:
        append_event(buffer_path(cfg.weave_dir, "h-1"), dict(event), harness="")
    fake = FakeDecisionProvider(nouls={"n-gate0001": 0.8, "n-start01": 0.1})
    monkeypatch.setattr("thinkweave.operations.decisions.build_from_vault",
                        lambda *a, **k: fake)
    idx = Indexer(config=cfg)
    idx.rebuild(full=True)
    idx.close()
    return cfg, folder, fake


def _verdicts(cfg, phase: str) -> list[tuple]:
    idx = Indexer(config=cfg)
    try:
        return sorted(
            tuple(r) for r in idx.db.execute(
                "SELECT ifnull(turn, 0), source, subject_id, action FROM verdicts "
                "WHERE phase = ? AND session_id = 'ses-ex'", (phase,),
            )
        )
    finally:
        idx.close()


def test_the_pass_judges_turn_items_per_turn_and_startup_items_per_session(replayed):
    cfg, folder, fake = replayed
    archive_buffer(cfg.weave_dir, "h-1", folder)

    decisions.ex_post_pass(cfg, decisions.Ledger.read(cfg, "h-1", folder))

    assert _verdicts(cfg, "ex_post") == [
        (0, "startup", "n-start01", "served"),
        (1, "onthefly", "n-fly00001", "served"),
        (1, "prompttime", "n-gate0001", "served"),
        (1, "prompttime", "n-gate0002", "rejected"),
        (2, "onthefly", "n-fly00002", "served"),
    ]
    asked = [q.subject for _state, qs in fake.calls for q in qs]
    assert not [s for s in asked if s.startswith("tsk-")]
    session_state = next(state for state, qs in fake.calls
                         if [q.subject for q in qs] == ["n-start01"])
    assert "Fixed the parser." in session_state and "Docs updated." in session_state


def test_startup_items_get_ex_post_rows_and_no_ex_ante_row(replayed):
    cfg, folder, _fake = replayed
    archive_buffer(cfg.weave_dir, "h-1", folder)

    decisions.ex_post_pass(cfg, decisions.Ledger.read(cfg, "h-1", folder))

    ex_ante = {row[2] for row in _verdicts(cfg, "ex_ante")}
    ex_post = {row[2] for row in _verdicts(cfg, "ex_post")}
    assert "n-start01" in ex_post and "n-start01" not in ex_ante
    assert {"n-gate0001", "n-gate0002"} <= ex_ante


def test_sweeping_a_session_twice_writes_its_rows_once(replayed):
    cfg, _folder, fake = replayed

    first = decisions.sweep(cfg)
    second = decisions.sweep(cfg)

    assert (first.sessions, first.rows) == (1, 5)
    assert (second.sessions, second.rows) == (0, 0)
    assert len(_verdicts(cfg, "ex_post")) == 5
    assert len(fake.calls) == 4


def test_a_backend_failure_logs_a_miss_and_the_wrap_still_completes(replayed, monkeypatch):
    from thinkweave.operations.wrap import finalize_wrap

    cfg, folder, _fake = replayed
    archive_buffer(cfg.weave_dir, "h-1", folder)
    broken = FakeDecisionProvider(error=RuntimeError("429 Too Many Requests"))
    monkeypatch.setattr("thinkweave.operations.decisions.build_from_vault",
                        lambda *a, **k: broken)

    result = finalize_wrap(cfg, session_id="ses-ex", project="p", prune=False)

    misses = [json.loads(line) for line in
              (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()
              if '"decision_miss"' in line]
    assert {(m["site"], m["source"], m["turn"]) for m in misses} == {
        ("ex_post", "prompttime", 1), ("ex_post", "onthefly", 1),
        ("ex_post", "onthefly", 2), ("ex_post", "startup", None),
    }
    assert all("429" in m["reason"] for m in misses)
    assert any(w.startswith("ex_post:") for w in result.warnings)
    assert not any("ex_post" in e for e in result.errors)
    assert "drift" in result.timings
    assert _verdicts(cfg, "ex_post") == []
    startup = {(r.site, r.source): r for r in decisions.readout(cfg)}[("ex_post", "startup")]
    assert (startup.calls, startup.misses, startup.miss_rate) == (0, 1, 1.0)


def test_readout_counts_each_call_once_and_tables_served_against_used(replayed):
    cfg, folder, _fake = replayed
    archive_buffer(cfg.weave_dir, "h-1", folder)
    decisions.ex_post_pass(cfg, decisions.Ledger.read(cfg, "h-1", folder))

    sites = {(r.site, r.source): r for r in decisions.readout(cfg, threshold=0.7)}

    gate = sites[("prompt_gate", "prompttime")]
    assert (gate.calls, gate.rows, gate.cost) == (1, 2, 0.001)
    assert gate.latency_ms_p50 == gate.latency_ms_p95 == 40
    assert gate.calibration == [
        decisions.CalibrationBin("0.0-0.2", 0, None),
        decisions.CalibrationBin("0.2-0.4", 1, 0.5),
        decisions.CalibrationBin("0.4-0.6", 0, None),
        decisions.CalibrationBin("0.6-0.8", 0, None),
        decisions.CalibrationBin("0.8-1.0", 1, 0.8),
    ]
    judged = sites[("ex_post", "prompttime")]
    assert judged.usage == {"served": decisions.Usage(used=1, unused=0),
                            "rejected": decisions.Usage(used=0, unused=1)}
    assert sites[("ex_post", "startup")].usage == {"served": decisions.Usage(used=0, unused=1)}
    assert gate.as_dict()["calibration"][1] == {"ex_ante": "0.2-0.4", "n": 1, "mean_ex_post": 0.5}
    assert sites[("ex_post", "onthefly")].calls == 2


def test_a_truncated_turn_end_line_is_reported_not_dropped(replayed, monkeypatch):
    from thinkweave.operations.wrap import finalize_wrap

    cfg, folder, _fake = replayed
    archive_buffer(cfg.weave_dir, "h-1", folder)
    events = folder / "events.jsonl"
    lines = events.read_text(encoding="utf-8").splitlines()
    last_end = max(i for i, line in enumerate(lines) if '"turn_end"' in line)
    lines[last_end] = lines[last_end][:40]
    events.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ledger = decisions.Ledger.read(cfg, "h-1", folder)
    result = finalize_wrap(cfg, session_id="ses-ex", project="p", prune=False)

    assert [m.split(":")[0] for m in ledger.malformed] == [str(events)]
    assert any(w.startswith("ex_post:") and "malformed" in w and str(events) in w
               for w in result.warnings)
    assert 2 not in {row[0] for row in _verdicts(cfg, "ex_post")}
