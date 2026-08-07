"""Host-side contracts for the /issue-loop workflow.

devloop itself lives in funloops now (#151) and its ~165 unit tests went with
it — this file keeps only what a HOST owns and funloops CI cannot check:

* **thinkweave's gate pipeline** — the [gates] in ``docs/agents/loop.toml`` are
  this repo's, not the packaged rail's. (The packaged defaults legitimately
  differ: #149 emptied ``triage.sensitive_paths`` because a packaged rail
  cannot know a host's layout. thinkweave declares its own, which is why the
  byte-compat golden in test_devloop_boundaries.py is unmoved by that change.)
* **thinkweave's own files** — ``hooks/hooks.json``, and the two command docs
  that stayed behind because they are thinkweave workflows rather than loop
  mechanism: ``arch-proposal.command.md`` and ``plan-distill.command.md``.

Everything keyed to a doc that moved (issue-loop.command.md, issue-loop.md,
issue-tracker.md, triage-labels.md, devloop-boundaries.md, the vendored
ponytail files) went with the doc. The consumption seam — the pin, the shim's
byte-compat, the schema pin — is in tests/test_devloop_boundaries.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from devloop import cli

REPO_ROOT = Path(__file__).resolve().parent.parent


def _plan_distill_doc() -> str:
    doc = REPO_ROOT / "docs" / "agents" / "plan-distill.command.md"
    return doc.read_text(encoding="utf-8")


def _arch_proposal_doc() -> str:
    return (REPO_ROOT / "docs" / "agents" / "arch-proposal.command.md").read_text(
        encoding="utf-8"
    )


def test_repo_loop_toml_parses_and_gate_ids_unique():
    cfg = cli.load_config()
    ids = [g["id"] for g in cfg["gates"]]
    assert len(ids) == len(set(ids)) and len(ids) >= 4
    assert all(g["kind"] in {"command", "diff", "acceptance", "review", "simplify"}
               for g in cfg["gates"])


def test_gate_pipeline_order_is_pinned():
    """The full pipeline order is a contract: diff-guard → tests → acceptance
    → review → simplify. simplify runs LAST, after review, so it only ever
    shrinks an already-verified diff."""
    cfg = cli.load_config()
    ids = [g["id"] for g in cfg["gates"]]
    assert ids == ["diff-guard", "tests", "acceptance", "review", "simplify"]


def test_simplify_gate_shape():
    """The simplify gate is a non-required LLM/orchestrator kind whose
    'failure' mode is a revert (never a pipeline block): it re-runs the
    verification gates on the simplified diff and, if either goes red, ships
    the pre-simplify diff with the revert note."""
    cfg = cli.load_config()
    gate = next(g for g in cfg["gates"] if g["id"] == "simplify")
    assert gate["kind"] == "simplify"
    # required=false: simplify can never fail the pipeline — its failure ships
    # the pre-simplify diff (documented in issue-loop.command.md §1c-simplify).
    assert gate["required"] is False
    # It re-verifies the shrunk diff against exactly the deterministic +
    # behavioral gates, in order.
    assert gate["rerun"] == ["tests", "acceptance"]
    assert "simplify-reverted" in gate["revert_note"]
    # The delete-list comes from the ponytail-review skill, which the
    # orchestrator reads from funloops (packages/devloop/docs/agents/).
    assert gate["skill"] == "ponytail-review"


def test_committed_hooks_carry_no_ponytail_entries():
    """Acceptance criterion: vendoring the skill installs NO ponytail hooks.
    The committed hook manifest must contain no ponytail UserPromptSubmit /
    PreToolUse entry (ponytail's plugin would collide with weave's own
    UserPromptSubmit hook)."""
    hooks = (REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
    assert "ponytail" not in hooks.lower()


def test_dispatch_persona_knob_defaults_on():
    """[dispatch] persona = true is the default AND file-backed in loop.toml
    (3-edit config pattern: file entry + DEFAULT_CONFIG + override path)."""
    cfg = cli.load_config()
    assert cfg["dispatch"]["persona"] is True
    assert cli.DEFAULT_CONFIG["dispatch"]["persona"] is True
    toml_text = (REPO_ROOT / "docs" / "agents" / "loop.toml").read_text(
        encoding="utf-8"
    )
    assert "[dispatch]" in toml_text
    assert "persona = true" in toml_text


def test_dispatch_persona_overridable_per_run():
    """--set dispatch.persona=false flips the knob for one run; an unknown key
    in [dispatch] stays a hard error (typo protection, same as every section)."""
    cfg = cli.apply_overrides(cli.load_config(), ["dispatch.persona=false"])
    assert cfg["dispatch"]["persona"] is False
    with pytest.raises(ValueError):
        cli.apply_overrides(cli.load_config(), ["dispatch.personna=false"])


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
    from thinkweave.surfaces.cli.notes import _parse_fm_token
    from thinkweave.surfaces.cli.parser import build_parser

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
        cwd=REPO_ROOT, capture_output=True, text=True,
    ).stdout
    assert "plan-distill" not in out


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
    """The command consults ARCHITECTURE.md (the invariant authority) and prior
    decisions before proposing, so it does not re-propose against already-decided
    work (a skip-list of decided-against directions). Input *context* comes from
    the project snapshot instead — see the thinkweave-native test below."""
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


def test_arch_proposal_input_context_is_thinkweave_native():
    """§1's input context comes from a thinkweave-native surface, not a
    hand-curated doc list: the project snapshot (whose `state` section IS
    STATE.md), with the CLI parity command as the headless degrade. The choice
    of surface must be stated, not left implicit."""
    text = _arch_proposal_doc()
    start = text.index("\n## 1. ")
    section = text[start:text.index("\n## 2. ", start)]
    # The surface lives in §1, named as such rather than left implicit.
    assert "weave_project_snapshot" in section
    assert "input-context surface" in section.lower()
    # Headless degrade: the CLI parity command, then STATE.md on disk.
    assert "weave project-snapshot" in section
    assert "STATE.md" in section
    # The curated-doc-list instruction it replaced must NOT come back.
    assert "Read `ARCHITECTURE.md` end-to-end" not in text
