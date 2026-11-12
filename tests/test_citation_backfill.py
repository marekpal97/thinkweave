"""The citation backfill: the ``citation`` question set asks which served
notes a decision's reasoning relied on, and the accepted ones become links."""

from __future__ import annotations

import json

from thinkweave.core.buffer import verdicts_log_path
from thinkweave.core.config import QuestionSet
from thinkweave.core.decision_provider import FakeDecisionProvider
from thinkweave.core.schemas import NoteType
from thinkweave.operations.rlvr_export import assemble_row
from thinkweave.operations.verdicts import CitationProposal, backfill_citations


def _vault_with_served_decision(vault_factory):
    """A decision whose session served two notes and the session itself;
    returns the config, the decision's path and id, the ids of the relied-on
    and the ignored note, and a provider that scores the relied-on note and
    the session high and the ignored note low."""
    handle = vault_factory()
    vault = handle.vault
    relied, ignored = (
        vault.read_note(vault.create_note(NoteType.NOTE, title, body=body)).id
        for title, body in (
            ("Indexer mtime gate", "Skip unchanged files by mtime."),
            ("Sourdough starter", "Feed it daily."),
        )
    )
    session = vault.create_note(
        NoteType.SESSION, "S", body="## Summary\nseed\n", project="t",
        extra_frontmatter={"processed": True},
    )
    session_id = vault.read_note(session).id
    decision = vault.create_note(
        NoteType.DECISION, "Gate the reindex on mtime",
        body="## Context\n\nFull reindex was slow.\n\n## Decision\n\nGate on mtime.\n",
        project="t", output_dir=session.parent,
        extra_frontmatter={"status": "accepted", "source_session": session_id},
    )
    (session.parent / "retrieval_log.jsonl").write_text(
        json.dumps({"ts": "t1", "type": "retrieval", "returned_ids": [relied, ignored, session_id]}) + "\n",
        encoding="utf-8",
    )
    handle.indexed()
    cfg = handle.config
    cfg.verdicts.question_set["citation"] = QuestionSet(enabled=True, accept=0.7)
    provider = FakeDecisionProvider(nouls={relied: 0.9, ignored: 0.2, session_id: 0.95})
    return cfg, decision, vault.read_note(decision).id, relied, ignored, provider


def test_a_dry_run_lists_proposals_and_writes_nothing(vault_factory):
    cfg, decision, decision_id, relied, _ignored, provider = _vault_with_served_decision(
        vault_factory
    )
    before = decision.read_text(encoding="utf-8")

    result = backfill_citations(cfg, dry_run=True, provider=provider)

    assert result.proposals == [
        CitationProposal(decision_id, "Gate the reindex on mtime", (relied,))
    ]
    assert result.written == 0
    assert decision.read_text(encoding="utf-8") == before
    assert not verdicts_log_path(cfg.vault_root).exists()


def test_two_real_runs_write_the_cites_once(vault_factory):
    cfg, decision, decision_id, relied, ignored, provider = _vault_with_served_decision(
        vault_factory
    )

    first = backfill_citations(cfg, provider=provider)
    after_first = decision.read_text(encoding="utf-8")
    second = backfill_citations(cfg, provider=provider)

    assert (first.written, second.written) == (1, 0)
    assert second.proposals == []
    assert len(provider.calls) == 1
    assert decision.read_text(encoding="utf-8") == after_first
    assert after_first.count(f"|{relied}]]") == 1
    assert ignored not in after_first
    assert assemble_row(cfg, decision_id).context["cited_onthefly_ids"] == [relied]


def test_a_failed_call_is_a_miss_and_leaves_the_decision_for_the_next_run(vault_factory):
    cfg, decision, decision_id, _relied, _ignored, provider = _vault_with_served_decision(
        vault_factory
    )
    before = decision.read_text(encoding="utf-8")

    failed = backfill_citations(cfg, provider=FakeDecisionProvider(error=RuntimeError("429")))
    after_failure = decision.read_text(encoding="utf-8")
    retried = backfill_citations(cfg, provider=provider)

    assert failed.written == 0 and len(failed.misses) == 1
    assert decision_id in failed.misses[0] and "429" in failed.misses[0]
    assert after_failure == before
    assert retried.written == 1
