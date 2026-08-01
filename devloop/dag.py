"""Tracker-as-DAG math + the issue-body grammar that serializes its edges.

Pure over issue-snapshot dicts (shape owned by ``devloop.github``).
"""

from __future__ import annotations

import re

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
