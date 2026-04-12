"""Deterministic extraction from session events — no LLM required.

Produces structured metadata from JSONL event buffers:
- Summary from files, commits, tests
- Decision skeletons from multi-file commits
- Concept assignment from file paths using ontology patterns
- Failure tagging from insight content keywords
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DecisionSkeleton:
    """A candidate decision extracted from commit data."""
    title: str
    file_paths: list[str]
    commit_hash: str = ""
    concepts: list[str] = field(default_factory=list)


@dataclass
class FailureSignal:
    """A failure detected from insight content."""
    title: str
    body: str
    source: str = "insight"  # "insight" or "test"


@dataclass
class ExtractResult:
    """Result of deterministic extraction."""
    summary: str
    files_touched: list[str]
    commits: list[dict]
    test_runs: list[dict]
    insights: list[str]
    git_branch: str
    decision_skeletons: list[DecisionSkeleton]
    failure_signals: list[FailureSignal]
    concepts: list[str]  # concepts inferred from file paths


# Patterns mapping file path components to concepts
_PATH_CONCEPT_PATTERNS: list[tuple[str, str]] = [
    (r"test[_s]?", "pytest"),
    (r"\.py$", "python"),
    (r"\.ts$|\.tsx$", "typescript"),
    (r"\.rs$", "rust"),
    (r"\.go$", "go"),
    (r"docker|Dockerfile", "docker"),
    (r"sql|\.db$|sqlite", "sqlite"),
    (r"mcp", "mcp"),
    (r"hook", "claude-code"),
    (r"obsidian|vault", "obsidian"),
    (r"embed", "embeddings"),
    (r"index|fts|search", "fts5"),
    (r"graph|edge", "knowledge-graph"),
    (r"pipeline", "pipeline"),
    (r"fastapi|api", "api"),
    (r"pydantic|model", "pydantic"),
]

# Keywords in insight text that signal failures
_FAILURE_KEYWORDS = re.compile(
    r"\b(fail(?:ed|ure|s)?|broke|broken|revert(?:ed)?|abandoned|didn't work|wrong approach|"
    r"backed out|rolled back|gave up|dead end|mistake)\b",
    re.IGNORECASE,
)


def extract_deterministic(
    events: list[dict],
    ontology: dict[str, list[str]] | None = None,
) -> ExtractResult:
    """Extract structured metadata from session events without LLM.

    Args:
        events: List of event dicts from JSONL buffer.
        ontology: Optional domain→[concepts] dict for enriched concept assignment.

    Returns:
        ExtractResult with summary, decision skeletons, failure signals, etc.
    """
    # Summarize events (same logic as handler._summarize_events)
    files: list[str] = []
    commits: list[dict] = []
    test_runs: list[dict] = []
    insights: list[str] = []
    git_branch = ""

    for ev in events:
        if "file" in ev:
            files.append(ev["file"])
        if "commit" in ev:
            commits.append(ev["commit"])
        if "test_run" in ev:
            test_runs.append(ev["test_run"])
        if "insights" in ev:
            insights.extend(ev["insights"])
        if "git_branch" in ev:
            git_branch = ev["git_branch"]

    # Deduplicate files preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            deduped.append(f)

    # Build summary
    summary = _build_summary(deduped, commits, test_runs, len(events))

    # Generate decision skeletons from commits with 3+ files
    skeletons = _extract_decision_skeletons(commits)

    # Detect failure signals from insights
    failures = _detect_failure_signals(insights, test_runs)

    # Assign concepts from file paths
    all_paths = deduped[:]
    for c in commits:
        if isinstance(c, dict):
            all_paths.extend(c.get("files", []))
    concepts = assign_concepts_from_paths(all_paths, ontology)

    # Also assign concepts to decision skeletons
    for skeleton in skeletons:
        skeleton.concepts = assign_concepts_from_paths(skeleton.file_paths, ontology)

    return ExtractResult(
        summary=summary,
        files_touched=deduped,
        commits=commits,
        test_runs=test_runs,
        insights=insights,
        git_branch=git_branch,
        decision_skeletons=skeletons,
        failure_signals=failures,
        concepts=concepts,
    )


def _build_summary(
    files_touched: list[str],
    commits: list[dict],
    test_runs: list[dict],
    event_count: int,
) -> str:
    """Build a metadata-based auto-summary."""
    parts: list[str] = []
    if files_touched:
        basenames = [Path(f).name for f in files_touched[:5]]
        more = f" (+{len(files_touched) - 5} more)" if len(files_touched) > 5 else ""
        parts.append(f"Edited {len(files_touched)} files: {', '.join(basenames)}{more}")
    if commits:
        msgs = []
        for c in commits[:3]:
            if isinstance(c, dict):
                msgs.append(c.get("message", "")[:60])
            else:
                msgs.append(str(c)[:60])
        parts.append(f"Commits: {'; '.join(msgs)}")
    if test_runs:
        for tr in test_runs[:2]:
            if isinstance(tr, dict):
                p = tr.get("passed", 0)
                f = tr.get("failed", 0)
                parts.append(f"Tests: {p} passed, {f} failed")
    if not parts:
        parts.append(f"{event_count} tool events recorded")
    return ". ".join(parts) + "."


def _extract_decision_skeletons(commits: list[dict]) -> list[DecisionSkeleton]:
    """Generate decision skeletons from commits that touch 3+ files."""
    skeletons = []
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        commit_files = commit.get("files", [])
        if len(commit_files) >= 3:
            message = commit.get("message", "Untitled commit")
            skeletons.append(DecisionSkeleton(
                title=message,
                file_paths=commit_files,
                commit_hash=commit.get("hash", ""),
            ))
    return skeletons


def _detect_failure_signals(
    insights: list[str], test_runs: list[dict]
) -> list[FailureSignal]:
    """Detect failure signals from insight content and test results."""
    failures = []

    for insight in insights:
        if _FAILURE_KEYWORDS.search(insight):
            # Extract first meaningful line as title
            lines = [l.strip() for l in insight.split("\n") if l.strip()]
            title = lines[0][:80] if lines else "Failure detected"
            failures.append(FailureSignal(
                title=title,
                body=insight,
                source="insight",
            ))

    for tr in test_runs:
        if isinstance(tr, dict) and tr.get("failed", 0) > 0:
            cmd = tr.get("command", "pytest")[:80]
            failures.append(FailureSignal(
                title=f"Test failures: {tr['failed']} failed",
                body=f"Command: {cmd}\nPassed: {tr.get('passed', 0)}, Failed: {tr['failed']}",
                source="test",
            ))

    return failures


def assign_concepts_from_paths(
    file_paths: list[str],
    ontology: dict[str, list[str]] | None = None,
) -> list[str]:
    """Infer concepts from file paths using regex patterns and ontology.

    Returns deduplicated list of concepts.
    """
    concepts: set[str] = set()
    combined = " ".join(file_paths).lower()

    for pattern, concept in _PATH_CONCEPT_PATTERNS:
        if re.search(pattern, combined):
            concepts.add(concept)

    # If ontology provided, also match concept names against path components
    if ontology:
        path_parts = set()
        for fp in file_paths:
            for part in Path(fp).parts:
                path_parts.add(part.lower().replace("_", "-").replace(".", "-"))
            path_parts.add(Path(fp).stem.lower().replace("_", "-"))

        for _domain, domain_concepts in ontology.items():
            for concept in domain_concepts:
                if concept in path_parts:
                    concepts.add(concept)

    return sorted(concepts)
