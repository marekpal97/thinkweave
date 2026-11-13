"""The duplicate judgment and its two question sets: acquisition dedup at
enqueue and the concept guard behind the ontology gate."""

from __future__ import annotations

from thinkweave.acquisition.sources.queue import Queue
from thinkweave.core.buffer import verdicts_log_path
from thinkweave.core.config import QuestionSet
from thinkweave.core.decision_provider import Answer, Decision, FakeDecisionProvider
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.operations.extract import extract_session
from thinkweave.operations.notes import create_note, update_note
from thinkweave.operations.queue import Enqueued, enqueue
from thinkweave.operations.verdicts import ConceptGuard, judge_duplicate

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

    fields = guard.gate(["retrieval", "embeddings"], ontology_keep=_ONTOLOGY)

    assert fields == {
        "concepts": ["retrieval", "embedding"],
        "concept_synonyms": {"embeddings": "embedding"},
    }
    assert _verdict_rows(guard.cfg) == [
        ("concept_guard", "embeddings", "choice", "embedding", "duplicate")
    ]


def test_a_likely_synonym_stays_proposed_with_a_hint(vault_factory):
    guard = _guarded(vault_factory, FakeDecisionProvider(confidence=0.6))

    fields = guard.gate(["retrieval", "embeddings"], ontology_keep=_ONTOLOGY)

    assert fields == {
        "concepts": ["retrieval"],
        "proposed_concepts": ["embeddings"],
        "concept_hints": {"embeddings": "embedding"},
    }


def test_a_term_with_no_near_canonical_concept_is_not_judged(vault_factory):
    fake = FakeDecisionProvider()
    guard = _guarded(vault_factory, fake)

    fields = guard.gate(["quantum-foam"], ontology_keep=_ONTOLOGY)

    assert fields == {"proposed_concepts": ["quantum-foam"]}
    assert fake.calls == []


class _Unoffered:
    """A backend that answers with an option nobody offered."""

    def decide(self, state, questions):
        answer = Answer("duplicate", "choice", "vector-databases", {"vector-databases": 1.0})
        return Decision((answer,), model="stub", backend="stub", latency_ms=0, cost=0.0)


def test_a_choice_that_was_never_offered_is_a_miss_and_changes_nothing(vault_factory):
    guard = _guarded(vault_factory, _Unoffered())

    fields = guard.gate(["embeddings"], ontology_keep=_ONTOLOGY)

    assert fields == {"proposed_concepts": ["embeddings"]}
    assert _verdict_rows(guard.cfg) == []
    log = verdicts_log_path(guard.cfg.vault_root).read_text(encoding="utf-8")
    assert '"verdict_miss"' in log and "vector-databases" in log


def test_an_update_judges_only_the_terms_it_adds(vault_factory):
    """Against the seed ontology: ``embeddings`` and ``knowledge-graph`` are canonical."""
    cfg = vault_factory().config
    note = create_note(
        cfg, note_type=NoteType.NOTE, title="Carried",
        extra_frontmatter={"proposed_concepts": ["embedding"]},
    ).note
    cfg.verdicts.question_set["concept_guard"] = QuestionSet(enabled=True, backend="fake")

    updated = update_note(
        cfg, note.id, frontmatter_updates={"concepts": ["embedding", "knowledge-graphs"]}
    )

    assert updated.frontmatter["concepts"] == ["knowledge-graph"]
    assert updated.frontmatter["proposed_concepts"] == ["embedding"]
    assert updated.frontmatter["concept_synonyms"] == {"knowledge-graphs": "knowledge-graph"}
    assert [row[1] for row in _verdict_rows(cfg)] == ["knowledge-graphs"]


def test_one_extraction_judges_a_repeated_term_once(vault_factory, monkeypatch):
    """Against the seed ontology: ``embeddings`` and ``knowledge-graph`` are canonical."""
    cfg = vault_factory(indexed=True).config
    cfg.verdicts.question_set["concept_guard"] = QuestionSet(enabled=True)
    fake = FakeDecisionProvider()
    monkeypatch.setattr(
        "thinkweave.operations.verdicts.build_from_vault", lambda *a, **k: fake
    )

    out = extract_session(
        cfg, session_id="ses-repeat-1", project="t", summary="x",
        insights=[
            {"title": "First", "body": "b", "concepts": ["embedding"]},
            {"title": "Second", "body": "b", "concepts": ["embedding", "knowledge-graphs"]},
        ],
        decisions=[{
            "title": "Third", "rationale": "## Context\n\n## Decision\n",
            "concepts": ["embedding"],
        }],
    )

    assert out.error == ""
    assert len(fake.calls) == 2
    synonyms = [
        n.frontmatter.get("concept_synonyms")
        for n in out.created_notes + out.created_decisions
    ]
    assert synonyms == [
        {"embedding": "embeddings"},
        {"embedding": "embeddings", "knowledge-graphs": "knowledge-graph"},
        {"embedding": "embeddings"},
    ]


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
    log = verdicts_log_path(cfg.vault_root).read_text(encoding="utf-8")
    assert '"verdict_miss"' in log and "RuntimeError: 429" in log
