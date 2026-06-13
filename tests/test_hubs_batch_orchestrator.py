"""Smoke tests for the rewritten hubs/enrich orchestrators.

After C2 (2026-06-06, ``go-back-to-the-scalable-firefly.md``), neither
orchestrator carries provider-Batches submission/poll/fetch logic. Both
delegate to :func:`thinkweave.core.agent_client.batch_completions_sync`.

These tests verify the new contract:

  • dry_run path doesn't issue calls.
  • empty work-list short-circuits cleanly.
  • the orchestrator forwards the api.yaml-resolved provider/model into
    the wrapper.

Heavy end-to-end coverage stays in the existing CLI tests, which are
unchanged and continue to pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Hubs orchestrator
# ---------------------------------------------------------------------------


def test_run_hubs_batch_empty_plan_short_circuits(tmp_path: Path, monkeypatch):
    from thinkweave.core.config import Config
    from thinkweave.operations.hubs_batch import run_hubs_batch

    plan_path = tmp_path / ".weave" / "hubs_plan.json"
    plan_path.parent.mkdir()
    plan_path.write_text(json.dumps({"concepts": []}), encoding="utf-8")

    cfg = Config(vault_root=tmp_path)
    stats = run_hubs_batch(cfg, plan_path=plan_path)
    assert stats == {"applied": 0, "concepts": 0}


def test_run_hubs_batch_missing_plan_exits(tmp_path: Path, monkeypatch):
    from thinkweave.core.config import Config
    from thinkweave.operations.hubs_batch import run_hubs_batch

    cfg = Config(vault_root=tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        run_hubs_batch(cfg, plan_path=tmp_path / "no-such-plan.json")
    assert excinfo.value.code == 1


def test_run_hubs_batch_passes_resolved_provider_to_wrapper(
    tmp_path: Path, monkeypatch
):
    """When ``api.yaml`` declares an override for ``hubs_run``, the
    orchestrator forwards the resolved provider/model into
    ``batch_completions_sync``."""
    # Seed api.yaml with a non-default override.
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "api.yaml").write_text(
        "overrides:\n"
        "  hubs_run:\n"
        "    provider: gemini\n"
        "    model: gemini-2.5-flash\n",
        encoding="utf-8",
    )
    # Seed a plan with one note.
    plan_path = tmp_path / ".weave" / "hubs_plan.json"
    plan_path.parent.mkdir()
    plan_path.write_text(
        json.dumps({
            "concepts": [{
                "concept": "test/concept",
                "unprocessed_notes": [{
                    "path": "notes/n-abc.md",
                    "id": "n-abc",
                    "type": "note",
                    "project": "",
                    "date": "2026-01-01",
                    "title": "T",
                }],
            }],
        }),
        encoding="utf-8",
    )
    # Stub the note file the orchestrator reads from the vault.
    note_dir = tmp_path / "notes"
    note_dir.mkdir()
    (note_dir / "n-abc.md").write_text(
        "---\nid: n-abc\ntype: note\n---\n\nbody\n", encoding="utf-8"
    )

    captured: dict = {}

    def fake_batch(prompts, *, provider, model, **kw):
        captured["provider"] = provider
        captured["model"] = model
        captured["n_prompts"] = len(prompts)
        # Return an empty-text result per prompt — orchestrator then
        # walks (skip), reindexes, returns stats.
        return [("", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
                for _ in prompts]

    monkeypatch.setattr(
        "thinkweave.core.agent_client.batch_completions_sync", fake_batch
    )

    from thinkweave.core.config import Config
    from thinkweave.operations.hubs_batch import run_hubs_batch

    cfg = Config(vault_root=tmp_path)
    stats = run_hubs_batch(cfg, plan_path=plan_path)
    assert captured["provider"] == "gemini"
    assert captured["model"] == "gemini-2.5-flash"
    assert captured["n_prompts"] == 1
    assert stats["concepts"] == 1


def test_run_hubs_batch_dry_run_does_not_call_wrapper(
    tmp_path: Path, monkeypatch
):
    plan_path = tmp_path / ".weave" / "hubs_plan.json"
    plan_path.parent.mkdir()
    plan_path.write_text(
        json.dumps({
            "concepts": [{
                "concept": "x",
                "unprocessed_notes": [{
                    "path": "notes/n-q.md",
                    "id": "n-q",
                    "date": "2026-01-01",
                }],
            }],
        }),
        encoding="utf-8",
    )
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    (notes_dir / "n-q.md").write_text(
        "---\nid: n-q\n---\n\nbody\n", encoding="utf-8"
    )

    called = {"n": 0}

    def fake_batch(*args, **kwargs):
        called["n"] += 1
        return []

    monkeypatch.setattr(
        "thinkweave.core.agent_client.batch_completions_sync", fake_batch
    )

    from thinkweave.core.config import Config
    from thinkweave.operations.hubs_batch import run_hubs_batch

    cfg = Config(vault_root=tmp_path)
    stats = run_hubs_batch(cfg, plan_path=plan_path, dry_run=True)
    assert stats["dry_run"] is True
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# Enrich orchestrator
# ---------------------------------------------------------------------------


def test_run_enrichment_batch_empty_short_circuits(tmp_path: Path, monkeypatch):
    from thinkweave.core.config import Config
    from thinkweave.onboarding.enrich_batch import run_enrichment_batch

    # No vault/projects/ → no pending sessions.
    cfg = Config(vault_root=tmp_path)
    stats = run_enrichment_batch(cfg)
    assert stats["pending"] == 0
    assert stats["enriched"] == 0


def test_run_enrichment_batch_resolves_provider_from_api_yaml(
    tmp_path: Path, monkeypatch
):
    """api.yaml override for ``claude_code_enrich`` flows into the wrapper."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "api.yaml").write_text(
        "overrides:\n"
        "  claude_code_enrich:\n"
        "    provider: openai\n"
        "    model: gpt-5-mini\n",
        encoding="utf-8",
    )

    # Mint one pending session.
    from thinkweave.onboarding.enrich_batch import PendingSession
    sessions = [
        PendingSession(
            note_id="ses-abc",
            project="proj",
            note_path=tmp_path / "ses-abc.md",
            transcript="hello",
            title="T",
        )
    ]
    monkeypatch.setattr(
        "thinkweave.onboarding.enrich_batch.find_pending_sessions",
        lambda *a, **k: sessions,
    )

    captured: dict = {}

    def fake_batch(prompts, *, provider, model, **kw):
        captured["provider"] = provider
        captured["model"] = model
        # Empty JSON => writeback skipped, no decisions/insights created.
        return [('{"decisions": [], "insights": [], "concepts": []}', {})
                for _ in prompts]

    monkeypatch.setattr(
        "thinkweave.core.agent_client.batch_completions_sync", fake_batch
    )
    # Stub the writeback so we don't touch real Indexer/VaultManager.
    monkeypatch.setattr(
        "thinkweave.onboarding.enrich_batch._writeback_one",
        lambda *a, **k: {"decisions_created": 0, "insights_appended": 0},
    )

    from thinkweave.core.config import Config
    from thinkweave.onboarding.enrich_batch import run_enrichment_batch

    cfg = Config(vault_root=tmp_path)
    stats = run_enrichment_batch(cfg)
    assert captured["provider"] == "openai"
    assert captured["model"] == "gpt-5-mini"
    assert stats["submitted"] == 1
    assert stats["enriched"] == 1


