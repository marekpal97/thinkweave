"""Drift v2 integration tests — scan pair pool, verdict memory, apply paths.

Covers the 2026-06-11 ontology-geometry work end-to-end on a tmp vault:
scan's cosine∪string pair pool with evidence packets, judged-pair
exclusion via the maintenance log, the theme_dup_candidates surface,
apply's fold-not-delete concept merge, theme_merges, distinct_pairs
recording, and the seam-link handoff.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.embeddings import EMBEDDINGS_SCHEMA, _pack_embedding
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager, parse_frontmatter
from thinkweave.operations import seam_link_queue
from thinkweave.operations.dream import (
    apply,
    maintenance_log_path,
    scan,
    validate_plan_fragment,
)
from thinkweave.synthesis import geometry


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


def _seed_embeddings(config: Config, vectors: dict[str, list[float]]) -> None:
    config.weave_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(config.embeddings_db))
    db.executescript(EMBEDDINGS_SCHEMA)
    for note_id, vec in vectors.items():
        db.execute(
            "INSERT OR REPLACE INTO embeddings "
            "(note_id, content_hash, embedding, model, created_at) "
            "VALUES (?, 'h', ?, 'test', '2026-01-01')",
            (note_id, _pack_embedding(vec)),
        )
    db.commit()
    db.close()


def _notes_with_concept(vm: VaultManager, concept: str, n: int) -> list[str]:
    ids = []
    for i in range(n):
        path = vm.create_note(
            NoteType.NOTE,
            f"{concept} note {i}",
            body="body",
            project="t",
            extra_frontmatter={"concepts": [concept]},
        )
        fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        ids.append(fm["id"])
    return ids


def _make_theme(vm: VaultManager, title: str, *, entries: list[str],
                cites: list[str] | None = None) -> tuple[str, Path]:
    log = "\n".join(entries)
    extra = {"status": "active", "concepts": ["geopolitics"]}
    if cites:
        extra["cites"] = cites
    path = vm.create_note(
        NoteType.THEME,
        title,
        body=f"## Essence\n\nArc thesis for {title}.\n\n"
        f"## Catalyst log\n\n{log}\n\n## Open questions\n",
        extra_frontmatter=extra,
    )
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    return fm["id"], path


def _hub_file(config: Config, concept: str, entries: list[str]) -> Path:
    topics = config.vault_root / "concepts" / "topics"
    topics.mkdir(parents=True, exist_ok=True)
    path = topics / f"{concept}.md"
    log = "\n".join(entries)
    path.write_text(
        f"""---
type: concept-hub
concept: {concept}
---
# {concept}

## Essence

*No synthesis yet.*

## Catalyst log

