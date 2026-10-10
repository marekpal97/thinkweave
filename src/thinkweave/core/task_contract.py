"""The ``kind: task`` note contract — one durable task shape shared by every
execution route (child dispatches, wrapped sessions, devloop runs).

A task note is a structured ledger, never narrative: closed key sets reject
prose fields, and every field is a join key into substrate that already
exists (sessions, decisions, ``context_served``, commits, envelope return
files). :class:`SessionRef` and :class:`Round` are the declared records the
routes build; each validates once, at ``from_dict``. ``validate_task_note``,
``validate_envelope`` and ``validate_wrap_declaration`` return a list of
error strings; an empty list means the shape conforms. A malformed value is
always an error naming its field and position, never a silent pass.

The discriminator is ``kind: task`` on ``type: note`` — there is no task
NoteType. Task ids are vault-minted (``tsk-`` + 8 hex); harness ids never
anchor identity and travel only as a :class:`SessionRef`. Devloop's
execution trace (``reviews``, ``criteria``, ``skills``) nests inside one
work-grain round, never at top level.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields

TASK_KIND = "task"
TASK_STATUSES = frozenset({"open", "closed"})
# Note grain: one note per homogeneous fan-out (batch, N envelope rows),
# one note per dispatch, or one accreting work-grain note whose rounds[]
# spans sessions.
TASK_GRAINS = frozenset({"batch", "per-dispatch", "work"})
TASK_ID_RE = re.compile(r"\btsk-[0-9a-f]{8}\b")

# Capture-richness tiers — the same vocabulary HarnessProfile.task_correlation
# declares. The wrap pass gates evidence-dependent duties on it: an absent
# close is orphan evidence only at "boundary" richness.
SPARSITY_TIERS = frozenset({"boundary", "task-id-only"})

SESSION_REF_KEYS = frozenset({"harness", "kind", "value"})

# How a round's work ran, and what it produced. A work-grain round assigns
# each output a role; a per-dispatch child records bare {kind, ref} and the
# parent's round decides whether that ref was the deliverable.
ROUTES = frozenset({"session", "devloop"})
OUTPUT_KINDS = frozenset({"file", "url", "artifact", "pr", "commit", "note"})
OUTPUT_ROLES = frozenset({"deliverable", "intermediate"})

# The grains each route's round lands on; a closed task takes no round. A
# dispatch round is a child's close and carries no ``route`` key on disk.
ROUND_GRAINS = {
    "dispatch": frozenset({"per-dispatch", "batch"}),
    "session": frozenset({"work"}),
    "devloop": frozenset({"work"}),
}

# Devloop's trace vocabulary; valid only inside a work-grain round entry.
DEVLOOP_TRACE_KEYS = frozenset({"reviews", "criteria", "skills"})


@dataclass(frozen=True)
class SessionRef:
    """A harness identity, qualified: a ``{harness, kind, value}`` triple."""

    harness: str
    kind: str
    value: str

    @classmethod
    def agent(cls, harness: str, agent_id: str) -> SessionRef:
        return cls(harness, "agent_id", agent_id)

    @classmethod
    def session(cls, harness: str, session_key: str) -> SessionRef:
        return cls(harness, "session_id", session_key)

    @classmethod
    def note(cls, harness: str, note_id: str) -> SessionRef:
        return cls(harness, "note", note_id)

    @classmethod
    def from_dict(cls, value: object, where: str = "session_ref") -> SessionRef:
        """The ref a stored triple names; raises ``ValueError`` when it is
        not a well-formed triple."""
        errors = _session_ref(value, where)
        if errors:
            raise ValueError("; ".join(errors))
        assert isinstance(value, dict)
        return cls(**value)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Round:
    """One ``rounds[]`` entry: one stint of work (a child dispatch, a wrapped
    session, a devloop run) as references into the surfaces that own its
    content. A field left ``None`` stays off disk."""

    route: str | None = None
    session_ref: SessionRef | None = None
    # The tracker ref this stint worked, when the task's ask is wider (an epic).
    asked: str | None = None
    notes: list | None = None
    decisions: dict | None = None
    feedback: list | None = None
    outputs: list | None = None
    did: dict | None = None
    children: list | None = None
    envelopes: list | None = None
    served: list | None = None
    tools: dict | None = None
    tool_errors: int | None = None
    cost: dict | None = None
    digest: dict | None = None
    # Devloop's trace (DEVLOOP_TRACE_KEYS), valid on a work-grain round only.
    reviews: list | None = None
    criteria: list | None = None
    skills: list | None = None

    @classmethod
    def from_dict(cls, data: object, *, grain: str, where: str = "round") -> Round:
        """The round a stored mapping holds, checked under the note's grain;
        raises ``ValueError`` naming every field that does not conform."""
        errors = _round_errors(data, grain, where)
        if errors:
            raise ValueError("; ".join(errors))
        assert isinstance(data, dict)
        ref = data.get("session_ref")
        return cls(**{**data, "session_ref": SessionRef(**ref) if ref else None})

    @classmethod
    def from_devloop(
        cls, payload: object, *, task_id: str, trajectory: str = ""
    ) -> Round:
        """One devloop run's emitted trajectory payload as a ``route:
        devloop`` round naming its issue: each stage-dispatch record is one
        envelope row, the semantic trace nests inside, the trajectory note
        (when its id is known) is the session ref, and the PR is the
        deliverable. Keys the emitter dropped stay absent; a value that
        cannot land raises ``ValueError`` naming the field."""
        src = _devloop_frontmatter(payload)
        stages = src.get("skills") or []
        data: dict = {
            "route": "devloop",
            "asked": normalize_tracker_ref(_issue_ref(src)),
            "envelopes": [_stage_envelope(s, task_id) for s in stages],
            "did": {
                "paths": list(src.get("files_touched") or []),
                "attempts": int(src.get("fix_rounds") or 0),
            },
        }
        if trajectory:
            data["session_ref"] = SessionRef.note("devloop", trajectory).to_dict()
        if src.get("pr_url"):
            data["outputs"] = [
                {"kind": "pr", "ref": str(src["pr_url"]), "role": "deliverable"}
            ]
        if "served" in src:
            data["served"] = list(src["served"])
        trace = src.get("trace") or {}
        for key in ("reviews", "criteria"):
            if key in trace:
                data[key] = _drop_nones(trace[key])
        if stages:
            data["skills"] = [
                {
                    "id": s.get("id", ""),
                    "role": s.get("role", ""),
                    **({"posture": s["posture"]} if s.get("posture") else {}),
                    "outcome": s.get("outcome", ""),
                    "fix_rounds_attributed": int(s.get("fix_rounds_attributed") or 0),
                }
                for s in stages
            ]
        return cls.from_dict(data, grain="work", where="devloop round")

    def to_dict(self) -> dict:
        out = {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if getattr(self, f.name) is not None
        }
        if self.session_ref:
            out["session_ref"] = self.session_ref.to_dict()
        return out


def envelope_return_name(task_id: str) -> str:
    """The return file a dispatch appends its envelope rows to."""
    return f"{task_id}.jsonl"


def normalize_tracker_ref(value: str, repo: str = "") -> str:
    """A tracker reference in its identity form: ``github:<owner>/<repo>#<n>``
    or ``jira:<KEY>-<n>``. A bare ``#<n>`` resolves against ``repo``
    (``owner/name``) and stays bare without one; an ``owner/name#<n>`` or a
    GitHub issue or PR URL folds to its ref; anything else (free-text asks)
    passes through."""
    value = value.strip()
    if bare := re.fullmatch(r"#(\d+)", value):
        return f"github:{repo}#{bare[1]}" if repo else value
    if short := re.fullmatch(r"([\w.-]+/[\w.-]+)#(\d+)", value):
        return f"github:{short[1]}#{short[2]}"
    if url := _GITHUB_ITEM_URL.fullmatch(value):
        return f"github:{url[1]}#{url[2]}"
    return value


def devloop_ask(payload: object) -> str:
    """The tracker ref whose task a devloop run lands on, as emitted: the
    epic URL, else the issue URL, else ``#<issue>``. Raises ``ValueError``
    for a payload that is not an emitted trajectory payload."""
    src = _devloop_frontmatter(payload)
    return str(src.get("epic_url") or _issue_ref(src))


def round_refusal(fm: dict, route: str) -> str:
    """Why this task note cannot take a ``route`` round, or ``""`` when it can."""
    if fm.get("kind") != TASK_KIND:
        return "is not a task note"
    if fm.get("status") != "open":
        return "is closed — a closed task takes no new round"
    grains = ROUND_GRAINS[route]
    if fm.get("grain") not in grains:
        return (
            f"is {fm.get('grain')} grain — a {route} round lands on "
            f"{'/'.join(sorted(grains))} grain only"
        )
    return ""


def accepts_round(fm: dict, route: str) -> bool:
    """Whether this task note can take a ``route`` round."""
    return not round_refusal(fm, route)


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
            errors += _round_errors(
                entry["round"], entry.get("grain", "work"), f"{where}.round"
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
# Devloop payload plumbing


def _devloop_frontmatter(payload: object) -> dict:
    if not isinstance(payload, dict) or not isinstance(
        payload.get("frontmatter"), dict
    ):
        raise ValueError(
            "devloop payload: expected the emitted trajectory payload "
            "with a frontmatter mapping"
        )
    return payload["frontmatter"]


def _issue_ref(src: dict) -> str:
    return str(src.get("issue_url") or f"#{src.get('issue', '')}")


def _stage_envelope(stage: dict, task_id: str) -> dict:
    """One stage-dispatch record as one execution-grain envelope row; the
    dispatch join keys it carries ride along, a bare session id is
    qualified into a session ref."""
    row: dict = {"task_id": task_id, "outcome": stage.get("outcome", "")}
    for key in ("role", "harness", "model"):
        if stage.get(key):
            row[key] = stage[key]
    if stage.get("session_ref"):
        row["session_ref"] = SessionRef.session(
            stage.get("harness", ""), stage["session_ref"]
        ).to_dict()
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
    if not isinstance(value, str) or not TASK_ID_RE.fullmatch(value):
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


def _str_int_map(value, where):
    if not isinstance(value, dict):
        return [f"{where}: expected a mapping of name to count"]
    return [
        e for k, v in value.items() for e in _int(v, f"{where}.{k}")
    ]


def _outputs(role_required: bool):
    """Round outputs: ``{kind, ref}`` plus a ``role`` the work grain owes."""
    entries = _dict_list({
        "kind": _enum(OUTPUT_KINDS),
        "ref": _str,
        "role": _enum(OUTPUT_ROLES),
    })
    required = ("kind", "ref", "role") if role_required else ("kind", "ref")

    def check(value, where):
        errors = entries(value, where)
        if isinstance(value, list):
            errors += [
                f"{where}[{i}]: missing {k!r}"
                for i, v in enumerate(value) if isinstance(v, dict)
                for k in required if k not in v
            ]
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
    "reviews": _dict_list({
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
    "skills": _dict_list({
        "id": _str,
        "role": _str,
        "posture": _str,
        "outcome": _str,
        "fix_rounds_attributed": _int,
    }),
}

# One round entry (:class:`Round`): references into the surfaces that own
# the content (session or trajectory note, insight notes, decisions, verdict
# events, outputs, commits, child tasks), never the content itself.
_ROUND_CHECKERS = {
    "route": _enum(ROUTES),
    "session_ref": _session_ref,
    "asked": _str,
    "notes": _str_list,
    "children": _task_id_list,
    "tools": _str_int_map,
    "tool_errors": _int,
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
    # The child digest's provenance: the harness version whose transcript
    # it read, and what that read could not recover.
    "digest": _closed_dict({"version": _str, "gaps": _str_list}),
}

_GITHUB_ITEM_URL = re.compile(
    r"https?://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)/?"
)

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
    return [
        e for i, entry in enumerate(value)
        for e in _round_errors(entry, grain, f"{where}[{i}]")
    ]


def _round_errors(entry, grain, at) -> list[str]:
    if not isinstance(entry, dict):
        return [f"{at}: expected a mapping"]
    errors = []
    for key, v in entry.items():
        if key == "outputs":
            errors += _outputs(grain == "work")(v, f"{at}.{key}")
        elif key in _ROUND_CHECKERS:
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
