"""Tests for the deterministic rail of the issue-to-PR loop.

Everything here is pure: parsing and frontier computation take plain dicts
and strings — no gh, no git, no network.
"""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "issue_loop", Path(__file__).resolve().parent.parent / "scripts" / "issue_loop.py"
)
issue_loop = importlib.util.module_from_spec(_SPEC)
sys.modules["issue_loop"] = issue_loop
_SPEC.loader.exec_module(issue_loop)


# ---------------------------------------------------------------------------
# parse_blockers — both serializations


def test_header_form_single_blocker():
    body = "Track: A-ontology | Wave: 2 | Blocked-by: #16 | Parallel-safe: yes | Epic: #11"
    assert issue_loop.parse_blockers(body) == [16]


def test_header_form_epic_ref_is_not_a_blocker():
    body = "Track: B-core | Wave: 1 | Blocked-by: — | Parallel-safe: yes | Epic: #11"
    assert issue_loop.parse_blockers(body) == []


def test_header_form_multiple_blockers():
    body = "Wave: 3 | Blocked-by: #16, #17 | Epic: #11"
    assert issue_loop.parse_blockers(body) == [16, 17]


def test_section_form():
    body = "## What to build\nStuff referencing #5.\n\n## Blocked by\n\n- #12\n- #14\n\n## Acceptance criteria\n- [ ] done"
    assert issue_loop.parse_blockers(body) == [12, 14]


def test_section_form_none():
    body = "## What to build\nStuff.\n\n## Blocked by\n\nNone - can start immediately\n"
    assert issue_loop.parse_blockers(body) == []


def test_no_blocked_by_anywhere():
    assert issue_loop.parse_blockers("Fix the thing in #33's shadow.") == []
    assert issue_loop.parse_blockers("") == []


def test_wave_and_parallel_safe():
    body = "Track: A | Wave: 2 | Blocked-by: — | Parallel-safe: no | Epic: #11"
    assert issue_loop.parse_wave(body) == 2
    assert issue_loop.parse_parallel_safe(body) is False
    assert issue_loop.parse_wave("no header") is None
    assert issue_loop.parse_parallel_safe("no header") is True  # default: don't serialize the loop


# ---------------------------------------------------------------------------
# compute_frontier


CFG = {
    "labels": {"runnable": "ready-for-agent", "claimed": "agent-claimed"},
    "loop": {},
}


def _issue(number, state="OPEN", labels=("ready-for-agent",), body="", **extra):
    return {
        "number": number,
        "title": f"Issue {number}",
        "state": state,
        "labels": [{"name": l} for l in labels],
        "body": body,
        **extra,
    }


def test_frontier_requires_closed_blockers():
    issues = [
        _issue(1, state="CLOSED"),
        _issue(2, body="Blocked-by: #1"),
        _issue(3, body="Blocked-by: #2"),
    ]
    result = issue_loop.compute_frontier(issues, CFG)
    assert [e["number"] for e in result["frontier"]] == [2]
    assert [e["number"] for e in result["blocked"]] == [3]
    assert result["blocked"][0]["open_blockers"] == [2]


def test_frontier_excludes_unlabeled_and_claimed():
    issues = [
        _issue(1, labels=("bug",)),  # not ready-for-agent
        _issue(2, labels=("ready-for-agent", "agent-claimed")),
        _issue(3),
    ]
    result = issue_loop.compute_frontier(issues, CFG)
    assert [e["number"] for e in result["frontier"]] == [3]
    assert [e["number"] for e in result["claimed"]] == [2]


def test_assignee_is_a_claim():
    issues = [
        _issue(1, assignees=[{"login": "marekpal97"}]),
        _issue(2),
    ]
    result = issue_loop.compute_frontier(issues, CFG)
    assert [e["number"] for e in result["frontier"]] == [2]
    assert result["claimed"][0]["assignees"] == ["marekpal97"]


def test_native_dependencies_gate_frontier():
    # native count gates even without the edge list
    issues = [_issue(2, native_blocked_count=1)]
    result = issue_loop.compute_frontier(issues, CFG)
    assert result["frontier"] == []
    assert "native" in result["blocked"][0]["open_blockers_note"]
    # with the edge list, blockers are named and closure unblocks
    issues = [
        _issue(1, state="CLOSED"),
        _issue(2, native_blocked_count=0, native_blockers=[1]),
        _issue(3, native_blocked_count=1, native_blockers=[4]),
        _issue(4),
    ]
    result = issue_loop.compute_frontier(issues, CFG)
    assert [e["number"] for e in result["frontier"]] == [2, 4]
    assert result["blocked"][0]["open_blockers"] == [4]


def test_union_of_native_and_body_edges():
    issues = [
        _issue(2, body="Blocked-by: #5", native_blockers=[6], native_blocked_count=1),
        _issue(5),
        _issue(6),
    ]
    result = issue_loop.compute_frontier(issues, CFG)
    blocked = result["blocked"][0]
    assert blocked["blockers"] == [5, 6]
    assert blocked["open_blockers"] == [5, 6]


def test_components_split_unrelated_dags():
    issues = [
        _issue(1),
        _issue(2, body="Blocked-by: #1"),
        _issue(10),
        _issue(11, native_blockers=[10], native_blocked_count=1),
        _issue(20),  # isolated
    ]
    comp = issue_loop.compute_components(issues)
    assert comp[1] == comp[2] == 1
    assert comp[10] == comp[11] == 10
    assert comp[20] == 20
    result = issue_loop.compute_frontier(issues, CFG)
    by_num = {e["number"]: e for e in result["frontier"]}
    assert by_num[1]["component"] == 1 and by_num[10]["component"] == 10
    assert by_num[20]["component"] == 20


def test_components_ignore_closed_issues():
    issues = [
        _issue(1, state="CLOSED"),
        _issue(2, body="Blocked-by: #1"),
        _issue(3, body="Blocked-by: #1"),
    ]
    comp = issue_loop.compute_components(issues)
    # 2 and 3 only share a CLOSED blocker — no open edge between them
    assert comp[2] != comp[3]


def test_frontier_wave_ordering_and_limit():
    issues = [
        _issue(5, body="Wave: 2 | Blocked-by: —"),
        _issue(6, body="Wave: 1 | Blocked-by: —"),
        _issue(7),  # no wave → sorts last
        _issue(8, body="Wave: 1 | Blocked-by: —"),
    ]
    result = issue_loop.compute_frontier(issues, CFG)
    assert [e["number"] for e in result["frontier"]] == [6, 8, 5, 7]
    limited = issue_loop.compute_frontier(issues, CFG, limit=2)
    assert [e["number"] for e in limited["frontier"]] == [6, 8]


def test_frontier_missing_blocker_is_satisfied_but_warned():
    issues = [_issue(2, body="Blocked-by: #999")]
    result = issue_loop.compute_frontier(issues, CFG)
    assert [e["number"] for e in result["frontier"]] == [2]
    assert any("#999" in w for w in result["warnings"])


def test_closed_issues_never_in_frontier():
    issues = [_issue(1, state="CLOSED")]
    result = issue_loop.compute_frontier(issues, CFG)
    assert result["frontier"] == [] and result["blocked"] == []


# ---------------------------------------------------------------------------
# diff gate — pure evaluation over numstat text


def test_diff_gate_forbidden_path():
    gate = {"id": "g", "forbidden_paths": [".github/workflows/"], "max_changed_lines": 100}
    numstat = "3\t1\tsrc/thinkweave/core/config.py\n2\t0\t.github/workflows/ci.yml\n"
    result = issue_loop.evaluate_diff_gate(gate, numstat)
    assert result["passed"] is False
    assert ".github/workflows/ci.yml" in result["summary"]


