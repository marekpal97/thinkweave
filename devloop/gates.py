"""The Gate protocol: deterministic executors and judgment validators.

Every gate kind has exactly one verb, and which verb it has states which
plane runs it (boundary spec §3): a DETERMINISTIC kind is *executed* here
(``execute(gate_cfg, cwd, base_ref) -> GateResult``); a JUDGMENT kind is
never executed by the rail — the /issue-loop orchestrator dispatches a
subagent and the rail *validates* its return
(``validate(gate_cfg, raw) -> GateResult``), rejecting a schema-violating
return so the orchestrator re-asks. ``GateResult`` is a plain dict —
``{id, kind, passed, summary, detail}`` — shared by both verbs so downstream
consumers never care which produced it; judgment results add ``reasons``,
the rejection's per-field detail (empty on a real verdict).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from devloop import paths


def run_command_gate(gate: dict, cwd: Path, base_ref: str | None = None) -> dict:
    """``base_ref`` is unused — it is in the signature so both deterministic
    executors share the registry's one calling convention."""
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
    """Pure evaluation of `git diff --numstat` output against constraints.

    ``forbidden_paths`` patterns use :func:`devloop.paths.match`'s three forms;
    every shipped entry is the trailing-``/`` prefix case (the old ``startswith``).
    """
    forbidden = gate.get("forbidden_paths", [])
    max_lines = gate.get("max_changed_lines")
    touched_forbidden, total = [], 0
    for line in numstat.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        total += (0 if added == "-" else int(added)) + (0 if deleted == "-" else int(deleted))
        if any(paths.match(path, p) for p in forbidden):
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
# Judgment kinds — the rail never executes them. The orchestrator dispatches a
# subagent and hands its JSON return back here: `validate(gate, raw)` checks it
# against the kind's schema and returns the same GateResult shape plus
# `reasons`. Non-empty `reasons` means SCHEMA rejection — the orchestrator
# re-asks the same subagent — and is categorically different from a schema-valid
# `passed: False`, which is a gate verdict (a fix round). Nothing is coerced:
# an invented enum or a blank evidence line is rejected naming its field path,
# because coercing it would put the judge's mistake in the trajectory as fact.

ACCEPTANCE_VERDICTS = ("met", "not-met")
REVIEW_SEVERITIES = ("critical", "major", "minor", "nit")
SIMPLIFY_OUTCOMES = ("applied", "reverted", "lean")


def reject(gate: dict, reasons: list[str]) -> dict:
    """A GateResult for a return that never became a verdict."""
    return {
        "id": gate["id"],
        "kind": gate["kind"],
        "passed": False,
        "summary": f"schema-rejected: {'; '.join(reasons)}",
        "detail": "\n".join(reasons),
        "reasons": reasons,
    }


def _verdict(gate: dict, reasons: list[str], *, passed: bool, summary: str) -> dict:
    return (reject(gate, reasons) if reasons else
            {"id": gate["id"], "kind": gate["kind"], "passed": passed,
             "summary": summary, "detail": "", "reasons": []})


def _entries(raw: object, key: str, reasons: list[str],
             *, allow_empty: bool = False) -> list[tuple[int, dict]]:
    """The list-of-objects unwrap every judgment schema starts with."""
    if not isinstance(raw, dict):
        reasons.append(f"payload: expected a JSON object, got {type(raw).__name__}")
        return []
    value = raw.get(key)
    if not isinstance(value, list):
        reasons.append(f"{key}: expected a list, got {type(value).__name__}")
        return []
    if not value and not allow_empty:
        reasons.append(f"{key}: expected at least one entry, got none")
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            reasons.append(f"{key}[{i}]: expected an object, got {type(entry).__name__}")
    return [(i, e) for i, e in enumerate(value) if isinstance(e, dict)]


def _text(entry: dict, where: str, key: str, reasons: list[str]) -> None:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        reasons.append(f"{where}.{key}: expected a non-empty string, got {value!r}")


def _enum(entry: dict, where: str, key: str, allowed: tuple[str, ...],
          reasons: list[str]) -> str:
    value = entry.get(key)
    if value not in allowed:
        reasons.append(f"{where}.{key}: {value!r} is not one of {' | '.join(allowed)}")
        return ""
    return value


def validate_acceptance(gate: dict, raw: object) -> dict:
    """``{criteria: [{id, verdict: met|not-met, evidence}]}``, one entry per
    acceptance criterion. Passes per the gate's ``threshold``: ``majority``
    needs strictly more than half met, anything else is read as ``all`` (gates
    are file-only config, a trusted input — unlike the subagent return here)."""
    reasons: list[str] = []
    verdicts = []
    for i, entry in _entries(raw, "criteria", reasons):
        where = f"criteria[{i}]"
        _text(entry, where, "id", reasons)
        _text(entry, where, "evidence", reasons)
        verdicts.append(_enum(entry, where, "verdict", ACCEPTANCE_VERDICTS, reasons))
    threshold = gate.get("threshold", "all")
    met = sum(v == "met" for v in verdicts)
    passed = (met * 2 > len(verdicts) if threshold == "majority"
              else met == len(verdicts))
    return _verdict(gate, reasons, passed=passed,
                    summary=f"{met}/{len(verdicts)} criteria met (threshold: {threshold})")


def validate_review(gate: dict, raw: object) -> dict:
    """``{findings: [{severity: critical|major|minor|nit, finding}]}``. An empty
    list is a clean review; the gate fails iff a severity is in ``block_on``."""
    reasons: list[str] = []
    severities = []
    for i, entry in _entries(raw, "findings", reasons, allow_empty=True):
        where = f"findings[{i}]"
        _text(entry, where, "finding", reasons)
        severities.append(_enum(entry, where, "severity", REVIEW_SEVERITIES, reasons))
    blocking = [s for s in severities if s in gate.get("block_on", [])]
    return _verdict(gate, reasons, passed=not blocking,
                    summary=(f"{len(blocking)} blocking of {len(severities)} findings"
                             if severities else "no findings"))


def validate_simplify(gate: dict, raw: object) -> dict:
    """``{outcome: applied|reverted|lean, lines_delta, cuts[], kept[]}`` — the
    same envelope the trajectory trace stores, so a validated return carries
    into the note unchanged. Never fails the pipeline (``required = false``):
    a schema-valid return always passes, its "failure" mode being the revert.
    """
    reasons: list[str] = []
    outcome = ""
    if isinstance(raw, dict):
        outcome = _enum(raw, "payload", "outcome", SIMPLIFY_OUTCOMES, reasons)
        delta = raw.get("lines_delta")
        if isinstance(delta, bool) or not isinstance(delta, int):
            reasons.append(f"payload.lines_delta: expected an int, got {delta!r}")
    for key in ("cuts", "kept"):
        for i, entry in _entries(raw, key, reasons, allow_empty=True):
            _text(entry, f"{key}[{i}]", "what", reasons)
            _text(entry, f"{key}[{i}]", "why", reasons)
    return _verdict(gate, reasons, passed=True,
                    summary=f"{outcome}: {raw.get('lines_delta') if isinstance(raw, dict) else 0} lines")


# The two registries the protocol's structural claim reduces to: every kind has
# exactly one verb, and which verb it has states which plane runs it. `check`
# dispatches ONLY through DETERMINISTIC — any other kind, judgment-side or typo,
# gets the LLM-judged error; `validate` dispatches ONLY through JUDGMENT.
DETERMINISTIC = {"command": run_command_gate, "diff": run_diff_gate}
JUDGMENT = {"acceptance": validate_acceptance, "review": validate_review,
            "simplify": validate_simplify}
