#!/usr/bin/env python3
"""Deterministic rail for the /issue-loop dev workflow.

The issue tracker IS the DAG: blocking edges live as GitHub-native issue
dependencies (what /to-tickets and /wayfinder publish since Pocock skills
v1.1.0), with issue-body text (``Blocked-by: #16`` header form, or a
``## Blocked by`` section) as the fallback serialization — the rail gates on
the union of both. The graph advances through GitHub's own state machine —
a merged PR closes its issue via ``Closes #N``, which unblocks dependents on
the next run. This script never stores state; it re-reads the tracker and
computes the current frontier, plus the weakly-connected components that
tell the orchestrator which open issues belong to one DAG (chase
sequentially, ``run_mode=exhaust``) vs unrelated work (parallel-safe across
components). LLM judgment stays in the /issue-loop command (implementer,
acceptance judge, reviewer); everything schedulable is plain graph math here.

Subcommands:
  plan     — snapshot issues via `gh`, compute frontier + components (JSON)
  claim    — claim an issue for a run (assignee by default, label mode kept)
  release  — drop the claim
  config     — print resolved loop config (defaults merged with loop.toml)
  check      — run one deterministic gate (kind: command | diff) and emit JSON
  prime      — assemble prior-trajectory prime context for an issue at claim
               time (reads the derived index read-only; holdout-aware)
  trajectory — assemble a per-issue trajectory payload for the memory feed
               (see docs/agents/issue-loop-memory.md)

Stdlib only. Config: docs/agents/loop.toml.
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "docs" / "agents" / "loop.toml"

DEFAULT_CONFIG: dict = {
    "loop": {
        "max_issues_per_run": 3,
        "max_parallel": 1,
        "max_fix_rounds": 2,
        "training_mode": True,
        "draft_pr": True,
        "branch_prefix": "loop/issue-",
        "require_green_baseline": True,
        "claim_mode": "assign",  # assign: assignee IS the claim (wayfinder) | label
        "run_mode": "pass",      # pass: one frontier pass | exhaust: re-plan until dry
        "delivery": "pr-per-issue",  # pr-per-issue | stacked (one branch, one final PR)
        "prime_holdout": 5,      # every Nth run dispatches unprimed (0 = never hold out)
    },
    "tdd": {
        "mode": "auto",  # auto: enforced iff the baseline probe is green
    },
    "labels": {
        "runnable": "ready-for-agent",
        "claimed": "agent-claimed",
        "on_gate_failure": "ready-for-human",
    },
    "triage": {
        # Ship conservative: green (auto-merge-ok) is OFF until a repo has
        # branch protection + required CI and graduates out of training mode.
        "green_enabled": False,
        # Sensitive paths → always red. Three pattern forms (see classify_pr):
        # dir prefix (trailing '/'), bare basename, glob. Translated to THIS
        # repo's layout: the SessionStart/Stop hooks, the CLI+MCP surface
        # (surfaces/ contains the MCP tool-signature files under mcp/), the
        # ontology + sources config by basename, and any *schema* file.
        "sensitive_paths": [
            "hooks/",
            "src/thinkweave/surfaces/",
            "ontology.yaml",
            "sources.yaml",
            "*schema*",
        ],
        # Watched paths → at most yellow (skim, don't gate). Empty by default.
        "watched_paths": [],
        "green_max_diff_lines": 150,   # green requires diff below this
        "green_requires_first_try": True,  # green requires fix_rounds == 0
        "red_min_diff_lines": 800,     # "big diff" → red
    },
    "gates": [],
}


# ---------------------------------------------------------------------------
# Config


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Defaults merged with loop.toml. Gates come only from the file."""
    cfg = {
        "loop": dict(DEFAULT_CONFIG["loop"]),
        "labels": dict(DEFAULT_CONFIG["labels"]),
        "tdd": dict(DEFAULT_CONFIG["tdd"]),
        "triage": dict(DEFAULT_CONFIG["triage"]),
        "gates": [],
    }
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        cfg["loop"].update(data.get("loop", {}))
        cfg["labels"].update(data.get("labels", {}))
        cfg["tdd"].update(data.get("tdd", {}))
        cfg["triage"].update(data.get("triage", {}))
        cfg["gates"] = data.get("gates", [])
    return cfg


