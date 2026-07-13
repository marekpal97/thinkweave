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
  config   — print resolved loop config (defaults merged with loop.toml)
  check    — run one deterministic gate (kind: command | diff) and emit JSON

Stdlib only. Config: docs/agents/loop.toml.
"""

from __future__ import annotations

import argparse
import json
import re
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
    },
    "tdd": {
        "mode": "auto",  # auto: enforced iff the baseline probe is green
    },
    "labels": {
        "runnable": "ready-for-agent",
        "claimed": "agent-claimed",
        "on_gate_failure": "ready-for-human",
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
        "gates": [],
    }
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        cfg["loop"].update(data.get("loop", {}))
        cfg["labels"].update(data.get("labels", {}))
        cfg["tdd"].update(data.get("tdd", {}))
        cfg["gates"] = data.get("gates", [])
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


# ---------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="compute the runnable frontier")
    p_plan.add_argument("--limit", type=int, default=None)

    p_claim = sub.add_parser("claim", help="claim an issue for a run")
    p_claim.add_argument("number", type=int)
    p_claim.add_argument("--run-id", required=True)

    p_release = sub.add_parser("release", help="release a claimed issue")
    p_release.add_argument("number", type=int)

    sub.add_parser("config", help="print resolved config as JSON")

    p_check = sub.add_parser("check", help="run one deterministic gate")
    p_check.add_argument("--gate", required=True)
    p_check.add_argument("--cwd", default=".")
    p_check.add_argument("--base-ref", default="origin/main")

    args = parser.parse_args(argv)
    cfg = load_config()

    if args.cmd == "config":
        print(json.dumps(cfg, indent=2))
    elif args.cmd == "plan":
        limit = args.limit if args.limit is not None else cfg["loop"]["max_issues_per_run"]
        result = compute_frontier(fetch_issues(), cfg, limit=limit)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
