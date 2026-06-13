"""Deterministic extraction from session events — no LLM required.

Produces structured metadata from JSONL event buffers:
- Summary from files, commits, tests
- Decision skeletons from multi-file commits
- Concept assignment from file paths using ontology patterns
- Failure tagging from insight content keywords
- Prompt primitive (E2): typed user-prompt events lifted from the buffer,
  with a conservative probe classifier
- Auto-todo extraction (E5): lift "TODO: X" / "we should X" / "next step: X"
  patterns out of session text into ``Todo`` entries
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
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


# ---------------------------------------------------------------------------
# Prompt primitive (Phase 4 E)
# ---------------------------------------------------------------------------


@dataclass
class Prompt:
    """A captured user prompt event lifted from the JSONL buffer.

    The shape mirrors what ``surfaces/hooks/handler._handle_user_prompt_submit``
    writes (one JSONL line per submission). ``ts`` is the wall-clock UTC
    moment of capture; ``session_id`` is the Claude Code session UUID
    (``hook_input["session_id"]``), not a vault note id.
    """

    ts: datetime
    text: str
    session_id: str
    project: str | None = None
    cwd: str | None = None
    classification: str | None = None


def _parse_ts(raw: str) -> datetime:
    """Tolerant ISO timestamp parser for prompt events.

    Handler emits ``datetime.now(timezone.utc).isoformat()`` which yields
    a ``+00:00`` suffix Python parses cleanly. Older buffers used a ``Z``
    suffix; we strip it. On any failure we fall back to ``datetime.min``
    so callers can keep going on corrupt rows.
    """
    if not raw:
        return datetime.min
    cleaned = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return datetime.min


def extract_prompts(events_jsonl: Path) -> list[Prompt]:
    """Read an events JSONL file and return its ``Prompt`` entries.

    Filters by ``type == "prompt"``. Skips malformed lines (we never
    abort an extraction over a single bad row — the buffer is append-only
    and can be killed mid-write). Returns an empty list when the file
    doesn't exist.

    Each returned ``Prompt`` carries a populated ``classification``
    field: ``"probe"`` when :func:`classify_probe` flags it, ``None``
    otherwise. The classifier needs the surrounding event stream
    (to peek at follow-up tool calls), so we read every event row here
    instead of filtering early.
    """
    if not events_jsonl.exists():
        return []

    events: list[dict] = []
    out: list[Prompt] = []
    for line in events_jsonl.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        events.append(row)
        if row.get("type") != "prompt":
            continue
        text = row.get("text", "")
        session_id = row.get("session_id", "")
        if not text or not session_id:
            continue
        out.append(
            Prompt(
                ts=_parse_ts(row.get("ts", "")),
                text=str(text),
                session_id=str(session_id),
                project=row.get("project"),
                cwd=row.get("cwd"),
            )
        )

    for prompt in out:
        if classify_probe(prompt, events):
            prompt.classification = "probe"

    return out


_PROBE_FOLLOW_LOOKAHEAD = 3
_UNFAMILIAR_HINTS = ("look at", "look up", "what is", "what's", "explain", "where", "how does")

# Minimum concept-slug length for probe→concept attribution. Defends
# against the single/2-char garbage pool (2026-06-07 str-iter bug class:
# entries like ``-``, ``[``, individual letters survived as
# ``proposed_concepts``) — those slugs substring-match every probe and
# drown real signal. Two-char concepts (``ai``, ``hf``) are real but the
# false-positive cost dominates; canonicalise them with longer aliases.
_PROBE_CONCEPT_MIN_LEN = 3


def match_probe_concepts(text: str, vocabulary: Iterable[str]) -> set[str]:
    """Concept attribution for a prompt text: case-insensitive substring
    match of concept slugs against the text.

    THE shared rule between the live probe-pressure path
    (``operations.prompts.recent_probe_details``) and the SQL projection
    (``Indexer._project_session_prompts`` → ``prompt_concepts``) — keep
    both on this function so the table never disagrees with what the
    pressure aggregate would compute. Substring (not word-split) matching
    is deliberate: concepts like ``write-ahead-log`` contain hyphens and
    naive whitespace tokenisation would break them apart.
    """
    text_lower = (text or "").strip().lower()
    if not text_lower:
        return set()
    return {
        concept
        for concept in vocabulary
        if concept
        and len(concept) >= _PROBE_CONCEPT_MIN_LEN
        and concept in text_lower
    }


def classify_probe(prompt: Prompt, events: list[dict]) -> bool:
    """Conservative heuristic: is this prompt a *probe* (an exploratory
    user question rather than an instruction)?

    Returns True only when:

    1. Text ends with ``?`` (after trimming), OR contains a probe-style
       lead phrase ("what is", "how does", "explain", …) AND
    2. No ``Edit`` / ``Write`` event appears within the next
       :data:`_PROBE_FOLLOW_LOOKAHEAD` events of the buffer (i.e. the
       prompt didn't immediately translate into a code change), OR a
       ``Read`` of an unfamiliar file follows.

    False negatives are preferred over false positives — the state-of-
    play landing doc's "Open Probes" section is more useful when sparse
    and accurate. This is a deliberately small heuristic; tuning lives
    downstream.

    TODO(post-E5): empirically tune the lookahead window + lead-phrase
    list against real captured prompts once the hook has been live for
    a few sessions. Current values are an educated first cut.
    """
    text = (prompt.text or "").strip()
    if not text:
        return False

    looks_like_question = text.rstrip(" .!").endswith("?") or any(
        hint in text.lower() for hint in _UNFAMILIAR_HINTS
    )
    if not looks_like_question:
        return False

    # Locate the prompt in the event stream. Match by ts + text — the hook
    # writes a unique (ts, type, text) tuple per submission. Fall back to
    # the first prompt event with the same text if ts comparison fails.
    target_iso = prompt.ts.isoformat() if prompt.ts != datetime.min else ""
    idx: int | None = None
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        if ev.get("type") != "prompt":
            continue
        if ev.get("text") != prompt.text:
            continue
        if target_iso and ev.get("ts") != target_iso:
            continue
        idx = i
        break

    if idx is None:
        # Couldn't locate — fall back to text-shape signal alone. This is
        # conservative: a question that ends with `?` and never made it
        # into the buffer log is treated as a probe.
        return True

    # Look ahead in the buffer for code-modifying tools
    follow_window = events[idx + 1 : idx + 1 + _PROBE_FOLLOW_LOOKAHEAD]
    for ev in follow_window:
        if not isinstance(ev, dict):
            continue
        if ev.get("tool") in ("Edit", "Write"):
            return False

    return True


# ---------------------------------------------------------------------------
# Auto-todo extraction (Phase 4 E5)
# ---------------------------------------------------------------------------


@dataclass
class Todo:
    """A candidate TODO lifted from session text."""

    text: str
    source_session_id: str = ""
    source_event_idx: int = -1


# Match an explicit TODO/FIXME/next-step/follow-up marker and capture the
# action text after it. High-precision by design:
#   - The marker needs a real ``:`` separator followed by whitespace, so a
#     compound word like "todo-tag" no longer fires (the bare hyphen in
#     ``[:\-]`` used to let "todo-tag queue" through as a false positive).
#   - ``\b`` after the keyword prevents "todos"/"fixmexyz" mid-word matches.
# The old soft ``we should / i should`` pattern was removed: it fired on any
# rhetorical "we should …" in narrative prose, which is almost all noise.
_TODO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bTODO\b\s*:\s+(.+)", re.IGNORECASE),
    re.compile(r"\bFIXME\b\s*:\s+(.+)", re.IGNORECASE),
    re.compile(r"\bnext\s+step\b\s*:\s+(.+)", re.IGNORECASE),
    re.compile(r"\bfollow[\- ]?up\b\s*:\s+(.+)", re.IGNORECASE),
)


def extract_todos(
    session_text: str,
    source_session_id: str = "",
) -> list[Todo]:
    """Heuristic TODO extraction over raw session narrative.

    Scans line-by-line for explicit ``TODO: …`` / ``FIXME: …`` /
    ``next step: …`` / ``follow-up: …`` markers (each needs a real ``:``
    separator + whitespace, so compound words like "todo-tag" don't fire).
    Returns one ``Todo`` per matching line; the captured text is trimmed
    and clamped to 200 chars.

    Intended for the raw session body only — NOT for curated insight bodies
    or decision rationales (callers must not pass those; the deliberate-todo
    channel for curated notes is the explicit ``todo`` tag). Conservative by
    design — auto-todos go to the user's backlog with an ``[auto]`` marker,
    so a noisy false positive only costs a deletion, not lost knowledge.
    Still: prefer high precision so the marker stays trustworthy.
    """
    if not session_text:
        return []

    seen: set[str] = set()
    out: list[Todo] = []
    for idx, line in enumerate(session_text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        for pat in _TODO_PATTERNS:
            m = pat.search(stripped)
            if not m:
                continue
            todo_text = m.group(1).strip(" .,;-—:")
            if not todo_text or len(todo_text) < 3:
                break
            todo_text = todo_text[:200]
            key = todo_text.lower()
            if key in seen:
                break
            seen.add(key)
            out.append(
                Todo(
                    text=todo_text,
                    source_session_id=source_session_id,
                    source_event_idx=idx,
                )
            )
            break
    return out


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
