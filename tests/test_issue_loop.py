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
# plan-distill (issue #72) — grill-fork → plan-time decisions doc-pinning.
# The command is agent-facing docs; these pins assert the load-bearing rules
# survive edits (stable tokens, not brittle prose). Mirror the ponytail pins.


def _plan_distill_doc() -> str:
    doc = issue_loop.REPO_ROOT / "docs" / "agents" / "plan-distill.command.md"
    assert doc.exists(), "plan-distill.command.md must ship under docs/agents/"
    return doc.read_text(encoding="utf-8")


def test_plan_distill_fork_gate_requires_both_conditions():
    """The fork-gate mints a decision only when BOTH a concrete
    considered-and-rejected alternative AND a falsifiable predicted_outcome
    are present — clarifying answers (fact elicitation) never qualify."""
    text = _plan_distill_doc().lower()
    # Both gate conditions named as jointly required.
    assert "alternative" in text
    assert "considered and rejected" in text
    assert "falsifiable" in text and "predicted_outcome" in text
    assert "both" in text  # both-required framing, not either/or


def test_plan_distill_clarifying_questions_yield_none():
    """Acceptance criterion 1: clarifying questions mint zero decisions."""
    text = _plan_distill_doc().lower()
    assert "clarifying" in text
    # An explicit "no decision from a clarifying answer" statement.
    assert "never mint" in text or "yield no decision" in text or "mints nothing" in text


def test_plan_distill_no_count_cap_scales_with_contention():
    """Acceptance criterion 2: question count does not drive decision count;
    the fork-gate replaces any cap and scales with real contention."""
    text = _plan_distill_doc().lower()
    assert "no count cap" in text or "no cap" in text or "replaces any count cap" in text
    # The scaling claim: a 40-question grill with 3 forks yields ~3 decisions.
    assert "does not drive" in text or "question count" in text


def test_plan_distill_body_budget_is_1k_chars():
    """Acceptance criterion 3: bodies respect the ~1K-char wrap-body budget."""
    text = _plan_distill_doc()
    assert "1K" in text or "1,000" in text or "1000" in text


def test_plan_distill_frontmatter_and_alternatives_section_required():
    """Each minted decision carries the counterfactual as an
    '## Alternatives considered' body section plus predicted_outcome + plan_ref
    frontmatter (acceptance criterion 1)."""
    text = _plan_distill_doc()
    assert "## Alternatives considered" in text
    assert "predicted_outcome" in text
    assert "plan_ref" in text


def test_plan_distill_plan_ref_placeholder_convention():
    """plan_ref links to /to-spec → /to-tickets refs when they exist, and uses
    a documented placeholder (updated by /to-tickets) when they don't yet."""
    text = _plan_distill_doc()
    assert "[pending]" in text
    assert "/to-tickets" in text and "/to-spec" in text


def test_plan_distill_executable_fallback_command_shape():
    """MCP-absent fallback is an executable `weave add` decision, verified
    against the real CLI flag shape (--type decision, -f key=value)."""
    text = _plan_distill_doc()
    assert "weave add" in text
    assert "--type decision" in text
    # Frontmatter carried via repeatable -f, matching _parser_basics.py.
    assert "-f predicted_outcome=" in text
    assert "-f plan_ref=" in text


def test_plan_distill_write_surface_is_enumerated():
    """Write-surface enumeration: the entire write surface is
    weave_create / weave add decisions — no code edits, no gh, no PRs."""
    text = _plan_distill_doc()
    assert "entire write surface" in text
    assert "weave_create" in text and "weave add" in text
    low = text.lower()
    # No PRs, no code edits — stated plainly (each token load-bearing on its own).
    assert "no prs" in low
    assert "no code edits" in low


def test_plan_distill_mcp_example_nests_fields_in_frontmatter():
    """CRITICAL fix: the MCP weave_create schema
    (surfaces/mcp/tools/notes.py) accepts only type/title/body/project/tags/
    frontmatter/session_id — extra top-level kwargs are silently dropped. So
    concepts / predicted_outcome / plan_ref MUST be nested under frontmatter=,
    or the minted decision carries none of them (and the ontology gate, which
    keys off fm['concepts'], never runs). Pin the dict-style (quoted-key)
    nesting, which only appears inside a frontmatter={...} block."""
    text = _plan_distill_doc()
    assert "frontmatter={" in text
    assert '"concepts":' in text
    assert '"predicted_outcome":' in text
    assert '"plan_ref":' in text
    # The dropped-kwarg trap named so a future editor doesn't re-flatten it.
    assert "silently dropped" in text or "top-level kwarg" in text


