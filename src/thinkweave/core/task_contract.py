"""The ``kind: task`` note contract — one durable task shape shared by every
execution route (in-session subagents, devloop runs, headless workers, solo
declared work).

A task note is a structured ledger, never narrative: closed key sets reject
prose fields, and every field is a join key into substrate that already
exists (sessions, decisions, ``context_served``, the feedback register,
commits, envelope return files). ``validate_task_note`` and
``validate_envelope`` return a list of error strings; an empty list means
the shape conforms. A malformed value is always an error naming its field
and position, never a silent pass.

The discriminator is ``kind: task`` on ``type: note`` — there is no task
NoteType. Task ids are vault-minted (``tsk-`` + 8 hex); harness session ids
never anchor identity and travel only as a qualified ``{harness, kind,
value}`` triple under ``session_ref``. Devloop's execution trace (review
``rounds``, ``criteria``, ``simplify``, ``skills``) nests inside one
work-grain entry of the note's ``rounds[]`` ledger, never at top level.
"""

from __future__ import annotations

import re

TASK_KIND = "task"
TASK_STATUSES = frozenset({"open", "closed"})
# Note grain: one note per homogeneous fan-out (batch, N envelope rows),
# one note per dispatch, or one accreting work-grain note whose rounds[]
# spans sessions.
TASK_GRAINS = frozenset({"batch", "per-dispatch", "work"})
TASK_ID_RE = re.compile(r"^tsk-[0-9a-f]{8}$")

# Capture-richness tiers — the same vocabulary HarnessProfile.task_correlation
# declares. The wrap pass gates evidence-dependent duties on it: an absent
# close is orphan evidence only at "boundary" richness.
SPARSITY_TIERS = frozenset({"boundary", "task-id-only"})

SESSION_REF_KEYS = frozenset({"harness", "kind", "value"})

# Devloop's trace vocabulary; valid only inside a work-grain round entry.
DEVLOOP_TRACE_KEYS = frozenset({"rounds", "criteria", "simplify", "skills"})

# Reserved on feedback events for task attribution; optional, no consumer
# reads it yet.
FEEDBACK_TASK_REF_FIELD = "task_ref"


def envelope_return_name(task_id: str) -> str:
    """The return file a dispatch appends its envelope rows to."""
    return f"{task_id}.jsonl"


def normalize_devloop_run(payload: object, *, task_id: str) -> dict:
    """Compile one devloop run's emitted trajectory payload into a
    work-grain task note: each stage-dispatch record becomes one envelope
    row, and the semantic trace nests inside the single round entry the run
    compiles to. Keys the emitter dropped are simply absent; the result is
    validated against the contract and a value that cannot land raises
    ``ValueError`` naming the field. The note stays ``status: open`` —
    closure for loop work is the PR merge, never this compile."""
    if not isinstance(payload, dict) or not isinstance(
        payload.get("frontmatter"), dict
    ):
        raise ValueError(
            "devloop payload: expected the emitted trajectory payload "
            "with a frontmatter mapping"
        )
    src = payload["frontmatter"]
    stages = src.get("skills") or []
    entry: dict = {
        "envelopes": [_stage_envelope(s, task_id) for s in stages],
        "did": {
            "paths": list(src.get("files_touched") or []),
            "attempts": int(src.get("fix_rounds") or 0),
        },
    }
    if "served" in src:
        entry["served"] = list(src["served"])
    trace = src.get("trace") or {}
    for key in ("rounds", "criteria", "simplify"):
        if key in trace:
            entry[key] = _drop_nones(trace[key])
    if stages:
        entry["skills"] = [
            {
                "id": s.get("id", ""),
                "role": s.get("role", ""),
                "outcome": s.get("outcome", ""),
                "fix_rounds_attributed": int(s.get("fix_rounds_attributed") or 0),
            }
            for s in stages
        ]
    fm = {
        "type": "note",
        "kind": TASK_KIND,
        "id": task_id,
        "title": str(payload.get("title", "")),
        "status": "open",
        "grain": "work",
        "asked": f"#{src.get('issue', '')}",
        "rounds": [entry],
    }
    errors = validate_task_note(fm)
    if errors:
        raise ValueError("devloop payload does not land in the contract: " + "; ".join(errors))
    return fm