def test_diff_gate_max_lines():
    gate = {"id": "g", "forbidden_paths": [], "max_changed_lines": 5}
    numstat = "4\t3\tsrc/a.py\n"
    result = issue_loop.evaluate_diff_gate(gate, numstat)
    assert result["passed"] is False and "7 changed lines" in result["summary"]


def test_diff_gate_passes_and_handles_binary():
    gate = {"id": "g", "forbidden_paths": ["vault/"], "max_changed_lines": 100}
    numstat = "4\t3\tsrc/a.py\n-\t-\tassets/logo.png\n"
    result = issue_loop.evaluate_diff_gate(gate, numstat)
    assert result["passed"] is True


# ---------------------------------------------------------------------------
# config


def test_load_config_defaults_when_missing(tmp_path):
    cfg = issue_loop.load_config(tmp_path / "nope.toml")
    assert cfg["loop"]["max_issues_per_run"] == 3
    assert cfg["loop"]["require_green_baseline"] is True
    assert cfg["loop"]["claim_mode"] == "assign"
    assert cfg["loop"]["run_mode"] == "pass"
    assert cfg["labels"]["runnable"] == "ready-for-agent"
    assert cfg["tdd"]["mode"] == "auto"
    assert cfg["gates"] == []


def test_load_config_tdd_override(tmp_path):
    p = tmp_path / "loop.toml"
    p.write_text('[tdd]\nmode = "never"\n', encoding="utf-8")
    assert issue_loop.load_config(p)["tdd"]["mode"] == "never"


def test_load_config_merges_file(tmp_path):
    p = tmp_path / "loop.toml"
    p.write_text(
        '[loop]\nmax_issues_per_run = 5\n\n[[gates]]\nid = "tests"\nkind = "command"\ncmd = "pytest"\n',
        encoding="utf-8",
    )
    cfg = issue_loop.load_config(p)
    assert cfg["loop"]["max_issues_per_run"] == 5
    assert cfg["loop"]["max_fix_rounds"] == 2  # default survives partial override
    assert cfg["gates"][0]["id"] == "tests"


def test_repo_loop_toml_parses_and_gate_ids_unique():
    cfg = issue_loop.load_config()
    ids = [g["id"] for g in cfg["gates"]]
    assert len(ids) == len(set(ids)) and len(ids) >= 4
    assert all(g["kind"] in {"command", "diff", "acceptance", "review", "simplify"}
               for g in cfg["gates"])


# ---------------------------------------------------------------------------
# simplify gate (issue #58) — ponytail over-engineering trim, applying gate


def test_gate_pipeline_order_is_pinned():
    """The full pipeline order is a contract: diff-guard → tests → acceptance
    → review → simplify. simplify runs LAST, after review, so it only ever
    shrinks an already-verified diff."""
    cfg = issue_loop.load_config()
    ids = [g["id"] for g in cfg["gates"]]
    assert ids == ["diff-guard", "tests", "acceptance", "review", "simplify"]


def test_simplify_gate_shape():
    """The simplify gate is a non-required LLM/orchestrator kind whose
    'failure' mode is a revert (never a pipeline block): it re-runs the
    verification gates on the simplified diff and, if either goes red, ships
    the pre-simplify diff with the revert note."""
    cfg = issue_loop.load_config()
    gate = next(g for g in cfg["gates"] if g["id"] == "simplify")
    assert gate["kind"] == "simplify"
    # required=false: simplify can never fail the pipeline — its failure ships
    # the pre-simplify diff (documented in issue-loop.command.md §1c-simplify).
    assert gate["required"] is False
    # It re-verifies the shrunk diff against exactly the deterministic +
    # behavioral gates, in order.
    assert gate["rerun"] == ["tests", "acceptance"]
    assert "simplify-reverted" in gate["revert_note"]
    # The delete-list comes from the vendored ponytail-review skill.
    assert gate["skill"] == "ponytail-review"


def test_check_rejects_simplify_as_orchestrator_kind(tmp_path, capsys):
    """`check` only executes deterministic kinds (command/diff). An unknown /
    LLM-judged kind like simplify must be PASSED THROUGH — surfaced with the
    same 'run it from the command' error as acceptance/review, not rejected by
    the loader. Regression guard: the config loader does not hard-validate
    kinds, so a new orchestrator gate parses and surfaces without a code change
    to the rail."""
    rc = issue_loop.main(["check", "--gate", "simplify", "--cwd", str(tmp_path)])
    assert rc == 2
    err = json.loads(capsys.readouterr().out)
    assert "LLM-judged" in err["error"]