def test_plan_distill_plan_ref_is_scalar_string_no_flow_list():
    """MAJOR fix: plan_ref is a string (mcp/tools/_extract_schemas.py:108,
    consumed as a string in synthesis/judge.py:138). Represent it as a scalar
    string everywhere — `plan_ref: "[pending]"`, multi-refs as one comma-joined
    string — never a YAML flow list. The old `[spec-4c1, #91, #92]` example was
    literally unparseable (# starts a YAML comment); it must be gone."""
    text = _plan_distill_doc()
    assert '"[pending]"' in text  # quoted scalar-string form
    assert "#91" not in text and "#92" not in text  # unparseable flow-list gone
    low = text.lower()
    assert "string" in low and ("comma-joined" in low or "comma joined" in low)


def test_plan_distill_fallback_warns_comma_split():
    """MINOR fix: `weave add -f key=value` comma-splits any comma-bearing value
    into a list (surfaces/cli/notes.py::_parse_fm_token). A prose
    predicted_outcome with commas would silently become a list on the CLI path,
    so the doc must warn: comma-free phrasing on -f, or use the MCP path for
    prose predictions."""
    low = _plan_distill_doc().lower()
    assert "comma-split" in low or "comma splits" in low or "splits" in low
    assert "comma-free" in low or "comma free" in low


def test_plan_distill_located_outside_the_loop():
    """MINOR fix: plan-distill is human-invoked at grill/plan time — OUTSIDE the
    issue-loop. Naming this keeps vault-issue-contract.md's 'session note is the
    sole decision owner' readable as loop-scoped, not contradicted."""
    low = _plan_distill_doc().lower()
    assert "outside the loop" in low or "not the loop" in low or "outside the issue-loop" in low


def test_plan_distill_fallback_parses_through_real_weave_argparse():
    """Executability pin (NIT): the documented `weave add` fallback resolves
    through the REAL weave argparse, and `plan_ref=[pending]` round-trips as the
    scalar string '[pending]' (not a list) — _parse_fm_token JSON-probes the
    leading '[', fails, and falls through to the string branch. Catches schema
    drift in either the parser or the doc's flag shape."""
    from thinkweave.surfaces.cli.parser import build_parser
    from thinkweave.surfaces.cli.notes import _parse_fm_token

    ns = build_parser().parse_args(
        ["add", "t", "--type", "decision", "-f", "plan_ref=[pending]"]
    )
    assert ns.command == "add"
    assert ns.type == "decision"
    assert "plan_ref=[pending]" in ns.frontmatter
    # The subtle bit the doc relies on: [pending] survives as a scalar string.
    assert _parse_fm_token("plan_ref=[pending]") == ("plan_ref", "[pending]")


def test_plan_distill_rides_installed_skill_never_edits_it():
    """Acceptance criterion 4: the installed grilling/grill-me skill is
    untouched; the command rides it and explicitly never edits it. (A test can't
    see the home dir — assert the doc instructs no-touch instead.)"""
    text = _plan_distill_doc().lower()
    assert "grilling" in text
    assert "installed" in text
    assert "never edit" in text or "do not edit" in text or "not fork" in text


def test_plan_distill_symlink_header_wiring():
    """The machine-local symlink convention is documented in-header, mirroring
    arch-proposal/ponytail (symlinks into .claude/commands/ are not committed)."""
    text = _plan_distill_doc()
    assert ".claude/commands/" in text and "ln -s" in text


def test_plan_distill_symlink_is_not_committed():
    """The symlink itself is machine-local — never committed.
    Mirror the arch-proposal/issue-loop convention: git must not track it."""
    import subprocess

    out = subprocess.run(
        ["git", "ls-files", ".claude/commands/"],
        cwd=issue_loop.REPO_ROOT, capture_output=True, text=True,
    ).stdout
    assert "plan-distill" not in out


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
    # Issue #85: the Lessons section is retired from the body skeleton — the
    # run-causal register is What / How it went only.
    assert "## How it went" in payload["body_skeleton"]
    assert "## Lessons" not in payload["body_skeleton"]


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


