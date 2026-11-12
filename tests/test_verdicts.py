"""The ``verdicts`` table: a rebuildable projection of archived verdict events."""

from __future__ import annotations

from thinkweave.core.buffer import append_event, archive_buffer, buffer_path
from thinkweave.core.decision_provider import FakeDecisionProvider
from thinkweave.core.indexer import Indexer
from thinkweave.operations.verdicts import run_site

from tests.conftest import write_session_md

_SLOTS = {"prompt": "How do I bake bread?", "candidate": "Sourdough starter notes"}
_COLUMNS = (
    "site, session_id, turn, subject_id, phase, question, kind, probs, value, "
    "action, source, model, backend, latency_ms, cost, ts"
)


def _rows(cfg) -> list[tuple]:
    idx = Indexer(config=cfg)
    try:
        return [
            tuple(r)
            for r in idx.db.execute(f"SELECT {_COLUMNS} FROM verdicts ORDER BY ts, site")
        ]
    finally:
        idx.close()


def _full_rebuild(cfg) -> None:
    idx = Indexer(config=cfg)
    try:
        idx.rebuild(full=True)
    finally:
        idx.close()


def test_dropping_verdicts_and_rebuilding_reproduces_every_row(vault_factory):
    cfg = vault_factory().config
    session_dir = write_session_md(
        cfg.vault_root / "projects" / "p" / "sessions" / "s1",
        type="session", id="ses-abc", project="p", date="2026-11-01",
    )
    live = buffer_path(cfg.weave_dir, "harness-1")
    append_event(live, {"type": "prompt", "text": "hi"}, harness="")

    fake = FakeDecisionProvider()
    run_site(cfg, "smoke", _SLOTS, provider=fake, session_id="harness-1",
             turn=3, subject_id="n-1", phase="ex_ante", action="served")
    run_site(cfg, "smoke", _SLOTS, provider=fake, session_id="harness-1",
             turn=3, subject_id="n-1", phase="ex_post")
    run_site(cfg, "smoke", _SLOTS, provider=fake, subject_id="src-9")
    archive_buffer(cfg.weave_dir, "harness-1", session_dir)

    _full_rebuild(cfg)
    before = _rows(cfg)

    assert len(before) == 3
    session_rows = [r for r in before if r[1] == "ses-abc"]
    assert [(r[2], r[3], r[4], r[9]) for r in session_rows] == [
        (3, "n-1", "ex_ante", "served"), (3, "n-1", "ex_post", "none"),
    ]
    vault_row = next(r for r in before if r[1] is None)
    assert vault_row[3:8] == (
        "src-9", "ex_ante", "relevance", "choice",
        '{"relevant": 1.0, "irrelevant": 0.0}',
    )
    assert vault_row[8] == "relevant"
    assert (vault_row[11], vault_row[12]) == ("fake", "fake")

    idx = Indexer(config=cfg)
    idx.db.execute("DROP TABLE verdicts")
    idx.db.commit()
    idx.close()
    _full_rebuild(cfg)

    assert _rows(cfg) == before
