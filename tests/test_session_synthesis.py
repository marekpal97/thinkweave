"""Session synthesis — the imported-CC-session → wrap-shaped note spine.

Covers the shared spec helpers (parse / map / transcript archival) and the
end-to-end batch backend, which prior to the 2026-06-14 unification never
ran successfully (the writeback `NameError`d on a missing `datetime` import).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.vault import VaultManager, parse_frontmatter


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(vault_root=tmp_path / "vault")


@pytest.fixture
def vault(config: Config) -> VaultManager:
    vm = VaultManager(config=config)
    vm.ensure_dirs()
    return vm


# ---------------------------------------------------------------------------
# Spec helpers (pure)
# ---------------------------------------------------------------------------


def test_parse_synthesis_handles_fences_and_garbage():
    from thinkweave.synthesis.session_synthesis import parse_synthesis

    assert parse_synthesis('{"summary": "x"}') == {"summary": "x"}
    assert parse_synthesis('```json\n{"summary": "y"}\n```') == {"summary": "y"}
    assert parse_synthesis("not json") is None
    assert parse_synthesis("") is None
    assert parse_synthesis("[1, 2]") is None  # array, not the expected object


def test_to_extract_inputs_maps_and_is_defensive():
    from thinkweave.synthesis.session_synthesis import to_extract_inputs

    out = to_extract_inputs(
        {
            "summary": "  did a thing  ",
            "concepts": ["fts5", "", 7],  # non-str dropped
            "insights": [
                {"title": "T", "body": "B", "concepts": ["sqlite"]},
                {"title": "", "body": ""},  # empty → dropped
                "junk",  # non-dict → dropped
            ],
            "decisions": [
                {"title": "Do X", "rationale": "because", "outcome": "committed",
                 "file_paths": ["a.py", 9], "concepts": ["x"]},
                {"rationale": "no title"},  # no title → dropped
            ],
        }
    )
    assert out["summary"] == "did a thing"
    assert out["concepts"] == ["fts5"]
    assert len(out["insights"]) == 1
    assert out["insights"][0] == {"title": "T", "body": "B", "concepts": ["sqlite"]}
    assert len(out["decisions"]) == 1
    assert out["decisions"][0]["file_paths"] == ["a.py"]  # non-str scrubbed


def test_archive_transcript_moves_body_and_retires_enrichment_status(
    config: Config, vault: VaultManager
):
    from thinkweave.core.schemas import NoteType
    from thinkweave.synthesis.session_synthesis import archive_transcript

    path = vault.create_note(
        NoteType.SESSION,
        "S",
        body="## Source\n\nfrom CC\n\n## Transcript\n\n### User (turn 1)\n\nhi\n",
        project="p",
        extra_frontmatter={"imported_from": "claude-code", "enrichment_status": "pending"},
    )
    assert archive_transcript(path) is True

    companion = path.parent / "transcript.md"
    assert companion.exists()
    assert "### User (turn 1)" in companion.read_text(encoding="utf-8")

    fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert "## Transcript" not in body          # moved out
    assert "enrichment_status" not in fm        # retired
    assert fm["transcript_file"] == "transcript.md"

    # Idempotent: nothing left to archive.
    assert archive_transcript(path) is False


# ---------------------------------------------------------------------------
# End-to-end batch backend
# ---------------------------------------------------------------------------


def _materialize_one_session(config: Config, vault: VaultManager) -> str:
    """Materialise one imported CC session via the real seed path; return id."""
    from thinkweave.onboarding.claude_code_seed import (
        ClaudeCodeSession,
        materialize_session,
    )

    sess = ClaudeCodeSession(
        uuid="cc-uuid-1",
        project="proj",
        cwd="/home/u/proj",
        git_branch="main",
        started_at=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        user_turns=["Let's add an FTS index", "ship it"],
        assistant_turns=["I'll wire up sqlite FTS5", "done, committed"],
        file_path=Path("/tmp/cc-uuid-1.jsonl"),
    )
    note_id = materialize_session(config, vault, sess, manifest={})
    assert note_id
    return note_id


def test_materialize_does_not_set_enrichment_status(config: Config, vault: VaultManager):
    note_id = _materialize_one_session(config, vault)
    sessions = list((config.vault_root / "projects" / "proj" / "sessions").glob("*/session.md"))
    assert len(sessions) == 1
    fm, body = parse_frontmatter(sessions[0].read_text(encoding="utf-8"))
    assert "enrichment_status" not in fm     # retired at materialize
    assert not fm.get("processed")           # pending = absence of processed
    assert "## Transcript" in body           # born holding the dump


def test_run_enrichment_batch_end_to_end(config: Config, vault: VaultManager, monkeypatch):
    """A pending import → real extract_session writeback: summary in body,
    transcript archived, insight + decision notes minted, processed stamped."""
    _materialize_one_session(config, vault)
    Indexer(config=config).rebuild(full=True)

    synthesis_json = (
        '{"summary": "Added a sqlite FTS5 index and shipped it.",'
        ' "concepts": ["fts5", "widgetcorp-xyz"],'
        ' "insights": [{"title": "FTS5 needs a rebuild trigger",'
        '   "body": "External-content FTS5 tables need a rebuild after bulk insert.",'
        '   "concepts": ["fts5"]}],'
        ' "decisions": [{"title": "Use sqlite FTS5 for search",'
        '   "rationale": "Keyword search over the vault index.",'
        '   "outcome": "committed", "file_paths": ["search.py"], "concepts": ["fts5"]}]}'
    )

    def fake_batch(prompts, **kw):
        return [(synthesis_json, {}) for _ in prompts]

    monkeypatch.setattr("thinkweave.core.agent_client.batch_completions_sync", fake_batch)

    from thinkweave.onboarding.enrich_batch import find_pending_sessions, run_enrichment_batch

    assert len(find_pending_sessions(config)) == 1  # pending before
    stats = run_enrichment_batch(config)

    assert stats["synthesized"] == 1
    assert stats["decisions_created"] == 1
    assert stats["insights_created"] == 1
    assert stats["errors"] == []

    session_dir = next(
        (config.vault_root / "projects" / "proj" / "sessions").glob("*/")
    )
    fm, body = parse_frontmatter((session_dir / "session.md").read_text(encoding="utf-8"))

    # Body is now the synthesis, not the dump.
    assert "## Summary" in body
    assert "Added a sqlite FTS5 index" in body
    assert "## Transcript" not in body
    assert (session_dir / "transcript.md").exists()

    # Synthesised == processed (same marker as a live wrap); enrichment_status gone.
    assert fm.get("processed") is True
    assert "enrichment_status" not in fm

    # Non-ontology term routed to proposed_concepts by the gate.
    proposed = fm.get("proposed_concepts", [])
    assert "widgetcorp-xyz" in proposed

    # Derived insight + decision notes minted into the session folder.
    md_files = {p.name for p in session_dir.glob("*.md")}
    assert any(n.startswith("dec-") or "decision" in n.lower() for n in md_files) or any(
        f.read_text(encoding="utf-8").startswith("---")
        and "type: decision" in f.read_text(encoding="utf-8")
        for f in session_dir.glob("*.md")
        if f.name != "session.md"
    )

    # No longer pending.
    assert len(find_pending_sessions(config)) == 0


def test_extract_session_archives_transcript_for_imports(config: Config, vault: VaultManager):
    """The inline backend reaches ``extract_session`` directly (via
    weave_extract) — so archival must live there, not only in the batch
    wrapper. Driving extract_session on an imported session archives the
    transcript and produces the same shape as the batch path."""
    note_id = _materialize_one_session(config, vault)
    Indexer(config=config).rebuild(full=True)

    from thinkweave.operations.extract import extract_session

    out = extract_session(
        config,
        session_id=note_id,
        summary="Wired FTS5 search and shipped.",
        insights=[{"title": "FTS5 rebuild", "body": "needs a rebuild trigger", "concepts": ["fts5"]}],
        decisions=[{"title": "Use FTS5", "rationale": "keyword search", "outcome": "committed",
                    "file_paths": ["search.py"], "concepts": ["fts5"]}],
    )
    assert out.error == ""
    assert len(out.created_decisions) == 1

    session_dir = next((config.vault_root / "projects" / "proj" / "sessions").glob("*/"))
    fm, body = parse_frontmatter((session_dir / "session.md").read_text(encoding="utf-8"))
    assert (session_dir / "transcript.md").exists()
    assert "## Transcript" not in body
    assert "## Summary" in body
    assert fm.get("processed") is True


def test_live_wrap_session_is_not_archived(config: Config, vault: VaultManager):
    """The archival branch is tightly gated — a normal (non-imported) session
    with no transcript dump must be untouched by extract_session."""
    from thinkweave.core.schemas import NoteType
    from thinkweave.operations.extract import extract_session

    path = vault.create_note(
        NoteType.SESSION, "live work",
        body="## Summary\n\n## Events\n", project="proj",
    )
    fm0, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    Indexer(config=config).rebuild(full=True)

    extract_session(config, session_id=fm0["id"], summary="did stuff", insights=[], decisions=[])

    assert not (path.parent / "transcript.md").exists()  # no spurious companion


def test_provider_defaults_to_completion_no_anthropic_imposition(
    config: Config, vault: VaultManager, monkeypatch
):
    """With the seeded `overrides: {}`, session synthesis resolves to
    `completion.provider` (openai) — never a hardcoded anthropic."""
    _materialize_one_session(config, vault)
    Indexer(config=config).rebuild(full=True)

    captured: dict = {}

    def fake_batch(prompts, *, provider, model, **kw):
        captured["provider"] = provider
        return [('{"summary": "x", "concepts": [], "insights": [], "decisions": []}', {})
                for _ in prompts]

    monkeypatch.setattr("thinkweave.core.agent_client.batch_completions_sync", fake_batch)

    from thinkweave.onboarding.enrich_batch import run_enrichment_batch

    run_enrichment_batch(config)
    assert captured["provider"] == "openai"