# ---------------------------------------------------------------------------
# Prime v2 (issue #85) — serve insight-note bodies by following builds_on links
# from concept-matched trajectories; fall back to inline Lessons for v1 notes;
# prefer merged-clean-labeled trajectories over reworked when labels exist.


def _add_note(path, *, note_id, title, body, concepts=(), tags=(),
              date="2026-07-18", frontmatter=None, note_type="note"):
    """Insert one more note (+ its tags/concepts) into an existing seeded db —
    the tables prime's queries join. Insight notes carry no loop-run tag.
    ``note_type`` lets a test seed a non-note (decision/session) to prove prime
    only serves ``type='note'`` bodies as color."""
    import json as _json
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(path)
    conn.execute(
        "INSERT INTO notes (id, type, title, path, date, frontmatter, body_text)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (note_id, note_type, title, f"{note_id}.md", date,
         _json.dumps(frontmatter or {}), body),
    )
    for t in tags:
        conn.execute("INSERT INTO note_tags VALUES (?, ?)", (note_id, t))
    for c in concepts:
        conn.execute("INSERT INTO note_concepts VALUES (?, ?)", (note_id, c))
    conn.commit()
    conn.close()


def test_query_trajectories_follows_builds_on_to_insight_bodies(tmp_path):
    """A v2 trajectory (no inline Lessons) carrying a builds_on link resolves
    the linked insight note's BODY — the portable lesson now lives there."""
    db = tmp_path / "index.db"
    # Trajectory: loop-run, concept-matched, NO Lessons section, builds_on link.
    _seed_index_db(
        db, note_id="n-traj", title="loop trajectory #85",
        concepts=["retrieval"],
        body="## What\nprime v2.\n\n## How it went\none fix round.\n",
        frontmatter={"issue": 85, "outcome": "shipped",
                     "builds_on": ["n-ins1"]},
    )
    _add_note(db, note_id="n-ins1", title="portable lesson",
              body="Follow builds_on before falling back to inline Lessons.")
    conn = issue_loop._open_index_ro(str(db))
    try:
        hits = issue_loop.query_trajectories(conn, ["retrieval"], 3)
    finally:
        conn.close()
    assert [h["id"] for h in hits] == ["n-traj"]
    assert hits[0]["insights"] == [
        {"id": "n-ins1",
         "body": "Follow builds_on before falling back to inline Lessons."},
    ]
    # No inline Lessons on a v2 note.
    assert hits[0]["lessons"] == ""


def test_query_trajectories_v1_inline_lessons_still_served(tmp_path):
    """Backward compat: a v1 trajectory (inline Lessons, no builds_on) is still
    a served candidate — its insights list is empty, lessons carries the text."""
    db = tmp_path / "index.db"
    _seed_index_db(
        db, note_id="n-v1", title="v1 trajectory",
        concepts=["retrieval"],
        body="## What\nx\n\n## Lessons\nWiden the CHECK before the migration.\n",
        frontmatter={"issue": 60, "outcome": "shipped"},  # no builds_on
    )
    conn = issue_loop._open_index_ro(str(db))
    try:
        hits = issue_loop.query_trajectories(conn, ["retrieval"], 3)
    finally:
        conn.close()
    assert [h["id"] for h in hits] == ["n-v1"]
    assert hits[0]["insights"] == []
    assert hits[0]["lessons"] == "Widen the CHECK before the migration."


def test_query_trajectories_prefers_merged_clean_over_reworked(tmp_path):
    """When labels exist, merged-clean sorts before reworked regardless of
    recency; the sort is a deterministic stable tweak."""
    db = tmp_path / "index.db"
    # The reworked note is NEWER (would win on recency); merged-clean is older.
    _seed_index_db(
        db, note_id="n-rew", title="reworked", concepts=["retrieval"],
        body="## What\nx\n\n## Lessons\nrework lesson\n", date="2026-07-18",
        frontmatter={"issue": 1, "outcome": "shipped", "outcome_label": "reworked"},
    )
    _add_note(
        db, note_id="n-clean", title="clean", concepts=["retrieval"],
        tags=["loop-run"], date="2026-07-10",
        body="## What\nx\n\n## Lessons\nclean lesson\n",
        frontmatter={"issue": 2, "outcome": "shipped", "outcome_label": "merged-clean"},
    )
    conn = issue_loop._open_index_ro(str(db))
    try:
        hits = issue_loop.query_trajectories(conn, ["retrieval"], 3)
    finally:
        conn.close()
    assert [h["id"] for h in hits] == ["n-clean", "n-rew"]


