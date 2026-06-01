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
    append_maintenance_log,
    apply,
    dream_report_path,
    dream_reports_dir,
    maintenance_log_path,
    recent_dream_reports,
    scan,
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
        themes = list((config.vault_root / "themes").glob("thm-*.md"))
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
            essence_rewrites_logged=1,
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
        assert ".mem/dream_reports/dream-state-test.md" in out


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
        report = (config.vault_root / ".mem" / "dream_reports"
                  / f"{r.cycle_id}.md").read_text(encoding="utf-8")
        assert "What I queued" in report
        assert "dynamic-batching" in report
        assert "Survey on dynamic-batching" in report
        assert "What I noted" in report
        assert "llm" in report
        assert "| Priority signals enqueued | 1 |" in report
        assert "| Priority signals logged | 1 |" in report
