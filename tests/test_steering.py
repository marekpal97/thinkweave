"""Tests for ``operations/steering.py`` — evidence-gated steering + weekly
budget for the slow loop's proposals (issue #62).

The slow loop (#61, not yet built) runs improve-arch / ponytail-audit and wants
to file self-improvement issues. This module is the gate #61 must call: every
proposal must cite evidence from the self-improvement substrate (trajectory
outcome labels from #60/#56, superseded-decision density, gate-failure
hotspots, concept-hub pressure), a proposal with no cited evidence is dropped,
and only the top-``weekly_budget`` by evidence weight survive per run.

Coverage layers, mirroring the #60 trajectory-outcome split (pure functions
over queried rows; the index/CLI are thin seams):

- pure aggregators (``aggregate_rework`` / ``aggregate_gate_failures`` /
  ``aggregate_superseded`` / ``hub_pressure_from_ranks``) — hand-computed
  expecteds over fixture rows, never the index;
- ``evidence_for`` / ``has_evidence`` — per-candidate evidence block assembly;
- ``gate_proposals`` — the two acceptance criteria (no-evidence drop, budget
  cap) plus ranking and evidence-block content;
- ``build_evidence_index`` — the index scan over a seeded tmp vault;
- the ``weave steering`` CLI contract + ``[steering]`` config plumbing.

All vault state is tmp-path via ``vault_factory``; no ambient config, no real
vault, no network.
"""

from __future__ import annotations

import json

import pytest

from thinkweave.core.config import Config
from thinkweave.core.schemas import NoteType
from thinkweave.operations import steering


# ---------------------------------------------------------------------------
# Pure aggregators — over hand-built rows (never the index)
# ---------------------------------------------------------------------------


class TestAggregateRework:
    def test_reworked_label_counts_per_touched_file(self):
        rows = [
            {"outcome_label": "reworked", "fix_rounds": 0,
             "files_touched": ["a.py", "b.py"]},
            {"outcome_label": "reworked-post-merge", "fix_rounds": 0,
             "files_touched": ["a.py"]},
            {"outcome_label": "merged-clean", "fix_rounds": 0,
             "files_touched": ["a.py", "c.py"]},
        ]
        rework, fix = steering.aggregate_rework(rows)
        # a.py: reworked twice (row0 + row1); b.py once; c.py never (clean merge)
        assert rework == {"a.py": 2, "b.py": 1}
        assert fix == {}

    def test_fix_rounds_summed_over_files_regardless_of_verdict(self):
        rows = [
            {"outcome_label": "merged-clean", "fix_rounds": 2,
             "files_touched": ["a.py"]},
            {"outcome_label": "reworked", "fix_rounds": 3,
             "files_touched": ["a.py", "b.py"]},
        ]
        rework, fix = steering.aggregate_rework(rows)
        assert rework == {"a.py": 1, "b.py": 1}
        # a.py: 2 (clean) + 3 (reworked) = 5; b.py: 3
        assert fix == {"a.py": 5, "b.py": 3}

    def test_empty_and_missing_fields_are_zero(self):
        rework, fix = steering.aggregate_rework(
            [{"files_touched": ["a.py"]}, {"outcome_label": "reworked"}]
        )
        # first row: no label/rounds → nothing; second: reworked but no files → nothing
        assert rework == {}
        assert fix == {}


class TestAggregateGateFailures:
    def test_failed_gates_attributed_to_every_touched_file(self):
        rows = [
            {"gates": [{"id": "tests", "passed": False},
                       {"id": "lint", "passed": True},
                       {"id": "types", "passed": False}],
             "files_touched": ["a.py", "b.py"]},
            {"gates": [{"id": "tests", "passed": True}],
             "files_touched": ["a.py"]},
        ]
        out = steering.aggregate_gate_failures(rows)
        # row0 has 2 failed gates over a.py+b.py; row1 has 0 failed
        assert out == {"a.py": 2, "b.py": 2}

    def test_no_failed_gates_yields_empty(self):
        rows = [{"gates": [{"id": "t", "passed": True}], "files_touched": ["a.py"]}]
        assert steering.aggregate_gate_failures(rows) == {}


