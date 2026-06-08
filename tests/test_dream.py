"""Tests for the dream cycle (operations/dream.py + CLI).

``mem dream`` is the deterministic backbone of ``/dream`` — the cron-friendly
successor to ``/mem-resolve-concepts`` and ``/themes-resolve``. These tests
build a tmp vault with seeded proposed concepts + event-grain source clusters,
then exercise both the scan and apply phases.

Theme surface (post-2026-05-30 teardown): scan emits enriched
``theme_cluster_signals`` (raw ``proposed_names`` tally + ``covering_themes``);
apply mints (``theme_mints``) or extends (``theme_extensions``). No candidate
stubs, no vote winner, no lifecycle/status changes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager, parse_frontmatter
from personal_mem.operations.dream import (
    DreamCycleResult,
    DreamCycleScan,
    PlanValidationError,
    append_maintenance_log,
    apply,
    dream_report_path,
    dream_reports_dir,
    maintenance_log_path,
    recent_dream_reports,
    scan,
    validate_plan_fragment,
    write_dream_report,
)


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def config(vault_dir: Path) -> Config:
    return Config(vault_root=vault_dir)


@pytest.fixture
def vault(config: Config) -> VaultManager:
    vm = VaultManager(config=config)
    vm.ensure_dirs()
    return vm


def _index(config: Config) -> None:
    idx = Indexer(config=config)
    idx.rebuild(full=True)
    idx.close()


def _seed_proposed_concept(vm: VaultManager, term: str, n_notes: int) -> None:
    """Seed ``n_notes`` notes carrying ``term`` in proposed_concepts.

    Uses a non-stopword, non-domain-prefixed term so
    ``filter_promotion_candidates`` lets it through.
    """
    for i in range(n_notes):
        vm.create_note(
            NoteType.NOTE,
            f"note {i}",
            body="body",
            project="t",
            extra_frontmatter={
                "concepts": ["seeded"],
                "proposed_concepts": [term],
            },
        )


def _make_source(
    vm: VaultManager,
    title: str,
    *,
    concepts: list[str],
    source_type: str = "substack",
    proposed_theme: str = "",
) -> Path:
    """Create an event-grain source note (default substack)."""
    extra = {"source_type": source_type, "concepts": concepts}
    if proposed_theme:
        extra["proposed_theme"] = proposed_theme
    return vm.create_note(
        NoteType.SOURCE, title, body=f"# {title}\n", extra_frontmatter=extra
    )


def _src_ids(paths: list[Path]) -> list[str]:
    out = []
    for p in paths:
        fm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
        out.append(fm["id"])
    return out


def _make_active_theme(vm: VaultManager, title: str, *, concepts: list[str]) -> str:
    theme = vm.create_note(
        NoteType.THEME,
        title,
        body="## Essence\n\nx\n\n## Catalyst log\n\n## Open questions\n",
        extra_frontmatter={"concepts": concepts, "status": "active"},
    )
    fm, _ = parse_frontmatter(theme.read_text(encoding="utf-8"))
    return fm["id"]


def _add_catalyst(
    theme_path: Path, *, days_ago: int, citation: str = "n-abcd1234"
) -> None:
    """Append one catalyst entry to a theme's ``## Catalyst log`` section.

    Uses the canonical entry grammar so ``Hub.parse`` reads it back as a
    valid ``HubLogEntry``. The injected line sits between the heading and
    the next ``##`` heading, matching ``extract_section``'s slice.
    """
    from datetime import date as _date, timedelta as _td

    text = theme_path.read_text(encoding="utf-8")
    entry_date = (_date.today() - _td(days=days_ago)).isoformat()
    new_entry = (
        f"- {entry_date} · *new* — Catalyst from {days_ago}d ago — [[{citation}]]"
    )
    text = text.replace(
        "## Catalyst log\n", f"## Catalyst log\n\n{new_entry}\n", 1
    )
    theme_path.write_text(text, encoding="utf-8")


class TestScan:
    def test_returns_structured_plan(
        self, config: Config, vault: VaultManager
    ):
        _seed_proposed_concept(vault, "diagnostics", 6)
        for i in range(3):
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"])
        _index(config)

        result = scan(config, project="t", promotion_cap=20)

        assert isinstance(result, DreamCycleScan)
        assert result.cycle_id.startswith("dream-")
        # promotion-eligible (count >= 5), passes filter_promotion_candidates
        assert any(
            p["concept"] == "diagnostics" for p in result.promotion_candidates
        )
        # event-grain cluster surfaces as a signal
        assert len(result.theme_cluster_signals) >= 1
        # every step contributes a timing entry
        for step in ("drift", "promotion", "theme_cluster_signals"):
            assert step in result.timings
            assert result.timings[step] >= 0.0

    def test_promotion_cap_respected(
        self, config: Config, vault: VaultManager
    ):
        for i in range(10):
            _seed_proposed_concept(vault, f"term-x{i:02d}", 6)
        _index(config)

        result = scan(config, project="t", promotion_cap=3)
        assert len(result.promotion_candidates) <= 3

    def test_empty_vault_no_errors(
        self, config: Config, vault: VaultManager
    ):
        _index(config)
        result = scan(config, project="t")
        assert result.errors == []
        assert result.stats["promotion_candidates"] == 0
        assert result.stats["theme_cluster_signals"] == 0

    def test_signal_carries_raw_proposed_names(
        self, config: Config, vault: VaultManager
    ):
        for i in range(3):
            _make_source(
                vault,
                f"S{i}",
                concepts=["ai-capex", "hyperscaler"],
                proposed_theme="ai-capex-unwind",
            )
        _index(config)
        result = scan(config, project="t")
        sig = result.theme_cluster_signals[0]
        # raw tally, no exact-match winner collapse
        assert sig["proposed_names"]["ai-capex-unwind"] == 3

    def test_covered_cluster_surfaces_as_extend(
        self, config: Config, vault: VaultManager
    ):
        for i in range(3):
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"])
        _make_active_theme(
            vault, "AI capex", concepts=["ai-capex", "hyperscaler"]
        )
        _index(config)
        result = scan(config, project="t")
        ai = [
            s for s in result.theme_cluster_signals
            if "ai-capex" in s["shared_concepts"]
        ]
        assert len(ai) == 1
        assert ai[0]["covering_themes"]
        assert ai[0]["covering_themes"][0]["slug"] == "AI capex"

    def test_active_themes_surface_recent_catalysts(
        self, config: Config, vault: VaultManager
    ):
        """A theme with a recent catalyst surfaces in ``active_themes``."""
        from datetime import date as _date, timedelta as _td

        theme_id = _make_active_theme(
            vault, "AI capex", concepts=["ai-capex", "hyperscaler"]
        )
        # Add one fresh catalyst (today) and one old (60d ago).
        theme_path = next(
            (config.vault_root / "themes").glob("*.md")
        )
        _add_catalyst(theme_path, days_ago=0, citation="n-aaaa1111")
        _add_catalyst(theme_path, days_ago=60, citation="n-bbbb2222")
        _index(config)

        result = scan(config, project="t")
        matching = [
            t for t in result.active_themes if t["theme_id"] == theme_id
        ]
        assert len(matching) == 1
        t = matching[0]
        assert t["title"] == "AI capex"
        # Essence section is pre-loaded — worker doesn't need to mem_read.
        assert "essence" in t
        # Both catalysts surface; last_catalyst_date is today.
        assert t["total_catalysts"] == 2
        assert t["last_catalyst_date"] == _date.today().isoformat()
        # Recent catalysts are sorted newest-first.
        assert len(t["recent_catalysts"]) == 2
        assert t["recent_catalysts"][0]["date"] >= t["recent_catalysts"][1]["date"]

    def test_active_themes_skips_themes_without_recent_activity(
        self, config: Config, vault: VaultManager
    ):
        """A theme whose newest catalyst is older than the cutoff is skipped."""
        _make_active_theme(vault, "Old arc", concepts=["ai-capex"])
        theme_path = next(
            (config.vault_root / "themes").glob("*.md")
        )
        # Catalyst from 60d ago — outside the 30d default window.
        _add_catalyst(theme_path, days_ago=60, citation="n-ccccdddd")
        _index(config)

        result = scan(config, project="t")
        assert result.active_themes == []

    def test_active_themes_skips_non_active_status(
        self, config: Config, vault: VaultManager
    ):
        """A theme with ``status: merged-into:...`` is skipped even with recent catalysts."""
        # Build a merged theme directly so we control the status frontmatter.
        merged = vault.create_note(
            NoteType.THEME,
            "Merged arc",
            body="## Essence\n\nx\n\n## Catalyst log\n\n## Open questions\n",
            extra_frontmatter={
                "concepts": ["ai-capex"],
                "status": "merged-into:thm-aaaa1111",
            },
        )
        _add_catalyst(merged, days_ago=1, citation="n-eeee3333")
        _index(config)

        result = scan(config, project="t")
        assert result.active_themes == []

    def test_active_themes_empty_when_no_themes(
        self, config: Config, vault: VaultManager
    ):
        """Vault with zero themes ⇒ active_themes is empty, no errors."""
        _index(config)
        result = scan(config, project="t")
        assert result.active_themes == []
        assert result.stats["active_themes"] == 0
        assert "active_themes" in result.timings

    # --- unwrapped_sessions surface (phase-2 wrap-worker input) ----------

    def test_unwrapped_sessions_surfaces_session_with_events(
        self, config: Config, vault: VaultManager
    ):
        """Session with a non-empty ``events.jsonl`` and no ``processed:`` flag surfaces."""
        sess_path = vault.create_note(
            NoteType.SESSION,
            "live session",
            body="# live session\n",
            project="t",
        )
        # Pad the events file to >0 bytes — the scan's "non-empty" test.
        (sess_path.parent / "events.jsonl").write_text(
            '{"type":"prompt","text":"hi","session_id":"x","ts":"2026-06-06T00:00:00Z"}\n',
            encoding="utf-8",
        )
        _index(config)

        result = scan(config, project="t")
        ids = [e["session_id"] for e in result.unwrapped_sessions]
        fm, _ = parse_frontmatter(sess_path.read_text(encoding="utf-8"))
        assert fm["id"] in ids
        entry = next(e for e in result.unwrapped_sessions if e["session_id"] == fm["id"])
        assert entry["events_jsonl_path"].endswith("events.jsonl")
        assert entry["project"] == "t"
        assert "unwrapped_sessions" in result.timings
        assert result.stats["unwrapped_sessions"] >= 1

    def test_unwrapped_sessions_skips_processed_sessions(
        self, config: Config, vault: VaultManager
    ):
        """A session with ``processed: true`` is NOT a candidate."""
        sess_path = vault.create_note(
            NoteType.SESSION,
            "wrapped session",
            body="# wrapped\n",
            project="t",
            extra_frontmatter={"processed": True},
        )
        (sess_path.parent / "events.jsonl").write_text(
            '{"type":"prompt","text":"hi"}\n', encoding="utf-8"
        )
        _index(config)

        result = scan(config, project="t")
        fm, _ = parse_frontmatter(sess_path.read_text(encoding="utf-8"))
        ids = [e["session_id"] for e in result.unwrapped_sessions]
        assert fm["id"] not in ids

    def test_unwrapped_sessions_skips_missing_events(
        self, config: Config, vault: VaultManager
    ):
        """Conservative default: no ``events.jsonl`` ⇒ already wrapped."""
        vault.create_note(
            NoteType.SESSION,
            "empty session",
            body="# empty\n",
            project="t",
        )
        # No events.jsonl written deliberately.
        _index(config)
        result = scan(config, project="t")
        assert result.unwrapped_sessions == []

    def test_unwrapped_sessions_empty_vault_no_errors(
        self, config: Config, vault: VaultManager
    ):
        _index(config)
        result = scan(config, project="t")
        assert result.unwrapped_sessions == []
        assert result.errors == []
        assert result.stats["unwrapped_sessions"] == 0
        assert "unwrapped_sessions" in result.timings

    # --- rejudge_queue surface (phase-2 judge-worker input) --------------

    def test_rejudge_queue_drains_disk_entries(
        self, config: Config, vault: VaultManager
    ):
        """Entries on ``.mem/rejudge_queue.jsonl`` surface in the scan."""
        queue_path = config.vault_root / ".mem" / "rejudge_queue.jsonl"
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text(
            json.dumps({
                "decision_id": "dec-abc1234",
                "predecessor_decision_id": "dec-def5678",
                "queued_at": "2026-06-05T00:00:00+00:00",
                "reason": "superseded",
            }) + "\n",
            encoding="utf-8",
        )
        _index(config)
        result = scan(config, project="t")
        ids = [e["decision_id"] for e in result.rejudge_queue]
        assert "dec-abc1234" in ids
        entry = next(e for e in result.rejudge_queue if e["decision_id"] == "dec-abc1234")
        assert entry["reason"] == "superseded"
        assert entry["predecessor_decision_id"] == "dec-def5678"
        assert "rejudge_queue" in result.timings

    def test_rejudge_queue_surfaces_stale_pending_decisions(
        self, config: Config, vault: VaultManager
    ):
        """A pending verdict whose ``judged_at`` is older than 7d surfaces."""
        from datetime import datetime, timedelta, timezone

        old_ts = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat()
        vault.create_note(
            NoteType.DECISION,
            "stale prediction",
            body="# stale\n",
            project="t",
            extra_frontmatter={
                "predicted_outcome": "X will happen",
                "prediction_match": "pending",
                "judged_at": old_ts,
            },
        )
        _index(config)
        result = scan(config, project="t")
        # Look for the stale entry (fabricated by the scan helper).
        stale = [e for e in result.rejudge_queue if e["reason"] == "stale_pending"]
        assert len(stale) >= 1
        assert stale[0]["predecessor_decision_id"] is None

    def test_rejudge_queue_empty_no_errors(
        self, config: Config, vault: VaultManager
    ):
        _index(config)
        result = scan(config, project="t")
        assert result.rejudge_queue == []
        assert result.errors == []
        assert result.stats["rejudge_queue"] == 0
        assert "rejudge_queue" in result.timings

    # --- knowledge_delta surface (phase-2 digest-worker input) -----------

    def test_knowledge_delta_collects_recent_landings(
        self, config: Config, vault: VaultManager
    ):
        """Sources created in the last 24h appear in the right grain slice.

        Post-2026-06-07 grain split: news is event-grain
        (``temporal_grain='event'`` on its SourceTypeSpec), so the landing
        surfaces in ``knowledge_delta['event']['landings_24h']``, not the
        concept slice.
        """
        _make_source(
            vault,
            "Today's landing",
            concepts=["ai-capex"],
            source_type="news",
        )
        _index(config)
        result = scan(config, project="t")
        event_landings = result.knowledge_delta["event"]["landings_24h"]
        titles = [l["title"] for l in event_landings]
        assert "Today's landing" in titles
        # Concept slice didn't capture this event-grain landing.
        concept_titles = [
            l["title"]
            for l in result.knowledge_delta["concept"]["landings_24h"]
        ]
        assert "Today's landing" not in concept_titles
        assert "window_start" in result.knowledge_delta
        assert "window_end" in result.knowledge_delta
        # theme_mutations_this_cycle is the orchestrator's slot — empty on
        # each grain initially.
        assert result.knowledge_delta["event"]["theme_mutations_this_cycle"] == {
            "theme_mints": [],
            "theme_extensions": [],
        }
        assert result.knowledge_delta["concept"]["theme_mutations_this_cycle"] == {
            "theme_mints": [],
            "theme_extensions": [],
        }

    def test_knowledge_delta_skips_old_landings(
        self, config: Config, vault: VaultManager
    ):
        """A source whose date is outside the 24h window does NOT surface."""
        path = _make_source(
            vault,
            "ancient landing",
            concepts=["ai-capex"],
        )
        # Backdate via frontmatter rewrite — bypasses the create_note
        # default which uses now().
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            "date:",
            "date: '2020-01-01T00:00:00+00:00'\n#orig_date:",
            1,
        )
        # That replacement is a bit aggressive — rewrite frontmatter cleanly.
        from personal_mem.core.vault import parse_frontmatter as _pf
        fm, body = _pf(path.read_text(encoding="utf-8"))
        fm["date"] = "2020-01-01T00:00:00+00:00"
        import yaml as _yaml
        new = "---\n" + _yaml.safe_dump(fm, sort_keys=False) + "---\n" + body
        path.write_text(new, encoding="utf-8")
        _index(config)
        result = scan(config, project="t")
        # Check both slices — substack is event-grain by default.
        event_titles = [
            l["title"]
            for l in result.knowledge_delta["event"]["landings_24h"]
        ]
        concept_titles = [
            l["title"]
            for l in result.knowledge_delta["concept"]["landings_24h"]
        ]
        assert "ancient landing" not in event_titles
        assert "ancient landing" not in concept_titles

    def test_knowledge_delta_empty_vault_no_errors(
        self, config: Config, vault: VaultManager
    ):
        _index(config)
        result = scan(config, project="t")
        kd = result.knowledge_delta
        for grain in ("concept", "event"):
            assert kd[grain]["landings_24h"] == []
            assert kd[grain]["catalyst_additions_24h"] == []
            assert kd[grain]["probe_matches_24h"] == []
            assert kd[grain]["verdict_flips_24h"] == []
            assert kd[grain]["predictions_landed_24h"] == []
        assert result.errors == []
        assert "knowledge_delta" in result.timings
        # stats records the sub-counts per grain
        assert isinstance(result.stats["knowledge_delta"], dict)
        assert result.stats["knowledge_delta"]["concept"]["landings_24h"] == 0
        assert result.stats["knowledge_delta"]["event"]["landings_24h"] == 0

    def test_knowledge_delta_collects_recent_catalyst_additions(
        self, config: Config, vault: VaultManager
    ):
        """A theme catalyst line surfaces on the event slice.

        Post-2026-06-07 grain split: theme hubs are event-grain by
        construction (``hub_kind == 'theme'``), so the catalyst lands in
        ``knowledge_delta['event']['catalyst_additions_24h']``.
        """
        _make_active_theme(vault, "AI capex", concepts=["ai-capex"])
        theme_path = next((config.vault_root / "themes").glob("*.md"))
        _add_catalyst(theme_path, days_ago=0, citation="n-aaaa1111")
        _index(config)

        result = scan(config, project="t")
        adds = result.knowledge_delta["event"]["catalyst_additions_24h"]
        cited = [a for a in adds if a["cited_note_id"] == "n-aaaa1111"]
        assert len(cited) == 1
        assert cited[0]["hub_kind"] == "theme"
        # Concept slice didn't pick it up.
        concept_adds = result.knowledge_delta["concept"]["catalyst_additions_24h"]
        assert not any(a["cited_note_id"] == "n-aaaa1111" for a in concept_adds)

    def test_knowledge_delta_grain_routing_concept_landing(
        self, config: Config, vault: VaultManager
    ):
        """Paper (concept-grain) lands on the concept slice only."""
        _make_source(
            vault,
            "Today's paper",
            concepts=["fts5"],
            source_type="paper",
        )
        _index(config)
        result = scan(config, project="t")
        concept_titles = [
            l["title"]
            for l in result.knowledge_delta["concept"]["landings_24h"]
        ]
        event_titles = [
            l["title"]
            for l in result.knowledge_delta["event"]["landings_24h"]
        ]
        assert "Today's paper" in concept_titles
        assert "Today's paper" not in event_titles


class TestApply:
    def test_promotes_proposed_concept(
        self, config: Config, vault: VaultManager
    ):
        _seed_proposed_concept(vault, "diagnostics", 6)
        _index(config)

        plan = {
            "promotions": [{"concept": "diagnostics", "domain": "swe"}],
        }
        result = apply(config, plan=plan, project="t")

        assert isinstance(result, DreamCycleResult)
        assert result.promotions_applied == 1
        # ontology was updated
        from personal_mem.synthesis.concepts import load_ontology

        ontology = load_ontology()
        all_terms = {t.lower() for terms in ontology.values() for t in terms}
        assert "diagnostics" in all_terms

    def test_mints_theme(self, config: Config, vault: VaultManager):
        paths = [
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"])
            for i in range(3)
        ]
        _index(config)
        plan = {
            "theme_mints": [
                {
                    "slug": "ai-capex-unwind",
                    "essence": "AI capex pulls back.",
                    "source_ids": _src_ids(paths),
                    "concepts": ["ai-capex", "hyperscaler"],
                }
            ]
        }
        result = apply(config, plan=plan, project="t")
        assert result.themes_minted == 1
        # Theme files are pure-slug (slug.md); the thm-id lives in frontmatter.
        themes = list((config.vault_root / "themes").glob("*.md"))
        assert len(themes) == 1
        # sources got relates_to backfilled
        fm, _ = parse_frontmatter(paths[0].read_text(encoding="utf-8"))
        assert any(r.startswith("thm-") for r in (fm.get("relates_to") or []))

    def test_extends_theme(self, config: Config, vault: VaultManager):
        theme_id = _make_active_theme(
            vault, "AI capex", concepts=["ai-capex", "hyperscaler"]
        )
        paths = [
            _make_source(vault, f"S{i}", concepts=["ai-capex", "hyperscaler"])
            for i in range(3)
        ]
        _index(config)
        plan = {
            "theme_extensions": [
                {
                    "theme_id": theme_id,
                    "source_ids": _src_ids(paths),
                    "reason": "new drops",
                }
            ]
        }
        result = apply(config, plan=plan, project="t")
        assert result.themes_extended == 1
        fm, _ = parse_frontmatter(paths[0].read_text(encoding="utf-8"))
        assert theme_id in (fm.get("relates_to") or [])

    def test_theme_mint_missing_fields_is_error(
        self, config: Config, vault: VaultManager
    ):
        _index(config)
        plan = {"theme_mints": [{"essence": "no slug"}]}
        result = apply(config, plan=plan, project="t")
        assert any("theme_mint" in e for e in result.errors)
        assert result.themes_minted == 0

    def test_theme_extend_missing_fields_is_error(
        self, config: Config, vault: VaultManager
    ):
        _index(config)
        plan = {"theme_extensions": [{"reason": "no ids"}]}
        result = apply(config, plan=plan, project="t")
        assert any("theme_extend" in e for e in result.errors)
        assert result.themes_extended == 0

    def test_empty_plan_runs_clean(
        self, config: Config, vault: VaultManager
    ):
        _index(config)
        result = apply(config, plan={}, project="t")
        assert result.errors == []
        assert result.promotions_applied == 0
        # No structural changes ⇒ no index step ⇒ index timing still recorded
        assert "index" in result.timings

    def test_logs_to_maintenance_jsonl(
        self, config: Config, vault: VaultManager
    ):
        _seed_proposed_concept(vault, "diagnostics", 6)
        _index(config)

        plan = {"promotions": [{"concept": "diagnostics", "domain": "swe"}]}
        result = apply(config, plan=plan, project="t")

        log_path = maintenance_log_path(config)
        assert log_path.exists()
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["cycle_id"] == result.cycle_id
        assert entry["summary"]["promotions"] == 1
        assert "ontology_grew" in entry["summary"]
        assert "ts" in entry

    def test_repeat_promotion_is_sweep_only(
        self, config: Config, vault: VaultManager, monkeypatch
    ):
        """Re-promoting a canonical concept is a sweep, not growth."""
        monkeypatch.setenv("PERSONAL_MEM_VAULT", str(config.vault_root))
        _seed_proposed_concept(vault, "diagnostics", 6)
        _index(config)

        plan = {"promotions": [{"concept": "diagnostics", "domain": "swe"}]}

        first = apply(config, plan=plan, project="t")
        assert first.promotions_applied == 1
        assert first.ontology_grew is True

        hubs_dir = config.vault_root / "concepts" / "topics"
        hubs_after_first = (
            set(hubs_dir.glob("*.md")) if hubs_dir.exists() else set()
        )

        _seed_proposed_concept(vault, "diagnostics", 1)
        _index(config)
        second = apply(config, plan=plan, project="t")

        assert second.promotions_applied == 1
        assert second.ontology_grew is False, (
            "second-pass promotion of a canonical term must not trigger "
            "hub regeneration — that's the routine speed win"
        )
        hubs_after_second = (
            set(hubs_dir.glob("*.md")) if hubs_dir.exists() else set()
        )
        assert hubs_after_second == hubs_after_first

    def test_new_concept_grows_ontology(
        self, config: Config, vault: VaultManager, monkeypatch
    ):
        """Promoting a term *not* in the seed ontology flips ``ontology_grew``."""
        monkeypatch.setenv("PERSONAL_MEM_VAULT", str(config.vault_root))
        _seed_proposed_concept(vault, "synaptic-pruning-2026", 6)
        _index(config)

        plan = {
            "promotions": [
                {"concept": "synaptic-pruning-2026", "domain": "ml-novelty"}
            ]
        }
        result = apply(config, plan=plan, project="t")

        assert result.promotions_applied == 1
        assert result.ontology_grew is True
        hub_path = (
            config.vault_root / "concepts" / "topics" / "synaptic-pruning-2026.md"
        )
        assert hub_path.exists()

    def test_essence_rewrite_actually_writes(
        self, config: Config, vault: VaultManager
    ):
        """An entry with ``new_essence`` rewrites the theme's ``## Essence`` section.

        The surrounding sections (``## Catalyst log``, ``## Open questions``)
        and frontmatter must remain intact.
        """
        theme_id = _make_active_theme(
            vault, "AI capex", concepts=["ai-capex"]
        )
        theme_path = next((config.vault_root / "themes").glob("*.md"))
        # Seed a catalyst so we can verify it's preserved through the rewrite.
        _add_catalyst(theme_path, days_ago=1, citation="n-keepme1")
        _index(config)

        before = theme_path.read_text(encoding="utf-8")
        assert "## Catalyst log" in before
        assert "## Open questions" in before

        new_essence = "Capex is unwinding fast. Margins compress as supply rises."
        plan = {
            "essence_rewrites": [
                {
                    "theme_id": theme_id,
                    "new_essence": new_essence,
                    "reason": "recent catalysts contradict prior framing",
                }
            ]
        }
        result = apply(config, plan=plan, project="t")
        assert result.essence_rewrites_applied == 1
        assert result.errors == []

        after = theme_path.read_text(encoding="utf-8")
        # Essence body replaced
        assert new_essence in after
        # Surrounding sections preserved
        assert "## Catalyst log" in after
        assert "## Open questions" in after
        # The seeded catalyst citation still present
        assert "n-keepme1" in after
        # Frontmatter intact
        fm, _ = parse_frontmatter(after)
        assert fm.get("id") == theme_id
        assert fm.get("status") == "active"

    def test_essence_rewrite_without_new_essence_is_log_only(
        self, config: Config, vault: VaultManager
    ):
        """Legacy entries (no ``new_essence``) are counted but don't mutate.

        Back-compat: the pre-2026-06-06 shape was ``{theme_id, reason}``.
        """
        theme_id = _make_active_theme(
            vault, "AI capex", concepts=["ai-capex"]
        )
        theme_path = next((config.vault_root / "themes").glob("*.md"))
        _index(config)

        before = theme_path.read_text(encoding="utf-8")
        plan = {
            "essence_rewrites": [
                {"theme_id": theme_id, "reason": "noted only"}
            ]
        }
        result = apply(config, plan=plan, project="t")
        assert result.essence_rewrites_applied == 1
        assert result.errors == []
        # File is byte-for-byte unchanged.
        after = theme_path.read_text(encoding="utf-8")
        assert before == after


class TestMaintenanceLog:
    def test_append_creates_directory_and_file(
        self, config: Config, vault: VaultManager
    ):
        entry = {"cycle_id": "dream-test", "summary": {}, "ts": "2026-05-23"}
        path = append_maintenance_log(config, entry)
        assert path.exists()
        assert path.parent.name == ".mem"
        loaded = json.loads(path.read_text(encoding="utf-8").strip())
        assert loaded["cycle_id"] == "dream-test"

    def test_append_is_idempotent_per_line(
        self, config: Config, vault: VaultManager
    ):
        for i in range(3):
            append_maintenance_log(config, {"cycle_id": f"dream-{i}"})
        path = maintenance_log_path(config)
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3


class TestDreamReport:
    """Per-cycle markdown report at vault/.mem/dream_reports/<cycle_id>.md."""

    def test_summary_table_always_rendered(
        self, config: Config, vault: VaultManager
    ):
        result = DreamCycleResult(cycle_id="dream-test", project="t")
        path = write_dream_report(config, result, plan={})
        assert path.exists()
        assert path == dream_report_path(config, "dream-test")

        body = path.read_text(encoding="utf-8")
        assert "# Dream cycle `dream-test`" in body
        assert "## Summary" in body
        assert "Concept merges" in body
        assert "Notes indexed" in body
        # Empty sections skipped
        assert "## Concept merges" not in body
        assert "## Themes minted" not in body

    def test_populated_plan_emits_per_action_sections(
        self, config: Config, vault: VaultManager
    ):
        result = DreamCycleResult(
            cycle_id="dream-x",
            project="t",
            merges_applied=1,
            promotions_applied=2,
            themes_minted=1,
            themes_extended=1,
            essence_rewrites_applied=1,
        )
        plan = {
            "merges": [{"from": "fastapi", "to": "api", "reason": "subset"}],
            "promotions": [
                {"concept": "diagnostics", "domain": "swe", "reason": "hit"},
                {"concept": "alpha", "domain": "finance", "reason": "hit"},
            ],
            "theme_mints": [
                {
                    "slug": "ai-capex",
                    "essence": "AI infra capex unwind.",
                    "source_ids": ["src-a", "src-b", "src-c"],
                    "concepts": ["semis", "data-center"],
                }
            ],
            "theme_extensions": [
                {"theme_id": "thm-Y", "source_ids": ["src-d"], "reason": "more"}
            ],
            "essence_rewrites": [
                {"theme_id": "thm-Z", "reason": "tightened"}
            ],
        }
        path = write_dream_report(config, result, plan=plan)
        body = path.read_text(encoding="utf-8")

        assert "## Concept merges (1)" in body
        assert "fastapi → api" in body
        assert "## Concept promotions (2)" in body
        assert "diagnostics" in body and "alpha" in body
        assert "## Themes minted (1)" in body
        assert "ai-capex" in body
        assert "## Themes extended (1)" in body
        assert "## Essence rewrites (1)" in body

    def test_errors_section_emits_when_present(
        self, config: Config, vault: VaultManager
    ):
        result = DreamCycleResult(cycle_id="dream-err", project="t")
        result.errors.append("merges: boom")
        path = write_dream_report(config, result, plan={})
        body = path.read_text(encoding="utf-8")
        assert "## Errors (1)" in body
        assert "merges: boom" in body

    def test_apply_writes_report_and_populates_result_field(
        self, config: Config, vault: VaultManager
    ):
        result = apply(config, plan={}, project="t", cycle_id="dream-e2e")
        assert result.report_path
        assert Path(result.report_path).exists()
        assert Path(result.report_path).name == "dream-e2e.md"

    def test_recent_dream_reports_returns_newest_first(
        self, config: Config, vault: VaultManager
    ):
        import time

        for cid in ("dream-old", "dream-mid", "dream-new"):
            r = DreamCycleResult(cycle_id=cid, project="t")
            write_dream_report(config, r, plan={})
            time.sleep(0.01)

        recent = recent_dream_reports(config, n=2)
        assert len(recent) == 2
        assert recent[0]["cycle_id"] == "dream-new"
        assert recent[1]["cycle_id"] == "dream-mid"

    def test_recent_dream_reports_empty_when_no_dir(
        self, config: Config, vault: VaultManager
    ):
        assert not dream_reports_dir(config).exists()
        assert recent_dream_reports(config, n=3) == []


class TestStateOfPlayMaintenance:
    """state_of_play surfaces recent dream reports under 'Recent Maintenance'."""

    def test_no_section_when_no_reports(
        self, config: Config, vault: VaultManager
    ):
        from personal_mem.synthesis.landing import state_of_play

        _index(config)
        out = state_of_play(config, "t")
        assert "Recent Maintenance" not in out

    def test_section_present_with_link_when_report_exists(
        self, config: Config, vault: VaultManager
    ):
        from personal_mem.synthesis.landing import state_of_play

        r = DreamCycleResult(cycle_id="dream-state-test", project="t")
        write_dream_report(config, r, plan={})

        _index(config)
        out = state_of_play(config, "t")
        assert "## Recent Maintenance" in out
        assert "dream-state-test" in out
        assert "reports/dream/dream-state-test.md" in out


class TestDreamCLI:
    def test_scan_json_output_parses(
        self, config: Config, vault: VaultManager, monkeypatch, capsys
    ):
        _seed_proposed_concept(vault, "diagnostics", 6)
        _index(config)

        monkeypatch.setenv("PERSONAL_MEM_VAULT", str(config.vault_root))
        monkeypatch.setenv("PERSONAL_MEM_PROJECT", "t")

        from personal_mem.surfaces.cli.dream import cmd_dream

        args = type(
            "Args",
            (),
            {
                "dream_action": "scan",
                "project": "t",
                "promotion_cap": 20,
                "promotion_threshold": 5,
                "json": True,
            },
        )()
        try:
            cmd_dream(args)
        except SystemExit as e:
            assert e.code == 0

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "cycle_id" in payload
        assert "promotion_candidates" in payload

    def test_apply_dry_run_does_not_write(
        self, config: Config, vault: VaultManager, monkeypatch, capsys, tmp_path
    ):
        _seed_proposed_concept(vault, "diagnostics", 6)
        _index(config)
        monkeypatch.setenv("PERSONAL_MEM_VAULT", str(config.vault_root))
        monkeypatch.setenv("PERSONAL_MEM_PROJECT", "t")

        plan_path = tmp_path / "plan.json"
        plan_path.write_text(
            json.dumps(
                {"promotions": [{"concept": "diagnostics", "domain": "swe"}]}
            ),
            encoding="utf-8",
        )

        from personal_mem.surfaces.cli.dream import cmd_dream

        args = type(
            "Args",
            (),
            {
                "dream_action": "apply",
                "project": "t",
                "plan": str(plan_path),
                "dry_run": True,
                "json": True,
            },
        )()
        with pytest.raises(SystemExit) as exc:
            cmd_dream(args)
        assert exc.value.code == 0

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["dry_run"] is True
        assert payload["would_apply"]["promotions"] == 1
        assert not maintenance_log_path(config).exists()


# --- Priority signals (Slice 1.5) -------------------------------------------


def _seed_probe(config: Config, project: str, text: str) -> None:
    """Write a single probe-classified prompt event in ``project``'s
    session JSONL. Uses a recent timestamp so the 14-day window catches it."""
    import datetime as _dt
    sess_dir = config.vault_root / "projects" / project / "sessions" / "ses-ps"
    sess_dir.mkdir(parents=True, exist_ok=True)
    now = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)
    (sess_dir / "events.jsonl").write_text(
        json.dumps({
            "type": "prompt", "text": text,
            "session_id": "cc-ps", "ts": now.isoformat(),
        }) + "\n",
        encoding="utf-8",
    )


class TestPrioritySignalsScan:
    def test_scan_attaches_recent_probes(
        self, config: Config, vault: VaultManager
    ):
        # llm is canonical in the shipped ontology — a probe touching
        # it lands in recent_probes.
        _index(config)
        _seed_probe(config, "t", "How does the llm choose?")
        result = scan(config, project="t", promotion_cap=20)
        assert result.recent_probes.get("llm", 0) == 1
        assert result.stats.get("recent_probes", 0) == 1


class TestPrioritySignalsApply:
    """Apply phase's 3d step: split on action + gate. Errors don't
    cascade — a bad signal logs an error and the next ones still run."""

    def test_log_action_counts_logged(
        self, config: Config, vault: VaultManager
    ):
        plan = {
            "priority_signals": [
                {"concept": "llm", "probe_count": 3,
                 "action": "log", "reason": "well sourced"},
            ],
        }
        r = apply(config, plan=plan, project="t")
        assert r.priority_signals_enqueued == 0
        assert r.priority_signals_logged == 1
        assert r.errors == []

    def test_enqueue_with_gate_disabled_counts_logged(
        self, config: Config, vault: VaultManager
    ):
        # Default config: dream_enqueue_priority_signals is False.
        assert config.dream_enqueue_priority_signals is False
        plan = {
            "priority_signals": [
                {"concept": "dynamic-batching", "probe_count": 3,
                 "action": "enqueue",
                 "queue_item": {
                     "source_type": "article",
                     "title": "Survey", "concept": "dynamic-batching",
                 },
                 "reason": "asked 3x, no coverage"},
            ],
        }
        r = apply(config, plan=plan, project="t")
        # Gate disabled → counts as logged, no queue mutation.
        assert r.priority_signals_enqueued == 0
        assert r.priority_signals_logged == 1
        queue_file = config.vault_root / ".mem" / "queues" / "article.jsonl"
        assert not queue_file.exists()

    def test_enqueue_with_gate_hot_writes_queue(
        self, config: Config, vault: VaultManager
    ):
        config.dream_enqueue_priority_signals = True
        plan = {
            "priority_signals": [
                {"concept": "dynamic-batching", "probe_count": 4,
                 "action": "enqueue",
                 "queue_item": {
                     "source_type": "article",
                     "title": "Survey on dynamic-batching",
                     "concept": "dynamic-batching",
                     "source": "dream-priority-signal",
                 },
                 "reason": "asked 4x, no coverage"},
            ],
        }
        r = apply(config, plan=plan, project="t")
        assert r.priority_signals_enqueued == 1
        assert r.priority_signals_logged == 0
        queue_file = config.vault_root / ".mem" / "queues" / "article.jsonl"
        assert queue_file.exists()
        lines = [
            json.loads(line)
            for line in queue_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 1
        assert lines[0]["title"] == "Survey on dynamic-batching"
        assert lines[0]["concept"] == "dynamic-batching"

    def test_enqueue_missing_source_type_logs_error(
        self, config: Config, vault: VaultManager
    ):
        config.dream_enqueue_priority_signals = True
        plan = {
            "priority_signals": [
                {"concept": "x", "probe_count": 2,
                 "action": "enqueue",
                 "queue_item": {"title": "no source_type"},
                 "reason": "x"},
            ],
        }
        r = apply(config, plan=plan, project="t")
        assert r.priority_signals_enqueued == 0
        assert r.priority_signals_logged == 1
        assert any("source_type" in e for e in r.errors)

    def test_log_entry_carries_both_counters(
        self, config: Config, vault: VaultManager
    ):
        config.dream_enqueue_priority_signals = True
        plan = {
            "priority_signals": [
                {"concept": "llm", "probe_count": 3, "action": "log",
                 "reason": "well sourced"},
                {"concept": "dynamic-batching", "probe_count": 4,
                 "action": "enqueue",
                 "queue_item": {"source_type": "article",
                                "title": "Survey", "concept": "dynamic-batching"},
                 "reason": "asked 4x"},
            ],
        }
        r = apply(config, plan=plan, project="t")
        entry = r.log_entry(plan)
        assert entry["summary"]["priority_signals_enqueued"] == 1
        assert entry["summary"]["priority_signals_logged"] == 1


class TestPrioritySignalsReport:
    def test_renders_what_i_queued_and_noted(
        self, config: Config, vault: VaultManager
    ):
        config.dream_enqueue_priority_signals = True
        plan = {
            "priority_signals": [
                {"concept": "llm", "probe_count": 3, "action": "log",
                 "reason": "well sourced"},
                {"concept": "dynamic-batching", "probe_count": 4,
                 "action": "enqueue",
                 "queue_item": {"source_type": "article",
                                "title": "Survey on dynamic-batching",
                                "concept": "dynamic-batching"},
                 "reason": "asked 4x"},
            ],
        }
        r = apply(config, plan=plan, project="t")
        report = (config.vault_root / "reports" / "dream"
                  / f"{r.cycle_id}.md").read_text(encoding="utf-8")
        assert "What I queued" in report
        assert "dynamic-batching" in report
        assert "Survey on dynamic-batching" in report
        assert "What I noted" in report
        assert "llm" in report
        assert "| Priority signals enqueued | 1 |" in report
        assert "| Priority signals logged | 1 |" in report


# --- Plan-fragment validation (Item 3) --------------------------------------


class TestPlanValidation:
    """``validate_plan_fragment`` + strict-mode wiring on ``apply``.

    The pair catches worker drift that previously no-opped silently — the
    2026-06 examples were ``add_source_ids`` for ``source_ids`` inside
    ``theme_extensions`` and ``rationale`` for ``essence`` inside
    ``theme_mints``. Strict mode raises; non-strict mode logs onto
    ``result.errors`` and still runs the rest of apply.
    """

    def test_clean_plan_yields_no_warnings(self):
        plan = {
            "merges": [{"from": "fastapi", "to": "api", "reason": "subset"}],
            "promotions": [{"concept": "diagnostics", "domain": "swe"}],
            "theme_mints": [
                {
                    "slug": "ai-capex-unwind",
                    "essence": "Capex pulls back.",
                    "source_ids": ["src-a", "src-b"],
                    "concepts": ["ai-capex"],
                }
            ],
            "theme_extensions": [
                {"theme_id": "thm-X", "source_ids": ["src-c"], "reason": "more"}
            ],
            "essence_rewrites": [
                {"theme_id": "thm-Y", "new_essence": "tighter", "reason": "ok"}
            ],
            "priority_signals": [
                {
                    "concept": "llm",
                    "probe_count": 3,
                    "action": "enqueue",
                    "queue_item": {
                        "source_type": "article",
                        "title": "Survey",
                        "concept": "llm",
                    },
                    "reason": "asked 3x",
                }
            ],
            "cycle_id": "dream-test",
        }
        assert validate_plan_fragment(plan) == []

    def test_unknown_top_level_key_warns(self):
        """Top-level drift — e.g. an old plan key surfacing in a fragment."""
        plan = {
            "promotions": [{"concept": "diagnostics", "domain": "swe"}],
            "theme_status_changes": [{"theme_id": "thm-X", "status": "dormant"}],
        }
        warnings = validate_plan_fragment(plan)
        assert any("theme_status_changes" in w for w in warnings)

    def test_unknown_sub_key_warns_with_index(self):
        """Sub-key drift inside ``theme_extensions`` — the real-world bug.

        ``add_source_ids`` instead of ``source_ids`` is the case that
        silently no-opped before the gate landed.
        """
        plan = {
            "theme_extensions": [
                {
                    "theme_id": "thm-X",
                    "add_source_ids": ["src-D", "src-E"],
                    "reason": "...",
                }
            ]
        }
        warnings = validate_plan_fragment(plan)
        # The warning must name the offending sub-key AND its position.
        assert any(
            "add_source_ids" in w and "theme_extensions" in w
            for w in warnings
        )

    def test_unknown_sub_key_in_theme_mints_warns(self):
        """``rationale`` instead of ``essence`` — the other real-world drift."""
        plan = {
            "theme_mints": [
                {
                    "slug": "iran-war",
                    "rationale": "1-sentence narrative description.",
                    "source_ids": ["src-A"],
                }
            ]
        }
        warnings = validate_plan_fragment(plan)
        assert any(
            "rationale" in w and "theme_mints" in w for w in warnings
        )

    def test_unknown_queue_item_sub_key_warns(self):
        plan = {
            "priority_signals": [
                {
                    "concept": "x",
                    "probe_count": 2,
                    "action": "enqueue",
                    "queue_item": {"source_type": "article", "bogus_key": "x"},
                    "reason": "x",
                }
            ]
        }
        warnings = validate_plan_fragment(plan)
        assert any("bogus_key" in w and "queue_item" in w for w in warnings)

    def test_apply_strict_default_raises_on_drift(
        self, config: Config, vault: VaultManager
    ):
        """Strict mode is ON by default — drift aborts apply."""
        plan = {
            "theme_extensions": [
                {"theme_id": "thm-X", "add_source_ids": ["src-D"]},
            ]
        }
        with pytest.raises(PlanValidationError) as exc:
            apply(config, plan=plan, project="t")
        assert any("add_source_ids" in w for w in exc.value.warnings)
        # No maintenance log line was written — strict mode aborts upfront.
        assert not maintenance_log_path(config).exists()

    def test_apply_non_strict_records_drift_and_runs(
        self, config: Config, vault: VaultManager
    ):
        """Non-strict mode logs the drift onto ``errors`` but still runs.

        The unknown sub-key is silently ignored at the apply step (the loop
        reads only canonical keys), but the warning surfaces on
        ``result.errors`` so a human grepping the maintenance log can see it.
        """
        _seed_proposed_concept(vault, "diagnostics", 6)
        _index(config)
        plan = {
            # canonical key, will succeed
            "promotions": [{"concept": "diagnostics", "domain": "swe"}],
            # drifted key, surfaces as a warning
            "theme_extensions": [
                {"theme_id": "thm-X", "add_source_ids": ["src-D"]},
            ],
        }
        result = apply(config, plan=plan, project="t", strict=False)
        # promotion ran normally
        assert result.promotions_applied == 1
        # drift surfaced as an error
        assert any(
            "plan_validation" in e and "add_source_ids" in e
            for e in result.errors
        )
        # maintenance log written (non-strict doesn't abort)
        assert maintenance_log_path(config).exists()

    def test_apply_strict_allows_clean_plan(
        self, config: Config, vault: VaultManager
    ):
        """Strict mode is non-intrusive when the plan is clean."""
        _seed_proposed_concept(vault, "diagnostics", 6)
        _index(config)
        plan = {"promotions": [{"concept": "diagnostics", "domain": "swe"}]}
        # Default strict — clean plan should not raise.
        result = apply(config, plan=plan, project="t")
        assert result.promotions_applied == 1
        assert not any("plan_validation" in e for e in result.errors)


# --- Grain-split digest routing (Item 6) ------------------------------------


class TestDigestPathRouting:
    """``vault/digests/`` (vault-global) replaces ``vault/projects/X/digests/``.

    Post-2026-06-07 grain split: digest notes live at the vault root,
    flat layout, with ``YYYY-MM-DD-<grain>`` as the title slug.
    """

    def test_digest_filed_under_vault_root(
        self, config: Config, vault: VaultManager
    ):
        path = vault.create_note(
            NoteType.DIGEST,
            "2026-06-07-concept",
            body="# 2026-06-07-concept\n\nbody\n",
            project="t",
            extra_frontmatter={
                "date": "2026-06-07T00:00:00+00:00",
                "grain": "concept",
            },
        )
        # Vault-global path, not project-scoped.
        assert path.parent == config.vault_root / "digests"
        # Project frontmatter is informational; doesn't change filing.
        assert path.parent != config.vault_root / "projects" / "t" / "digests"

    def test_two_grain_digests_sit_side_by_side(
        self, config: Config, vault: VaultManager
    ):
        """The two daily digests can coexist at the vault root."""
        cpath = vault.create_note(
            NoteType.DIGEST,
            "2026-06-07-concept",
            body="# concept\n",
            project="t",
            extra_frontmatter={"grain": "concept"},
        )
        epath = vault.create_note(
            NoteType.DIGEST,
            "2026-06-07-event",
            body="# event\n",
            project="t",
            extra_frontmatter={"grain": "event"},
        )
        assert cpath.parent == epath.parent == config.vault_root / "digests"
        assert cpath != epath
        # Both files actually exist on disk.
        assert cpath.exists()
        assert epath.exists()


class TestKnowledgeDeltaGrainSplit:
    """``_collect_knowledge_delta`` routes by ``SourceTypeSpec.temporal_grain``."""

    def test_grain_split_routes_both_landings(
        self, config: Config, vault: VaultManager
    ):
        """Concept + event landings each land in their own slice."""
        _make_source(
            vault, "paper-today", concepts=["fts5"], source_type="paper"
        )
        _make_source(
            vault, "news-today", concepts=["ai-capex"], source_type="news"
        )
        _index(config)
        result = scan(config, project="t")

        concept_titles = [
            l["title"] for l in result.knowledge_delta["concept"]["landings_24h"]
        ]
        event_titles = [
            l["title"] for l in result.knowledge_delta["event"]["landings_24h"]
        ]
        assert "paper-today" in concept_titles
        assert "paper-today" not in event_titles
        assert "news-today" in event_titles
        assert "news-today" not in concept_titles

    def test_concept_only_day_leaves_event_slice_empty(
        self, config: Config, vault: VaultManager
    ):
        """No event-grain landings → event slice has empty buckets only."""
        _make_source(
            vault, "paper-today", concepts=["fts5"], source_type="paper"
        )
        _index(config)
        result = scan(config, project="t")

        event = result.knowledge_delta["event"]
        for bucket in (
            "landings_24h",
            "catalyst_additions_24h",
            "probe_matches_24h",
            "verdict_flips_24h",
            "predictions_landed_24h",
        ):
            assert event[bucket] == []
        # Concept slice has the landing.
        concept = result.knowledge_delta["concept"]
        assert len(concept["landings_24h"]) == 1
        assert concept["landings_24h"][0]["title"] == "paper-today"

    def test_event_only_day_leaves_concept_landings_empty(
        self, config: Config, vault: VaultManager
    ):
        _make_source(
            vault, "news-today", concepts=["ai-capex"], source_type="news"
        )
        _index(config)
        result = scan(config, project="t")

        assert result.knowledge_delta["concept"]["landings_24h"] == []
        assert len(result.knowledge_delta["event"]["landings_24h"]) == 1

    def test_unknown_source_type_defaults_to_concept(
        self, config: Config, vault: VaultManager
    ):
        """Unregistered source types route to the concept slice (mirrors
        SourceTypeSpec's own default temporal_grain='concept').

        Surfaced via a stamp the source-type hook leaves unrouted so the
        scan sees a real ``source_type`` string without a registry hit.
        """
        path = _make_source(
            vault,
            "exotic landing",
            concepts=["test-concept"],
            source_type="paper",  # will write through registry as paper
        )
        # Rewrite the source_type in frontmatter to a fully unknown slug.
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            "source_type: paper", "source_type: ad-hoc-experiment", 1
        )
        path.write_text(text, encoding="utf-8")
        _index(config)
        result = scan(config, project="t")

        concept_titles = [
            l["title"] for l in result.knowledge_delta["concept"]["landings_24h"]
        ]
        event_titles = [
            l["title"] for l in result.knowledge_delta["event"]["landings_24h"]
        ]
        assert "exotic landing" in concept_titles
        assert "exotic landing" not in event_titles
