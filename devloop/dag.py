"""Tracker-as-DAG math over GitHub-native issue dependencies.

Pure over issue-snapshot dicts (shape owned by ``devloop.github``). Blocker
edges come from the snapshot's native dependency fields only — the
``Blocked-by:`` body grammar was deleted in #95. ``Wave:`` and
``Parallel-safe:`` have no native counterpart, so they stay body metadata and
keep their parsers here.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Body metadata — the fields with no native GitHub field to migrate to

_WAVE_RE = re.compile(r"Wave:\s*(\d+)", re.IGNORECASE)
_PARALLEL_RE = re.compile(r"Parallel[- ]safe:\s*(yes|no)", re.IGNORECASE)


def parse_wave(body: str) -> int | None:
    m = _WAVE_RE.search(body or "")
    return int(m.group(1)) if m else None


def parse_parallel_safe(body: str) -> bool:
    """Default True: absence of the hint must not serialize the whole loop."""
    m = _PARALLEL_RE.search(body or "")
    return m.group(1).lower() == "yes" if m else True


# ---------------------------------------------------------------------------
# Frontier computation — pure functions over an issue snapshot


def blockers(issue: dict) -> list[int]:
    """The issue's native ``blocked_by`` edges, as fetched into the snapshot.

    Absent key = no edge list (either no blockers, or the fetch failed and
    ``native_blocked_count`` is carrying the gate on its own).
    """
    return sorted(set(issue.get("native_blockers") or []))


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
        for ref in blockers(issue):
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
    claim label still counts), and has no open blocker. Blocking gates on the
    native dependencies attached by fetch_issues (``native_blockers``, or
    ``native_blocked_count`` alone when the edge list wasn't fetched). A
    blocker missing from the snapshot is cross-repo or deleted: GitHub counted
    it as blocking, so it keeps blocking, and the plan says why.
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
            "blockers": blockers(issue),
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
        for ref in entry["blockers"]:
            blocker = by_number.get(ref)
            if blocker is None:
                warnings.append(f"#{issue['number']}: blocker #{ref} not in snapshot "
                                "(cross-repo or deleted); treated as blocking")
            if blocker is None or blocker["state"].upper() == "OPEN":
                open_blockers.append(ref)
        # native_blocked_count is GitHub's own open-blocker count — it gates
        # even when the edge list wasn't fetched (list is enrichment only).
        if open_blockers or (issue.get("native_blocked_count", 0) > 0 and not entry["blockers"]):
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