def test_query_trajectories_unlabeled_keeps_recency(tmp_path):
    """No outcome_label anywhere → pure recency order (byte-stable v1 behavior);
    the weighting only fires when labels exist."""
    db = tmp_path / "index.db"
    _seed_index_db(
        db, note_id="n-older", title="older", concepts=["retrieval"],
        body="## What\nx\n\n## Lessons\nold\n", date="2026-07-10",
        frontmatter={"issue": 1, "outcome": "shipped"},
    )
    _add_note(
        db, note_id="n-newer", title="newer", concepts=["retrieval"],
        tags=["loop-run"], date="2026-07-18",
        body="## What\nx\n\n## Lessons\nnew\n",
        frontmatter={"issue": 2, "outcome": "shipped"},
    )
    conn = issue_loop._open_index_ro(str(db))
    try:
        hits = issue_loop.query_trajectories(conn, ["retrieval"], 3)
    finally:
        conn.close()
    assert [h["id"] for h in hits] == ["n-newer", "n-older"]


def test_render_prime_block_serves_insight_bodies_over_lessons():
    """render_prime_block serves the linked insight BODIES when a trajectory
    carries them (served records the INSIGHT ids), and falls back to inline
    Lessons (served records the TRAJECTORY id) for a v1 trajectory."""
    trajectories = [
        {"id": "n-traj", "title": "v2", "issue": 85, "outcome": "shipped",
         "lessons": "", "insights": [
             {"id": "n-ins1", "body": "Portable lesson one."},
             {"id": "n-ins2", "body": "Portable lesson two."}]},
        {"id": "n-v1", "title": "v1", "issue": 60, "outcome": "shipped",
         "lessons": "Inline lesson.", "insights": []},
    ]
    block, served = issue_loop.render_prime_block(trajectories, decisions=[])
    assert "Portable lesson one." in block
    assert "Portable lesson two." in block
    assert "Inline lesson." in block
    # v2 serves the insight ids; v1 serves the trajectory id.
    assert served == ["n-ins1", "n-ins2", "n-v1"]


def test_render_prime_block_v1_dicts_without_insights_key_unchanged():
    """Byte-stable: a pre-#85 trajectory dict with no 'insights' key renders
    exactly as before (inline Lessons, served = trajectory id)."""
    trajectories = [
        {"id": "n-aaa111", "title": "prime rail", "issue": 57,
         "outcome": "shipped", "lessons": "Widen the CHECK first."},
    ]
    block, served = issue_loop.render_prime_block(trajectories, decisions=[])
    assert "Widen the CHECK first." in block
    assert served == ["n-aaa111"]


def test_build_prime_payload_serves_insight_bodies_end_to_end(tmp_path):
    """Acceptance: a concept-matched trajectory with a builds_on insight serves
    the insight body in the block and records the insight id as served."""
    db = tmp_path / "index.db"
    _seed_index_db(
        db, note_id="n-traj", title="loop trajectory",
        concepts=["self-improvement"],
        body="## What\nx\n\n## How it went\ny\n",  # no Lessons — v2
        frontmatter={"issue": 85, "outcome": "shipped", "builds_on": ["n-ins1"]},
    )
    _add_note(db, note_id="n-ins1", title="portable lesson",
              body="Serve insight bodies via builds_on links.")
    conn = issue_loop._open_index_ro(str(db))
    try:
        payload = issue_loop.build_prime_payload(
            85, "loop-run-0", ["self-improvement"], conn=conn, holdout=5,
        )
    finally:
        conn.close()
    assert payload["primed"] is True
    assert payload["served"] == ["n-ins1"]
    assert "Serve insight bodies via builds_on links." in payload["block"]


# --- Review round 1 (issue #85) — hardening the prime v2 seams --------------