class TestAggregateSuperseded:
    def test_status_and_link_both_count_once_per_file(self):
        rows = [
            {"status": "superseded", "file_paths": ["a.py"]},
            {"status": "deprecated", "file_paths": ["a.py", "b.py"]},
            {"status": "accepted", "supersedes": ["dec-1"], "file_paths": ["b.py"]},
            {"status": "accepted", "file_paths": ["c.py"]},  # not contested
        ]
        out = steering.aggregate_superseded(rows)
        # a.py: row0 + row1 = 2; b.py: row1 + row2 (supersedes link) = 2; c.py: 0
        assert out == {"a.py": 2, "b.py": 2}

    def test_superseded_by_link_counts(self):
        rows = [{"status": "accepted", "superseded_by": ["dec-9"],
                 "file_paths": ["x.py"]}]
        assert steering.aggregate_superseded(rows) == {"x.py": 1}


class TestHubPressureFromRanks:
    def test_max_score_per_concept(self):
        rows = [
            ("pagerank:indexer", 0.4),
            ("pagerank:indexer", 0.7),
            ("pagerank:retrieval", 0.2),
            ("betweenness:indexer", 0.9),  # not a pagerank row → ignored
        ]
        out = steering.hub_pressure_from_ranks(rows)
        assert out == {"indexer": 0.7, "retrieval": 0.2}

    def test_empty_when_no_pagerank_rows(self):
        assert steering.hub_pressure_from_ranks([]) == {}


# ---------------------------------------------------------------------------
# evidence_for / has_evidence
# ---------------------------------------------------------------------------


def _index(**kw) -> steering.EvidenceIndex:
    return steering.EvidenceIndex(
        rework=kw.get("rework", {}),
        fix_rounds=kw.get("fix_rounds", {}),
        superseded=kw.get("superseded", {}),
        gate_failures=kw.get("gate_failures", {}),
        hub_pressure=kw.get("hub_pressure", {}),
    )


class TestEvidenceFor:
    def test_prefix_match_sums_signals_under_a_directory(self):
        idx = _index(
            rework={"src/ops/dream.py": 2, "src/ops/steering.py": 1, "src/cli/x.py": 5},
            fix_rounds={"src/ops/dream.py": 4},
            gate_failures={"src/ops/dream.py": 1},
        )
        block = steering.evidence_for(idx, {"module": "src/ops/"})
        # only the two src/ops/ files, not src/cli/x.py
        assert block["rework_count"] == 3
        assert block["fix_rounds"] == 4
        assert block["gate_failures"] == 1
        assert block["superseded_decisions"] == 0

    def test_exact_file_match(self):
        idx = _index(rework={"a/b.py": 2, "a/bc.py": 9})
        # "a/b.py" must not swallow "a/bc.py" — exact-or-slash-boundary
        block = steering.evidence_for(idx, {"paths": ["a/b.py"]})
        assert block["rework_count"] == 2

    def test_hub_pressure_from_candidate_concepts(self):
        idx = _index(hub_pressure={"indexer": 0.7, "retrieval": 0.2})
        block = steering.evidence_for(
            idx, {"module": "src/x.py", "concepts": ["indexer", "retrieval"]}
        )
        assert block["hub_pressure"] == pytest.approx(0.9)

    def test_weight_is_weighted_sum_of_raw_counts(self):
        idx = _index(
            rework={"m.py": 2}, fix_rounds={"m.py": 3},
            superseded={"m.py": 1}, gate_failures={"m.py": 4},
            hub_pressure={"c": 0.5},
        )
        weights = {"rework": 2.0, "fix_rounds": 1.0, "superseded": 3.0,
                   "gate_failures": 1.0, "hub_pressure": 10.0}
        block = steering.evidence_for(
            idx, {"module": "m.py", "concepts": ["c"]}, weights=weights
        )
        # 2*2 + 1*3 + 3*1 + 1*4 + 10*0.5 = 4+3+3+4+5 = 19
        assert block["weight"] == pytest.approx(19.0)
        # raw counts preserved, never a composite only
        assert block["rework_count"] == 2
        assert block["superseded_decisions"] == 1

    def test_has_evidence_true_iff_any_nonzero_raw_signal(self):
        empty = steering.evidence_for(_index(), {"module": "m.py"})
        assert steering.has_evidence(empty) is False
        one = steering.evidence_for(_index(gate_failures={"m.py": 1}), {"module": "m.py"})
        assert steering.has_evidence(one) is True