def parse_override(spec: str) -> tuple[str, str, object]:
    """Parse one ``--set [section.]key=value`` spec.

    The section defaults to ``loop`` (the common case: ``--set
    delivery=stacked``). The value is parsed as a TOML scalar so the
    override language is exactly loop.toml's (``6`` → int, ``true`` → bool,
    quoted or bare words → str).
    """
    head, sep, raw = spec.partition("=")
    if not sep or not head.strip() or not raw.strip():
        raise ValueError(f"malformed --set '{spec}' (expected [section.]key=value)")
    section, dot, key = head.strip().partition(".")
    if not dot:
        section, key = "loop", section
    try:
        value = tomllib.loads(f"v = {raw.strip()}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw.strip()  # bare word: a plain string, e.g. delivery=stacked
    return section, key, value


def apply_overrides(cfg: dict, specs: list[str]) -> dict:
    """Per-run config overrides, applied after loop.toml.

    Only existing scalar knobs may be overridden — an unknown section or key
    is a hard error (typo protection), and gates are file-only by design
    (the gate pipeline is a trust boundary, not a run-time posture).
    """
    for spec in specs:
        section, key, value = parse_override(spec)
        if section not in ("loop", "labels", "tdd", "triage"):
            raise ValueError(f"--set section '{section}' not overridable (loop | labels | tdd | triage)")
        if key not in DEFAULT_CONFIG[section]:
            known = ", ".join(sorted(DEFAULT_CONFIG[section]))
            raise ValueError(f"--set unknown key '{section}.{key}' (known: {known})")
        cfg[section][key] = value
    return cfg


# ---------------------------------------------------------------------------
# DAG parsing — pure functions over issue bodies

_HEADER_RE = re.compile(r"Blocked[- ]by:\s*(?P<refs>[^|\n]*)", re.IGNORECASE)
_SECTION_RE = re.compile(
    r"^##\s*Blocked\s*by\s*$(?P<refs>.*?)(?=^##\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_WAVE_RE = re.compile(r"Wave:\s*(\d+)", re.IGNORECASE)
_PARALLEL_RE = re.compile(r"Parallel[- ]safe:\s*(yes|no)", re.IGNORECASE)


def parse_blockers(body: str) -> list[int]:
    """Extract blocking issue numbers from either serialization.

    Only the Blocked-by fragment is scanned for ``#N`` refs, so ``Epic: #11``
    or refs elsewhere in the body never count as blockers.
    """
    fragment = None
    m = _HEADER_RE.search(body or "")
    if m:
        fragment = m.group("refs")
    else:
        m = _SECTION_RE.search(body or "")
        if m:
            fragment = m.group("refs")
    if not fragment:
        return []
    return sorted({int(n) for n in re.findall(r"#(\d+)", fragment)})


def parse_wave(body: str) -> int | None:
    m = _WAVE_RE.search(body or "")
    return int(m.group(1)) if m else None


def parse_parallel_safe(body: str) -> bool:
    """Default True: absence of the hint must not serialize the whole loop."""
    m = _PARALLEL_RE.search(body or "")
    return m.group(1).lower() == "yes" if m else True


# ---------------------------------------------------------------------------
# Frontier computation — pure functions over an issue snapshot


def all_blockers(issue: dict) -> list[int]:
    """Union of native dependency edges and body-parsed blockers."""
    return sorted(set(parse_blockers(issue.get("body", ""))) | set(issue.get("native_blockers", [])))


def compute_components(issues: list[dict]) -> dict[int, int]:
    """Weakly-connected components over blocker edges among OPEN issues.

    Component id = the smallest issue number in the component, so ids are
    stable across runs as long as the component's oldest issue stays open.
    Two open issues in the same component belong to one DAG — the
    orchestrator must not work them concurrently; distinct components are
    unrelated work and parallel-safe by construction.
    """
    open_numbers = {i["number"] for i in issues if i["state"].upper() == "OPEN"}
    parent = {n: n for n in open_numbers}

    def find(n: int) -> int:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for issue in issues:
        n = issue["number"]
        if n not in open_numbers:
            continue
        for ref in all_blockers(issue):
            if ref in open_numbers:
                ra, rb = find(n), find(ref)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    return {n: find(n) for n in open_numbers}


def scope_to_dag(issues: list[dict], root: int) -> list[dict]:
    """Keep only the DAG component containing `root` (plus all closed issues,
    which blocker-satisfaction checks still need). Raises if `root` is not an
    open issue — a closed root means that DAG has no open work to scope to."""
    comp = compute_components(issues)
    if root not in comp:
        raise ValueError(f"#{root} is not an open issue — cannot scope to its DAG")
    target = comp[root]
    return [i for i in issues
            if i["state"].upper() != "OPEN" or comp[i["number"]] == target]


def apply_assume_done(issues: list[dict], done: set[int]) -> list[dict]:
    """Treat the listed issues as CLOSED (stacked delivery: their slices are
    already commits on the run's branch, so dependents may proceed even
    though the tracker still shows them open until the final PR merges)."""
    return [{**i, "state": "CLOSED"} if i["number"] in done else i for i in issues]


def compute_frontier(issues: list[dict], cfg: dict, limit: int | None = None) -> dict:
    """Partition issues into frontier / blocked / claimed, with reasons.

    An issue is runnable when it is OPEN, carries the runnable label, is not
    claimed (an assignee IS a claim — wayfinder convention — and the legacy
    claim label still counts), and has no open blocker. Blocking gates on
    the union of native dependencies (``native_blocked_count`` /
    ``native_blockers``, attached by fetch_issues) and body-parsed refs.
    Body refs missing from the snapshot are treated as satisfied but flagged
    (deleted or cross-repo).
    """
    runnable_label = cfg["labels"]["runnable"]
    claimed_label = cfg["labels"]["claimed"]
    by_number = {i["number"]: i for i in issues}
    component = compute_components(issues)

    frontier, blocked, claimed, warnings = [], [], [], []
    for issue in issues:
        if issue["state"].upper() != "OPEN":
            continue
        labels = {l["name"] if isinstance(l, dict) else l for l in issue.get("labels", [])}
        if runnable_label not in labels:
            continue
        entry = {
            "number": issue["number"],
            "title": issue.get("title", ""),
            "blockers": all_blockers(issue),
            "wave": parse_wave(issue.get("body", "")),
            "parallel_safe": parse_parallel_safe(issue.get("body", "")),
            "component": component[issue["number"]],
        }
        assignees = issue.get("assignees", [])
        if assignees or claimed_label in labels:
            entry["assignees"] = [a["login"] if isinstance(a, dict) else a for a in assignees]
            claimed.append(entry)
            continue
        open_blockers = []
        for ref in set(entry["blockers"]) - set(issue.get("native_blockers", [])):
            blocker = by_number.get(ref)
            if blocker is None:
                warnings.append(f"#{issue['number']}: blocker #{ref} not in snapshot; treated as satisfied")
            elif blocker["state"].upper() == "OPEN":
                open_blockers.append(ref)
        # native_blocked_count is GitHub's own open-blocker count — it gates
        # even when the edge list wasn't fetched (list is enrichment only).
        open_blockers += [r for r in issue.get("native_blockers", [])
                          if by_number.get(r, {}).get("state", "OPEN").upper() == "OPEN"]
        if open_blockers or (issue.get("native_blocked_count", 0) > 0 and not issue.get("native_blockers")):
            entry["open_blockers"] = sorted(set(open_blockers))
            if not open_blockers:
                entry["open_blockers_note"] = "native blocked_by count > 0 (edge list not fetched)"
            blocked.append(entry)
        else:
            frontier.append(entry)

    frontier.sort(key=lambda e: (e["wave"] if e["wave"] is not None else 10**9, e["number"]))
    if limit is not None:
        frontier = frontier[:limit]
    return {"frontier": frontier, "blocked": blocked, "claimed": claimed, "warnings": warnings}


# ---------------------------------------------------------------------------
# Deterministic gates


def run_command_gate(gate: dict, cwd: Path) -> dict:
    proc = subprocess.run(
        gate["cmd"],
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=gate.get("timeout_sec", 900),
    )
    tail = "\n".join((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-30:])
    return {
        "id": gate["id"],
        "kind": "command",
        "passed": proc.returncode == 0,
        "summary": f"`{gate['cmd']}` exited {proc.returncode}",
        "detail": tail,
    }


def evaluate_diff_gate(gate: dict, numstat: str) -> dict:
    """Pure evaluation of `git diff --numstat` output against constraints."""
    forbidden = gate.get("forbidden_paths", [])
    max_lines = gate.get("max_changed_lines")
    touched_forbidden, total = [], 0
    for line in numstat.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        total += (0 if added == "-" else int(added)) + (0 if deleted == "-" else int(deleted))
        if any(path.startswith(p) for p in forbidden):
            touched_forbidden.append(path)
    failures = []
    if touched_forbidden:
        failures.append(f"touches forbidden paths: {', '.join(touched_forbidden)}")
    if max_lines is not None and total > max_lines:
        failures.append(f"{total} changed lines > max {max_lines}")
    return {
        "id": gate["id"],
        "kind": "diff",
        "passed": not failures,
        "summary": "; ".join(failures) or f"{total} changed lines, no forbidden paths",
        "detail": "",
    }


def run_diff_gate(gate: dict, cwd: Path, base_ref: str) -> dict:
    numstat = subprocess.run(
        ["git", "diff", "--numstat", f"{base_ref}...HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return evaluate_diff_gate(gate, numstat)


# ---------------------------------------------------------------------------
# Risk-lane PR triage — pure classification over a shipped PR's signal set
#
# The rail already computes every triage signal (gate results incl. review
# severity, diff size, files touched, fix_rounds, degraded baseline). This
# classifies each shipped PR into green/yellow/red so a human reviews only what
# matters — escalation-not-gates, matching the loop's existing ready-for-human
# rung. Labels are APPLIED by the orchestrator (via gh); the rail only decides.

# Recognized enum values. "none"/"minor" review stay green-eligible (the
# issue's "review <= minor"); "met" acceptance is the only clean pass. A value
# OUTSIDE these sets is not benign — LLM-assembled signals make enum drift
# ("high", "partial", "blocker") realistic, so an unrecognized value fails
# closed to red rather than slipping through green-eligible.
_VALID_REVIEW = {"none", "minor", "major", "critical"}
_RED_REVIEW = {"major", "critical"}
_VALID_ACCEPTANCE = {"met", "uncertain", "not-met"}
_RED_ACCEPTANCE = {"uncertain", "not-met"}

# Green/yellow labels are loop-internal vocabulary. The red label is NOT here —
# it is sourced from labels.on_gate_failure (classify_pr's red_label arg) so
# triage-red and gate-failure share one label with no duplicate literal.
TRIAGE_LABELS = {"green": "auto-merge-ok", "yellow": "review-light"}


def _path_matches(path: str, pattern: str) -> bool:
    """Match one repo-relative path against one sensitive/watched pattern.

    Three forms, dispatched by shape (issue #59):
      - dir prefix — trailing ``/`` (``hooks/``, ``src/thinkweave/surfaces/``):
        the path is under that directory (``startswith``, same convention as
        the diff-guard gate's ``forbidden_paths``).
      - glob — contains ``*``/``?``/``[`` (``*schema*``): fnmatched
        case-insensitively against the basename (or the whole path when the
        glob itself spans directories), so ``docs/SCHEMA.md`` is caught too.
      - bare filename — anything else (``ontology.yaml``): the path's basename
        equals it, so the file matches at any depth (a different basename that
        merely shares the stem as a prefix does not).
    """
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if any(c in pattern for c in "*?["):
        target = path if "/" in pattern else path.rsplit("/", 1)[-1]
        return fnmatch.fnmatch(target.lower(), pattern.lower())
    return path.rsplit("/", 1)[-1] == pattern


def _path_hits(files: list[str], patterns: list[str]) -> list[str]:
    """``"<path> (matches <pattern>)"`` for each file that hits any pattern,
    deduped and sorted — the human-readable reason fragments."""
    hits = set()
    for path in files:
        for pat in patterns:
            if _path_matches(path, pat):
                hits.add(f"{path} (matches {pat})")
    return sorted(hits)


def classify_pr(signals: dict, cfg: dict,
                red_label: str = DEFAULT_CONFIG["labels"]["on_gate_failure"]) -> dict:
    """Classify one shipped PR into a risk lane. Pure over (signals, cfg).

    ``cfg`` is the resolved ``[triage]`` config section. ``red_label`` is the
    tracker label for the red lane — sourced from ``labels.on_gate_failure`` by
    the caller (default keeps the canonical ``ready-for-human``) so triage-red
    and gate-failure stay one label. Precedence is red > yellow > green, and
    every triggered rule is listed in ``reasons`` (short-circuit reasons: report
    all of them, not just the first). Returns ``{lane, label, reasons}``.

    **Fail-closed.** The three safety-critical signals — ``baseline_green``,
    ``acceptance``, ``review_severity`` — are REQUIRED: an absent key or an
    unrecognized enum value goes RED (naming the key/value), never
    green-eligible, because LLM-assembled signals make that drift realistic.
    The rest default benignly (absence is not a safety hole).

    Signals schema:
      - ``fix_rounds`` int — implement→gate→fix iterations (0 = first try) [opt, →0]
      - ``diff_lines`` int — total changed lines in the PR's diff [opt, →0]
      - ``files_touched`` list[str] — repo-relative paths changed [opt, →[]]
      - ``tests_touched`` bool — the change carries test coverage [opt, →False]
      - ``review_severity`` str — worst review finding: none|minor|major|critical [REQUIRED]
      - ``baseline_green`` bool — tests gate green on the pristine worktree [REQUIRED]
      - ``acceptance`` str — acceptance verdict: met|uncertain|not-met [REQUIRED]
    """
    fix_rounds = int(signals.get("fix_rounds", 0) or 0)
    diff_lines = int(signals.get("diff_lines", 0) or 0)
    files = signals.get("files_touched") or []
    tests_touched = bool(signals.get("tests_touched", False))

    # --- red: any hard-escalation rule. List them all. -----------------------
    red: list[str] = []
    sensitive = _path_hits(files, cfg.get("sensitive_paths", []))
    if sensitive:
        red.append("sensitive path(s): " + ", ".join(sensitive))
    if diff_lines >= cfg["red_min_diff_lines"]:
        red.append(f"large diff: {diff_lines} lines >= {cfg['red_min_diff_lines']}")

    # baseline_green — required; missing, non-bool (a truthy "false" string must
    # not pass), or False → red.
    if "baseline_green" not in signals:
        red.append("baseline_green signal missing (fail-closed)")
    elif not isinstance(signals["baseline_green"], bool):
        red.append(f"non-bool baseline_green {signals['baseline_green']!r} (fail-closed)")
    elif not signals["baseline_green"]:
        red.append("degraded baseline (tests not green on the pristine worktree)")

    # review_severity — required; missing or off-enum → red.
    if "review_severity" not in signals:
        red.append("review_severity signal missing (fail-closed)")
    else:
        review = str(signals["review_severity"]).lower()
        if review not in _VALID_REVIEW:
            red.append(f"unrecognized review_severity '{signals['review_severity']}' (fail-closed)")
        elif review in _RED_REVIEW:
            red.append(f"review severity {review}")

    # acceptance — required; missing or off-enum → red.
    if "acceptance" not in signals:
        red.append("acceptance signal missing (fail-closed)")
    else:
        acceptance = str(signals["acceptance"]).lower()
        if acceptance not in _VALID_ACCEPTANCE:
            red.append(f"unrecognized acceptance '{signals['acceptance']}' (fail-closed)")
        elif acceptance in _RED_ACCEPTANCE:
            red.append(f"acceptance {acceptance}")

    if red:
        return {"lane": "red", "label": red_label, "reasons": red}

    # --- yellow: passed, but warrants a human skim. List them all. -----------
    yellow: list[str] = []
    if cfg.get("green_requires_first_try", True) and fix_rounds > 0:
        yellow.append(f"{fix_rounds} fix round(s)")
    if diff_lines >= cfg["green_max_diff_lines"]:
        yellow.append(f"medium diff: {diff_lines} lines >= {cfg['green_max_diff_lines']}")
    watched = _path_hits(files, cfg.get("watched_paths", []))
    if watched:
        yellow.append("watched path(s): " + ", ".join(watched))
    if not tests_touched:
        yellow.append("no test coverage signal (tests_touched=false)")
    if yellow:
        return {"lane": "yellow", "label": TRIAGE_LABELS["yellow"], "reasons": yellow}

    # --- green criteria all met. Only auto-merge-ok where green is enabled. ---
    if cfg.get("green_enabled", False):
        return {"lane": "green", "label": TRIAGE_LABELS["green"], "reasons": []}
    return {"lane": "yellow", "label": TRIAGE_LABELS["yellow"],
            "reasons": ["green lane disabled (training-mode graduation pending)"]}


# ---------------------------------------------------------------------------
# Trajectory assembly (memory feed) — pure function + subcommand


def _normalize_skill(entry: dict) -> dict:
    """Project one dispatch record down to the invocation-trajectory shape.

    A stage skill is effectively a gate/subagent the loop already dispatches
    (implementer, acceptance judge, reviewer, and future ponytail/tdd), so we
    keep only the four fields that make the invocation first-class:
    ``id`` (which skill), ``role`` (its stage role), ``outcome`` (how the
    invocation resolved), and ``fix_rounds_attributed`` (how many fix rounds
    this skill/gate caused — the explicit attribution). Extra keys the
    orchestrator carries for its own bookkeeping are dropped; a missing
    attribution count defaults to 0.
    """
    return {
        "id": entry.get("id", ""),
        "role": entry.get("role", ""),
        "outcome": entry.get("outcome", ""),
        "fix_rounds_attributed": int(entry.get("fix_rounds_attributed", 0) or 0),
    }


# ---------------------------------------------------------------------------
# Semantic execution trace (issue #85) — the run-bound register the gate agents
# already compose (reviewer findings + reasoning, simplify cut/keep rationale,
# judge criterion evidence + verdict flips, TDD red-confirmation), condensed by
# the orchestrator into structured envelopes FROM THOSE REPORTS. No new model
# calls: this rail only accepts and shapes. Prose-valued fields carry the
# distilled signal; counts (lines_delta, flipped_by_round) stay as filter/join
# keys. The normalization posture is the hybrid the sibling mirror flags settled:
# strict on the top-level TYPE (a non-dict trace raises, like #57's served
# list-guard) and lenient on KEYS (unknown keys dropped, each item projected to
# its known subfields, like #56's skills projection). Only provided top-level
# sections appear — an absent section is omitted, never emitted empty.

_TRACE_SECTIONS = ("rounds", "criteria", "simplify", "edge_cases", "tdd")


def _as_int_or_none(value: object) -> int | None:
    """Coerce a nullable count (``flipped_by_round``): an int stays an int,
    a bool or anything non-int-like becomes ``None`` (unflipped)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _normalize_trace_round(entry: dict) -> dict:
    """Project one review/fix round to ``{gate, finding, severity, disposition,
    fixed_by}`` — the reviewer's finding + reasoning + how it was resolved."""
    return {
        "gate": str(entry.get("gate", "") or ""),
        "finding": str(entry.get("finding", "") or ""),
        "severity": str(entry.get("severity", "") or ""),
        "disposition": str(entry.get("disposition", "") or ""),
        "fixed_by": str(entry.get("fixed_by", "") or ""),
    }


def _normalize_trace_criterion(entry: dict) -> dict:
    """Project one acceptance criterion to ``{id, verdict, flipped_by_round}`` —
    the judge's per-criterion evidence + the round its verdict flipped (or None)."""
    return {
        "id": str(entry.get("id", "") or ""),
        "verdict": str(entry.get("verdict", "") or ""),
        "flipped_by_round": _as_int_or_none(entry.get("flipped_by_round")),
    }


def _normalize_trace_whatwhy(entry: dict) -> dict:
    """Project one simplify cut/keep to ``{what, why}`` — the over-engineering
    (or load-bearing) description and the rationale."""
    return {
        "what": str(entry.get("what", "") or ""),
        "why": str(entry.get("why", "") or ""),
    }


def _normalize_trace(raw: object) -> dict:
    """Shape an incoming semantic-trace object into its stored envelope.

    Strict on type: a non-dict ``raw`` raises ``ValueError`` (a list or bare
    string pasted by mistake must not silently corrupt the run-bound trace).
    Lenient on keys: unknown top-level keys are dropped and each section is
    projected to its known subfields; a section whose value is the wrong
    container is skipped (omitted), never emitted malformed.
    """
    if not isinstance(raw, dict):
        raise ValueError("trace must be a JSON object")
    out: dict = {}
    rounds = raw.get("rounds")
    if isinstance(rounds, list):
        out["rounds"] = [_normalize_trace_round(e) for e in rounds if isinstance(e, dict)]
    criteria = raw.get("criteria")
    if isinstance(criteria, list):
        out["criteria"] = [_normalize_trace_criterion(e) for e in criteria if isinstance(e, dict)]
    simplify = raw.get("simplify")
    if isinstance(simplify, dict):
        cuts = simplify.get("cuts")
        kept = simplify.get("kept")
        out["simplify"] = {
            "outcome": str(simplify.get("outcome", "") or ""),
            "cuts": [_normalize_trace_whatwhy(c) for c in cuts if isinstance(c, dict)]
                    if isinstance(cuts, list) else [],
            "kept": [_normalize_trace_whatwhy(c) for c in kept if isinstance(c, dict)]
                    if isinstance(kept, list) else [],
            "lines_delta": int(simplify.get("lines_delta", 0) or 0),
        }
    edge_cases = raw.get("edge_cases")
    if isinstance(edge_cases, list):
        out["edge_cases"] = [str(x) for x in edge_cases if isinstance(x, str)]
    tdd = raw.get("tdd")
    if isinstance(tdd, dict):
        out["tdd"] = {"red_confirmed": bool(tdd.get("red_confirmed", False))}
    return out


def build_trajectory(issue: dict, *, branch: str, commits: list[str],
                     numstat: str, gates: list[dict], fix_rounds: int,
                     outcome: str, pr_url: str = "", run_id: str = "",
                     skills: list[dict] | None = None,
                     skill_centric: bool = False,
                     primed: bool | None = None,
                     served: list[str] | None = None,
                     trace: dict | None = None) -> dict:
    """Assemble the deterministic half of a per-issue trajectory note.

    Emits a weave_create-shaped payload: everything mechanical (files, gate
    verdicts, rounds, refs, skill invocations) goes in frontmatter; the body
    is left as a skeleton for the orchestrator to fill with judgment (what
    was learned, why fix rounds happened) — concepts are chosen at creation
    time by the LLM in the loop, never backfilled.

    ``skills`` is the loop's stage-dispatch log — each dispatched stage skill
    (implementer / acceptance judge / reviewer / ponytail / tdd) as
    ``{id, role, outcome, fix_rounds_attributed}``. Existing callers pass
    nothing and get an empty ``skills: []`` (backward compatible). Set
    ``skill_centric`` when the record is primarily about a skill invocation
    (SkillOpt raw material) — it adds the ``skill-invocation`` tag alongside
    the always-present ``loop-run``.

    ``primed``/``served`` mirror the claim-time prime verdict (``prime <N>``):
    ``primed=True`` with the served note ids when the run received prior-
    trajectory context, ``primed=False`` with an empty list when it was a
    deliberate holdout. Together with #60's ``outcome`` this frontmatter is the
    served-context regression's raw material. ``primed=None`` (the default —
    pre-#57 callers) omits both keys, leaving the note shape unchanged.

    ``trace`` (issue #85) is the run-bound semantic execution trace — the gate
    agents' own reports condensed by the orchestrator into structured envelopes
    (see :func:`_normalize_trace`). It is stored under a single ``trace``
    frontmatter key: the machine-readable half of the tracker's gate evidence,
    never a second prose owner. ``trace=None`` (the default) omits the key, so
    a caller without a trace produces a byte-stable pre-#85 payload — and the
    RLVR export row, which never reads ``trace``, stays locked.
    """
    files = [line.split("\t")[2] for line in numstat.strip().splitlines()
             if len(line.split("\t")) == 3]
    tags = ["loop-run"] + (["skill-invocation"] if skill_centric else [])
    frontmatter = {
        "issue": issue["number"],
        "issue_url": issue.get("html_url", ""),
        "pr_url": pr_url,
        "run_id": run_id,
        "branch": branch,
        "outcome": outcome,  # shipped | routed-to-human | awaiting-approval
        "fix_rounds": fix_rounds,
        "commits": len(commits),
        "files_touched": sorted(set(files)),
        "gates": [{"id": g["id"], "passed": g["passed"], "summary": g.get("summary", "")}
                  for g in gates],
        "skills": [_normalize_skill(s) for s in (skills or [])],
    }
    if primed is not None:
        if served is not None and (
            not isinstance(served, list)
            or not all(isinstance(s, str) for s in served)
        ):
            # A dict (e.g. the whole prime payload) or a bare string would
            # silently corrupt the served-context regression's raw material —
            # served must be a flat list of note-id strings.
            raise ValueError("served must be a list of note-id strings")
        frontmatter["primed"] = primed
        frontmatter["served"] = list(served or [])
    if trace is not None:
        # The machine-readable half of the tracker's gate evidence (issue #85):
        # a run-bound envelope, never a second prose owner. Absent → no key, so
        # the pre-#85 frontmatter shape is byte-stable for callers without a
        # trace (and the RLVR export row, which never reads it, stays locked).
        frontmatter["trace"] = _normalize_trace(trace)
    return {
        "type": "note",
        "title": f"loop trajectory #{issue['number']}: {issue.get('title', '')[:80]}",
        "tags": tags,
        "frontmatter": frontmatter,
        "body_skeleton": (
            "## What\n<1-2 sentences: the slice delivered>\n\n"
            "## How it went\n<fix rounds and why; seams chosen; surprises>\n\n"
            "## Lessons\n<only what a future run would reuse — omit section if none>"
        ),
        "concept_hints": [l["name"] if isinstance(l, dict) else l
                          for l in issue.get("labels", [])],
    }


# ---------------------------------------------------------------------------
# Claim-time priming — serve prior trajectories' Lessons to the implementer
#
# The native analog of ``bd prime``: before implementing issue N, fetch the
# reusable half of prior similar work (trajectory notes' Lessons sections) and
# splice it into the implementer prompt. Everything here is a pure function of
# (read-only index, issue concepts, run-id); the rail stays stdlib-only and
# never imports thinkweave — it reads the derived SQLite index directly (the
# `weave` CLI may be absent on PATH in some installs; a direct read-only
# sqlite3 open is robust and needs no PATH).


def is_holdout(run_id: str, holdout: int) -> bool:
    """Deterministic per-run holdout: every Nth run dispatches unprimed.

    Loop runs are numerous, comparable, and gate-scored (#60's ``outcome``),
    so periodically withholding prime context lets the outcome regression
    separate "context helped" from "easy issue". The decision is
    ``sha1(run_id) mod N == 0`` — stable across processes (no PYTHONHASHSEED
    dependence, unlike ``hash()``) and date/random-free, so it is
    hand-computable and testable. ``holdout <= 0`` disables holdout entirely.
    """
    if holdout <= 0:
        return False
    digest = int(hashlib.sha1(run_id.encode("utf-8")).hexdigest(), 16)
    return digest % holdout == 0


_LESSONS_RE = re.compile(
    r"^##\s+Lessons\s*$(?P<txt>.*?)(?=^##\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def extract_lessons(body: str) -> str:
    """Text under a ``## Lessons`` heading (until the next ``##`` or EOF).

    Returns ``''`` when there is no Lessons section — trajectory notes omit it
    when a run taught nothing reusable, and those notes carry no prime value.
    """
    m = _LESSONS_RE.search(body or "")
    return m.group("txt").strip() if m else ""


def _open_index_ro(db_path: str) -> sqlite3.Connection:
    """Open the derived index strictly read-only (never mutate derived state)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _coerce_builds_on(raw: object) -> list[str]:
    """Normalize a trajectory's ``builds_on`` frontmatter to a list of note ids.

    Accepts the plain ``["n-xxxxxx", …]`` form weave_create writes, and tolerates
    path-based wikilinks (``[[path|n-xxxxxx]]``) by taking the trailing id. Any
    non-list / non-string element is dropped — a bad link never crashes prime.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if s.startswith("[[") and s.endswith("]]"):
            s = s[2:-2]
        if "|" in s:
            s = s.split("|")[-1]
        s = s.strip()
        if s:
            out.append(s)
    return out


def resolve_insights(conn: sqlite3.Connection, ids: list[str]) -> list[dict]:
    """Read-only: fetch the bodies of the insight notes a trajectory builds on.

    Returns ``[{id, body}]`` in ``builds_on`` order, skipping ids that don't
    resolve to a note or resolve to an empty body. The index already holds these
    notes (they are ordinary notes minted at ship time); prime reads their
    ``body_text`` via the same sqlite path it uses for trajectories.
    """
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, body_text FROM notes WHERE id IN ({placeholders})", ids
    ).fetchall()
    by_id = {r["id"]: (r["body_text"] or "").strip() for r in rows}
    return [{"id": i, "body": by_id[i]} for i in ids if by_id.get(i)]


# Outcome-weighting rank for prime ordering (issue #85). merged-clean/stable
# float to the top, reworked/closed/reverted sink; unlabeled and unknown stay
# neutral (rank 1) so an all-unlabeled match set keeps pure recency order — the
# byte-stable v1 behavior. Python's stable sort preserves recency within a rank.
_OUTCOME_RANK = {
    "merged-clean": 0, "stable": 0,
    "reworked": 2, "reworked-post-merge": 2,
    "closed-unmerged": 2, "reverted": 2, "routed-to-human": 2,
}


def _outcome_rank(label: object) -> int:
    return _OUTCOME_RANK.get(str(label or ""), 1)


def query_trajectories(
    conn: sqlite3.Connection, concepts: list[str], limit: int, scan_cap: int = 40
) -> list[dict]:
    """Read-only: ``[loop-run]`` notes matching ANY concept that carry reusable
    color — a linked insight note (v2, ``builds_on``) or an inline Lessons
    section (v1 fallback).

    Returns ``{id, title, issue, outcome, outcome_label, lessons, insights}``
    dicts, at most ``limit``. ``insights`` is the resolved list of linked
    insight-note bodies (``[{id, body}]``); a v1 note has ``insights == []`` and
    non-empty ``lessons``. Empty ``concepts`` matches nothing. The scan reads up
    to ``scan_cap`` candidates in recency order, keeps those with reusable color,
    then applies the outcome-weighting sort (:data:`_OUTCOME_RANK`) — stable, so
    recency is preserved within a rank and an all-unlabeled set is pure recency —
    before truncating to ``limit``.
    """
    if not concepts:
        return []
    placeholders = ",".join("?" * len(concepts))
    rows = conn.execute(
        f"""SELECT DISTINCT n.id, n.title, n.date, n.frontmatter, n.body_text
            FROM notes n
            JOIN note_tags t ON t.note_id = n.id AND t.tag = 'loop-run'
            JOIN note_concepts c ON c.note_id = n.id
            WHERE c.concept IN ({placeholders})
            ORDER BY n.date DESC, n.id DESC
            LIMIT ?""",
        [*concepts, scan_cap],
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            fm = json.loads(r["frontmatter"] or "{}")
        except json.JSONDecodeError:
            fm = {}
        insights = resolve_insights(conn, _coerce_builds_on(fm.get("builds_on")))
        lessons = extract_lessons(r["body_text"] or "")
        if not insights and not lessons:
            # No reusable color (v2 link resolved nothing AND no inline Lessons).
            continue
        out.append({
            "id": r["id"],
            "title": r["title"] or "",
            "issue": fm.get("issue"),
            "outcome": fm.get("outcome", ""),
            "outcome_label": fm.get("outcome_label", ""),
            "lessons": lessons,
            "insights": insights,
        })
    out.sort(key=lambda t: _outcome_rank(t.get("outcome_label")))
    return out[:limit]


def render_prime_block(
    trajectories: list[dict], decisions: list[str] | None = None,
    budget_chars: int = 1200,
) -> tuple[str, list[str]]:
    """Render the primed-context markdown + the flat served-id list.

    Each trajectory renders its reusable color first (each capped-in as a whole
    piece until the char budget is spent — at least one always lands if any
    exist). A v2 trajectory serves the BODIES of the insight notes it builds on
    (``served`` records the insight ids — that is what the run received); a v1
    trajectory serves its inline Lessons (``served`` records the trajectory id).
    ``decisions`` (the decisions_for_file note ids the orchestrator already
    resolved) are appended as an adjacency line so the served log records both
    kinds. ``served`` carries every id actually rendered. Empty input →
    ``('', [])`` so the caller skips cleanly.
    """
    decisions = decisions or []
    if not trajectories and not decisions:
        return "", []
    pieces = ["## Prior trajectories — reusable lessons from similar prior runs\n"]
    served: list[str] = []
    for t in trajectories:
        head = f"### #{t.get('issue')} — {t.get('title', '')} ({t.get('outcome', '')})".rstrip()
        insights = t.get("insights") or []
        if insights:
            body_text = "\n".join(ins["body"] for ins in insights)
            piece_ids = [ins["id"] for ins in insights]
        else:
            body_text = t.get("lessons", "")
            piece_ids = [t["id"]]
        piece = f"{head}\n{body_text}\n"
        if served and sum(len(x) for x in pieces) + len(piece) > budget_chars:
            break
        pieces.append(piece)
        served.extend(piece_ids)
    if decisions:
        pieces.append("Prior decisions for touched files: " + ", ".join(decisions))
        served.extend(decisions)
    return "\n".join(pieces).strip() + "\n", served


def build_prime_payload(
    issue_number: int, run_id: str, concepts: list[str], *,
    conn: sqlite3.Connection | None = None, holdout: int = 5,
    limit: int = 3, budget_chars: int = 1200, decisions: list[str] | None = None,
) -> dict:
    """Assemble the claim-time prime payload the orchestrator splices verbatim.

    Output keys: ``primed`` (received prime context this run), ``holdout``
    (deliberately withheld), ``served`` (note ids served — trajectory + decisions,
    capped ``limit`` per kind), ``block`` (markdown to splice; ``''`` when
    unprimed), ``note`` (why unprimed, when it is). A held-out or empty-match
    run returns ``primed=False`` with no served ids and an empty block, so the
    loop runs unchanged.
    """
    payload = {
        "issue": issue_number, "run_id": run_id, "concepts": list(concepts),
        "holdout": is_holdout(run_id, holdout), "primed": False,
        "served": [], "block": "", "note": "",
    }
    if payload["holdout"]:
        payload["note"] = (
            f"held out (every {holdout}th run runs unprimed for the outcome regression)"
        )
        return payload
    # The query — not the connect — is where a foreign/corrupt file
    # (DatabaseError) or an older index missing note_tags/note_concepts
    # (OperationalError) raises. Guard here so a bad index degrades to unprimed
    # rather than crashing the loop (this module's never-crash invariant).
    index_error = False
    trajectories: list[dict] = []
    if conn is not None:
        try:
            trajectories = query_trajectories(conn, concepts, limit)
        except sqlite3.Error:
            index_error = True
    decisions = (decisions or [])[:limit]
    block, served = render_prime_block(trajectories, decisions, budget_chars)
    payload["block"] = block
    payload["served"] = served
    payload["primed"] = bool(served)
    if not served:
        payload["note"] = (
            "index unreadable (corrupt or schema-drift) — ran unprimed"
            if index_error else "no matching prior trajectories"
        )
    return payload


# Sentinel tool name stamped on the served-context buffer event. The indexer's
# context_served projection keys off this to assign source='loop-prime' — the
# exact mechanism prompt-time retrieval uses (its PROMPT_TIME_TOOL sentinel →
# source='prompttime'), so context_served stays a pure projection of the
# per-session retrieval_log.jsonl event log.
LOOP_PRIME_TOOL = "loop_prime"


def _append_served_event(
    buffer_path: str, run_id: str, issue_number: int,
    served: list[str], session_id: str = "",
) -> None:
    """Append one loop-prime served-context event to the session buffer JSONL.

    Mirrors the prompt-time serving surface: a ``retrieval``-typed event tagged
    with the ``loop_prime`` sentinel tool. ``archive_buffer`` folds it into the
    session's ``retrieval_log.jsonl`` (append-only) at Stop, and the indexer
    projects it to ``context_served(source='loop-prime')`` — recoverable per run
    from the index, derived and rebuildable from the markdown-adjacent log.
    """
    event = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "type": "retrieval",
        "tool": LOOP_PRIME_TOOL,
        "args": {"run_id": run_id, "issue": issue_number, "session_id": session_id},
        "returned_ids": served,
    }
    p = Path(buffer_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _split_csv(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def _read_weave_dir_override(vault_root: Path) -> Path | None:
    """Honor a top-level ``weave_dir`` in the vault's config.toml.

    PR #10 relocates derived state (index.db, embeddings.db, buffer/) off the
    vault path — on 9P-mounted vaults the live index is ``<weave_dir>/index.db``,
    NOT ``<vault>/.weave/index.db``. Mirror ``core.config``'s resolution: ``~``
    expands, a relative value anchors at ``vault_root``, absolute passes
    through. Read ``config/config.toml`` first, then the legacy
    ``.weave/config.toml``. Malformed/unreadable config or an absent key →
    ``None`` (fall back to the legacy layout; never crash).
    """
    for rel in ("config/config.toml", ".weave/config.toml"):
        path = vault_root / rel
        if not path.exists():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        value = data.get("weave_dir")
        if isinstance(value, str) and value.strip():
            resolved = Path(value).expanduser()
            return resolved if resolved.is_absolute() else vault_root / resolved
    return None


def _resolve_index_db(db: str | None, vault: str | None) -> str | None:
    """Resolve the read-only index db path without importing thinkweave.

    ``--db`` wins; else derive from ``--vault``: ``<weave_dir>/index.db`` when
    the vault's config.toml overrides ``weave_dir`` (PR #10), otherwise the
    legacy ``<vault>/.weave/index.db``; else ``THINKWEAVE_INDEX_DB``. Returns
    None when nothing resolves — the prime then serves an empty (unprimed)
    block rather than guessing a path (never touch an ambient real vault).
    """
    if db:
        return db
    if vault:
        vault_root = Path(vault)
        weave_dir = _read_weave_dir_override(vault_root) or (vault_root / ".weave")
        return str(weave_dir / "index.db")
    return os.environ.get("THINKWEAVE_INDEX_DB") or None


# ---------------------------------------------------------------------------
# gh plumbing


def _gh(args: list[str]) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def fetch_issues() -> list[dict]:
    """Snapshot all issues with native-dependency enrichment.

    Uses the REST issues endpoint (not `gh issue list --json`) because it
    carries ``issue_dependencies_summary`` — GitHub's own count of OPEN
    blockers, maintained natively since /to-tickets and /wayfinder publish
    blocking as issue dependencies. For open issues with a nonzero count,
    the actual blocker numbers are fetched (one extra call each) so plans
    can name them and components can include the edges.
    """
    # --jq '.[]' flattens each page to NDJSON — works on gh versions
    # predating --slurp, and never confuses body text for page boundaries.
    out = _gh(["api", "--paginate", "--jq", ".[]",
               "repos/{owner}/{repo}/issues?state=all&per_page=100"])
    issues = []
    for line in out.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if "pull_request" in item:
            continue
        issue = {
            "number": item["number"],
            "title": item.get("title", ""),
            "state": item["state"],
            "labels": item.get("labels", []),
            "assignees": item.get("assignees", []),
            "body": item.get("body") or "",
            "native_blocked_count": (item.get("issue_dependencies_summary") or {}).get("blocked_by", 0),
        }
        if issue["state"].upper() == "OPEN" and issue["native_blocked_count"] > 0:
            try:
                refs = _gh(["api", f"repos/{{owner}}/{{repo}}/issues/{issue['number']}/dependencies/blocked_by",
                            "--jq", "[.[].number]"])
                issue["native_blockers"] = json.loads(refs)
            except subprocess.CalledProcessError:
                issue["native_blockers"] = []  # count still gates; list is enrichment
        issues.append(issue)
    return issues


def _fetch_labels(number: int) -> list[str]:
    """Issue label names via gh (network). Empty list on any failure — a prime
    with no concepts serves an empty block, never crashes the loop."""
    try:
        out = _gh(["issue", "view", str(number), "--json", "labels",
                   "--jq", "[.labels[].name]"])
        return json.loads(out or "[]")
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return []


# ---------------------------------------------------------------------------
# CLI


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser (factory so the argparse contract is testable
    without going through main → gh → git)."""
    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--set", action="append", dest="overrides", default=[],
        metavar="[SECTION.]KEY=VALUE",
        help="per-run config override, e.g. --set delivery=stacked "
             "--set max_issues_per_run=6 (section defaults to 'loop'; "
             "repeatable; applied after loop.toml; gates are file-only)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="compute the runnable frontier", parents=[common])
    p_plan.add_argument("--limit", type=int, default=None)
    p_plan.add_argument("--dag", type=int, default=None, metavar="N",
                        help="scope to the DAG component containing issue N")
    p_plan.add_argument("--assume-done", default="", metavar="N,N",
                        help="treat these issues as closed (stacked delivery: slices already on the branch)")

    p_claim = sub.add_parser("claim", help="claim an issue for a run", parents=[common])
    p_claim.add_argument("number", type=int)
    p_claim.add_argument("--run-id", required=True)

    p_release = sub.add_parser("release", help="release a claimed issue", parents=[common])
    p_release.add_argument("number", type=int)

    sub.add_parser("config", help="print resolved config as JSON", parents=[common])

    p_check = sub.add_parser("check", help="run one deterministic gate", parents=[common])
    p_check.add_argument("--gate", required=True)
    p_check.add_argument("--cwd", default=".")
    p_check.add_argument("--base-ref", default="origin/main")

    p_prime = sub.add_parser("prime", help="assemble prior-trajectory prime context for an issue", parents=[common])
    p_prime.add_argument("number", type=int)
    p_prime.add_argument("--run-id", required=True)
    p_prime.add_argument("--labels", default=None,
                         help="comma-separated issue label names; omit to fetch via gh")
    p_prime.add_argument("--concepts", default=None,
                         help="comma-separated match concepts; omit to derive from --labels")
    p_prime.add_argument("--db", default=None, help="index db path (opened read-only)")
    p_prime.add_argument("--vault", default=None,
                         help="vault root; resolves the index under the vault's "
                              "weave_dir override (config.toml) when --db is absent, "
                              "else <vault>/.weave/index.db")
    p_prime.add_argument("--limit", type=int, default=3,
                         help="max prior trajectories (and decisions) to splice — top-N per kind")
    p_prime.add_argument("--budget-chars", type=int, default=1200,
                         help="char budget for the spliced block")
    p_prime.add_argument("--decisions", default=None,
                         help="comma-separated decisions_for_file note ids to fold into served context")
    p_prime.add_argument("--buffer", default=None,
                         help="session buffer JSONL to append the loop_prime served-context event to")
    p_prime.add_argument("--session-id", default="",
                         help="loop session id, stamped into the served-context event")

    p_triage = sub.add_parser("triage", help="classify a shipped PR into a risk lane", parents=[common])
    p_triage.add_argument("number", type=int, nargs="?", default=None,
                          help="issue/PR number for the output (optional; the "
                               "signals JSON may also carry an 'issue' key)")
    p_triage.add_argument("--signals-json", required=True,
                          help="file with the PR's signal set: {fix_rounds, "
                               "diff_lines, files_touched, tests_touched, "
                               "review_severity, baseline_green, acceptance}")

    p_traj = sub.add_parser("trajectory", help="assemble a per-issue trajectory payload (memory feed)", parents=[common])
    p_traj.add_argument("number", type=int)
    p_traj.add_argument("--cwd", default=".", help="the issue's implementer worktree")
    p_traj.add_argument("--base-ref", default="origin/main")
    p_traj.add_argument("--gates-json", required=True, help="file with the gate results list")
    p_traj.add_argument("--skills-json", default=None,
                        help="file with the stage-dispatch log: a list of "
                             "{id, role, outcome, fix_rounds_attributed} — the "
                             "skills the loop dispatched (implementer, acceptance "
                             "judge, reviewer, ...). Omit for an empty skills[].")
    p_traj.add_argument("--skill-centric", action="store_true",
                        help="mark this record skill-centric (adds the "
                             "skill-invocation tag alongside loop-run)")
    p_traj.add_argument("--primed", action=argparse.BooleanOptionalAction, default=None,
                        help="mirror the claim-time prime verdict: --primed (received "
                             "prior-trajectory context) / --no-primed (deliberate holdout). "
                             "Omit to leave both prime keys out (pre-#57 shape).")
    p_traj.add_argument("--served-json", default=None,
                        help="file with the served note ids (prime output's `served`) to "
                             "mirror into the trajectory note frontmatter")
    p_traj.add_argument("--trace-json", default=None,
                        help="file with the semantic execution trace (issue #85): a JSON "
                             "object {rounds[], criteria[], simplify, edge_cases[], tdd} "
                             "the orchestrator condenses from the gate agents' own reports. "
                             "Omit to leave the trace key out (pre-#85 shape).")
    p_traj.add_argument("--fix-rounds", type=int, default=0)
    p_traj.add_argument("--outcome", required=True,
                        choices=["shipped", "routed-to-human", "awaiting-approval"])
    p_traj.add_argument("--pr-url", default="")
    p_traj.add_argument("--run-id", default="")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        cfg = apply_overrides(load_config(), args.overrides)
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        return 2

    if args.cmd == "config":
        print(json.dumps(cfg, indent=2))
    elif args.cmd == "plan":
        limit = args.limit if args.limit is not None else cfg["loop"]["max_issues_per_run"]
        issues = fetch_issues()
        if args.dag is not None:
            try:
                issues = scope_to_dag(issues, args.dag)
            except ValueError as e:
                print(json.dumps({"error": str(e)}))
                return 2
        if args.assume_done:
            done = {int(n) for n in args.assume_done.split(",") if n.strip()}
            issues = apply_assume_done(issues, done)
        result = compute_frontier(issues, cfg, limit=limit)
        print(json.dumps(result, indent=2))
    elif args.cmd == "claim":
        if cfg["loop"]["claim_mode"] == "assign":
            # wayfinder convention: the assignee IS the claim — renders
            # natively in the tracker UI, no label vocabulary consumed.
            _gh(["issue", "edit", str(args.number), "--add-assignee", "@me"])
        else:
            label = cfg["labels"]["claimed"]
            subprocess.run(
                ["gh", "label", "create", label, "--description",
                 "Claimed by an /issue-loop run", "--color", "1d76db"],
                capture_output=True,
            )  # idempotent: fails silently if it exists
            _gh(["issue", "edit", str(args.number), "--add-label", label])
        _gh(["issue", "comment", str(args.number), "--body",
             f"🤖 issue-loop: claimed by run `{args.run_id}`."])
        print(f"claimed #{args.number}")
    elif args.cmd == "release":
        if cfg["loop"]["claim_mode"] == "assign":
            _gh(["issue", "edit", str(args.number), "--remove-assignee", "@me"])
        else:
            _gh(["issue", "edit", str(args.number), "--remove-label", cfg["labels"]["claimed"]])
        print(f"released #{args.number}")
    elif args.cmd == "check":
        gate = next((g for g in cfg["gates"] if g["id"] == args.gate), None)
        if gate is None:
            print(json.dumps({"error": f"no gate '{args.gate}' in config"}))
            return 2
        cwd = Path(args.cwd).resolve()
        if gate["kind"] == "command":
            result = run_command_gate(gate, cwd)
        elif gate["kind"] == "diff":
            result = run_diff_gate(gate, cwd, args.base_ref)
        else:
            print(json.dumps({"error": f"gate kind '{gate['kind']}' is LLM-judged — run it from the /issue-loop command, not the script"}))
            return 2
        print(json.dumps(result, indent=2))
        return 0 if result["passed"] else 1
    elif args.cmd == "prime":
        labels = _split_csv(args.labels) if args.labels is not None else _fetch_labels(args.number)
        concepts = _split_csv(args.concepts) if args.concepts is not None else labels
        holdout = cfg["loop"].get("prime_holdout", 5)
        conn = None
        db_path = _resolve_index_db(args.db, args.vault)
        if db_path and Path(db_path).exists():
            try:
                conn = _open_index_ro(db_path)
            except sqlite3.Error:
                conn = None
        try:
            payload = build_prime_payload(
                args.number, args.run_id, concepts, conn=conn, holdout=holdout,
                limit=args.limit, budget_chars=args.budget_chars,
                decisions=_split_csv(args.decisions) if args.decisions else None,
            )
        finally:
            if conn is not None:
                conn.close()
        if args.buffer and payload["primed"] and payload["served"]:
            _append_served_event(args.buffer, args.run_id, args.number,
                                 payload["served"], args.session_id)
        print(json.dumps(payload, indent=2))
    elif args.cmd == "triage":
        signals = json.loads(Path(args.signals_json).read_text(encoding="utf-8"))
        if not isinstance(signals, dict):
            print(json.dumps({"error": "signals-json must be a JSON object"}))
            return 2
        result = classify_pr(signals, cfg["triage"],
                             red_label=cfg["labels"]["on_gate_failure"])
        issue = args.number if args.number is not None else signals.get("issue")
        print(json.dumps({"issue": issue, **result}, indent=2))
    elif args.cmd == "trajectory":
        cwd = Path(args.cwd).resolve()
        issue = json.loads(_gh(["api", f"repos/{{owner}}/{{repo}}/issues/{args.number}"]))
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=cwd,
                                capture_output=True, text=True, check=True).stdout.strip()
        commits = subprocess.run(
            ["git", "log", "--oneline", f"{args.base_ref}..HEAD"],
            cwd=cwd, capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()
        numstat = subprocess.run(
            ["git", "diff", "--numstat", f"{args.base_ref}...HEAD"],
            cwd=cwd, capture_output=True, text=True, check=True,
        ).stdout
        gates = json.loads(Path(args.gates_json).read_text(encoding="utf-8"))
        skills = (json.loads(Path(args.skills_json).read_text(encoding="utf-8"))
                  if args.skills_json else [])
        served = (json.loads(Path(args.served_json).read_text(encoding="utf-8"))
                  if args.served_json else None)
        trace = (json.loads(Path(args.trace_json).read_text(encoding="utf-8"))
                 if args.trace_json else None)
        try:
            payload = build_trajectory(
                issue, branch=branch, commits=commits, numstat=numstat, gates=gates,
                fix_rounds=args.fix_rounds, outcome=args.outcome,
                pr_url=args.pr_url, run_id=args.run_id,
                skills=skills, skill_centric=args.skill_centric,
                primed=args.primed, served=served, trace=trace,
            )
        except ValueError as e:
            print(json.dumps({"error": str(e)}))
            return 2
        print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
