"""The trajectory note's write face: payload assembly for the memory feed.

Internal to this file: the trace normalizers and the skill projection.
"""

from __future__ import annotations


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


def _normalize_trace_simplify(section: dict) -> dict:
    """Project one simplify envelope to ``{outcome, cuts, kept, lines_delta}``.

    Shared by per-slice ``simplify`` (#58) and stack-tip ``stack_simplify`` (#90).
    """
    cuts = section.get("cuts")
    kept = section.get("kept")
    return {
        "outcome": str(section.get("outcome", "") or ""),
        "cuts": [_normalize_trace_whatwhy(c) for c in cuts if isinstance(c, dict)]
                if isinstance(cuts, list) else [],
        "kept": [_normalize_trace_whatwhy(c) for c in kept if isinstance(c, dict)]
                if isinstance(kept, list) else [],
        # A count like any other join key: a malformed value (list/dict)
        # degrades to 0 via _as_int_or_none rather than escaping as a
        # TypeError that would crash the trajectory command (rc-1).
        "lines_delta": _as_int_or_none(section.get("lines_delta")) or 0,
    }


def _normalize_trace(raw: object) -> dict:
    """Shape an incoming semantic-trace object into its stored envelope.

    **A backstop, not the enforcement seam (issue #99).** Judgment-gate
    returns are schema-checked where the subagent returns them — the rail's
    ``validate`` verb (:mod:`devloop.gates`), which rejects a malformed return
    with per-field reasons so the orchestrator re-asks. What arrives here is
    therefore already validated; this projection only backstops the legacy
    (pre-#99) and degraded paths, where dropping a stray key beats crashing
    the trajectory write at the end of a shipped run. It never repairs a field
    a caller should have been re-asked for.

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
    for key in ("simplify", "stack_simplify"):
        section = raw.get(key)
        if isinstance(section, dict):
            out[key] = _normalize_trace_simplify(section)
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
    a caller without a trace produces byte-stable pre-#85 *frontmatter* (the
    body skeleton retires Lessons for all callers by design) — and the RLVR
    export row, which never reads ``trace``, stays locked.
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
            # Issue #85: the run-causal register only — What / How it went. The
            # Lessons section is retired; portable lessons are minted as separate
            # insight notes at ship time and linked via builds_on (see §3).
            "## What\n<1-2 sentences: the slice delivered>\n\n"
            "## How it went\n<fix rounds and why; seams chosen; surprises>"
        ),
        "concept_hints": [l["name"] if isinstance(l, dict) else l
                          for l in issue.get("labels", [])],
    }
