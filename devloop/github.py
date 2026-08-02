"""The one subprocess seam to ``gh``, plus the issue-snapshot dicts it makes.

This module owns the snapshot shape ``{number, title, state, labels,
assignees, body, native_blocked_count, native_blockers?}`` — the
``github``↔``dag`` contract. ``dag`` never sees raw gh output.
Tracker *mutations* (claim/release) stay in ``cli``, composed from ``run``:
the assign-vs-label convention is loop policy, not gh plumbing.
"""

from __future__ import annotations

import json
import subprocess


def run(args: list[str]) -> str:
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
    out = run(["api", "--paginate", "--jq", ".[]",
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
                refs = run(["api", f"repos/{{owner}}/{{repo}}/issues/{issue['number']}/dependencies/blocked_by",
                            "--jq", "[.[].number]"])
                issue["native_blockers"] = json.loads(refs)
            except subprocess.CalledProcessError:
                issue["native_blockers"] = []  # count still gates; list is enrichment
        issues.append(issue)
    return issues


def fetch_labels(number: int) -> list[str]:
    """Issue label names via gh (network). Empty list on any failure — a prime
    with no concepts serves an empty block, never crashes the loop."""
    try:
        out = run(["issue", "view", str(number), "--json", "labels",
                   "--jq", "[.labels[].name]"])
        return json.loads(out or "[]")
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return []