{log}
""",
        encoding="utf-8",
    )
    return path


class TestScanDriftV2:
    def test_string_pairs_carry_evidence_packets(self, config, vault):
        _notes_with_concept(vault, "embeddings", 2)
        _notes_with_concept(vault, "embedings", 1)
        _index(config)
        result = scan(config, project="t")
        pairs = {
            frozenset((p["from"], p["to"])): p for p in result.drift_pairs
        }
        key = frozenset(("embeddings", "embedings"))
        assert key in pairs
        p = pairs[key]
        assert "note_counts" in p and "sample_titles" in p
        assert "same_domain" in p
        assert result.stats["drift_pairs"] == len(result.drift_pairs)

    def test_cosine_pairs_join_pool(self, config, vault):
        a_ids = _notes_with_concept(vault, "llm-eval", 3)
        b_ids = _notes_with_concept(vault, "model-grading", 3)
        _index(config)
        vecs = {nid: [1.0, 0.0] for nid in a_ids + b_ids}
        _seed_embeddings(config, vecs)
        result = scan(config, project="t")
        keys = {frozenset((p["from"], p["to"])) for p in result.drift_pairs}
        # Zero string overlap — only the cosine generator can surface this.
        assert frozenset(("llm-eval", "model-grading")) in keys
        p = next(
            p for p in result.drift_pairs
            if frozenset((p["from"], p["to"]))
            == frozenset(("llm-eval", "model-grading"))
        )
        assert p["cosine"] and p["cosine"] >= 0.99

    def test_judged_pairs_excluded_until_rejudge(self, config, vault):
        _notes_with_concept(vault, "derivative", 1)
        _notes_with_concept(vault, "derivatives", 1)
        _index(config)
        # Record a distinct ruling the way apply does.
        from thinkweave.operations.dream import append_maintenance_log

        append_maintenance_log(
            config,
            {
                "cycle_id": "c0",
                "verdicts": {
                    "distinct_pairs": [
                        {"kind": "concept",
                         "pair": ["derivative", "derivatives"]}
                    ]
                },
            },
        )
        key = frozenset(("derivative", "derivatives"))
        result = scan(config, project="t")
        keys = {frozenset((p["from"], p["to"])) for p in result.drift_pairs}
        assert key not in keys
        rejudged = scan(config, project="t", rejudge_pairs=True)
        keys = {frozenset((p["from"], p["to"])) for p in rejudged.drift_pairs}
        assert key in keys

    def test_drift_cap_ranks_by_cosine(self, config, vault, monkeypatch):
        monkeypatch.setattr(config, "dream_drift_cap", 1, raising=False)
        ids_a = _notes_with_concept(vault, "alpha-term", 3)
        ids_b = _notes_with_concept(vault, "beta-term", 3)
        _notes_with_concept(vault, "embeddings", 1)
        _notes_with_concept(vault, "embedings", 1)
        _index(config)
        _seed_embeddings(
            config, {nid: [1.0, 0.0] for nid in ids_a + ids_b}
        )
        result = scan(config, project="t")
        assert len(result.drift_pairs) == 1
        # cosine-bearing pair outranks the string-only one
        assert result.drift_pairs[0]["cosine"] is not None


class TestThemeDupCandidates:
    def test_dup_themes_surfaced(self, config, vault):
        tid_a, _ = _make_theme(
            vault, "iran hormuz shock",
            entries=["- 2026-05-01 · *new* — strait closed. — [[src-aaaa1111]]"],
        )
        tid_b, _ = _make_theme(
            vault, "hormuz supply shock",
            entries=["- 2026-05-02 · *new* — tankers rerouted. — [[src-bbbb2222]]"],
        )
        _index(config)
        _seed_embeddings(config, {tid_a: [1.0, 0.0], tid_b: [1.0, 0.0]})
        result = scan(config, project="t")
        assert len(result.theme_dup_candidates) == 1
        cand = result.theme_dup_candidates[0]
        assert {cand["from_id"], cand["to_id"]} == {tid_a, tid_b}
        assert cand["slug_token_overlap"] > 0
        assert "essence_excerpts" in cand

    def test_non_active_excluded(self, config, vault):
        tid_a, path_a = _make_theme(
            vault, "arc a",
            entries=["- 2026-05-01 · *new* — x. — [[src-aaaa1111]]"],
        )
        tid_b, path_b = _make_theme(
            vault, "arc b",
            entries=["- 2026-05-02 · *new* — y. — [[src-bbbb2222]]"],
        )
        from thinkweave.synthesis.hub import set_frontmatter_keys

        set_frontmatter_keys(path_b, {"status": f"merged-into:{tid_a}"})
        _index(config)
        _seed_embeddings(config, {tid_a: [1.0, 0.0], tid_b: [1.0, 0.0]})
        result = scan(config, project="t")
        assert result.theme_dup_candidates == []


class TestApplyDriftV2:
    def test_plan_keys_validate(self):
        plan = {
            "theme_merges": [
                {"from_id": "thm-a", "to_id": "thm-b", "reason": "dup"}
            ],
            "distinct_pairs": [
                {"kind": "concept", "pair": ["a", "b"],
                 "reason": "homonym", "cosine": 0.83}
            ],
        }
        assert validate_plan_fragment(plan) == []
        bad = {"theme_merges": [{"from_id": "x", "to_id": "y", "speed": 1}]}
        assert any("speed" in w for w in validate_plan_fragment(bad))

    def test_concept_merge_folds_and_enqueues(self, config, vault):
        _notes_with_concept(vault, "embedings", 1)
        _notes_with_concept(vault, "embeddings", 1)
        _hub_file(config, "embeddings", [
            "- 2026-05-01 · *new* — winner entry. — [[src-aaaa1111]]",
        ])
        _hub_file(config, "embedings", [
            "- 2026-05-02 · *new* — loser entry. — [[src-bbbb2222]]",
        ])
        _index(config)
        plan = {"merges": [
            {"from": "embedings", "to": "embeddings", "reason": "typo"}
        ]}
        result = apply(config, plan=plan, project="t")
        assert result.merges_applied == 1
        assert result.seams_enqueued == 1
        assert result.applied_merges[0]["from"] == "embedings"

        topics = config.vault_root / "concepts" / "topics"
        assert not (topics / "embedings.md").exists()
        archived = topics / "_archive" / "embedings.md"
        assert archived.exists()
        fm, _ = parse_frontmatter(archived.read_text(encoding="utf-8"))
        assert fm.get("merged-into") == "embeddings"

        winner = (topics / "embeddings.md").read_text(encoding="utf-8")
        assert "loser entry" in winner
        wfm, _ = parse_frontmatter(winner)
        assert wfm.get("fold_pending_from") == "embedings"

        items = seam_link_queue.peek(config)
        assert len(items) == 1 and items[0]["hub_id"] == "embeddings"

        # Verdict memory: the merged pair is excluded from the next scan.
        judged = geometry.judged_pairs(config)
        assert geometry.pair_key("concept", "embedings", "embeddings") in judged

    def test_theme_merge_end_to_end(self, config, vault):
        tid_a, path_a = _make_theme(
            vault, "arc loser",
            entries=["- 2026-05-02 · *new* — loser catalyst. — [[src-bbbb2222]]"],
            cites=["src-bbbb2222"],
        )
        tid_b, path_b = _make_theme(
            vault, "arc survivor",
            entries=["- 2026-05-01 · *new* — survivor catalyst. — [[src-aaaa1111]]"],
            cites=["src-aaaa1111"],
        )
        # A source filed to the loser.
        src = vault.create_note(
            NoteType.NOTE,
            "filed source",
            body="b",
            project="t",
            extra_frontmatter={"relates_to": [tid_a]},
        )
        _index(config)
        plan = {"theme_merges": [
            {"from_id": tid_a, "to_id": tid_b, "reason": "same arc"}
        ]}
        result = apply(config, plan=plan, project="t")
        assert result.theme_merges_applied == 1
        assert result.errors == []

        lfm, _ = parse_frontmatter(path_a.read_text(encoding="utf-8"))
        assert lfm["status"] == f"merged-into:{tid_b}"
        sfm, sbody = parse_frontmatter(path_b.read_text(encoding="utf-8"))
        assert "loser catalyst" in sbody
        assert "src-bbbb2222" in sfm.get("cites", [])
        rfm, _ = parse_frontmatter(src.read_text(encoding="utf-8"))
        assert rfm["relates_to"] == [tid_b]

        items = seam_link_queue.peek(config)
        assert any(
            it["hub_id"] == tid_b and it["hub_kind"] == "theme"
            for it in items
        )
        # Registry tombstone.
        from thinkweave.synthesis import theme_registry

        reg = theme_registry.load(config)
        assert reg[tid_a]["status"] == f"merged-into:{tid_b}"

    def test_distinct_pairs_recorded_in_maintenance(self, config, vault):
        plan = {"distinct_pairs": [
            {"kind": "concept", "pair": ["derivative", "derivatives"],
             "reason": "math vs finance homonym", "cosine": 0.83}
        ]}
        result = apply(config, plan=plan, project="t")
        assert result.distinct_pairs_recorded == 1
        line = maintenance_log_path(config).read_text(
            encoding="utf-8"
        ).strip().splitlines()[-1]
        entry = json.loads(line)
        assert entry["verdicts"]["distinct_pairs"][0]["pair"] == [
            "derivative", "derivatives",
        ]
        judged = geometry.judged_pairs(config)
        assert (
            geometry.pair_key("concept", "derivative", "derivatives") in judged
        )


# ---------------------------------------------------------------------------
# Grain coarsening (drift v2, N-ary) — clusters, apply fold, anti-oscillation,
# revert/re-split, the coarsen_apply posture gate. 2026-06-13.
# ---------------------------------------------------------------------------


def _clique(config: Config, vault: VaultManager,
            concepts=("theta", "vega", "gamma")) -> dict[str, list[str]]:
    """Three fine concepts whose note centroids coincide → a near-clique."""
    ids = {c: _notes_with_concept(vault, c, 3) for c in concepts}
    _index(config)
    vecs = {nid: [1.0, 0.01] for nid_list in ids.values() for nid in nid_list}
    _seed_embeddings(config, vecs)
    return ids


class TestCoarsen:
    def test_scan_surfaces_coarsen_cluster(self, config, vault):
        _clique(config, vault)
        result = scan(config, project="t")
        assert result.coarsen_clusters, "expected a coarsen cluster"
        members = set(result.coarsen_clusters[0]["members"])
        assert {"theta", "vega", "gamma"} <= members
        assert result.coarsen_clusters[0]["min_cosine"] >= 0.85
        assert result.stats["coarsen_clusters"] == len(result.coarsen_clusters)

    def test_apply_coarsening_folds_and_snapshots(self, config, vault):
        _clique(config, vault)
        for c in ("theta", "vega", "gamma"):
            _hub_file(config, c,
                      [f"- 2026-01-01 · *new* — seed {c}. — [[src-{c}]]"])
        _index(config)
        plan = {"coarsenings": [{
            "members": ["theta", "vega", "gamma"], "target": "vol-greeks",
            "target_domain": "finance-options", "target_is_new": True,
            "reason": "greeks family", "min_cosine": 0.99,
        }]}
        result = apply(config, plan=plan, project="t")
        assert result.coarsenings_applied == 1
        idx = Indexer(config=config)
        try:
            n_greeks = idx.db.execute(
                "SELECT COUNT(*) n FROM note_concepts WHERE concept='vol-greeks'"
            ).fetchone()["n"]
            n_theta = idx.db.execute(
                "SELECT COUNT(*) n FROM note_concepts WHERE concept='theta'"
            ).fetchone()["n"]
        finally:
            idx.close()
        assert n_greeks >= 9 and n_theta == 0
        assert "vol-greeks" in (config.config_dir / "ontology.yaml").read_text()
        assert (config.vault_root / "concepts" / "topics" / "_archive"
                / "theta.md").exists()
        v = result.recorded_coarsenings[0]
        assert v["target"] == "vol-greeks" and v["target_was_new"] is True
        assert v["member_note_ids"]["theta"]  # snapshot present
        # ontology growth fires the hub-regen branch
        assert result.ontology_grew is True

    def test_coarsen_apply_gate_off_skips_but_force_overrides(
        self, config, vault
    ):
        _clique(config, vault)
        config.dream_coarsen_apply = False
        plan = {"coarsenings": [{
            "members": ["theta", "vega", "gamma"], "target": "vol-greeks",
            "target_domain": "finance-options", "target_is_new": True,
        }]}
        r1 = apply(config, plan=plan, project="t")
        assert r1.coarsenings_applied == 0
        assert any("coarsen_apply" in e for e in r1.errors)
        r2 = apply(config, plan=plan, project="t", force_coarsen=True)
        assert r2.coarsenings_applied == 1

    def test_distinct_cluster_recorded_and_excluded_until_rejudge(
        self, config, vault
    ):
        _clique(config, vault)
        plan = {"distinct_clusters": [{
            "kind": "concept", "members": ["theta", "vega", "gamma"],
            "reason": "genuinely distinct greeks", "min_cosine": 0.99,
        }]}
        r = apply(config, plan=plan, project="t")
        assert r.distinct_clusters_recorded == 1
        target = geometry.cluster_key("concept", ["theta", "vega", "gamma"])
        keys = {
            geometry.cluster_key("concept", c["members"])
            for c in scan(config, project="t").coarsen_clusters
        }
        assert target not in keys
        keys_rj = {
            geometry.cluster_key("concept", c["members"])
            for c in scan(config, project="t",
                          rejudge_pairs=True).coarsen_clusters
        }
        assert target in keys_rj

    def test_coarsened_members_excluded_next_scan(self, config, vault):
        # After a coarsening verdict, the cluster's members are stale and a
        # candidate touching them is dropped (the anti-oscillation guard).
        _clique(config, vault)
        from thinkweave.operations.dream import append_maintenance_log
        append_maintenance_log(config, {
            "cycle_id": "c0",
            "verdicts": {"coarsenings": [
                {"members": ["theta", "vega", "gamma"], "target": "greeks"}
            ]},
        })
        coarsened, _ = geometry.judged_clusters(config)
        assert ("concept", "theta") in coarsened

    def test_revert_coarsening_resplits(self, config, vault):
        _clique(config, vault)
        for c in ("theta", "vega", "gamma"):
            _hub_file(config, c,
                      [f"- 2026-01-01 · *new* — seed {c}. — [[src-{c}]]"])
        _index(config)
        apply(config, plan={"coarsenings": [{
            "members": ["theta", "vega", "gamma"], "target": "vol-greeks",
            "target_domain": "finance-options", "target_is_new": True,
            "reason": "r", "min_cosine": 0.99,
        }]}, project="t")
        from thinkweave.synthesis.concepts import revert_coarsening
        stats = revert_coarsening(config, "vol-greeks")
        assert set(stats["restored"]) == {"theta", "vega", "gamma"}
        assert stats["notes_demoted"] >= 9
        assert stats["ontology_removed"] is True
        assert (config.vault_root / "concepts" / "topics" / "theta.md").exists()
        idx = Indexer(config=config)
        try:
            assert idx.db.execute(
                "SELECT COUNT(*) n FROM note_concepts WHERE concept='theta'"
            ).fetchone()["n"] >= 3
            assert idx.db.execute(
                "SELECT COUNT(*) n FROM note_concepts WHERE concept='vol-greeks'"
            ).fetchone()["n"] == 0
        finally:
            idx.close()
        assert "vol-greeks" not in (config.config_dir / "ontology.yaml").read_text()


class TestStalenessResolve:
    def test_stale_active_theme_auto_resolves(self, config, vault):
        _, path = _make_theme(
            vault, "Old arc",
            entries=["- 2026-01-01 · *new* — old seed. — [[src-aaaa1111]]"],
        )
        _index(config)
        result = apply(config, plan={}, project="t")
        assert result.themes_resolved == 1
        fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert fm["status"] == "resolved"

    def test_fresh_active_theme_not_resolved(self, config, vault):
        from datetime import date
        _, path = _make_theme(
            vault, "Fresh arc",
            entries=[f"- {date.today().isoformat()} · *new* — recent. "
                     "— [[src-aaaa1111]]"],
        )
        _index(config)
        result = apply(config, plan={}, project="t")
        assert result.themes_resolved == 0
        fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert fm["status"] == "active"

    def test_disabled_when_zero(self, config, vault):
        config.theme_resolve_after_days = 0
        _, path = _make_theme(
            vault, "Old arc",
            entries=["- 2026-01-01 · *new* — old. — [[src-aaaa1111]]"],
        )
        _index(config)
        result = apply(config, plan={}, project="t")
        assert result.themes_resolved == 0