# ---------------------------------------------------------------------------
# gate_proposals — the two acceptance criteria + ranking + block content
# ---------------------------------------------------------------------------


class TestGateProposals:
    def _idx(self):
        return _index(
            rework={"hot.py": 5, "warm.py": 2, "cool.py": 1},
            gate_failures={"hot.py": 3},
        )

    def test_no_evidence_candidate_is_dropped_not_filed(self):
        idx = self._idx()
        cands = [{"module": "nowhere.py", "rationale": "invented work"}]
        out = steering.gate_proposals(cands, idx, weekly_budget=3)
        assert out["filed"] == []
        assert len(out["dropped"]) == 1
        assert out["dropped"][0]["reason"] == "no cited evidence"
        assert out["dropped"][0]["module"] == "nowhere.py"

    def test_budget_caps_filed_count_and_keeps_top_by_weight(self):
        idx = self._idx()
        cands = [
            {"module": "cool.py"},   # weight 1
            {"module": "warm.py"},   # weight 2
            {"module": "hot.py"},    # weight 5+3 = 8
        ]
        out = steering.gate_proposals(cands, idx, weekly_budget=2)
        filed_modules = [f["module"] for f in out["filed"]]
        # top-2 by weight: hot then warm; cool dropped for budget
        assert filed_modules == ["hot.py", "warm.py"]
        assert len(out["filed"]) == 2
        dropped = [d for d in out["dropped"]]
        assert any(d["module"] == "cool.py" and d["reason"] == "exceeded weekly budget"
                   for d in dropped)

    def test_filed_body_carries_machine_readable_evidence_block(self):
        idx = self._idx()
        out = steering.gate_proposals(
            [{"module": "hot.py", "rationale": "reduce churn"}], idx, weekly_budget=3
        )
        filed = out["filed"][0]
        body = filed["body"]
        assert "reduce churn" in body
        # a fenced ```json block parseable back to the real counts
        assert "```json" in body
        fenced = body.split("```json", 1)[1].split("```", 1)[0].strip()
        payload = json.loads(fenced)
        assert payload["module"] == "hot.py"
        assert payload["rework_count"] == 5
        assert payload["gate_failures"] == 3
        assert payload["weight"] == pytest.approx(8.0)

    def test_evidence_presence_gate_is_independent_of_zero_weights(self):
        # A candidate with real raw evidence but all-zero weights must NOT be
        # dropped as "no evidence" — admission is about evidence presence,
        # ranking is about weight.
        idx = _index(gate_failures={"m.py": 2})
        zero = {"rework": 0.0, "fix_rounds": 0.0, "superseded": 0.0,
                "gate_failures": 0.0, "hub_pressure": 0.0}
        out = steering.gate_proposals(
            [{"module": "m.py"}], idx, weekly_budget=3, weights=zero
        )
        assert len(out["filed"]) == 1
        assert out["filed"][0]["evidence"]["gate_failures"] == 2


# ---------------------------------------------------------------------------
# build_evidence_index — the index scan over a seeded tmp vault
# ---------------------------------------------------------------------------


def _loop_run(tv, *, issue, outcome_label="", fix_rounds=0, files=None, gates=None):
    tv.vault.create_note(
        note_type=NoteType.NOTE,
        title=f"loop trajectory #{issue}",
        tags=["loop-run"],
        extra_frontmatter={
            "issue": issue,
            "outcome_label": outcome_label,
            "fix_rounds": fix_rounds,
            "files_touched": files or [],
            "gates": gates or [],
        },
    )


def _decision(tv, *, title, status="accepted", file_paths=None, supersedes=None):
    fm = {"status": status, "file_paths": file_paths or []}
    if supersedes:
        fm["supersedes"] = supersedes
    tv.vault.create_note(
        note_type=NoteType.DECISION, title=title, extra_frontmatter=fm
    )