def test_run_enrichment_batch_dry_run_does_not_call_wrapper(
    tmp_path: Path, monkeypatch
):
    from thinkweave.onboarding.enrich_batch import PendingSession
    sessions = [
        PendingSession(
            note_id="ses-abc",
            project="p",
            note_path=tmp_path / "x.md",
            transcript="hi",
            title="t",
        )
    ]
    monkeypatch.setattr(
        "thinkweave.onboarding.enrich_batch.find_pending_sessions",
        lambda *a, **k: sessions,
    )
    called = {"n": 0}
    monkeypatch.setattr(
        "thinkweave.core.agent_client.batch_completions_sync",
        lambda *a, **k: (called.__setitem__("n", called["n"] + 1) or []),
    )

    from thinkweave.core.config import Config
    from thinkweave.onboarding.enrich_batch import run_enrichment_batch

    cfg = Config(vault_root=tmp_path)
    stats = run_enrichment_batch(cfg, dry_run=True)
    assert called["n"] == 0
    assert stats["enriched"] == 0


def test_run_enrichment_batch_handles_exception_per_item(
    tmp_path: Path, monkeypatch
):
    """``return_exceptions=True`` from the wrapper surfaces per-item
    failures into stats['errors'] without breaking the batch."""
    from thinkweave.onboarding.enrich_batch import PendingSession
    sessions = [
        PendingSession(note_id=f"ses-{i}", project="p", note_path=tmp_path / f"x{i}.md",
                       transcript="t", title="t") for i in range(3)
    ]
    monkeypatch.setattr(
        "thinkweave.onboarding.enrich_batch.find_pending_sessions",
        lambda *a, **k: sessions,
    )

    def fake_batch(prompts, **kw):
        return [
            ('{"decisions": [], "insights": [], "concepts": []}', {}),
            RuntimeError("rate limit"),
            ('{"decisions": [], "insights": [], "concepts": []}', {}),
        ]

    monkeypatch.setattr(
        "thinkweave.core.agent_client.batch_completions_sync", fake_batch
    )
    monkeypatch.setattr(
        "thinkweave.onboarding.enrich_batch._writeback_one",
        lambda *a, **k: {"decisions_created": 0, "insights_appended": 0},
    )

    from thinkweave.core.config import Config
    from thinkweave.onboarding.enrich_batch import run_enrichment_batch

    cfg = Config(vault_root=tmp_path)
    stats = run_enrichment_batch(cfg)
    assert stats["enriched"] == 2
    assert len(stats["errors"]) == 1
    assert "ses-1" in stats["errors"][0]
