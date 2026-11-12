"""The duplicate judgment and its two question sets: acquisition dedup at
enqueue and the concept guard behind the ontology gate."""

from __future__ import annotations

from thinkweave.acquisition.sources.queue import Queue
from thinkweave.core.config import QuestionSet
from thinkweave.core.decision_provider import FakeDecisionProvider
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.operations.queue import Enqueued, enqueue
from thinkweave.operations.verdicts import ConceptGuard, judge_duplicate
from thinkweave.synthesis.concepts import split_concepts_by_ontology

_ONTOLOGY = {"embedding", "retrieval"}
_TITLE = "Attention Is All You Need, explained"


def _guarded(vault_factory, provider):
    cfg = vault_factory().config
    cfg.verdicts.question_set["concept_guard"] = QuestionSet(enabled=True, accept=0.7)
    return ConceptGuard(cfg, provider=provider)


def _vault_with_source(vault_factory):
    handle = vault_factory(
        notes=[{
            "title": _TITLE,
            "note_type": NoteType.SOURCE,
            "body": "A walk through the transformer paper.",
            "extra_frontmatter": {
                "source_type": "article", "url": "https://old.example/attention",
            },
        }],
        indexed=True,
    )
    cfg = handle.config
    cfg.verdicts.question_set["acquisition_dedup"] = QuestionSet(enabled=True, accept=0.7)
    return cfg


def _verdict_rows(cfg) -> list[tuple]:
    idx = Indexer(config=cfg)
    try:
        idx.project_vault_verdicts()
        return [
            tuple(r) for r in idx.db.execute(
                "SELECT question_set, subject_id, kind, value, action FROM verdicts"
            )
        ]
    finally:
        idx.close()


def _source_id(cfg) -> str:
    idx = Indexer(config=cfg)
    try:
        return idx.db.execute("SELECT id FROM notes WHERE type = 'source'").fetchone()[0]
    finally:
        idx.close()


def test_a_confident_synonym_becomes_the_canonical_term_with_the_original_recorded(
    vault_factory,
):
    guard = _guarded(vault_factory, FakeDecisionProvider())

    canonical, proposed = split_concepts_by_ontology(
        ["retrieval", "embeddings"], ontology_keep=_ONTOLOGY, resolve=guard.resolve
    )

    assert canonical == ["retrieval", "embedding"]
    assert proposed == []
    assert guard.record() == {"concept_synonyms": {"embeddings": "embedding"}}
    assert _verdict_rows(guard.cfg) == [
        ("concept_guard", "embeddings", "choice", "embedding", "duplicate")
    ]


def test_a_likely_synonym_stays_proposed_with_a_hint(vault_factory):
    guard = _guarded(vault_factory, FakeDecisionProvider(confidence=0.6))

    canonical, proposed = split_concepts_by_ontology(
        ["retrieval", "embeddings"], ontology_keep=_ONTOLOGY, resolve=guard.resolve
    )

    assert canonical == ["retrieval"]
    assert proposed == ["embeddings"]
    assert guard.record() == {"concept_hints": {"embeddings": "embedding"}}


def test_a_term_with_no_near_canonical_concept_is_not_judged(vault_factory):
    fake = FakeDecisionProvider()
    guard = _guarded(vault_factory, fake)

    canonical, proposed = split_concepts_by_ontology(
        ["quantum-foam"], ontology_keep=_ONTOLOGY, resolve=guard.resolve
    )

    assert (canonical, proposed) == ([], ["quantum-foam"])
    assert fake.calls == [] and guard.record() == {}


def test_a_concept_grain_duplicate_is_recorded_and_not_enqueued(vault_factory):
    cfg = _vault_with_source(vault_factory)
    existing = _source_id(cfg)

    result = enqueue(
        cfg, "article", {"url": "https://new.example/attention", "title": _TITLE},
        provider=FakeDecisionProvider(),
    )

    assert result == Enqueued(conflict=existing) and result.deduped
    assert Queue.for_source_type("article", cfg.vault_root).peek(10) == []
    assert _verdict_rows(cfg) == [(
        "acquisition_dedup", "https://new.example/attention", "choice", existing,
        "duplicate",
    )]


def test_an_event_grain_item_never_reaches_the_dedup_question_set(vault_factory):
    cfg = _vault_with_source(vault_factory)
    fake = FakeDecisionProvider()

    result = enqueue(
        cfg, "news", {"url": "https://new.example/attention", "title": _TITLE},
        provider=fake,
    )

    assert fake.calls == []
    assert not result.deduped and result.id
    assert _verdict_rows(cfg) == []


def test_an_unconfident_duplicate_is_enqueued(vault_factory):
    cfg = _vault_with_source(vault_factory)

    result = enqueue(
        cfg, "article", {"url": "https://new.example/attention", "title": _TITLE},
        provider=FakeDecisionProvider(confidence=0.6),
    )

    assert not result.deduped and result.id


def test_a_failed_call_is_logged_as_a_miss_and_gives_no_verdict(vault_factory):
    cfg = _vault_with_source(vault_factory)

    verdict = judge_duplicate(
        cfg, "subject", {"src-1": "a neighbour"}, "acquisition_dedup",
        provider=FakeDecisionProvider(error=RuntimeError("429")),
    )

    assert verdict is None
    from thinkweave.core.buffer import verdicts_log_path

    log = verdicts_log_path(cfg.vault_root).read_text(encoding="utf-8")
    assert '"verdict_miss"' in log and "RuntimeError: 429" in log