def test_build_trajectory_trace_lines_delta_non_int_is_zero():
    """`simplify.lines_delta` must degrade a malformed value (list/dict) to 0,
    not escape as a TypeError — an uncaught TypeError would crash the trajectory
    command (rc-1) instead of the clean ValueError rc-2 path. It is a count, so
    the same coercion as every other filter/join key applies."""
    payload = issue_loop.build_trajectory(
        {"number": 1, "title": "x", "labels": []},
        branch="b", commits=[], numstat="", gates=[], fix_rounds=0,
        outcome="shipped",
        trace={"simplify": {"outcome": "applied", "lines_delta": [3],
                            "cuts": [], "kept": []}},
    )
    assert payload["frontmatter"]["trace"]["simplify"]["lines_delta"] == 0


def test_resolve_insights_only_serves_note_type(tmp_path):
    """builds_on could name a decision or session id; prime must NOT serve a
    non-note body as color — only `type='note'` insight notes are served, and a
    decision id resolves to nothing (falls back to inline Lessons upstream)."""
    db = tmp_path / "index.db"
    _seed_index_db(db, note_id="n-seed", title="seed", concepts=["x"],
                   body="## Lessons\ny\n")
    _add_note(db, note_id="dec-1", title="a decision", body="decision body",
              note_type="decision")
    _add_note(db, note_id="ses-1", title="a session", body="session body",
              note_type="session")
    _add_note(db, note_id="n-ins", title="insight", body="insight body")
    conn = issue_loop._open_index_ro(str(db))
    try:
        # A decision/session id in builds_on resolves to nothing; only the note
        # is served, preserving builds_on order.
        assert issue_loop.resolve_insights(conn, ["dec-1", "n-ins", "ses-1"]) == [
            {"id": "n-ins", "body": "insight body"},
        ]
    finally:
        conn.close()


def test_coerce_builds_on_forms():
    """Regression pin for the builds_on coercion: plain ids pass through,
    path-based wikilinks (`[[path|id]]`) strip to the trailing id, whitespace is
    trimmed, non-string elements are dropped, and a non-list is empty."""
    f = issue_loop._coerce_builds_on
    assert f(["n-plain"]) == ["n-plain"]
    assert f(["[[projects/x/note.md|n-wiki]]"]) == ["n-wiki"]
    assert f(["  n-space  "]) == ["n-space"]
    assert f(["n-a", 123, None, {"x": 1}, ""]) == ["n-a"]
    assert f("n-notalist") == []
    assert f(None) == []


def test_query_trajectories_dangling_builds_on_falls_back_to_lessons(tmp_path):
    """A builds_on link that resolves to nothing (missing / non-note id) must
    fall through to the inline Lessons — the v1 fallback chain, pinned."""
    db = tmp_path / "index.db"
    _seed_index_db(
        db, note_id="n-traj", title="t", concepts=["retrieval"],
        body="## What\nx\n\n## Lessons\nfallback lesson\n",
        frontmatter={"issue": 1, "outcome": "shipped", "builds_on": ["n-missing"]},
    )
    conn = issue_loop._open_index_ro(str(db))
    try:
        hits = issue_loop.query_trajectories(conn, ["retrieval"], 3)
    finally:
        conn.close()
    assert [h["id"] for h in hits] == ["n-traj"]
    assert hits[0]["insights"] == []
    assert hits[0]["lessons"] == "fallback lesson"


def test_query_trajectories_dangling_builds_on_no_lessons_filtered(tmp_path):
    """A dangling builds_on with NO inline Lessons has no reusable color at all
    → the trajectory is filtered out (not served), never crashes."""
    db = tmp_path / "index.db"
    _seed_index_db(
        db, note_id="n-traj", title="t", concepts=["retrieval"],
        body="## What\nx\n\n## How it went\ny\n",  # no Lessons
        frontmatter={"issue": 1, "outcome": "shipped", "builds_on": ["n-missing"]},
    )
    conn = issue_loop._open_index_ro(str(db))
    try:
        hits = issue_loop.query_trajectories(conn, ["retrieval"], 3)
    finally:
        conn.close()
    assert hits == []


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


# ---------------------------------------------------------------------------
# Semantic execution trace (issue #85) — the gate agents' own reports,
# condensed by the orchestrator into structured envelopes on the trajectory
# note frontmatter. No new model calls: build_trajectory only accepts + shapes.
# Every expected value below is hand-written from the #85 schema, not
# recomputed by the code under test.


