"""Tests for the dream cycle (operations/dream.py + CLI).

``mem dream`` is the deterministic backbone of ``/dream`` — the cron-friendly
successor to ``/mem-resolve-concepts`` and ``/themes-resolve``. These tests
build a tmp vault with seeded proposed concepts + a theme candidate + a
dormant theme, then exercise both the scan and apply phases.

Mirrors ``test_wrap_finalize.py``'s shape: tmp vault fixtures, end-to-end
plus error-path coverage, then a CLI integration test for ``--json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager
from personal_mem.operations.dream import (
    DreamCycleResult,
    DreamCycleScan,
    append_maintenance_log,
    apply,
    maintenance_log_path,
    scan,
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


def _write_theme_candidate(config: Config, candidate_id: str) -> Path:
    """Drop a minimal candidate stub under vault/themes/_candidates/."""
    cdir = config.vault_root / "themes" / "_candidates"
    cdir.mkdir(parents=True, exist_ok=True)
    path = cdir / f"{candidate_id}-test-cluster.md"
    path.write_text(
        "---\n"
        "type: theme\n"
        "candidacy: inferred-from-substack\n"
        "source_type: substack\n"
        "cluster_size: 3\n"
        "cluster_concepts: [finance-markets, semiconductors]\n"
        "cluster_sources: [src-aaa, src-bbb, src-ccc]\n"
        "title: test cluster\n"
        "---\n\n# test\n",
        encoding="utf-8",
    )
    return path


class TestScan:
    def test_returns_structured_plan(
        self, config: Config, vault: VaultManager
    ):
        _seed_proposed_concept(vault, "diagnostics", 6)
        _write_theme_candidate(config, "cand-abcd1234")
        _index(config)

        result = scan(config, project="t", promotion_cap=20)

        assert isinstance(result, DreamCycleScan)
        assert result.cycle_id.startswith("dream-")
        # promotion-eligible (count >= 5), passes filter_promotion_candidates
        assert any(
            p["concept"] == "diagnostics" for p in result.promotion_candidates
        )
        # candidate file picked up
        assert any(
            tc["candidate_id"] == "cand-abcd1234"
            for tc in result.theme_candidates
        )
        # every step contributes a timing entry
        for step in (
            "drift",
            "promotion",
            "theme_candidates",
            "dormant",
            "resolved",
        ):
            assert step in result.timings
        assert result.timings[step] >= 0.0  # last step in loop

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
        assert result.stats["theme_candidates"] == 0


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

    def test_archives_candidate(
        self, config: Config, vault: VaultManager
    ):
        path = _write_theme_candidate(config, "cand-deadbeef")
        _index(config)

        plan = {
            "candidates_archived": [
                {"candidate_id": "cand-deadbeef", "reason": "capability-named"}
            ],
        }
        result = apply(config, plan=plan, project="t")

        assert result.candidates_archived == 1
        assert not path.exists()
        archived = (
            config.vault_root / "themes" / "_candidates" / "_archive" / path.name
        )
        assert archived.exists()

    def test_theme_status_change(
        self, config: Config, vault: VaultManager
    ):
        """Setting a canonical theme's status via the plan."""
        themes_dir = config.vault_root / "themes"
        themes_dir.mkdir(parents=True, exist_ok=True)
        theme_path = themes_dir / "thm-feed1234-test.md"
        theme_path.write_text(
            "---\n"
            "type: theme\n"
            "id: thm-feed1234\n"
            'title: "test theme"\n'
            "status: active\n"
            "---\n\n# test theme\n\n## Essence\n\nx\n",
            encoding="utf-8",
        )
        _index(config)

        plan = {
            "theme_status_changes": [
                {
                    "theme_id": "thm-feed1234",
                    "new_status": "dormant",
                    "reason": "no catalysts in 90 days",
                }
            ],
        }
        result = apply(config, plan=plan, project="t")

        assert result.theme_status_changes == 1
        from personal_mem.core.vault import parse_frontmatter

        fm, _ = parse_frontmatter(theme_path.read_text(encoding="utf-8"))
        assert fm["status"] == "dormant"

    def test_unknown_theme_id_is_recorded_as_error(
        self, config: Config, vault: VaultManager
    ):
        _index(config)
        plan = {
            "theme_status_changes": [
                {"theme_id": "thm-nonexistent", "new_status": "dormant"}
            ],
        }
        result = apply(config, plan=plan, project="t")
        assert any("thm-nonexistent" in e for e in result.errors)
        # Errors don't cascade: result still returns and other counters are 0
        assert result.theme_status_changes == 0

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
        # ontology_grew surfaces in the log for post-hoc audit (sweep vs growth)
        assert "ontology_grew" in entry["summary"]
        assert "ts" in entry

    def test_repeat_promotion_is_sweep_only(
        self, config: Config, vault: VaultManager, monkeypatch
    ):
        """Re-promoting a canonical concept is a sweep, not growth.

        First promotion grows the ontology (term moves from missing to
        canonical, ``ontology_grew=True``). Second promotion of the same
        term is idempotent — it walks the vault for stragglers in
        ``proposed_concepts:`` but the ontology file itself is unchanged,
        so ``ontology_grew=False`` and the hub-regen chain in step 4 is
        gated off. This is the P1 fix landed 2026-05-24 after a first
        cycle measured 455s of 481s in the hub-regen + reindex tail.
        """
        # promote_proposed_concept resolves the vault ontology override
        # path via load_config() (env-pinned) — point that at the tmp
        # vault so the test can't leak into the real vault's ontology.
        monkeypatch.setenv("PERSONAL_MEM_VAULT", str(config.vault_root))
        _seed_proposed_concept(vault, "diagnostics", 6)
        _index(config)

        plan = {"promotions": [{"concept": "diagnostics", "domain": "swe"}]}

        # First pass — fresh vault, diagnostics is new vocabulary
        first = apply(config, plan=plan, project="t")
        assert first.promotions_applied == 1
        assert first.ontology_grew is True

        # Snapshot hub file set between passes
        hubs_dir = config.vault_root / "concepts" / "topics"
        hubs_after_first = (
            set(hubs_dir.glob("*.md")) if hubs_dir.exists() else set()
        )

        # Second pass — diagnostics is already canonical; pure sweep.
        # Seed another straggler so the walk has something to find
        # (otherwise notes_modified=0 and the test's signal is weaker).
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
        assert hubs_after_second == hubs_after_first, (
            "no new hub files should appear on a sweep-only cycle"
        )

    def test_new_concept_grows_ontology(
        self, config: Config, vault: VaultManager, monkeypatch
    ):
        """Promoting a term *not* in the seed ontology flips ``ontology_grew``.

        Counterpart to ``test_idempotent_sweep``. We pick a deliberately
        novel concept (``synaptic-pruning-2026``) that no ontology domain
        ships with, promote it under a domain, and assert that
        ``ontology_grew=True`` — which means the hub-regen chain in step 4
        would run on the real apply path.
        """
        # Same env-isolation as the sweep test: prevent test from writing
        # to the real vault's ontology.yaml override.
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
        # The hub-skeleton for the new concept was created.
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
        # Scan never raises SystemExit on success in non-error cases
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
        # No maintenance.jsonl file should be created on dry-run
        assert not maintenance_log_path(config).exists()
