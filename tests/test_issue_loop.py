"""Tests for the deterministic rail of the issue-to-PR loop.

Everything here is pure: parsing and frontier computation take plain dicts
and strings — no gh, no git, no network.
"""

import importlib.util
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