def test_committed_hooks_carry_no_ponytail_entries():
    """Acceptance criterion: vendoring the skill installs NO ponytail hooks.
    The committed hook manifest must contain no ponytail UserPromptSubmit /
    PreToolUse entry (ponytail's plugin would collide with weave's own
    UserPromptSubmit hook)."""
    hooks = (issue_loop.REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
    assert "ponytail" not in hooks.lower()


def test_vendored_ponytail_review_skill_present_with_provenance():
    """The ponytail-review skill is vendored as dev tooling under docs/agents/
    with pinned-upstream provenance (sha + source repo) and the machine-local
    symlink wiring documented in-header (symlinks into .claude/commands/ are
    not committed — mirrors how issue-loop.command.md is wired)."""
    vendored = issue_loop.REPO_ROOT / "docs" / "agents" / "ponytail-review.command.md"
    assert vendored.exists()
    text = vendored.read_text(encoding="utf-8")
    # Provenance: the canonical upstream repo and the pinned commit sha.
    assert "DietrichGebert/ponytail" in text
    assert "16f29800fd2681bdf24f3eb4ccffe38be3baec6b" in text
    # The wiring note (ln -s into .claude/commands/), since the symlink itself
    # is machine-local and not committed.
    assert ".claude/commands/" in text and "ln -s" in text
    # The skill's actual delete-list vocabulary survived the vendoring.
    for tag in ("delete:", "stdlib:", "yagni:", "shrink:"):
        assert tag in text


# ---------------------------------------------------------------------------
# trajectory payload (memory-feed proposal) — pure assembly


def test_build_trajectory_payload():
    issue = {
        "number": 26,
        "title": "D1: Queue.items_since() — close the archive leak",
        "html_url": "https://github.com/x/y/issues/26",
        "labels": [{"name": "ready-for-agent"}, {"name": "track:D-acquisition"}],
    }
    payload = issue_loop.build_trajectory(
        issue,
        branch="loop/issue-26",
        commits=["abc fix", "def test"],
        numstat="10\t2\tsrc/thinkweave/acquisition/queue.py\n5\t0\ttests/test_queue.py\n",
        gates=[{"id": "tests", "kind": "command", "passed": True, "summary": "exit 0"}],
        fix_rounds=1,
        outcome="shipped",
        pr_url="https://github.com/x/y/pull/99",
        run_id="loop-20260713-abcd",
    )
    fm = payload["frontmatter"]
    assert payload["type"] == "note" and payload["tags"] == ["loop-run"]
    assert fm["issue"] == 26 and fm["outcome"] == "shipped" and fm["fix_rounds"] == 1
    assert fm["commits"] == 2
    assert fm["files_touched"] == ["src/thinkweave/acquisition/queue.py", "tests/test_queue.py"]
    assert fm["gates"] == [{"id": "tests", "passed": True, "summary": "exit 0"}]
    assert "track:D-acquisition" in payload["concept_hints"]
    assert "Lessons" in payload["body_skeleton"]


def test_build_trajectory_defaults_to_empty_skills():
    """Existing callers pass no skills data (backward compat): frontmatter
    carries an empty skills[] and the record stays a plain [loop-run] note —
    no [skill-invocation] tag."""
    payload = issue_loop.build_trajectory(
        {"number": 1, "title": "x", "labels": []},
        branch="b", commits=[], numstat="", gates=[],
        fix_rounds=0, outcome="shipped",
    )
    assert payload["frontmatter"]["skills"] == []
    assert payload["tags"] == ["loop-run"]


def test_build_trajectory_skills_shape():
    """skills[] normalizes each dispatched stage into {id, role, outcome,
    fix_rounds_attributed}, preserving dispatch order, dropping extra keys,
    and defaulting a missing attribution count to 0. The skill-centric flag
    adds the [skill-invocation] tag. Expected values are hand-written from
    the issue's frontmatter schema, not recomputed by the code under test."""
    issue = {"number": 56, "title": "Generalize the trajectory note", "labels": []}
    skills_log = [
        {"id": "implementer", "role": "implementer", "outcome": "shipped",
         "fix_rounds_attributed": 0, "worktree": "/tmp/wt"},  # extra key dropped
        {"id": "acceptance-judge", "role": "acceptance", "outcome": "not-met",
         "fix_rounds_attributed": 2},
        {"id": "code-reviewer", "role": "reviewer", "outcome": "passed"},  # no count → 0
    ]
    payload = issue_loop.build_trajectory(
        issue, branch="loop/dag-54", commits=["a"], numstat="1\t0\tx.py\n",
        gates=[{"id": "acceptance", "kind": "acceptance", "passed": True, "summary": ""}],
        fix_rounds=2, outcome="shipped", skills=skills_log, skill_centric=True,
    )
    assert payload["frontmatter"]["skills"] == [
        {"id": "implementer", "role": "implementer", "outcome": "shipped",
         "fix_rounds_attributed": 0},
        {"id": "acceptance-judge", "role": "acceptance", "outcome": "not-met",
         "fix_rounds_attributed": 2},
        {"id": "code-reviewer", "role": "reviewer", "outcome": "passed",
         "fix_rounds_attributed": 0},
    ]
    assert payload["tags"] == ["loop-run", "skill-invocation"]


def test_trajectory_argparse_contract():
    """The trajectory subcommand exposes --skills-json (optional, default
    None) and --skill-centric (store_true, default False), so the
    orchestrator can pass its dispatch log and mark skill-centric records."""
    ns = issue_loop.build_arg_parser().parse_args([
        "trajectory", "56", "--gates-json", "g.json",
        "--skills-json", "s.json", "--skill-centric",
        "--outcome", "shipped",
    ])
    assert ns.skills_json == "s.json"
    assert ns.skill_centric is True
    ns2 = issue_loop.build_arg_parser().parse_args([
        "trajectory", "56", "--gates-json", "g.json", "--outcome", "shipped",
    ])
    assert ns2.skills_json is None
    assert ns2.skill_centric is False


def test_build_trajectory_mirrors_primed_and_served():
    """The frontmatter mirrors the prime verdict: primed:true + the served ids
    when the run was primed; primed:false + empty served when held out."""
    issue = {"number": 57, "title": "prime the implementer", "labels": []}
    primed = issue_loop.build_trajectory(
        issue, branch="loop/dag-54", commits=["a"], numstat="1\t0\tx.py\n",
        gates=[], fix_rounds=0, outcome="shipped",
        primed=True, served=["n-prior1", "dec-abc222"],
    )
    assert primed["frontmatter"]["primed"] is True
    assert primed["frontmatter"]["served"] == ["n-prior1", "dec-abc222"]

    held = issue_loop.build_trajectory(
        issue, branch="b", commits=[], numstat="", gates=[],
        fix_rounds=0, outcome="shipped", primed=False,
    )
    assert held["frontmatter"]["primed"] is False
    assert held["frontmatter"]["served"] == []


def test_build_trajectory_omits_prime_keys_when_unknown():
    """Backward compat: callers that pass no prime data (primed=None) get a
    note with no primed/served keys — the pre-#57 shape is unchanged."""
    payload = issue_loop.build_trajectory(
        {"number": 1, "title": "x", "labels": []},
        branch="b", commits=[], numstat="", gates=[],
        fix_rounds=0, outcome="shipped",
    )
    assert "primed" not in payload["frontmatter"]
    assert "served" not in payload["frontmatter"]


def test_trajectory_prime_argparse_contract():
    """--primed/--no-primed (default None) + --served-json (default None) let
    the orchestrator mirror the prime verdict into the trajectory note."""
    ns = issue_loop.build_arg_parser().parse_args([
        "trajectory", "57", "--gates-json", "g.json", "--outcome", "shipped",
        "--primed", "--served-json", "served.json",
    ])
    assert ns.primed is True and ns.served_json == "served.json"
    ns_held = issue_loop.build_arg_parser().parse_args([
        "trajectory", "57", "--gates-json", "g.json", "--outcome", "routed-to-human",
        "--no-primed",
    ])
    assert ns_held.primed is False
    ns_default = issue_loop.build_arg_parser().parse_args([
        "trajectory", "57", "--gates-json", "g.json", "--outcome", "shipped",
    ])
    assert ns_default.primed is None and ns_default.served_json is None


# ---------------------------------------------------------------------------
# --dag scoping and --assume-done (stacked delivery)


def test_scope_to_dag_keeps_component_and_closed_issues():
    issues = [
        _issue(1, state="CLOSED"),
        _issue(2, body="Blocked-by: #1"),   # component 2 (edge to 1 is closed)
        _issue(3, body="Blocked-by: #2"),   # component 2
        _issue(10),                          # unrelated component
    ]
    scoped = issue_loop.scope_to_dag(issues, 3)
    nums = sorted(i["number"] for i in scoped)
    assert nums == [1, 2, 3]  # closed #1 kept for blocker checks, #10 dropped


def test_scope_to_dag_rejects_closed_or_missing_root():
    with pytest.raises(ValueError):
        issue_loop.scope_to_dag([_issue(1, state="CLOSED")], 1)
    with pytest.raises(ValueError):
        issue_loop.scope_to_dag([_issue(1)], 99)


def test_assume_done_unblocks_dependents():
    issues = [
        _issue(16),
        _issue(21, body="Blocked-by: #16"),
        _issue(22, native_blockers=[16], native_blocked_count=1),
    ]
    before = issue_loop.compute_frontier(issues, CFG)
    assert [e["number"] for e in before["frontier"]] == [16]
    after = issue_loop.compute_frontier(
        issue_loop.apply_assume_done(issues, {16}), CFG)
    assert [e["number"] for e in after["frontier"]] == [21, 22]


def test_assume_done_does_not_mutate_input():
    issues = [_issue(16)]
    issue_loop.apply_assume_done(issues, {16})
    assert issues[0]["state"] == "OPEN"


# ---------------------------------------------------------------------------
# --set overrides — per-run posture on top of loop.toml defaults


def test_parse_override_section_defaults_to_loop():
    assert issue_loop.parse_override("delivery=stacked") == ("loop", "delivery", "stacked")


def test_parse_override_toml_scalars():
    assert issue_loop.parse_override("max_issues_per_run=6") == ("loop", "max_issues_per_run", 6)
    assert issue_loop.parse_override("training_mode=false") == ("loop", "training_mode", False)
    assert issue_loop.parse_override('tdd.mode="never"') == ("tdd", "mode", "never")
    assert issue_loop.parse_override("labels.runnable=agent-go") == ("labels", "runnable", "agent-go")


def test_parse_override_malformed():
    for bad in ("delivery", "=stacked", "delivery=", ""):
        with pytest.raises(ValueError):
            issue_loop.parse_override(bad)


def test_apply_overrides_wins_over_file(tmp_path):
    p = tmp_path / "loop.toml"
    p.write_text('[loop]\ndelivery = "pr-per-issue"\nmax_issues_per_run = 3\n', encoding="utf-8")
    cfg = issue_loop.apply_overrides(
        issue_loop.load_config(p), ["delivery=stacked", "max_issues_per_run=6"]
    )
    assert cfg["loop"]["delivery"] == "stacked"
    assert cfg["loop"]["max_issues_per_run"] == 6


def test_apply_overrides_rejects_unknown_key_and_section(tmp_path):
    cfg = issue_loop.load_config(tmp_path / "nope.toml")
    with pytest.raises(ValueError, match="unknown key"):
        issue_loop.apply_overrides(cfg, ["deliverey=stacked"])  # typo protection
    with pytest.raises(ValueError, match="not overridable"):
        issue_loop.apply_overrides(cfg, ["gates.tests=off"])  # gates are file-only


def test_apply_overrides_noop_without_specs(tmp_path):
    cfg = issue_loop.load_config(tmp_path / "nope.toml")
    assert issue_loop.apply_overrides(cfg, []) is cfg


def test_prime_holdout_is_an_overridable_loop_knob(tmp_path):
    """prime_holdout ships as a [loop] default and is --set-overridable via the
    existing mechanism (so `--set prime_holdout=0` disables holdout for a run)."""
    cfg = issue_loop.load_config(tmp_path / "nope.toml")
    assert cfg["loop"]["prime_holdout"] == 5
    overridden = issue_loop.apply_overrides(cfg, ["prime_holdout=0"])
    assert overridden["loop"]["prime_holdout"] == 0


# ---------------------------------------------------------------------------
# prime — claim-time priming from prior trajectories


def test_is_holdout_deterministic_and_disable():
    # Expected values independently computed from sha1(run_id) mod N:
    #   printf 'loop-run-10' | sha1sum  → 7a31...  int mod 5 == 0  → held out
    #   printf 'loop-run-0'  | sha1sum  → 47eb...  int mod 5 == 1  → NOT held out
    assert issue_loop.is_holdout("loop-run-10", 5) is True
    assert issue_loop.is_holdout("loop-run-0", 5) is False
    # Same run-id, same verdict across calls (no PYTHONHASHSEED dependence).
    assert issue_loop.is_holdout("loop-run-10", 5) is True
    # holdout <= 0 disables holdout entirely.
    assert issue_loop.is_holdout("loop-run-10", 0) is False
    assert issue_loop.is_holdout("loop-run-10", -1) is False


def test_extract_lessons_section_only():
    body = (
        "## What\nBuilt the prime rail.\n\n"
        "## How it went\nOne fix round on the CHECK constraint.\n\n"
        "## Lessons\nWiden the CHECK before the migration guard.\nProject from the event log.\n"
    )
    assert issue_loop.extract_lessons(body) == (
        "Widen the CHECK before the migration guard.\nProject from the event log."
    )
    # No Lessons section (the uneventful common case) → empty string.
    assert issue_loop.extract_lessons("## What\nx\n\n## How it went\ny\n") == ""
    assert issue_loop.extract_lessons("") == ""


def test_render_prime_block_splices_lessons_and_lists_served():
    trajectories = [
        {"id": "n-aaa111", "title": "prime rail", "issue": 57, "outcome": "shipped",
         "lessons": "Widen the CHECK first."},
        {"id": "n-bbb222", "title": "trajectory judge", "issue": 60, "outcome": "shipped",
         "lessons": "Judge from the PR timeline."},
    ]
    block, served = issue_loop.render_prime_block(trajectories, decisions=["dec-ccc333"])
    assert "Widen the CHECK first." in block
    assert "Judge from the PR timeline." in block
    assert "dec-ccc333" in block  # decisions folded in as adjacency
    assert served == ["n-aaa111", "n-bbb222", "dec-ccc333"]
    # Nothing to serve → clean skip.
    assert issue_loop.render_prime_block([], decisions=[]) == ("", [])


def test_render_prime_block_honors_char_budget():
    trajectories = [
        {"id": "n-1", "title": "t1", "issue": 1, "outcome": "shipped", "lessons": "L" * 400},
        {"id": "n-2", "title": "t2", "issue": 2, "outcome": "shipped", "lessons": "M" * 400},
        {"id": "n-3", "title": "t3", "issue": 3, "outcome": "shipped", "lessons": "N" * 400},
    ]
    block, served = issue_loop.render_prime_block(trajectories, budget_chars=600)
    # First piece always lands; the budget stops further pieces before all three.
    assert served == ["n-1"]
    assert "n-2" not in served and "n-3" not in served


def test_build_prime_payload_holdout_runs_unprimed():
    payload = issue_loop.build_prime_payload(
        57, "loop-run-10", ["self-improvement"], conn=None, holdout=5,
    )
    assert payload["holdout"] is True
    assert payload["primed"] is False
    assert payload["served"] == []
    assert payload["block"] == ""
    assert "held out" in payload["note"]


def test_build_prime_payload_no_index_is_a_clean_noop():
    # No conn (index absent) and not held out → empty, no crash, loop unchanged.
    payload = issue_loop.build_prime_payload(
        57, "loop-run-0", ["self-improvement"], conn=None, holdout=5,
    )
    assert payload["holdout"] is False
    assert payload["primed"] is False
    assert payload["served"] == [] and payload["block"] == ""


def _seed_index_db(path, *, note_id, title, concepts, body, tags=("loop-run",),
                   date="2026-07-18", frontmatter=None):
    """Build a minimal read-side index db (notes + note_tags + note_concepts)
    with one trajectory note — the exact tables prime's query joins."""
    import json as _json
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE notes (id TEXT PRIMARY KEY, type TEXT, title TEXT, path TEXT,"
        " date TEXT, frontmatter TEXT, body_text TEXT);"
        "CREATE TABLE note_tags (note_id TEXT, tag TEXT);"
        "CREATE TABLE note_concepts (note_id TEXT, concept TEXT);"
    )
    conn.execute(
        "INSERT INTO notes (id, type, title, path, date, frontmatter, body_text)"
        " VALUES (?, 'note', ?, ?, ?, ?, ?)",
        (note_id, title, f"{note_id}.md", date,
         _json.dumps(frontmatter or {"issue": 57, "outcome": "shipped"}), body),
    )
    for tag in tags:
        conn.execute("INSERT INTO note_tags VALUES (?, ?)", (note_id, tag))
    for c in concepts:
        conn.execute("INSERT INTO note_concepts VALUES (?, ?)", (note_id, c))
    conn.commit()
    conn.close()


def test_build_prime_payload_splices_matching_trajectory(tmp_path):
    """Acceptance: an issue whose concepts match a prior trajectory gets that
    trajectory's Lessons text in the block, and the search is issued with the
    issue's concepts (a note tagged differently / concept-mismatched is not
    served)."""
    db = tmp_path / "index.db"
    _seed_index_db(
        db, note_id="n-prior1", title="prime rail",
        concepts=["self-improvement", "retrieval"],
        body="## What\nx\n\n## Lessons\nProject context_served from the event log.\n",
    )
    conn = issue_loop._open_index_ro(str(db))
    try:
        payload = issue_loop.build_prime_payload(
            57, "loop-run-0", ["self-improvement"], conn=conn, holdout=5,
        )
    finally:
        conn.close()
    assert payload["primed"] is True
    assert payload["served"] == ["n-prior1"]
    assert "Project context_served from the event log." in payload["block"]


def test_query_trajectories_filters_by_concept_and_lessons(tmp_path):
    db = tmp_path / "index.db"
    # Note A matches concept + has Lessons; note B matches but has NO Lessons.
    _seed_index_db(db, note_id="n-hasl", title="A", concepts=["retrieval"],
                   body="## What\nx\n\n## Lessons\nreuse me\n")
    wconn = sqlite3.connect(str(db))
    wconn.execute("INSERT INTO notes (id, type, title, path, date, frontmatter, body_text)"
                  " VALUES ('n-nol', 'note', 'B', 'n-nol.md', '2026-07-18', '{}', '## What\nno lessons\n')")
    wconn.execute("INSERT INTO note_tags VALUES ('n-nol', 'loop-run')")
    wconn.execute("INSERT INTO note_concepts VALUES ('n-nol', 'retrieval')")
    wconn.commit()
    wconn.close()
    conn = issue_loop._open_index_ro(str(db))
    try:
        # Concept miss → nothing.
        assert issue_loop.query_trajectories(conn, ["unrelated-concept"], 3) == []
        # Concept hit → only the note that actually carries Lessons.
        hits = issue_loop.query_trajectories(conn, ["retrieval"], 3)
    finally:
        conn.close()
    assert [h["id"] for h in hits] == ["n-hasl"]
    assert hits[0]["lessons"] == "reuse me"


def test_prime_writes_loop_prime_served_event_to_buffer(tmp_path):
    """When --buffer is given and the run is primed, the rail appends one
    loop_prime retrieval event (the context_served source seed) with the served
    ids — mirroring the prompt-time serving surface."""
    db = tmp_path / "index.db"
    _seed_index_db(db, note_id="n-prior1", title="t", concepts=["retrieval"],
                   body="## Lessons\nreuse\n")
    buf = tmp_path / "buffer" / "ses-loop123.jsonl"
    rc = issue_loop.main([
        "prime", "57", "--run-id", "loop-run-0",
        "--concepts", "retrieval", "--db", str(db),
        "--buffer", str(buf), "--session-id", "ses-loop123",
    ])
    assert rc == 0
    lines = [l for l in buf.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    ev = json.loads(lines[0])
    assert ev["type"] == "retrieval" and ev["tool"] == "loop_prime"
    assert ev["returned_ids"] == ["n-prior1"]
    assert ev["args"]["run_id"] == "loop-run-0" and ev["args"]["issue"] == 57


def test_prime_holdout_writes_no_buffer_event(tmp_path):
    """A held-out run is unprimed: no served ids, no buffer event even if
    --buffer is supplied."""
    buf = tmp_path / "buffer" / "ses-loop123.jsonl"
    rc = issue_loop.main([
        "prime", "57", "--run-id", "loop-run-10",  # sha1 mod 5 == 0 → held out
        "--concepts", "retrieval", "--buffer", str(buf),
    ])
    assert rc == 0
    assert not buf.exists()


def test_prime_degrades_on_corrupt_index(tmp_path, capsys):
    """A foreign/corrupt file at the resolved --db path must NOT crash the loop
    (sqlite3.connect is lazy — the DatabaseError surfaces on the first query,
    past main's connect guard). Regression: rc 0 + unprimed payload."""
    db = tmp_path / "index.db"
    db.write_bytes(b"GIF89a this is definitely not a sqlite database\n" * 8)
    rc = issue_loop.main([
        "prime", "57", "--run-id", "loop-run-0",
        "--concepts", "retrieval", "--db", str(db),
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["primed"] is False
    assert payload["served"] == [] and payload["block"] == ""
    assert "unread" in payload["note"].lower()


def test_prime_degrades_on_schema_drift_index(tmp_path, capsys):
    """A valid but older index missing the tables prime joins (note_tags /
    note_concepts) raises OperationalError on the query — must also degrade to
    an unprimed payload, rc 0."""
    db = tmp_path / "index.db"
    wconn = sqlite3.connect(str(db))
    wconn.execute("CREATE TABLE notes (id TEXT PRIMARY KEY, title TEXT)")  # no join tables
    wconn.commit()
    wconn.close()
    rc = issue_loop.main([
        "prime", "57", "--run-id", "loop-run-0",
        "--concepts", "retrieval", "--db", str(db),
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["primed"] is False and payload["served"] == []
    assert "unread" in payload["note"].lower()


def test_build_prime_payload_index_error_notes_degradation(tmp_path):
    """At the seam: a query that raises sqlite3.Error degrades to primed:false
    with a note distinct from the no-match note."""
    db = tmp_path / "index.db"
    wconn = sqlite3.connect(str(db))
    wconn.execute("CREATE TABLE notes (id TEXT PRIMARY KEY, title TEXT)")
    wconn.commit()
    wconn.close()
    conn = issue_loop._open_index_ro(str(db))
    try:
        payload = issue_loop.build_prime_payload(
            57, "loop-run-0", ["retrieval"], conn=conn, holdout=5,
        )
    finally:
        conn.close()
    assert payload["primed"] is False and payload["served"] == []
    assert "unread" in payload["note"].lower()


def test_build_trajectory_rejects_non_list_or_non_string_served():
    """--served-json shape guard: a dict (e.g. the whole prime payload pasted by
    mistake) or a bare string must not silently become frontmatter — it would
    corrupt the served-context regression's raw material."""
    issue = {"number": 1, "title": "x", "labels": []}
    for bad in ({"issue": 57, "served": ["n-a"]}, "n-abc", 42):
        with pytest.raises((ValueError, TypeError)):
            issue_loop.build_trajectory(
                issue, branch="b", commits=[], numstat="", gates=[],
                fix_rounds=0, outcome="shipped", primed=True, served=bad,
            )
    # A list with a non-string element is rejected too.
    with pytest.raises((ValueError, TypeError)):
        issue_loop.build_trajectory(
            issue, branch="b", commits=[], numstat="", gates=[],
            fix_rounds=0, outcome="shipped", primed=True, served=["n-ok", 123],
        )


def test_resolve_index_db_honors_weave_dir_override(tmp_path):
    """PR #10 deployment class: <vault>/config/config.toml sets weave_dir off
    the vault (derived SQLite on native fs). --vault must resolve the index
    under weave_dir, not the stale <vault>/.weave/index.db."""
    vault = tmp_path / "vault"
    (vault / "config").mkdir(parents=True)
    weave = tmp_path / "native" / "weave"
    weave.mkdir(parents=True)
    (vault / "config" / "config.toml").write_text(
        f'weave_dir = "{weave}"\n', encoding="utf-8")
    assert issue_loop._resolve_index_db(None, str(vault)) == str(weave / "index.db")


def test_resolve_index_db_relative_weave_dir_anchors_at_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "config").mkdir(parents=True)
    (vault / "config" / "config.toml").write_text(
        'weave_dir = "derived/weave"\n', encoding="utf-8")
    assert issue_loop._resolve_index_db(None, str(vault)) == str(
        vault / "derived" / "weave" / "index.db")


def test_resolve_index_db_falls_back_to_legacy_weave_layout(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    # No config.toml at all → legacy <vault>/.weave/index.db.
    assert issue_loop._resolve_index_db(None, str(vault)) == str(
        vault / ".weave" / "index.db")


def test_resolve_index_db_malformed_config_falls_back(tmp_path):
    vault = tmp_path / "vault"
    (vault / "config").mkdir(parents=True)
    (vault / "config" / "config.toml").write_text(
        "this is = not valid toml = at all\n", encoding="utf-8")
    # Malformed config must not crash — degrade to the legacy layout.
    assert issue_loop._resolve_index_db(None, str(vault)) == str(
        vault / ".weave" / "index.db")


def test_resolve_index_db_explicit_db_wins_over_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "config").mkdir(parents=True)
    (vault / "config" / "config.toml").write_text(
        f'weave_dir = "{tmp_path / "elsewhere"}"\n', encoding="utf-8")
    assert issue_loop._resolve_index_db("/explicit/index.db", str(vault)) == "/explicit/index.db"


def test_prime_argparse_contract():
    ns = issue_loop.build_arg_parser().parse_args([
        "prime", "57", "--run-id", "loop-x", "--labels", "a,b",
        "--concepts", "c1,c2", "--db", "i.db", "--limit", "2",
        "--budget-chars", "800", "--decisions", "dec-1", "--buffer", "b.jsonl",
    ])
    assert ns.cmd == "prime" and ns.number == 57 and ns.run_id == "loop-x"
    assert ns.labels == "a,b" and ns.concepts == "c1,c2"
    assert ns.limit == 2 and ns.budget_chars == 800
    # Sensible defaults when omitted.
    ns2 = issue_loop.build_arg_parser().parse_args(["prime", "1", "--run-id", "r"])
    assert ns2.labels is None and ns2.concepts is None and ns2.db is None
    assert ns2.limit == 3 and ns2.budget_chars == 1200 and ns2.buffer is None


# ---------------------------------------------------------------------------
# Risk-lane PR triage — classify_pr over synthetic PR-signal sets
#
# Pure function: (signals, triage-cfg) -> {lane, label, reasons}. Precedence
# red > yellow > green, every triggered rule listed (short-circuit reasons).

# A green triage config (green enabled) so the green lane is reachable; the
# repo default ships green DISABLED (see test_load_config_triage_defaults).
TRIAGE_CFG = {
    "green_enabled": True,
    "sensitive_paths": [
        "hooks/", "src/thinkweave/surfaces/", "ontology.yaml",
        "sources.yaml", "*schema*",
    ],
    "watched_paths": ["docs/agents/"],
    "green_max_diff_lines": 150,
    "green_requires_first_try": True,
    "red_min_diff_lines": 800,
}


def _signals(**kw):
    """A first-try, small, test-covered, minor-review, green-baseline PR —
    the green archetype. Override one field per test to trip one lane."""
    base = {
        "fix_rounds": 0,
        "diff_lines": 20,
        "files_touched": ["src/thinkweave/core/foo.py", "tests/test_foo.py"],
        "tests_touched": True,
        "review_severity": "minor",
        "baseline_green": True,
        "acceptance": "met",
    }
    base.update(kw)
    return base


# --- green lane -------------------------------------------------------------


def test_classify_green_when_enabled():
    r = issue_loop.classify_pr(_signals(), TRIAGE_CFG)
    assert r["lane"] == "green"
    assert r["label"] == "auto-merge-ok"
    assert r["reasons"] == []


def test_classify_green_disabled_downgrades_to_review_light():
    # Acceptance criterion 2, second half: the same green archetype is
    # review-light (not auto-merge-ok) when green is disabled — the repo default.
    cfg = {**TRIAGE_CFG, "green_enabled": False}
    r = issue_loop.classify_pr(_signals(), cfg)
    assert r["lane"] == "yellow" and r["label"] == "review-light"
    assert any("disabled" in x for x in r["reasons"])


def test_minor_and_none_review_are_green_eligible():
    assert issue_loop.classify_pr(_signals(review_severity="none"), TRIAGE_CFG)["lane"] == "green"
    assert issue_loop.classify_pr(_signals(review_severity="minor"), TRIAGE_CFG)["lane"] == "green"


# --- red lane ---------------------------------------------------------------


def test_classify_hooks_path_always_red():
    # Acceptance criterion 1: hooks/ is red regardless of size / first-try /
    # coverage / review — the whole green archetype except the touched file.
    r = issue_loop.classify_pr(_signals(files_touched=["hooks/hooks.json"]), TRIAGE_CFG)
    assert r["lane"] == "red" and r["label"] == "ready-for-human"
    assert any("hooks/hooks.json" in x for x in r["reasons"])


def test_classify_dir_prefix_matches_mcp_surface():
    r = issue_loop.classify_pr(
        _signals(files_touched=["src/thinkweave/surfaces/mcp/server.py"]), TRIAGE_CFG)
    assert r["lane"] == "red"


def test_classify_glob_pattern_matches_schema_basename():
    r = issue_loop.classify_pr(
        _signals(files_touched=["src/thinkweave/core/schemas.py"]), TRIAGE_CFG)
    assert r["lane"] == "red"


def test_classify_bare_filename_matches_basename_only():
    # ontology.yaml at any depth is sensitive; a file that merely shares the
    # stem as a prefix (different basename) is not.
    hit = issue_loop.classify_pr(
        _signals(files_touched=["src/thinkweave/vault_templates/config/ontology.yaml"]),
        TRIAGE_CFG)
    assert hit["lane"] == "red"
    miss = issue_loop.classify_pr(
        _signals(files_touched=["src/thinkweave/core/ontology_helpers.py"]), TRIAGE_CFG)
    assert miss["lane"] != "red"


def test_classify_big_diff_red():
    r = issue_loop.classify_pr(_signals(diff_lines=900), TRIAGE_CFG)
    assert r["lane"] == "red"
    assert any("900" in x for x in r["reasons"])


def test_classify_degraded_baseline_red():
    assert issue_loop.classify_pr(_signals(baseline_green=False), TRIAGE_CFG)["lane"] == "red"


def test_classify_major_and_critical_review_red():
    assert issue_loop.classify_pr(_signals(review_severity="major"), TRIAGE_CFG)["lane"] == "red"
    assert issue_loop.classify_pr(_signals(review_severity="critical"), TRIAGE_CFG)["lane"] == "red"


def test_classify_uncertain_acceptance_red():
    assert issue_loop.classify_pr(_signals(acceptance="uncertain"), TRIAGE_CFG)["lane"] == "red"
    assert issue_loop.classify_pr(_signals(acceptance="not-met"), TRIAGE_CFG)["lane"] == "red"


def test_red_lists_every_triggered_rule():
    # Short-circuit reasons: not just the first — every red rule that fired.
    r = issue_loop.classify_pr(
        _signals(files_touched=["hooks/x.json"], diff_lines=900,
                 baseline_green=False, review_severity="critical"),
        TRIAGE_CFG)
    assert r["lane"] == "red"
    joined = " | ".join(r["reasons"])
    assert "hooks/x.json" in joined and "900" in joined
    assert "baseline" in joined.lower() and "critical" in joined
    assert len(r["reasons"]) >= 4


# --- yellow lane ------------------------------------------------------------


def test_classify_fix_rounds_yellow():
    r = issue_loop.classify_pr(_signals(fix_rounds=1), TRIAGE_CFG)
    assert r["lane"] == "yellow" and r["label"] == "review-light"
    assert any("fix round" in x for x in r["reasons"])


def test_classify_medium_diff_yellow():
    # >= green_max_diff_lines (150) but < red_min_diff_lines (800).
    r = issue_loop.classify_pr(_signals(diff_lines=300), TRIAGE_CFG)
    assert r["lane"] == "yellow"
    assert any("300" in x for x in r["reasons"])


def test_classify_watched_path_yellow():
    r = issue_loop.classify_pr(_signals(files_touched=["docs/agents/loop.toml"]), TRIAGE_CFG)
    assert r["lane"] == "yellow"
    assert any("watched" in x for x in r["reasons"])


def test_classify_no_test_coverage_yellow():
    r = issue_loop.classify_pr(_signals(tests_touched=False), TRIAGE_CFG)
    assert r["lane"] == "yellow"


def test_green_requires_first_try_knob_off_allows_fix_rounds():
    cfg = {**TRIAGE_CFG, "green_requires_first_try": False}
    assert issue_loop.classify_pr(_signals(fix_rounds=3), cfg)["lane"] == "green"


# --- thresholds are config, not hardcoded (acceptance criterion 3) ----------


def test_thresholds_read_from_config():
    sig = _signals(diff_lines=200)
    lenient = {**TRIAGE_CFG, "red_min_diff_lines": 800}
    strict = {**TRIAGE_CFG, "red_min_diff_lines": 100}
    assert issue_loop.classify_pr(sig, lenient)["lane"] == "yellow"
    assert issue_loop.classify_pr(sig, strict)["lane"] == "red"


# --- config plumbing --------------------------------------------------------


def test_load_config_triage_defaults(tmp_path):
    cfg = issue_loop.load_config(tmp_path / "nope.toml")
    t = cfg["triage"]
    assert t["green_enabled"] is False   # ship conservative
    assert "hooks/" in t["sensitive_paths"]
    assert "*schema*" in t["sensitive_paths"]
    assert t["green_requires_first_try"] is True
    assert isinstance(t["green_max_diff_lines"], int)
    assert isinstance(t["red_min_diff_lines"], int)
    assert t["red_min_diff_lines"] > t["green_max_diff_lines"]


def test_repo_loop_toml_has_triage_section():
    cfg = issue_loop.load_config()
    assert cfg["triage"]["green_enabled"] is False
    # sensitive-path defaults translated to THIS repo's layout.
    sp = cfg["triage"]["sensitive_paths"]
    assert "hooks/" in sp and "src/thinkweave/surfaces/" in sp


def test_triage_override_via_set(tmp_path):
    cfg = issue_loop.apply_overrides(
        issue_loop.load_config(tmp_path / "nope.toml"),
        ["triage.green_max_diff_lines=200", "triage.green_enabled=true"],
    )
    assert cfg["triage"]["green_max_diff_lines"] == 200
    assert cfg["triage"]["green_enabled"] is True


def test_triage_override_rejects_unknown_key(tmp_path):
    cfg = issue_loop.load_config(tmp_path / "nope.toml")
    with pytest.raises(ValueError, match="unknown key"):
        issue_loop.apply_overrides(cfg, ["triage.green_max_diff=200"])


# --- CLI contract -----------------------------------------------------------


def test_triage_argparse_contract():
    ns = issue_loop.build_arg_parser().parse_args(
        ["triage", "59", "--signals-json", "s.json"])
    assert ns.cmd == "triage" and ns.number == 59 and ns.signals_json == "s.json"
    # The issue number is optional context (signals already carry it).
    ns2 = issue_loop.build_arg_parser().parse_args(["triage", "--signals-json", "s.json"])
    assert ns2.number is None


def test_triage_cli_red_via_default_config(tmp_path, capsys):
    sig = tmp_path / "sig.json"
    sig.write_text(json.dumps({
        "fix_rounds": 0, "diff_lines": 10, "files_touched": ["hooks/hooks.json"],
        "tests_touched": True, "review_severity": "minor", "baseline_green": True,
    }), encoding="utf-8")
    rc = issue_loop.main(["triage", "59", "--signals-json", str(sig)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["lane"] == "red" and out["label"] == "ready-for-human"
    assert out["issue"] == 59


def test_triage_cli_green_needs_enable_override(tmp_path, capsys):
    sig = tmp_path / "sig.json"
    sig.write_text(json.dumps({
        "fix_rounds": 0, "diff_lines": 10,
        "files_touched": ["src/thinkweave/core/foo.py", "tests/test_foo.py"],
        "tests_touched": True, "review_severity": "minor", "baseline_green": True,
        "acceptance": "met",
    }), encoding="utf-8")
    # Default config → green disabled → review-light.
    issue_loop.main(["triage", "--signals-json", str(sig)])
    assert json.loads(capsys.readouterr().out)["label"] == "review-light"
    # Enable green for this run → auto-merge-ok.
    issue_loop.main(["triage", "--signals-json", str(sig), "--set", "triage.green_enabled=true"])
    assert json.loads(capsys.readouterr().out)["label"] == "auto-merge-ok"


# ---------------------------------------------------------------------------
# Fail-closed: LLM-assembled signals make enum drift / missing keys realistic.
# Unrecognized enum values and absent safety-critical signals must never
# classify green-eligible — they go RED, naming the offending value/key.


def _signals_no(*drop, **kw):
    """The green archetype with the named safety keys REMOVED (fail-closed
    coverage) plus any overrides."""
    s = _signals(**kw)
    for k in drop:
        s.pop(k, None)
    return s


def test_unrecognized_review_severity_is_red():
    r = issue_loop.classify_pr(_signals(review_severity="high"), TRIAGE_CFG)
    assert r["lane"] == "red"
    assert any("high" in x for x in r["reasons"])


def test_unrecognized_acceptance_is_red():
    r = issue_loop.classify_pr(_signals(acceptance="partial"), TRIAGE_CFG)
    assert r["lane"] == "red"
    assert any("partial" in x for x in r["reasons"])


def test_missing_review_severity_is_red():
    r = issue_loop.classify_pr(_signals_no("review_severity"), TRIAGE_CFG)
    assert r["lane"] == "red"
    assert any("review_severity" in x for x in r["reasons"])


def test_missing_baseline_green_is_red():
    r = issue_loop.classify_pr(_signals_no("baseline_green"), TRIAGE_CFG)
    assert r["lane"] == "red"
    assert any("baseline_green" in x for x in r["reasons"])


def test_non_bool_baseline_green_is_red():
    # baseline_green: "false" (string) is truthy — it must NOT pass green.
    # Same enum-drift class: a non-bool value fails closed to red.
    r = issue_loop.classify_pr(_signals(baseline_green="false"), TRIAGE_CFG)
    assert r["lane"] == "red"
    assert any("baseline_green" in x for x in r["reasons"])


def test_missing_acceptance_is_red():
    r = issue_loop.classify_pr(_signals_no("acceptance"), TRIAGE_CFG)
    assert r["lane"] == "red"
    assert any("acceptance" in x for x in r["reasons"])


def test_empty_signals_is_red_on_all_three_safety_keys():
    r = issue_loop.classify_pr({}, TRIAGE_CFG)
    assert r["lane"] == "red"
    joined = " | ".join(r["reasons"])
    assert "baseline_green" in joined and "acceptance" in joined and "review_severity" in joined


def test_benign_absence_does_not_trip_red():
    # diff_lines / fix_rounds / files_touched absent is NOT a safety hole:
    # with the three safety keys present and clean, the PR is still green.
    sig = {"tests_touched": True, "review_severity": "minor",
           "baseline_green": True, "acceptance": "met"}
    r = issue_loop.classify_pr(sig, TRIAGE_CFG)
    assert r["lane"] == "green"


def test_red_label_sourced_from_on_gate_failure(tmp_path, capsys):
    # classify_pr's red label is overridable (default 'ready-for-human'); the
    # CLI feeds it from labels.on_gate_failure so remapping that label moves the
    # triage-red label with it — no duplicate source of truth.
    assert issue_loop.classify_pr(
        _signals(baseline_green=False), TRIAGE_CFG,
        red_label="needs-a-human")["label"] == "needs-a-human"
    sig = tmp_path / "sig.json"
    sig.write_text(json.dumps(_signals(files_touched=["hooks/x.json"])), encoding="utf-8")
    issue_loop.main(["triage", "--signals-json", str(sig),
                     "--set", "labels.on_gate_failure=escalate-me"])
    assert json.loads(capsys.readouterr().out)["label"] == "escalate-me"


def test_schema_glob_is_case_insensitive():
    # docs/SCHEMA.md (uppercase) must be caught by the *schema* pattern.
    r = issue_loop.classify_pr(_signals(files_touched=["docs/SCHEMA.md"]), TRIAGE_CFG)
    assert r["lane"] == "red"


def test_triage_cli_non_object_signals_clean_error(tmp_path, capsys):
    sig = tmp_path / "sig.json"
    sig.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    rc = issue_loop.main(["triage", "--signals-json", str(sig)])
    assert rc == 2
    assert "error" in json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# Slow self-improvement loop (issue #61) — vendored ponytail-audit skill +
# the arch-proposal command doc. Doc-grep contracts, mirroring the #58
# vendoring test and the committed-hooks acceptance guard.


def test_vendored_ponytail_audit_skill_present_with_provenance():
    """The ponytail-audit (whole-repo) skill is vendored as dev tooling under
    docs/agents/ with the SAME pinned-upstream provenance as the #58
    ponytail-review vendoring (sha + source repo + MIT notice), and the
    machine-local symlink wiring documented in-header (symlinks into
    .claude/commands/ are not committed)."""
    vendored = issue_loop.REPO_ROOT / "docs" / "agents" / "ponytail-audit.command.md"
    assert vendored.exists()
    text = vendored.read_text(encoding="utf-8")
    # Provenance: canonical upstream repo + the pinned commit sha (same as #58).
    assert "DietrichGebert/ponytail" in text
    assert "16f29800fd2681bdf24f3eb4ccffe38be3baec6b" in text
    # The upstream path is the whole-repo audit, not the diff review.
    assert "skills/ponytail-audit/SKILL.md" in text
    # MIT license obligation carried per the upstream LICENSE (#58 lesson:
    # vendored deps carry license obligations even for internal tooling).
    assert "MIT" in text
    assert "Copyright (c) 2026 DietrichGebert" in text
    assert "Permission is hereby granted" in text
    # The wiring note (ln -s into .claude/commands/), since the symlink itself
    # is machine-local and not committed.
    assert ".claude/commands/" in text and "ln -s" in text
    # The skill's actual delete-list vocabulary survived the vendoring.
    for tag in ("delete:", "stdlib:", "native:", "yagni:", "shrink:"):
        assert tag in text
    # The whole-repo hunt list (what distinguishes audit from review) survived.
    assert "Hunt" in text


def test_vendored_ponytail_audit_installs_no_hooks():
    """Acceptance guard (mirrors #58): vendoring the audit skill installs NO
    ponytail hooks — the committed hook manifest carries no ponytail entry
    (ponytail's plugin installer would collide with weave's own
    UserPromptSubmit hook, which is why we vendor SKILL TEXT ONLY)."""
    hooks = (issue_loop.REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
    assert "ponytail" not in hooks.lower()


def _arch_proposal_doc() -> str:
    return (issue_loop.REPO_ROOT / "docs" / "agents" / "arch-proposal.command.md").read_text(
        encoding="utf-8"
    )


def test_arch_proposal_command_present():
    assert (issue_loop.REPO_ROOT / "docs" / "agents" / "arch-proposal.command.md").exists()


def test_arch_proposal_command_forbids_opening_prs():
    """Acceptance criterion: the slow loop PROPOSES (files issues), never opens
    PRs. The command doc must carry the explicit never-open-a-PR / never-modify
    -code rule so the mechanism can't drift into applying changes."""
    text = _arch_proposal_doc().lower()
    # An explicit prohibition on opening PRs and on modifying code.
    assert "never" in text
    assert "pr" in text  # sanity: the doc talks about PRs
    # The load-bearing rule, matched loosely on the two verbs it forbids.
    assert ("never open" in text or "not open" in text or "no pr" in text
            or "never opens" in text)
    assert ("gh pr create" not in text) or ("never" in text)


def test_arch_proposal_command_forbids_pr_opening_rule_is_explicit():
    """The never-PR rule is stated as a rule, not merely implied — the doc
    contains a sentence pairing 'PR' with a prohibition and 'issue' with the
    output. Regression guard against the doc losing the read-only contract."""
    text = _arch_proposal_doc()
    lowered = text.lower()
    # It files issues (the output) ...
    assert "gh issue create" in lowered
    # ... and it is labeled arch-proposal.
    assert "arch-proposal" in lowered
    # ... and it never opens PRs / modifies code (read-only + issue-filing).
    assert "read-only" in lowered or "read only" in lowered
    assert "never open" in lowered or "opens zero pr" in lowered or "zero pr" in lowered


def test_arch_proposal_command_wires_steering_gate():
    """The command routes candidate proposals through the #62 evidence gate and
    files ONLY what the gate returns — the anti-invention contract. The doc must
    invoke `weave steering gate` and reference the weekly budget cap."""
    text = _arch_proposal_doc().lower()
    assert "weave steering gate" in text
    assert "--proposals-json" in text
    assert "weekly_budget" in text or "weekly budget" in text
    # It files the gate's evidence-carrying output, not raw suggestions.
    assert "filed" in text


def test_arch_proposal_command_cites_architecture_and_prior_decisions():
    """The command reads ARCHITECTURE.md and prior decisions first so it does
    not re-propose against already-decided work (a skip-list of decided-against
    directions)."""
    text = _arch_proposal_doc()
    assert "ARCHITECTURE.md" in text
    lowered = text.lower()
    # Prior-decision query: the decisions_for_file graph walk or the search.
    assert "decisions_for_file" in lowered or "weave_search" in lowered or "type=decision" in text
    # A skip-list of already-decided-against directions.
    assert "skip" in lowered and "decid" in lowered


def test_arch_proposal_command_runs_both_axes():
    """Both improvement axes are wired: the installed improve-arch skill
    (deepening) and the vendored ponytail-audit (simplification)."""
    text = _arch_proposal_doc()
    assert "improve-codebase-architecture" in text or "improve-arch" in text
    assert "ponytail-audit" in text


def test_arch_proposal_command_creates_label_idempotently():
    """The command creates the arch-proposal label idempotently (so a fresh
    tracker gets it) — gh label create ... --force (or a check-then-create)."""
    text = _arch_proposal_doc()
    assert "gh label create arch-proposal" in text


def test_arch_proposal_documents_routine_spec():
    """A Routine/cron entry is specified: weekly cadence + the headless
    invocation with the repo's established headless posture
    (--dangerously-skip-permissions). Acceptance criterion 3: a Routine entry
    runs headless without permission prompts."""
    text = _arch_proposal_doc()
    lowered = text.lower()
    assert "routine" in lowered
    assert "weekly" in lowered
    assert "--dangerously-skip-permissions" in text
    # The headless invocation names the command.
    assert "arch-proposal" in lowered


def test_arch_proposal_documents_headless_symlink_gotcha():
    """The headless-skill-resolution gotcha (headless `claude -p "/skill"` only
    resolves .claude/commands/ symlinks) must be documented, with the
    machine-local symlink as Routine setup."""
    text = _arch_proposal_doc()
    assert ".claude/commands/" in text and "ln -s" in text


def test_arch_proposal_label_documented_in_triage_labels():
    """The arch-proposal label is documented in the tracker's label vocabulary
    so the slow loop's output label is a known role, not an ad-hoc string."""
    labels = (issue_loop.REPO_ROOT / "docs" / "agents" / "triage-labels.md").read_text(
        encoding="utf-8"
    )
    assert "arch-proposal" in labels
    # The human-triage transition it feeds: accept → ready-for-agent.
    assert "ready-for-agent" in labels
