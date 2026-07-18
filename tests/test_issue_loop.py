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
    assert all(g["kind"] in {"command", "diff", "acceptance", "review"} for g in cfg["gates"])


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