def _sample_trace() -> dict:
    """A hand-written semantic trace with prose-valued fields, carrying one
    extra orchestrator-bookkeeping key per level to prove projection drops it."""
    return {
        "rounds": [
            {"gate": "review", "finding": "standalone existence test duplicates "
             "the eight sibling guards", "severity": "minor",
             "disposition": "accepted", "fixed_by": "dropped the redundant test",
             "reviewer_note": "orchestrator bookkeeping — dropped"},
        ],
        "criteria": [
            {"id": "AC1", "verdict": "met", "flipped_by_round": 1, "extra": "x"},
            {"id": "AC2", "verdict": "met", "flipped_by_round": None},
        ],
        "simplify": {
            "outcome": "applied", "lines_delta": -12,
            "cuts": [{"what": "existence test", "why": "eight siblings already "
                      "guard existence", "note": "dropped"}],
            "kept": [{"what": "budget-cap test", "why": "the only novel invariant"}],
            "bookkeeping": "dropped",
        },
        "edge_cases": ["empty concepts → no prime", "corrupt index → unprimed"],
        "tdd": {"red_confirmed": True, "note": "dropped"},
        "orchestrator_scratch": {"anything": "dropped"},  # unknown top-level key
    }


def test_build_trajectory_round_trips_semantic_trace():
    """--trace-json carries the gate agents' condensed reports into
    frontmatter['trace'] as prose-valued structured envelopes; prose survives
    verbatim, counts stay ints, flipped_by_round is int-or-null."""
    issue = {"number": 85, "title": "Trajectory v2", "labels": []}
    payload = issue_loop.build_trajectory(
        issue, branch="loop/dag-54", commits=["a"], numstat="1\t0\tx.py\n",
        gates=[], fix_rounds=1, outcome="shipped", trace=_sample_trace(),
    )
    trace = payload["frontmatter"]["trace"]
    assert trace["rounds"] == [
        {"gate": "review",
         "finding": "standalone existence test duplicates the eight sibling guards",
         "severity": "minor", "disposition": "accepted",
         "fixed_by": "dropped the redundant test"},
    ]
    assert trace["criteria"] == [
        {"id": "AC1", "verdict": "met", "flipped_by_round": 1},
        {"id": "AC2", "verdict": "met", "flipped_by_round": None},
    ]
    assert trace["simplify"] == {
        "outcome": "applied", "lines_delta": -12,
        "cuts": [{"what": "existence test",
                  "why": "eight siblings already guard existence"}],
        "kept": [{"what": "budget-cap test", "why": "the only novel invariant"}],
    }
    assert trace["edge_cases"] == ["empty concepts → no prime",
                                   "corrupt index → unprimed"]
    assert trace["tdd"] == {"red_confirmed": True}
    # Unknown top-level key dropped (skills-style projection).
    assert "orchestrator_scratch" not in trace


def test_build_trajectory_omits_trace_when_absent():
    """Backward compat / byte-stability: a caller that passes no trace gets a
    payload with NO trace key — the pre-#85 frontmatter is unchanged."""
    payload = issue_loop.build_trajectory(
        {"number": 1, "title": "x", "labels": []},
        branch="b", commits=[], numstat="", gates=[],
        fix_rounds=0, outcome="shipped",
    )
    assert "trace" not in payload["frontmatter"]


def test_build_trajectory_trace_only_includes_provided_top_level_keys():
    """A partial trace (only the fields a run actually had) yields only those
    envelopes — absent sections are omitted, not emitted empty."""
    payload = issue_loop.build_trajectory(
        {"number": 2, "title": "x", "labels": []},
        branch="b", commits=[], numstat="", gates=[],
        fix_rounds=0, outcome="shipped",
        trace={"tdd": {"red_confirmed": False}},
    )
    assert payload["frontmatter"]["trace"] == {"tdd": {"red_confirmed": False}}


def test_build_trajectory_rejects_non_dict_trace():
    """Shape guard (mirrors the served list-guard, #57 posture): a non-dict
    trace — a list, a bare string, a number — must not silently land in
    frontmatter; the trace envelope is a JSON object."""
    issue = {"number": 3, "title": "x", "labels": []}
    for bad in ([{"gate": "review"}], "review: minor", 7):
        with pytest.raises((ValueError, TypeError)):
            issue_loop.build_trajectory(
                issue, branch="b", commits=[], numstat="", gates=[],
                fix_rounds=0, outcome="shipped", trace=bad,
            )


