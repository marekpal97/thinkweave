"""The Gate protocol and its deterministic executors.

Every gate kind has exactly one verb, and which verb it has states which
plane runs it (boundary spec §3): a DETERMINISTIC kind is *executed* here
(``execute(gate_cfg, cwd, base_ref) -> GateResult``); a JUDGMENT kind is
never executed by the rail — the /issue-loop orchestrator dispatches a
subagent and the rail validates its return (validators land with #99).
``GateResult`` is a plain dict — ``{id, kind, passed, summary, detail}`` —
shared by both verbs so downstream consumers never care which plane
produced it.
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

    ``forbidden_paths`` entries use the same three-form convention as triage's
    sensitive/watched paths (:func:`devloop.paths.match`): a trailing ``/`` is a
    dir prefix (exactly the old ``startswith``, which is what every shipped
    entry is), a bare name matches that basename at any depth, and a glob is
    fnmatched.
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


# The two registries the protocol's structural claim reduces to. `check`
# dispatches ONLY through DETERMINISTIC; anything else (a JUDGMENT kind, or
# an unrecognized one) gets the LLM-judged error. JUDGMENT ships as data +
# that existing error path only — the validators arrive with their consumer
# in #99, no stubs.
DETERMINISTIC = {"command": run_command_gate, "diff": run_diff_gate}
JUDGMENT = {"acceptance", "review", "simplify"}