def validate_task_note(fm: object) -> list[str]:
    """Validate one task note's frontmatter mapping; [] means it conforms."""
    if not isinstance(fm, dict):
        return ["task note: frontmatter is not a mapping"]
    if fm.get("type") != "note" or fm.get("kind") != TASK_KIND:
        return [
            "task note: requires type: note with kind: task "
            f"(got type: {fm.get('type')!r}, kind: {fm.get('kind')!r})"
        ]
    errors = [
        f"task note: missing required field {key!r}"
        for key in _TOP_REQUIRED
        if key not in fm
    ]
    grain = fm.get("grain")
    for key, value in fm.items():
        if key in ("type", "kind"):
            continue
        if key == "rounds":
            errors += _rounds_errors(value, grain)
        elif key in DEVLOOP_TRACE_KEYS:
            errors.append(
                f"task note: {key!r} is a devloop trace field; it nests "
                "inside a work-grain round entry, not at top level"
            )
        elif key in _TOP_CHECKERS:
            errors += _TOP_CHECKERS[key](value, f"task note.{key}")
        else:
            errors.append(f"task note: unknown field {key!r}")
    return errors


def validate_wrap_declaration(decl: object) -> list[str]:
    """Validate one wrap declaration's mapping; [] means it conforms.

    The declaration is the model's judgment about the session's work,
    written as data so the deterministic tail can apply it without prose
    parsing. ``declared`` holds one entry per task the model judged:
    ``continuing: tsk-…`` appends to that open task, otherwise ``title``
    mints a new one; ``done: true`` closes; ``children`` names the seam
    children this task dispatched; ``round`` is the segment's ledger
    entry. ``sparsity`` states how much the declarer could see —
    ``boundary`` for a model that was present, ``task-id-only`` for a
    catch-up declarer, which suppresses orphan judgment downstream.
    """
    if not isinstance(decl, dict):
        return ["declaration: not a mapping"]
    errors = []
    for key in decl:
        if key not in ("sparsity", "declared"):
            errors.append(f"declaration: unknown field {key!r}")
    if decl.get("sparsity", "boundary") not in SPARSITY_TIERS:
        errors.append(
            f"declaration.sparsity: expected one of {sorted(SPARSITY_TIERS)}"
        )
    declared = decl.get("declared")
    if not isinstance(declared, list):
        return errors + ["declaration.declared: expected a list of entries"]
    for i, entry in enumerate(declared):
        where = f"declaration.declared[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: expected a mapping")
            continue
        for key, value in entry.items():
            if key not in _DECLARED_CHECKERS:
                errors.append(f"{where}: unknown field {key!r}")
            elif key != "round":
                errors += _DECLARED_CHECKERS[key](value, f"{where}.{key}")
        if not entry.get("continuing") and not str(entry.get("title", "")):
            errors.append(f"{where}: a mint needs a title (no continuing id)")
        if "round" in entry:
            errors += _rounds_errors(
                [entry["round"]],
                entry.get("grain", "work"),
                where=f"{where}.round",
            )
    return errors


def validate_envelope(row: object, where: str = "envelope") -> list[str]:
    """Validate one unified envelope row; [] means it conforms."""
    if not isinstance(row, dict):
        return [f"{where}: not a mapping"]
    errors = [
        f"{where}: missing required field {key!r}"
        for key in ("task_id", "outcome")
        if not row.get(key)
    ]
    for key, value in row.items():
        if key not in _ENVELOPE_CHECKERS:
            errors.append(f"{where}: unknown field {key!r}")
            continue
        errors += _ENVELOPE_CHECKERS[key](value, f"{where}.{key}")
    return errors


# ---------------------------------------------------------------------------
# Devloop projection plumbing