class TestBuildEvidenceIndex:
    def test_scans_trajectories_and_decisions_from_the_index(self, vault_factory):
        tv = vault_factory()
        # Two reworked trajectories over src/ops/dream.py; one clean.
        _loop_run(tv, issue=1, outcome_label="reworked", fix_rounds=2,
                  files=["src/ops/dream.py", "src/ops/steering.py"])
        _loop_run(tv, issue=2, outcome_label="reworked-post-merge", fix_rounds=1,
                  files=["src/ops/dream.py"])
        _loop_run(tv, issue=3, outcome_label="merged-clean", fix_rounds=0,
                  files=["src/ops/dream.py"],
                  gates=[{"id": "tests", "passed": False}])
        # A contested decision touching src/ops/dream.py.
        _decision(tv, title="D1", status="superseded",
                  file_paths=["src/ops/dream.py"])
        _decision(tv, title="D2", status="accepted",
                  file_paths=["src/ops/steering.py"])  # not contested
        tv.indexed()

        idx = steering.build_evidence_index(tv.config)
        assert idx.rework == {"src/ops/dream.py": 2, "src/ops/steering.py": 1}
        assert idx.fix_rounds == {"src/ops/dream.py": 3, "src/ops/steering.py": 2}
        assert idx.gate_failures == {"src/ops/dream.py": 1}
        assert idx.superseded == {"src/ops/dream.py": 1}
        # No dream cycle ran → no PageRank → behavioral pressure empty (optional).
        assert idx.hub_pressure == {}

    def test_hub_pressure_reads_graph_ranks_when_present(self, vault_factory):
        tv = vault_factory()
        _loop_run(tv, issue=1, outcome_label="reworked", files=["a.py"])
        tv.indexed()
        # Simulate a dreamed vault: seed graph_ranks directly (the dream apply
        # phase's output). build_evidence_index must surface it as hub pressure.
        from thinkweave.core.indexer import Indexer

        idx_db = Indexer(config=tv.config)
        try:
            idx_db.db.execute(
                "INSERT INTO graph_ranks (note_id, rank_type, score, computed_at) "
                "VALUES (?,?,?,?)",
                ("n-1", "pagerank:indexer", 0.42, "2026-07-18"),
            )
            idx_db.db.commit()
        finally:
            idx_db.close()

        ev = steering.build_evidence_index(tv.config)
        assert ev.hub_pressure == {"indexer": 0.42}

    def test_gate_over_built_index_drops_no_evidence_candidate(self, vault_factory):
        tv = vault_factory()
        _loop_run(tv, issue=1, outcome_label="reworked", fix_rounds=2,
                  files=["src/ops/dream.py"])
        tv.indexed()
        ev = steering.build_evidence_index(tv.config)
        out = steering.gate_proposals(
            [
                {"module": "src/ops/dream.py", "rationale": "real churn"},
                {"module": "src/untouched.py", "rationale": "invented"},
            ],
            ev,
            weekly_budget=3,
        )
        assert [f["module"] for f in out["filed"]] == ["src/ops/dream.py"]
        assert any(d["module"] == "src/untouched.py"
                   and d["reason"] == "no cited evidence" for d in out["dropped"])


# ---------------------------------------------------------------------------
# Config plumbing — [steering] knobs
# ---------------------------------------------------------------------------


