"""Tests for ``operations/dream_tasks.py`` — the dream-orchestrator registry.

Covers:

- Registry sanity: every spec has a non-empty ``surface_key`` and
  ``worker_name``; phase-1 specs declare ``plan_keys``; phase-2 specs do
  not (they write directly); ``depends_on`` references only existing
  worker names.
- :func:`enabled_tasks` correctly filters by ``phase`` and
  ``has_signal``: an empty scan emits no phase-1 tasks; a populated scan
  surfaces the relevant ones; ``enabled=False`` removes a task.
- CLI round-trip: ``cmd_dream_tasks`` invoked in-process emits a JSON
  list against an empty temp vault.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_mem.core.config import Config
from personal_mem.core.vault import VaultManager
from personal_mem.operations.dream import DreamCycleScan
from personal_mem.operations.dream_tasks import (
    REGISTRY,
    DreamTaskSpec,
    enabled_tasks,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


def _empty_scan() -> DreamCycleScan:
    """A freshly-constructed scan with all default (empty) fields."""
    return DreamCycleScan(cycle_id="dream-test-0000")


def _populated_scan() -> SimpleNamespace:
    """Phase-1 surfaces populated; uses SimpleNamespace so we can
    freely attach the phase-2 attributes without depending on whether
    they've been added to :class:`DreamCycleScan` yet (Agent A's scope)."""
    return SimpleNamespace(
        cycle_id="dream-test-pop",
        promotion_candidates=[{"concept": "dynamic-batching", "count": 7}],
        drift_pairs=[{"from": "fastapi", "to": "api", "reason": "alias"}],
        theme_cluster_signals=[
            {"label": "iran-war", "source_count": 3, "shared_concepts": ["geopolitics"]}
        ],
        active_themes=[
            {
                "theme_id": "thm-aaaa1111",
                "title": "AI capex unwind 2026",
                "essence": "...",
                "recent_catalysts": [],
            }
        ],
        recent_probes={"dynamic-batching": 4},
        # Phase-2 surfaces — populated to exercise the phase-2 path too.
        unwrapped_sessions=[{"session_id": "ses-zzz"}],
        rejudge_queue=[{"decision_id": "dec-zzz"}],
        # Post-2026-06-07 grain split: knowledge_delta is {concept,event} →
        # has_signal needs at least one substantive bucket on *either* slice.
        knowledge_delta={
            "window_start": "2026-06-07T00:00:00+00:00",
            "window_end": "2026-06-07T23:59:59+00:00",
            "concept": {
                "landings_24h": [{"id": "src-xxx", "title": "..."}],
                "catalyst_additions_24h": [],
                "probe_matches_24h": [],
                "verdict_flips_24h": [],
                "predictions_landed_24h": [],
                "theme_mutations_this_cycle": {
                    "theme_mints": [],
                    "theme_extensions": [],
                },
            },
            "event": {
                "landings_24h": [],
                "catalyst_additions_24h": [],
                "probe_matches_24h": [],
                "verdict_flips_24h": [],
                "predictions_landed_24h": [],
                "theme_mutations_this_cycle": {
                    "theme_mints": [],
                    "theme_extensions": [],
                },
            },
        },
    )


# ---------------------------------------------------------------------------
# Registry sanity
# ---------------------------------------------------------------------------


class TestRegistrySanity:
    """The shape-correctness contract every spec promises."""

    def test_registry_has_eight_specs(self):
        assert len(REGISTRY) == 8

    def test_every_spec_has_nonempty_surface_and_worker(self):
        for spec in REGISTRY:
            assert spec.surface_key, f"empty surface_key on {spec.worker_name}"
            assert spec.worker_name, f"empty worker_name on {spec!r}"

    def test_phase_is_one_or_two(self):
        for spec in REGISTRY:
            assert spec.phase in (1, 2), f"bad phase on {spec.worker_name}"

    def test_phase1_specs_declare_plan_keys(self):
        for spec in REGISTRY:
            if spec.phase == 1:
                assert spec.plan_keys, (
                    f"phase-1 worker {spec.worker_name} has empty plan_keys"
                )

    def test_phase2_specs_have_no_plan_keys(self):
        for spec in REGISTRY:
            if spec.phase == 2:
                assert spec.plan_keys == (), (
                    f"phase-2 worker {spec.worker_name} declared plan_keys "
                    f"(should write directly via MCP/Bash): {spec.plan_keys}"
                )

    def test_depends_on_references_existing_workers(self):
        names = {spec.worker_name for spec in REGISTRY}
        for spec in REGISTRY:
            for dep in spec.depends_on:
                assert dep in names, (
                    f"{spec.worker_name} depends_on dangling {dep!r}"
                )

    def test_expected_worker_names_present(self):
        names = {spec.worker_name for spec in REGISTRY}
        expected = {
            "dream-promotion-worker",
            "dream-merge-worker",
            "dream-theme-worker",
            "dream-essence-worker",
            "dream-priority-worker",
            "dream-wrap-worker",
            "dream-judge-worker",
            "dream-digest-worker",
        }
        assert names == expected, f"registry missing {expected - names}"

    def test_digest_worker_depends_on_judge(self):
        digest = next(
            s for s in REGISTRY if s.worker_name == "dream-digest-worker"
        )
        assert digest.depends_on == ("dream-judge-worker",)


# ---------------------------------------------------------------------------
# enabled_tasks selector
# ---------------------------------------------------------------------------


class TestEnabledTasks:
    """The filter behavior — empty/populated/disabled paths."""

    def test_empty_scan_phase1_emits_nothing(self):
        assert enabled_tasks(_empty_scan(), phase=1) == []

    def test_empty_scan_phase2_emits_nothing(self):
        # An empty scan has no unwrapped_sessions / rejudge_queue /
        # knowledge_delta — phase 2 should be silent.
        assert enabled_tasks(_empty_scan(), phase=2) == []

    def test_populated_scan_phase1_emits_all_five(self):
        tasks = enabled_tasks(_populated_scan(), phase=1)
        names = {t["worker_name"] for t in tasks}
        assert names == {
            "dream-promotion-worker",
            "dream-merge-worker",
            "dream-theme-worker",
            "dream-essence-worker",
            "dream-priority-worker",
        }

    def test_populated_scan_phase2_emits_all_three(self):
        tasks = enabled_tasks(_populated_scan(), phase=2)
        names = {t["worker_name"] for t in tasks}
        assert names == {
            "dream-wrap-worker",
            "dream-judge-worker",
            "dream-digest-worker",
        }

    def test_partial_phase1_only_emits_promotion(self):
        scan = SimpleNamespace(
            promotion_candidates=[{"concept": "x", "count": 6}],
        )
        tasks = enabled_tasks(scan, phase=1)
        assert len(tasks) == 1
        assert tasks[0]["worker_name"] == "dream-promotion-worker"
        assert tasks[0]["plan_keys"] == ["promotions"]

    def test_disabled_spec_is_filtered(self, monkeypatch):
        # Build a synthetic disabled-promotion REGISTRY and verify
        # enabled_tasks honors the gate without removing the entry.
        from personal_mem.operations import dream_tasks as dt

        patched = tuple(
            replace(spec, enabled=False)
            if spec.worker_name == "dream-promotion-worker"
            else spec
            for spec in REGISTRY
        )
        monkeypatch.setattr(dt, "REGISTRY", patched)
        tasks = enabled_tasks(_populated_scan(), phase=1)
        names = {t["worker_name"] for t in tasks}
        assert "dream-promotion-worker" not in names
        # The other 4 phase-1 workers still fire.
        assert len(names) == 4

    def test_knowledge_delta_with_only_empty_buckets_does_not_fire(self):
        # knowledge_delta is always a dict — even with all-empty buckets,
        # bool(dict) is True. The digest worker's predicate must check
        # whether any substantive bucket is populated on either grain slice.
        empty_slice = {
            "landings_24h": [],
            "catalyst_additions_24h": [],
            "probe_matches_24h": [],
            "verdict_flips_24h": [],
            "predictions_landed_24h": [],
            "theme_mutations_this_cycle": {
                "theme_mints": [],
                "theme_extensions": [],
            },
        }
        scan = SimpleNamespace(
            unwrapped_sessions=[],
            rejudge_queue=[],
            knowledge_delta={
                "concept": dict(empty_slice),
                "event": dict(empty_slice),
            },
        )
        tasks = enabled_tasks(scan, phase=2)
        assert tasks == []

    def test_entry_shape_carries_required_keys(self):
        tasks = enabled_tasks(_populated_scan(), phase=1)
        for t in tasks:
            assert set(t.keys()) == {
                "surface_key",
                "worker_name",
                "plan_keys",
                "depends_on",
            }
            assert isinstance(t["plan_keys"], list)
            assert isinstance(t["depends_on"], list)

    def test_dataclass_spec_is_frozen(self):
        spec = REGISTRY[0]
        with pytest.raises((AttributeError, Exception)):
            spec.enabled = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CLI round-trip
# ---------------------------------------------------------------------------


class TestDreamTasksCLI:
    """``mem dream tasks --phase N --json`` is the contract the orchestrator
    consumes. Invoking the handler in-process keeps the test fast and free
    of subprocess plumbing while exercising the full path."""

    def test_phase1_json_empty_vault_emits_empty_list(
        self, config: Config, vault: VaultManager, monkeypatch, capsys
    ):
        monkeypatch.setenv("PERSONAL_MEM_VAULT", str(config.vault_root))
        monkeypatch.setenv("PERSONAL_MEM_PROJECT", "t")

        from personal_mem.surfaces.cli.dream import cmd_dream_tasks

        args = SimpleNamespace(
            phase=1,
            scan=None,
            apply_result=None,
            json=True,
            project="t",
        )
        with pytest.raises(SystemExit) as exc:
            cmd_dream_tasks(args)
        assert exc.value.code == 0

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert isinstance(payload, list)
        # Empty vault → all has_signal predicates return False.
        assert payload == []

    def test_phase1_json_from_scan_file(
        self, config: Config, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setenv("PERSONAL_MEM_VAULT", str(config.vault_root))
        scan_path = tmp_path / "scan.json"
        scan_path.write_text(
            json.dumps({
                "cycle_id": "dream-test",
                "promotion_candidates": [{"concept": "x", "count": 8}],
                "drift_pairs": [],
                "theme_cluster_signals": [],
                "active_themes": [],
                "recent_probes": {},
            }),
            encoding="utf-8",
        )

        from personal_mem.surfaces.cli.dream import cmd_dream_tasks

        args = SimpleNamespace(
            phase=1,
            scan=str(scan_path),
            apply_result=None,
            json=True,
            project="",
        )
        with pytest.raises(SystemExit) as exc:
            cmd_dream_tasks(args)
        assert exc.value.code == 0

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert len(payload) == 1
        assert payload[0]["worker_name"] == "dream-promotion-worker"
        assert payload[0]["plan_keys"] == ["promotions"]

    def test_phase2_json_from_populated_scan_file(
        self, config: Config, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setenv("PERSONAL_MEM_VAULT", str(config.vault_root))
        # Scan file carries phase-2 surfaces as extra keys — the CLI
        # must surface them as plain attributes (the rehydration path).
        scan_path = tmp_path / "scan.json"
        scan_path.write_text(
            json.dumps({
                "cycle_id": "dream-test",
                "promotion_candidates": [],
                "drift_pairs": [],
                "theme_cluster_signals": [],
                "active_themes": [],
                "recent_probes": {},
                "unwrapped_sessions": [{"session_id": "ses-zzz"}],
                "rejudge_queue": [{"decision_id": "dec-zzz"}],
                "knowledge_delta": {
                    "window_start": "2026-06-07T00:00:00+00:00",
                    "window_end": "2026-06-07T23:59:59+00:00",
                    "concept": {
                        "landings_24h": [{"id": "src-xxx"}],
                        "catalyst_additions_24h": [],
                        "probe_matches_24h": [],
                        "verdict_flips_24h": [],
                        "predictions_landed_24h": [],
                        "theme_mutations_this_cycle": {
                            "theme_mints": [],
                            "theme_extensions": [],
                        },
                    },
                    "event": {
                        "landings_24h": [],
                        "catalyst_additions_24h": [],
                        "probe_matches_24h": [],
                        "verdict_flips_24h": [],
                        "predictions_landed_24h": [],
                        "theme_mutations_this_cycle": {
                            "theme_mints": [],
                            "theme_extensions": [],
                        },
                    },
                },
            }),
            encoding="utf-8",
        )

        from personal_mem.surfaces.cli.dream import cmd_dream_tasks

        args = SimpleNamespace(
            phase=2,
            scan=str(scan_path),
            apply_result=None,
            json=True,
            project="",
        )
        with pytest.raises(SystemExit) as exc:
            cmd_dream_tasks(args)
        assert exc.value.code == 0

        out = capsys.readouterr().out
        payload = json.loads(out)
        names = {t["worker_name"] for t in payload}
        assert names == {
            "dream-wrap-worker",
            "dream-judge-worker",
            "dream-digest-worker",
        }
        digest = next(t for t in payload if t["worker_name"] == "dream-digest-worker")
        assert digest["depends_on"] == ["dream-judge-worker"]

    def test_human_readable_table_when_no_json_flag(
        self, config: Config, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setenv("PERSONAL_MEM_VAULT", str(config.vault_root))
        scan_path = tmp_path / "scan.json"
        scan_path.write_text(
            json.dumps({
                "cycle_id": "dream-test",
                "promotion_candidates": [{"concept": "x", "count": 8}],
                "drift_pairs": [],
                "theme_cluster_signals": [],
                "active_themes": [],
                "recent_probes": {},
            }),
            encoding="utf-8",
        )

        from personal_mem.surfaces.cli.dream import cmd_dream_tasks

        args = SimpleNamespace(
            phase=1,
            scan=str(scan_path),
            apply_result=None,
            json=False,
            project="",
        )
        cmd_dream_tasks(args)
        out = capsys.readouterr().out
        # Smoke-check the human readable surface — header + worker name
        # appear, no JSON dump.
        assert "dream tasks" in out
        assert "dream-promotion-worker" in out
        assert "promotions" in out


# ---------------------------------------------------------------------------
# Type / construction smoke
# ---------------------------------------------------------------------------


class TestDreamTaskSpec:
    def test_default_phase_is_1(self):
        spec = DreamTaskSpec(surface_key="x", worker_name="dream-x-worker")
        assert spec.phase == 1
        assert spec.enabled is True
        assert spec.depends_on == ()
        assert spec.plan_keys == ()

    def test_default_has_signal_returns_true(self):
        spec = DreamTaskSpec(surface_key="x", worker_name="dream-x-worker")
        assert spec.has_signal(SimpleNamespace()) is True