def _stage_envelope(stage: dict, task_id: str) -> dict:
    """One stage-dispatch record as one execution-grain envelope row; the
    dispatch join keys it carries ride along, a bare session id is
    qualified into the session_ref triple."""
    row: dict = {"task_id": task_id, "outcome": stage.get("outcome", "")}
    for key in ("role", "harness", "model"):
        if stage.get(key):
            row[key] = stage[key]
    if stage.get("session_ref"):
        row["session_ref"] = {
            "harness": stage.get("harness", ""),
            "kind": "session_id",
            "value": stage["session_ref"],
        }
    cost = {
        key: stage[emitted]
        for key, emitted in (("tokens", "tokens"), ("duration", "duration_sec"))
        if emitted in stage
    }
    if cost:
        row["cost"] = cost
    return row


def _drop_nones(value):
    """Strip null-valued keys the emitter writes for absent nullable counts."""
    if isinstance(value, dict):
        return {k: _drop_nones(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_nones(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Field checkers (value, where) -> errors


def _str(value, where):
    return [] if isinstance(value, str) else [f"{where}: expected a string"]


def _str_list(value, where):
    if not isinstance(value, list):
        return [f"{where}: expected a list of strings"]
    return [
        f"{where}[{i}]: expected a string"
        for i, v in enumerate(value)
        if not isinstance(v, str)
    ]


def _int(value, where):
    if isinstance(value, bool) or not isinstance(value, int):
        return [f"{where}: expected an integer"]
    return []


def _bool(value, where):
    return [] if isinstance(value, bool) else [f"{where}: expected a boolean"]


def _number(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return [f"{where}: expected a number"]
    return []


def _task_id(value, where):
    if not isinstance(value, str) or not TASK_ID_RE.match(value):
        return [f"{where}: expected a vault-minted task id (tsk- + 8 hex)"]
    return []


def _task_id_list(value, where):
    if not isinstance(value, list):
        return [f"{where}: expected a list of task ids"]
    return [e for i, v in enumerate(value) for e in _task_id(v, f"{where}[{i}]")]


def _enum(allowed):
    def check(value, where):
        if value not in allowed:
            return [f"{where}: expected one of {sorted(allowed)}"]
        return []

    return check


def _session_ref(value, where):
    if isinstance(value, str):
        return [
            f"{where}: bare-string session ref; harness ids ride as a "
            "{harness, kind, value} triple"
        ]
    if not isinstance(value, dict):
        return [f"{where}: expected a {{harness, kind, value}} triple"]
    errors = [
        f"{where}: triple missing {key!r}"
        for key in sorted(SESSION_REF_KEYS - value.keys())
    ]
    errors += [
        f"{where}: unknown triple field {key!r}"
        for key in sorted(value.keys() - SESSION_REF_KEYS)
    ]
    errors += [
        f"{where}.{key}: expected a string"
        for key in SESSION_REF_KEYS & value.keys()
        if not isinstance(value[key], str)
    ]
    return errors


def _closed_dict(spec):
    """A mapping whose keys are a subset of ``spec`` (key -> checker)."""

    def check(value, where):
        if not isinstance(value, dict):
            return [f"{where}: expected a mapping"]
        errors = []
        for key, v in value.items():
            if key not in spec:
                errors.append(f"{where}: unknown field {key!r}")
            else:
                errors += spec[key](v, f"{where}.{key}")
        return errors

    return check


def _dict_list(spec):
    """A list of mappings, each checked by :func:`_closed_dict`."""
    entry = _closed_dict(spec)

    def check(value, where):
        if not isinstance(value, list):
            return [f"{where}: expected a list"]
        errors = []
        for i, v in enumerate(value):
            errors += entry(v, f"{where}[{i}]")
        return errors

    return check


def _envelopes(value, where):
    if not isinstance(value, list):
        return [f"{where}: expected a list"]
    errors = []
    for i, row in enumerate(value):
        errors += validate_envelope(row, f"{where}[{i}]")
    return errors


# ---------------------------------------------------------------------------
# Shape tables

_COST = _closed_dict({"tokens": _number, "duration": _number})

_ENVELOPE_CHECKERS = {
    "task_id": _task_id,
    "outcome": _str,
    "session_ref": _session_ref,
    "harness": _str,
    "model": _str,
    "role": _str,
    "ts": _str,
    "outputs": _str_list,
    "error": _str,
    "cost": _COST,
}

# Devloop's trace shapes, as its trajectory normalizers emit them.
_TRACE_CHECKERS = {
    "rounds": _dict_list({
        "gate": _str,
        "finding": _str,
        "severity": _str,
        "disposition": _str,
        "fixed_by": _str,
    }),
    "criteria": _dict_list({
        "id": _str,
        "verdict": _str,
        "flipped_by_round": _int,
    }),
    "simplify": _closed_dict({
        "outcome": _str,
        "cuts": _dict_list({"what": _str, "why": _str}),
        "kept": _dict_list({"what": _str, "why": _str}),
        "lines_delta": _int,
    }),
    "skills": _dict_list({
        "id": _str,
        "role": _str,
        "outcome": _str,
        "fix_rounds_attributed": _int,
    }),
}

# One round entry: what a single /wrap (or devloop run) compiles into the
# ledger — session ref, envelope rows, context served, outputs delta,
# decision lifecycle, feedback refs.
_ROUND_CHECKERS = {
    "session_ref": _session_ref,
    "envelopes": _envelopes,
    "served": _str_list,
    "did": _closed_dict(
        {"paths": _str_list, "commits": _str_list, "attempts": _int}
    ),
    "decisions": _closed_dict(
        {"minted": _str_list, "re_served": _str_list, "reverted": _str_list}
    ),
    "feedback": _dict_list(
        {"register": _str, "prompt_ref": _str, "ts": _str}
    ),
    "cost": _COST,
}

_TOP_REQUIRED = ("type", "kind", "id", "status", "grain", "rounds")

_TOP_CHECKERS = {
    "id": _task_id,
    "status": _enum(TASK_STATUSES),
    "grain": _enum(TASK_GRAINS),
    "title": _str,
    "date": _str,
    "project": _str,
    "parent": _str,
    "harness": _str,
    "model": _str,
    "role": _str,
    "asked": _str,
    "aliases": _str_list,
    "concepts": _str_list,
    "proposed_concepts": _str_list,
    "tags": _str_list,
    "consumes": _str_list,
    # Evidence-backed flag, never a status: an open whose close the register
    # does not hold, stamped by the wrap pass at boundary sparsity.
    "orphan": _bool,
    "outcome": _dict_list(
        {"label": _str, "judged_at": _str, "evidence": _str}
    ),
}

# One wrap-declaration entry (``validate_wrap_declaration``); ``round`` is
# dispatched to the round checkers under the entry's grain.
_DECLARED_CHECKERS = {
    "continuing": _task_id,
    "title": _str,
    "asked": _str,
    "grain": _enum(TASK_GRAINS),
    "done": _bool,
    "consumes": _str_list,
    "children": _task_id_list,
    "round": None,
}


def _rounds_errors(value, grain, where="task note.rounds") -> list[str]:
    if not isinstance(value, list):
        return [f"{where}: expected a list of round entries"]
    errors = []
    for i, entry in enumerate(value):
        at = f"{where}[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{at}: expected a mapping")
            continue
        for key, v in entry.items():
            if key in _ROUND_CHECKERS:
                errors += _ROUND_CHECKERS[key](v, f"{at}.{key}")
            elif key in _TRACE_CHECKERS:
                if grain == "work":
                    errors += _TRACE_CHECKERS[key](v, f"{at}.{key}")
                else:
                    errors.append(
                        f"{at}: devloop trace field {key!r} is valid "
                        "only on a work-grain round entry"
                    )
            else:
                errors.append(f"{at}: unknown field {key!r}")
    return errors