class TestConfigKnobs:
    def test_defaults_when_unset(self):
        cfg = Config()
        assert steering._cfg_budget(cfg) == steering.DEFAULT_WEEKLY_BUDGET
        assert steering._cfg_weights(cfg) == steering.DEFAULT_WEIGHTS

    def test_toml_section_overrides_budget_and_weights(self, tmp_path):
        from thinkweave.core.config import load_config

        vault = tmp_path / "vault"
        (vault / "config").mkdir(parents=True)
        (vault / "config" / "config.toml").write_text(
            "vault_root = "
            + repr(str(vault))
            + "\n\n[steering]\nweekly_budget = 5\n"
            "weight_rework = 2.5\nweight_gate_failures = 0.0\n",
            encoding="utf-8",
        )
        import os

        old = os.environ.pop("THINKWEAVE_VAULT", None)
        os.environ["THINKWEAVE_VAULT"] = str(vault)
        try:
            cfg = load_config()
        finally:
            if old is not None:
                os.environ["THINKWEAVE_VAULT"] = old
            else:
                os.environ.pop("THINKWEAVE_VAULT", None)
        assert cfg.steering_weekly_budget == 5
        # cfg holds only the overrides; _cfg_weights merges over DEFAULT_WEIGHTS.
        merged = steering._cfg_weights(cfg)
        assert merged["rework"] == 2.5
        assert merged["gate_failures"] == 0.0
        # untouched weights keep their default
        assert merged["superseded"] == 1.0
        # the gate reads the same merged view through cfg
        assert steering._cfg_budget(cfg) == 5

    def test_budget_zero_is_honored_not_swallowed_to_default(self, tmp_path):
        # A config weekly_budget = 0 means "file nothing" (a valid pause knob);
        # it must NOT collapse to the default 3.
        from thinkweave.core.config import load_config

        vault = tmp_path / "vault"
        (vault / "config").mkdir(parents=True)
        (vault / "config" / "config.toml").write_text(
            "vault_root = " + repr(str(vault)) + "\n\n[steering]\nweekly_budget = 0\n",
            encoding="utf-8",
        )
        import os

        old = os.environ.get("THINKWEAVE_VAULT")
        os.environ["THINKWEAVE_VAULT"] = str(vault)
        try:
            cfg = load_config()
        finally:
            if old is not None:
                os.environ["THINKWEAVE_VAULT"] = old
            else:
                os.environ.pop("THINKWEAVE_VAULT", None)
        assert cfg.steering_weekly_budget == 0
        assert steering._cfg_budget(cfg) == 0
        # end-to-end: a candidate WITH real evidence still files nothing at budget 0
        idx = _index(rework={"m.py": 3})
        out = steering.gate_proposals([{"module": "m.py"}], idx, cfg)
        assert out["filed"] == []
        assert out["dropped"][0]["reason"] == "exceeded weekly budget"


# ---------------------------------------------------------------------------
# CLI contract — weave steering evidence / gate
# ---------------------------------------------------------------------------


class TestCliContract:
    def test_subcommand_registered_in_dispatch_and_parser(self):
        from thinkweave.surfaces.cli import _DISPATCH, build_parser
        import argparse as _argparse

        assert "steering" in _DISPATCH
        parser = build_parser()
        sub = next(
            a for a in parser._actions
            if isinstance(a, _argparse._SubParsersAction)
        )
        assert "steering" in sub.choices

    def test_gate_cli_over_seeded_vault_emits_filed_dropped(
        self, vault_factory, tmp_path, monkeypatch, capsys
    ):
        from thinkweave.surfaces.cli.steering import cmd_steering

        tv = vault_factory()
        _loop_run(tv, issue=1, outcome_label="reworked", fix_rounds=2,
                  files=["src/ops/dream.py"])
        tv.indexed()
        # The CLI loads config via load_config(); point it at this tmp vault.
        monkeypatch.setattr(
            "thinkweave.surfaces.cli.steering.load_config", lambda: tv.config
        )

        proposals = tmp_path / "proposals.json"
        proposals.write_text(
            json.dumps([
                {"module": "src/ops/dream.py", "rationale": "reduce churn"},
                {"module": "src/nowhere.py", "rationale": "invented"},
            ]),
            encoding="utf-8",
        )
        args = argparse_ns(steering_action="gate",
                           proposals_json=str(proposals), json=True)
        cmd_steering(args)
        out = capsys.readouterr().out
        result = json.loads(out)
        assert [f["module"] for f in result["filed"]] == ["src/ops/dream.py"]
        assert any(d["module"] == "src/nowhere.py"
                   and d["reason"] == "no cited evidence"
                   for d in result["dropped"])
        # filed proposal carries the machine-readable evidence block in its body
        assert "```json" in result["filed"][0]["body"]

    def test_evidence_cli_json_lists_modules(
        self, vault_factory, monkeypatch, capsys
    ):
        from thinkweave.surfaces.cli.steering import cmd_steering

        tv = vault_factory()
        _loop_run(tv, issue=1, outcome_label="reworked", fix_rounds=3,
                  files=["src/a.py"])
        tv.indexed()
        monkeypatch.setattr(
            "thinkweave.surfaces.cli.steering.load_config", lambda: tv.config
        )
        args = argparse_ns(steering_action="evidence", module="", json=True)
        cmd_steering(args)
        result = json.loads(capsys.readouterr().out)
        mods = {m["module"]: m for m in result["modules"]}
        assert mods["src/a.py"]["rework_count"] == 1
        assert mods["src/a.py"]["fix_rounds"] == 3


def argparse_ns(**kw):
    import argparse
    return argparse.Namespace(**kw)