def test_trajectory_trace_argparse_contract():
    """The trajectory subcommand exposes --trace-json (optional, default None),
    a sibling of --skills-json / --served-json."""
    ns = issue_loop.build_arg_parser().parse_args([
        "trajectory", "85", "--gates-json", "g.json",
        "--trace-json", "trace.json", "--outcome", "shipped",
    ])
    assert ns.trace_json == "trace.json"
    ns2 = issue_loop.build_arg_parser().parse_args([
        "trajectory", "85", "--gates-json", "g.json", "--outcome", "shipped",
    ])
    assert ns2.trace_json is None


# ---------------------------------------------------------------------------
# §3 command-doc pins (issue #85) — Lessons retired, insight-minting + builds_on
# linking instructed with the register test stated, and doc examples executable
# against the real schemas (the #72 trap: MCP custom fields nest under
# frontmatter=; CLI examples parse through the real argparse).


def _command_doc_section3() -> str:
    doc = issue_loop.REPO_ROOT / "docs" / "agents" / "issue-loop.command.md"
    assert doc.exists(), "issue-loop.command.md must ship under docs/agents/"
    lines = doc.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("## 3."))
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def test_command_doc_section3_retires_lessons_body():
    """§3 no longer instructs a Lessons body section — the body is the
    run-causal register (What / How it went) only."""
    sec = _command_doc_section3()
    assert "What / How it went" in sec
    # The old '(What / How it went / Lessons …)' compose instruction is gone.
    assert "How it went / Lessons" not in sec
    assert "no lessons section" in sec.lower()


def test_command_doc_section3_instructs_insight_minting_and_builds_on():
    """§3 instructs minting portable lessons as separate insight notes and
    linking them from the trajectory via builds_on."""
    low = _command_doc_section3().lower()
    assert "insight note" in low
    assert "builds_on" in low
    assert "concepts at creation" in low or "concepts-at-creation" in low


def test_command_doc_section3_states_register_test():
    """§3 states the register test that sorts every artifact."""
    low = _command_doc_section3().lower()
    assert "run-bound semantic trace" in low
    assert "portable lesson" in low
    assert "insight note" in low
    assert "frontmatter key" in low


def test_command_doc_section3_trace_cli_example_is_executable():
    """Executability pin (#72 trap): §3 documents the --trace-json flag on the
    trajectory command, and that exact invocation shape parses through the REAL
    argparse — not a drifted or hand-waved flag."""
    sec = _command_doc_section3()
    assert "--trace-json" in sec
    ns = issue_loop.build_arg_parser().parse_args([
        "trajectory", "85", "--cwd", "wt", "--gates-json", "g.json",
        "--trace-json", "trace.json", "--fix-rounds", "1",
        "--outcome", "shipped", "--pr-url", "u", "--run-id", "r",
    ])
    assert ns.trace_json == "trace.json" and ns.cmd == "trajectory"


def test_command_doc_section3_weave_create_nests_concepts_and_builds_on():
    """Executability pin (#72 trap): the MCP weave_create schema
    (surfaces/mcp/tools/notes.py) accepts only type/title/body/project/tags/
    frontmatter/session_id — extra top-level kwargs are silently dropped. So the
    insight note's `concepts` and the trajectory's `builds_on` link MUST be
    nested under frontmatter={…}. Pin the dict-style nesting."""
    sec = _command_doc_section3()
    assert "frontmatter={" in sec
    assert '"concepts":' in sec
    assert '"builds_on":' in sec
    # The dropped-kwarg trap named so a future editor doesn't re-flatten it.
    assert "silently dropped" in sec or "top-level kwarg" in sec


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
    # Real guard: the pr-creation command may appear ONLY inside a prohibition.
    # The doc names `gh pr create` exactly to forbid it, so every occurrence is
    # immediately preceded by "never" — the doc can never read as an instruction
    # to open a PR (regression guard against a copy-paste that drops the negation).
    assert "gh pr create" in text
    start = 0
    while (idx := text.find("gh pr create", start)) != -1:
        assert "never" in text[max(0, idx - 30):idx], "gh pr create not in a prohibition"
        start = idx + len("gh pr create")


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
