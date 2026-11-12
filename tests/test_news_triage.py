"""News triage on System 1: the ``news_triage`` question set decides an item
unless its top probability falls in the escalation band."""

from __future__ import annotations

from thinkweave.core.config import QuestionSet
from thinkweave.core.decision_provider import FakeDecisionProvider
from thinkweave.core.indexer import Indexer
from thinkweave.operations.news_triage import ESCALATE, Triage, triage

_ITEM = {"id": "q-1", "title": "Memory chip rally extends", "outlet": "cnbc", "tier": 1}


def _vault_with_theme(vault_factory, **policy):
    """A vault with one active theme and the question set on; returns the
    config and the theme's id."""
    handle = vault_factory(themes=["AI capex supercycle"], indexed=True)
    cfg = handle.config
    cfg.verdicts.question_set["news_triage"] = QuestionSet(enabled=True, **policy)
    idx = Indexer(config=cfg)
    try:
        theme_id = idx.db.execute("SELECT id FROM notes WHERE type = 'theme'").fetchone()[0]
    finally:
        idx.close()
    return cfg, theme_id


def _verdict_rows(cfg) -> list[tuple]:
    idx = Indexer(config=cfg)
    try:
        idx.project_vault_verdicts()
        return [
            tuple(r) for r in idx.db.execute(
                "SELECT subject_id, question, value, action FROM verdicts "
                "WHERE question_set = 'news_triage' ORDER BY question DESC"
            )
        ]
    finally:
        idx.close()


def test_an_item_outside_the_band_is_decided(vault_factory):
    cfg, theme_id = _vault_with_theme(vault_factory)

    [outcome] = triage(cfg, [_ITEM], provider=FakeDecisionProvider(confidence=0.9))

    assert outcome == Triage("q-1", "keep", theme_id, 0.9, outcome.reason)
    assert _verdict_rows(cfg) == [
        ("q-1", "verdict", "keep", "keep"), ("q-1", "theme", theme_id, "keep"),
    ]


def test_an_item_inside_the_band_is_escalated(vault_factory):
    cfg, _theme_id = _vault_with_theme(vault_factory)

    [outcome] = triage(cfg, [_ITEM], provider=FakeDecisionProvider(confidence=0.5))

    assert (outcome.verdict, outcome.theme_id, outcome.probability) == (ESCALATE, None, 0.5)
    assert [row[3] for row in _verdict_rows(cfg)] == [ESCALATE, ESCALATE]


def test_the_band_is_the_question_sets_knob(vault_factory):
    cfg, _theme_id = _vault_with_theme(vault_factory, escalate=(0.85, 0.95))

    [outcome] = triage(cfg, [_ITEM], provider=FakeDecisionProvider(confidence=0.9))

    assert outcome.verdict == ESCALATE


def test_a_failed_call_escalates_the_item_and_logs_a_miss(vault_factory):
    cfg, _theme_id = _vault_with_theme(vault_factory)

    [outcome] = triage(cfg, [_ITEM], provider=FakeDecisionProvider(error=RuntimeError("429")))

    assert outcome.verdict == ESCALATE and "429" in outcome.reason
    assert '"verdict_miss"' in (cfg.vault_root / "verdicts.jsonl").read_text(encoding="utf-8")


def test_a_question_set_that_is_off_escalates_every_item(vault_factory):
    cfg, _theme_id = _vault_with_theme(vault_factory)
    cfg.verdicts.question_set["news_triage"] = QuestionSet(enabled=False)
    provider = FakeDecisionProvider()

    [outcome] = triage(cfg, [_ITEM], provider=provider)

    assert outcome.verdict == ESCALATE and provider.calls == []
