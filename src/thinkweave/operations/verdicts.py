"""System 1 question sets, their verdicts, and the session ledger they read.

A question set is a named group of questions with one state text:
:func:`ask_question_set` renders a question set's text and
asks a decision provider, :func:`record_verdict` records the answer as a
``verdict`` event that the indexer projects into the ``verdicts`` table,
:func:`run_question_set` does both, and :func:`record_miss` records a call that gave
no verdict. :func:`judge_duplicate` is the one duplicate judgment: a ``choice``
over a subject's nearest neighbours plus ``new``, asked by the acquisition
dedup at enqueue and by the concept guard behind the ontology gate.

A session's :class:`Ledger` is the one reader of its event archive and live
buffer. The ex-post judge reads it: :func:`ex_post_pass` asks, per closed
turn, whether the turn's work relied on each note it was offered or served,
and asks the same of startup items against the whole session.
``wrap-finalize`` runs the pass, :func:`sweep` runs it for sessions no wrap
judged, and :func:`readout` reports the verdicts per question set and source.
:func:`backfill_citations` asks, per decision that cites no note, which of the
notes its session was served the reasoning relied on, and links those.
"""

from __future__ import annotations

import json
import math
import threading
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from thinkweave.core.buffer import (
    TURN_END,
    append_event,
    buffer_path,
    session_state_dir,
    verdicts_log_path,
)
from thinkweave.core.config import Config
from thinkweave.core.decision_provider import (
    Decision,
    DecisionProvider,
    Question,
    build_from_vault,
)
from thinkweave.core.schemas import NoteMeta
from thinkweave.core._utils import as_list
from thinkweave.synthesis.concepts import split_concepts_by_ontology, suggest_similar

PHASES = ("ex_ante", "ex_post")

#: Buffer event type for a question set call that produced no verdict: an error, a
#: 429 or a missed deadline.
VERDICT_MISS = "verdict_miss"

#: The question set that gates the prompt-time pool.
PROMPT_GATE = "prompt_gate"

#: The question set that judges, after the fact, whether work relied on a note.
EX_POST = "ex_post"

#: The question set ``weave decide smoke`` asks: one end-to-end call.
SMOKE = "smoke"

#: The question set that judges, at enqueue, whether a concept-grain item
#: duplicates a source already in the vault.
ACQUISITION_DEDUP = "acquisition_dedup"

#: The question set that judges whether a term the ontology gate shunts is a
#: synonym of a canonical concept.
CONCEPT_GUARD = "concept_guard"

#: The question set that judges which served notes a decision's reasoning
#: relied on.
CITATION = "citation"

#: The question set that triages a queued news item against the active themes.
NEWS_TRIAGE = "news_triage"

#: Every question set, each with a text under ``questions/``. The
#: ``[verdicts.<question_set>]`` knob tables are keyed by these names.
QUESTION_SETS = (
    PROMPT_GATE, EX_POST, SMOKE, ACQUISITION_DEDUP, CONCEPT_GUARD, CITATION, NEWS_TRIAGE,
)

#: The option of a duplicate judgment that names no neighbour.
NEW = "new"

#: Characters of the final assistant message a ``turn_end`` event keeps.
FINAL_MESSAGE_CHARS = 2000


def run_question_set(
    cfg: Config,
    question_set: str,
    slots: dict[str, str],
    *,
    provider: DecisionProvider | None = None,
    session_id: str = "",
    turn: int | None = None,
    subject_id: str = "",
    phase: str = "ex_ante",
    action: str = "none",
    source: str = "",
) -> Decision:
    """Ask ``question_set``'s questions about ``slots`` and record the verdict."""
    decision = ask_question_set(cfg, question_set, slots, provider=provider)
    record_verdict(
        cfg, question_set, decision, session_id=session_id, turn=turn,
        subject_id=subject_id, phase=phase, action=action, source=source,
    )
    return decision


def ask_question_set(
    cfg: Config,
    question_set: str,
    slots: dict[str, str],
    *,
    subjects: dict[str, dict[str, str]] | None = None,
    provider: DecisionProvider | None = None,
    deadline: float | None = None,
) -> Decision:
    """Render ``question_set``'s text and ask a decision provider; records nothing.

    ``subjects`` maps each subject id to its own slots, for the question set's
    per-subject questions. ``provider`` defaults to the question set's configured
    backend and model. With ``deadline`` (seconds) an unanswered call raises
    :class:`TimeoutError`.
    """
    state, questions = QuestionSetText.load(cfg, question_set).render(slots, subjects)
    if provider is None:
        policy = cfg.question_set(question_set)
        provider = build_from_vault(cfg.vault_root, policy.backend, policy.model)
    if deadline is None:
        return provider.decide(state, questions)
    return _within(deadline, lambda: provider.decide(state, questions))


def record_verdict(
    cfg: Config,
    question_set: str,
    decision: Decision,
    *,
    session_id: str = "",
    turn: int | None = None,
    subject_id: str = "",
    phase: str = "ex_ante",
    action: str = "none",
    actions: dict[str, str] | None = None,
    source: str = "",
    log: Path | None = None,
) -> None:
    """Write ``decision`` as one ``verdict`` event.

    The event goes to ``log`` when given; else, with ``session_id``, to that
    session's live buffer, which must exist; else to the vault's
    ``verdicts.jsonl``. A per-subject answer carries its subject and its
    action from ``actions``, else ``action``.
    """
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")
    event = {
        "type": "verdict",
        "ts": datetime.now(timezone.utc).isoformat(),
        "question_set": question_set,
        "turn": turn,
        "subject_id": subject_id,
        "phase": phase,
        "action": action,
        "source": source,
        "model": decision.model,
        "backend": decision.backend,
        "latency_ms": decision.latency_ms,
        "cost": decision.cost,
        "answers": [_answer_row(a, action, actions or {}) for a in decision.answers],
    }
    append_event(log or _event_log(cfg, session_id), event, harness="")


def record_miss(
    cfg: Config,
    question_set: str,
    reason: str,
    *,
    session_id: str = "",
    turn: int | None = None,
    source: str = "",
    log: Path | None = None,
    harness: str = "",
    delivery_id: str = "",
) -> bool:
    """Write one ``verdict_miss`` event for a ``question_set`` call that gave no verdict.

    The event goes where :func:`record_verdict` would put the verdict. With
    ``delivery_id`` a delivery already recorded for the session is skipped;
    returns whether the line was written.
    """
    event = {
        "ts": datetime.now(timezone.utc).isoformat(), "type": VERDICT_MISS,
        "question_set": question_set, "source": source, "turn": turn, "reason": reason,
    }
    receipts = None
    if delivery_id:
        event["delivery_id"] = delivery_id
        receipts = session_state_dir(cfg.weave_dir, session_id) if session_id else None
    return append_event(
        log or _event_log(cfg, session_id), event, harness=harness, receipts=receipts
    )


def judge_duplicate(
    cfg: Config,
    subject: str,
    neighbours: dict[str, str],
    question_set: str,
    *,
    subject_id: str = "",
    provider: DecisionProvider | None = None,
) -> "Duplicate | None":
    """Ask ``question_set`` which of ``neighbours`` (id to text, nearest
    first) ``subject`` duplicates, if any, and record the verdict.

    The verdict row's subject is ``subject_id``, else ``subject`` itself.
    Returns ``None`` when nothing was judged: the question set is off, there
    is no neighbour, or the call gave no usable answer. A failed call and a
    choice that was never offered are each logged as a ``verdict_miss``.
    """
    policy = cfg.question_set(question_set)
    if not policy.enabled or not neighbours:
        return None
    try:
        decision = ask_question_set(
            cfg, question_set, {"subject": subject},
            subjects={n: {"candidate": text} for n, text in neighbours.items()},
            provider=provider,
        )
    except Exception as exc:
        record_miss(cfg, question_set, f"{type(exc).__name__}: {exc}")
        return None
    answer = decision.answers[0]
    if answer.value != NEW and answer.value not in neighbours:
        record_miss(cfg, question_set, f"choice {answer.value!r} was never offered")
        return None
    of = "" if answer.value == NEW else str(answer.value)
    probability = float(answer.probs.get(answer.value, 0.0))
    verdict = Duplicate(of, probability, bool(of) and probability >= policy.accept)
    record_verdict(
        cfg, question_set, decision, subject_id=subject_id or subject,
        action="duplicate" if verdict.confident else "suggested" if of else "none",
    )
    return verdict


@dataclass(frozen=True)
class Duplicate:
    """One duplicate judgment: the neighbour the subject duplicates (empty
    for none), the probability behind that choice, and whether it names a
    neighbour at or above the question set's ``accept``."""

    of: str
    probability: float
    confident: bool


@dataclass
class ConceptGuard:
    """The ontology gate with the concept guard behind it.

    :meth:`gate` splits a note's concepts through the ontology and judges
    each term bound for ``proposed_concepts`` against the canonical concepts
    near it: a confident synonym becomes the canonical term, a likely one
    stays proposed with the canonical term as a hint. One guard judges a
    term once, however many notes carry it.
    """

    cfg: Config
    provider: DecisionProvider | None = None
    judged: dict[str, Duplicate | None] = field(default_factory=dict)

    def gate(
        self,
        concepts: list[str] | None,
        proposed: list[str] | None = None,
        *,
        ontology_keep: set[str] | None = None,
        prior: dict | None = None,
    ) -> dict[str, list[str] | dict[str, str]]:
        """A note's concept frontmatter: ``concepts``, ``proposed_concepts``,
        ``concept_synonyms`` (each rewritten term to the canonical concept it
        became) and ``concept_hints`` (each proposed term to its likely
        canonical concept); an empty field is left out.

        ``prior`` is the frontmatter of a note being updated: the proposed
        terms it carries are never judged, they stay proposed when
        ``proposed`` is ``None``, and its synonyms and hints are kept.
        """
        prior = prior or {}
        carried = [c.lower() for c in as_list(prior.get("proposed_concepts"))]
        found: dict[str, dict[str, str]] = {"concept_synonyms": {}, "concept_hints": {}}

        def resolve(term: str, keep: set[str]) -> str:
            verdict = None if term in carried else self._judge(term, keep)
            if verdict is None or not verdict.of:
                return term
            found["concept_synonyms" if verdict.confident else "concept_hints"][term] = verdict.of
            return verdict.of if verdict.confident else term

        canonical, shunted = split_concepts_by_ontology(
            concepts,
            proposed=carried if proposed is None else proposed,
            ontology_keep=ontology_keep,
            resolve=resolve,
        )
        fields = {"concepts": canonical, "proposed_concepts": shunted}
        fields.update({k: {**(prior.get(k) or {}), **v} for k, v in found.items() if v})
        return {key: value for key, value in fields.items() if value}

    def _judge(self, term: str, ontology_keep: set[str]) -> Duplicate | None:
        policy = self.cfg.question_set(CONCEPT_GUARD)
        if not policy.enabled:
            return None
        if term not in self.judged:
            by_length = sorted(ontology_keep, key=lambda c: (abs(len(c) - len(term)), c))
            near = suggest_similar(term, by_length, policy.pool_size)
            self.judged[term] = judge_duplicate(
                self.cfg, term, {c: c for c in near}, CONCEPT_GUARD, provider=self.provider
            )
        return self.judged[term]


@dataclass(frozen=True)
class QuestionSetText:
    """A question set's state-assembly text: a state template and its question templates."""

    state: str
    questions: dict[str, dict]

    @classmethod
    def load(cls, cfg: Config, question_set: str) -> "QuestionSetText":
        """The rail's ``thinkweave/questions/<question_set>.toml``, else the packaged default."""
        path = _text_path(cfg, question_set)
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls(state=data["state"], questions=data.get("questions", {}))

    def render(
        self, slots: dict[str, str], subjects: dict[str, dict[str, str]] | None = None
    ) -> tuple[str, list[Question]]:
        """Fill every template with ``slots`` via ``str.format``.

        A question with ``per_subject = true`` is asked once per entry of
        ``subjects``, its template filled with that subject's slots too. A
        ``choice`` question with an ``option`` template offers one option per
        entry of ``subjects``, keyed by the subject id, ahead of its ``criteria``.
        """
        questions = []
        for name, spec in self.questions.items():
            asked = (subjects or {}).items() if spec.get("per_subject") else [("", {})]
            for subject, own in asked:
                filled = {**slots, **own}
                criteria = {k: v.format(**filled) for k, v in spec.get("criteria", {}).items()}
                if "option" in spec:
                    offered = {
                        s: spec["option"].format(**{**slots, **o})
                        for s, o in (subjects or {}).items()
                    }
                    criteria = {**offered, **criteria}
                questions.append(Question(
                    name=name,
                    kind=spec["type"],
                    instructions=spec["instructions"].format(**filled),
                    criteria=criteria,
                    subject=subject,
                ))
        return self.state.format(**slots), questions


# ---------------------------------------------------------------------------
# The ex-post judge
# ---------------------------------------------------------------------------


def ex_post_pass(
    cfg: Config, ledger: "Ledger", *, provider: DecisionProvider | None = None
) -> "ExPostResult":
    """Ask every ex-post call ``ledger`` still owes and record each verdict in
    the session's folder, then re-project the session.

    A call that fails is logged in the folder as a ``verdict_miss`` and the
    pass goes on. Does nothing while the ``ex_post`` question set is off.
    """
    result = ExPostResult()
    if ledger.folder is None or not cfg.question_set(EX_POST).enabled:
        return result
    if ledger.malformed:
        result.malformed.append(
            f"{len(ledger.malformed)} malformed ledger line(s) skipped, a turn they "
            f"close stays open and unjudged: {', '.join(ledger.malformed)}"
        )
    asks = ledger.asks()
    if not asks:
        return result
    texts = _note_texts(cfg, {item.id for ask in asks for item in ask.items})
    for ask in asks:
        subjects = {item.id: {"candidate": texts[item.id]} for item in ask.items}
        try:
            decision = ask_question_set(cfg, EX_POST, ask.slots(), subjects=subjects, provider=provider)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            record_miss(
                cfg, EX_POST, reason, turn=ask.turn, source=ask.source, log=ledger.miss_log
            )
            result.misses.append(f"{ask.label()}: {reason}")
            continue
        record_verdict(
            cfg, EX_POST, decision, log=ledger.verdict_log, turn=ask.turn,
            phase="ex_post", source=ask.source,
            actions={item.id: item.action for item in ask.items},
        )
        result.calls += 1
        result.rows += len(decision.answers)
    if result.calls:
        result.sessions = 1
        _reproject(cfg, ledger.folder)
    return result


def sweep(
    cfg: Config,
    *,
    project: str = "",
    since: str = "",
    provider: DecisionProvider | None = None,
) -> "ExPostResult":
    """Run :func:`ex_post_pass` over every indexed session, or one project's,
    dated ``since`` or later; a session already judged costs one ledger read."""
    total = ExPostResult()
    for ledger in _session_ledgers(cfg, project, since):
        total.add(ex_post_pass(cfg, ledger, provider=provider))
    return total


def readout(
    cfg: Config, *, project: str = "", since: str = "", threshold: float | None = None
) -> list["QuestionSetReadout"]:
    """The verdicts of indexed sessions, per question set and source.

    Each decision call counts once, with its latency and cost, however many
    rows it wrote. ``threshold`` (default: the ``ex_post`` question set's ``accept``)
    splits ex-post verdicts into used and unused.
    """
    from thinkweave.core.indexer import Indexer

    if threshold is None:
        threshold = cfg.question_set(EX_POST).accept
    idx = Indexer(config=cfg)
    try:
        rows = idx.db.execute(
            "SELECT v.* FROM verdicts v JOIN notes n ON n.id = v.session_id "
            "WHERE n.type = 'session' AND (? = '' OR n.project = ?) "
            "AND (? = '' OR n.date >= ?) ORDER BY v.ts",
            (project, project, since, since),
        ).fetchall()
    finally:
        idx.close()

    question_sets: dict[tuple[str, str], QuestionSetReadout] = {}

    def at(question_set: str, source: str) -> QuestionSetReadout:
        return question_sets.setdefault(
            (question_set, source), QuestionSetReadout(question_set, source)
        )

    calls: dict[tuple, dict] = {}
    ex_post: dict[tuple, float] = {}
    for r in rows:
        at(r["question_set"], r["source"] or "").rows += 1
        calls.setdefault(
            (r["question_set"], r["source"] or "", r["session_id"], r["turn"], r["phase"], r["ts"]), r
        )
        if r["phase"] == "ex_post" and r["kind"] == "noul":
            ex_post[(r["session_id"], r["subject_id"], r["turn"], r["source"])] = r["value"]
            at(r["question_set"], r["source"] or "").count_use(r["action"], r["value"] >= threshold)
    for (question_set, source, *_key), r in calls.items():
        at(question_set, source).count_call(r["latency_ms"], r["cost"])
    for r in rows:
        if r["phase"] == "ex_ante" and r["kind"] == "noul":
            used = ex_post.get((r["session_id"], r["subject_id"], r["turn"], r["source"]))
            at(r["question_set"], r["source"] or "").calibrate(r["value"], used)
    for ledger in _session_ledgers(cfg, project, since):
        for ev in ledger.events:
            if ev.get("type") == VERDICT_MISS:
                at(str(ev.get("question_set") or ""), str(ev.get("source") or "")).misses += 1
    return [question_sets[key].finish() for key in sorted(question_sets)]


# ---------------------------------------------------------------------------
# The citation backfill
# ---------------------------------------------------------------------------


def backfill_citations(
    cfg: Config, *, dry_run: bool = False, provider: DecisionProvider | None = None
) -> "CitationBackfill":
    """Ask, per decision that cites no note, which notes served to its
    session the reasoning relied on, and append those as a ``cites:`` line
    of path-based links; then rebuild the index once.

    A decision is asked once: its verdicts mark it judged whatever they say.
    A call that fails is logged as a ``verdict_miss`` and the decision waits
    for the next run. With ``dry_run`` nothing is written: no link, no
    verdict, no miss. Does nothing while the ``citation`` question set is off.
    """
    from thinkweave.core.vault import VaultManager

    result = CitationBackfill()
    policy = cfg.question_set(CITATION)
    if not policy.enabled:
        return result
    vault = VaultManager(config=cfg)
    owed = _uncited_decisions(cfg, vault)
    texts = _note_texts(cfg, {nid for uncited in owed for nid in uncited.served})
    for uncited in owed:
        decision = uncited.note
        try:
            answered = [
                ask_question_set(
                    cfg, CITATION, uncited.slots(), provider=provider,
                    subjects={nid: {"candidate": texts[nid]} for nid in pool},
                )
                for pool in uncited.pools(policy.pool_size)
            ]
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            if not dry_run:
                record_miss(cfg, CITATION, reason, source=decision.id)
            result.misses.append(f"{decision.id}: {reason}")
            continue
        cited = tuple(
            a.subject for call in answered for a in call.answers if a.value >= policy.accept
        )
        result.proposals.append(CitationProposal(decision.id, decision.title, cited))
        if dry_run:
            continue
        for call in answered:
            record_verdict(
                cfg, CITATION, call, phase="ex_post", source=decision.id,
                actions={nid: "cited" for nid in cited},
            )
        if cited:
            vault.update_note(decision.path, body_append=uncited.cites_line(cited))
            result.written += 1
    if result.proposals and not dry_run:
        _rebuild(cfg)
    return result


@dataclass(frozen=True)
class UncitedDecision:
    """A decision the backfill owes a call: its note, and the vault notes its
    session was served, each id mapped to its vault path."""

    note: NoteMeta
    served: dict[str, str]

    def slots(self) -> dict[str, str]:
        return {"decision": f"{self.note.title}\n\n{self.note.body.strip()}"}

    def pools(self, size: int) -> list[list[str]]:
        """The served ids in calls of at most ``size`` candidates."""
        ids = list(self.served)
        return [ids[i:i + size] for i in range(0, len(ids), size)]

    def cites_line(self, cited: tuple[str, ...]) -> str:
        """The body line that links ``cited`` by path."""
        from thinkweave.synthesis.hub import reflink

        return "cites: " + ", ".join(reflink(nid, self.served) for nid in cited)


@dataclass(frozen=True)
class CitationProposal:
    """One decision the backfill judged and the served notes it would cite:
    those the ``citation`` question set scored at or above ``accept``."""

    decision_id: str
    title: str
    cited: tuple[str, ...]


@dataclass
class CitationBackfill:
    """What one backfill run did: a proposal per decision judged, the number
    of decisions that got a ``cites:`` line, and one line per decision whose
    call gave no verdict."""

    proposals: list[CitationProposal] = field(default_factory=list)
    written: int = 0
    misses: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Item:
    """A note put in front of the agent, or offered to it, from one source."""

    id: str
    source: str
    action: str = "served"


@dataclass
class Turn:
    """One prompt up to its ``turn_end``: what was asked, offered and done."""

    seq: int
    prompt: str
    items: list[Item] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    message: str = ""
    closed: bool = False

    def take(self, ev: dict) -> None:
        """Fold one of the turn's events in."""
        from thinkweave.core.indexer import served_source

        kind = ev.get("type")
        if kind == TURN_END:
            self.message = str(ev.get("message") or "")
            self.files = [str(f) for f in ev.get("files") or []]
            self.closed = True
        elif kind == "verdict" and ev.get("question_set") == PROMPT_GATE:
            for answer in ev.get("answers") or []:
                self._offer(Item(
                    str(answer.get("subject_id") or ""), str(ev.get("source") or ""),
                    str(answer.get("action") or "none"),
                ))
        elif kind == "retrieval":
            for nid in ev.get("returned_ids") or []:
                self._offer(Item(str(nid), served_source(ev)))
        elif ev.get("tool") in ("Write", "Edit") and ev.get("file"):
            if ev["file"] not in self.files:
                self.files.append(str(ev["file"]))

    def work(self) -> str:
        """The turn as the judge reads it."""
        return (
            f"Turn {self.seq}\nUser prompt: {self.prompt}\n"
            f"Agent's final message: {self.message or '(not captured)'}\n"
            f"Files touched: {', '.join(self.files) or '(none)'}"
        )

    def _offer(self, item: Item) -> None:
        if _judged(item.id) and all((i.id, i.source) != (item.id, item.source) for i in self.items):
            self.items.append(item)


@dataclass(frozen=True)
class ExPostAsk:
    """One ex-post call: one source's items, judged against one turn, or
    against the whole session when ``turn`` is ``None``."""

    turn: int | None
    source: str
    items: tuple[Item, ...]
    work: str

    def slots(self) -> dict[str, str]:
        span = "one turn of their session" if self.turn else "their whole session"
        return {"span": span, "work": self.work}

    def label(self) -> str:
        return f"turn {self.turn} {self.source}" if self.turn else f"session {self.source}"


@dataclass(frozen=True)
class TurnEnd:
    """The close of one turn: its sequence, final assistant message and files touched."""

    turn: int
    message: str
    files: list[str]

    def as_event(self) -> dict:
        """The ``turn_end`` buffer event."""
        return {"ts": datetime.now(timezone.utc).isoformat(), "type": TURN_END,
                "turn": self.turn, "message": self.message, "files": self.files}


class Ledger:
    """One session's event ledger: the archive in its folder and its live
    buffer, each line once, in time order."""

    def __init__(
        self,
        folder: Path | None,
        events: list[dict],
        malformed: tuple[str, ...] = (),
        session_key: str = "",
    ) -> None:
        self.session_key = session_key
        """The harness session id the ledger was read for, when it was."""
        self.folder = folder
        self.events = events
        self.malformed = malformed
        """``<stream>:<line number>`` of each line that is not a JSON object."""

    @classmethod
    def read(cls, cfg: Config, session_key: str, folder: Path | None = None) -> "Ledger":
        """The ledger of harness session ``session_key``; ``folder`` defaults to
        the folder of the session note stamped with that key."""
        if folder is None and session_key:
            from thinkweave.core.vault import VaultManager, find_session_note_by_source

            note = find_session_note_by_source(VaultManager(config=cfg), session_key)
            folder = note.parent if note else None
        streams = [folder / "events.jsonl", folder / "retrieval_log.jsonl"] if folder else []
        if session_key:
            streams.append(buffer_path(cfg.weave_dir, session_key))
        return cls.from_streams(folder, streams, session_key)

    @classmethod
    def live(cls, cfg: Config, session_key: str) -> "Ledger":
        """The live buffer alone, with no session-note lookup: every event
        since the buffer was last cleared."""
        return cls.from_streams(None, [buffer_path(cfg.weave_dir, session_key)], session_key)

    @classmethod
    def from_streams(
        cls, folder: Path | None, streams: list[Path], session_key: str = ""
    ) -> "Ledger":
        """Every distinct line of ``streams`` as an event, ordered by ``ts``;
        a line that is not a JSON object is recorded in :attr:`malformed`."""
        seen: set[str] = set()
        events: list[dict] = []
        malformed: list[str] = []
        for path in streams:
            if not path.exists():
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip() or line in seen:
                    continue
                seen.add(line)
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    ev = None
                if isinstance(ev, dict):
                    events.append(ev)
                else:
                    malformed.append(f"{path}:{n}")
        return cls(folder, sorted(events, key=_when), tuple(malformed), session_key)

    def prompts(self) -> list[str]:
        return [str(ev.get("text", "")) for ev in self.events if ev.get("type") == "prompt"]

    def served_ids(self) -> set[str]:
        """Every note id a startup payload or a retrieval put in front of the agent."""
        return {
            nid
            for ev in self.events if ev.get("type") in ("startup", "retrieval")
            for nid in ev.get("returned_ids") or []
            if isinstance(nid, str) and nid
        }

    def turns(self) -> list[Turn]:
        """Every turn from its prompt on; a turn closes once its ``turn_end`` lands."""
        turns: list[Turn] = []
        for ev in self.events:
            if ev.get("type") == "prompt":
                turns.append(Turn(len(turns) + 1, str(ev.get("text", ""))))
            elif turns and not turns[-1].closed:
                turns[-1].take(ev)
        return turns

    def startup(self) -> list[Item]:
        """The notes every startup payload served, each once; open tasks left out."""
        from thinkweave.core.indexer import served_source

        items: dict[str, Item] = {}
        for ev in self.events:
            if ev.get("type") == "startup":
                for nid in ev.get("returned_ids") or []:
                    if _judged(str(nid)):
                        items.setdefault(str(nid), Item(str(nid), served_source(ev)))
        return list(items.values())

    def turn_end(self, message: str) -> TurnEnd | None:
        """The ``turn_end`` that closes the open turn; ``None`` when no turn is open."""
        turns = self.turns()
        if not turns or turns[-1].closed:
            return None
        return TurnEnd(turns[-1].seq, message[:FINAL_MESSAGE_CHARS], list(turns[-1].files))

    def asks(self) -> list[ExPostAsk]:
        """The ex-post calls this ledger holds no verdict for yet: per closed
        turn and source, the turn's items; per source, the startup items
        against every closed turn."""
        closed = [t for t in self.turns() if t.closed]
        if not closed:
            return []
        done = {
            (ev.get("turn"), ev.get("source")) for ev in self.events
            if ev.get("type") == "verdict" and ev.get("phase") == "ex_post"
        }
        asks = [
            ExPostAsk(turn.seq, source, items, turn.work())
            for turn in closed for source, items in _by_source(turn.items).items()
        ]
        session = "\n\n".join(turn.work() for turn in closed)
        asks += [
            ExPostAsk(None, source, items, session)
            for source, items in _by_source(self.startup()).items()
        ]
        return [ask for ask in asks if (ask.turn, ask.source) not in done]

    @property
    def verdict_log(self) -> Path:
        return self.folder / "retrieval_log.jsonl"

    @property
    def miss_log(self) -> Path:
        return self.folder / "events.jsonl"


@dataclass
class ExPostResult:
    """What ex-post passes wrote: sessions judged, decision calls, verdict
    rows, one line per call that gave no verdict, and one per ledger whose
    malformed lines were skipped."""

    sessions: int = 0
    calls: int = 0
    rows: int = 0
    misses: list[str] = field(default_factory=list)
    malformed: list[str] = field(default_factory=list)

    def add(self, other: "ExPostResult") -> None:
        self.sessions += other.sessions
        self.calls += other.calls
        self.rows += other.rows
        self.misses.extend(other.misses)
        self.malformed.extend(other.malformed)


@dataclass
class Usage:
    """Ex-post verdicts of one action, split at the readout's threshold."""

    used: int = 0
    unused: int = 0


@dataclass(frozen=True)
class CalibrationBin:
    """Ex-ante verdicts in one probability bin and the mean ex-post probability
    of the same subjects in the same turns (``None`` when the bin is empty)."""

    ex_ante: str
    n: int
    mean_ex_post: float | None


@dataclass
class QuestionSetReadout:
    """One question set and source in a :func:`readout`.

    ``usage`` maps each ex-post verdict's action (served, capped, rejected) to
    its used and unused counts. ``calibration`` bins ex-ante probabilities and
    gives the mean ex-post probability of the same subject in the same turn.
    """

    question_set: str
    source: str
    calls: int = 0
    rows: int = 0
    misses: int = 0
    miss_rate: float | None = None
    latency_ms_p50: int | None = None
    latency_ms_p95: int | None = None
    cost: float = 0.0
    usage: dict[str, Usage] = field(default_factory=dict)
    calibration: list[CalibrationBin] = field(default_factory=list)
    _latencies: list[int] = field(default_factory=list, repr=False)
    _bins: list[list[float]] = field(default_factory=list, repr=False)

    def count_call(self, latency_ms: int | None, cost: float | None) -> None:
        self.calls += 1
        self.cost += cost or 0.0
        if latency_ms is not None:
            self._latencies.append(latency_ms)

    def count_use(self, action: str | None, used: bool) -> None:
        counts = self.usage.setdefault(action or "none", Usage())
        if used:
            counts.used += 1
        else:
            counts.unused += 1

    def calibrate(self, ex_ante: float, ex_post: float | None) -> None:
        self._bins = self._bins or [[] for _ in range(_BINS)]
        if ex_post is not None:
            self._bins[min(int(ex_ante * _BINS), _BINS - 1)].append(ex_post)

    def finish(self) -> "QuestionSetReadout":
        """Fill the derived fields from what was counted."""
        attempts = self.calls + self.misses
        self.miss_rate = round(self.misses / attempts, 4) if attempts else None
        self.latency_ms_p50 = _nearest_rank(self._latencies, 0.50)
        self.latency_ms_p95 = _nearest_rank(self._latencies, 0.95)
        self.cost = round(self.cost, 6)
        self.calibration = [
            CalibrationBin(
                f"{i / _BINS:.1f}-{(i + 1) / _BINS:.1f}",
                len(values),
                round(sum(values) / len(values), 4) if values else None,
            )
            for i, values in enumerate(self._bins)
        ]
        return self

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if not k.startswith("_")}


#: Equal-width bins of ex-ante probability in a readout's calibration.
_BINS = 5

#: Characters of a note's body the ex-post judge reads beside its title.
_NOTE_TEXT_CHARS = 300


def _judged(note_id: str) -> bool:
    """Whether the ex-post judge scores this id: open tasks are cues, not notes."""
    return bool(note_id) and not note_id.startswith("tsk-")


def _by_source(items: list[Item]) -> dict[str, tuple[Item, ...]]:
    grouped: dict[str, list[Item]] = {}
    for item in items:
        grouped.setdefault(item.source, []).append(item)
    return {source: tuple(group) for source, group in grouped.items()}


def _when(ev: dict) -> datetime:
    try:
        ts = datetime.fromisoformat(str(ev.get("ts") or ""))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _note_texts(cfg: Config, ids: set[str]) -> dict[str, str]:
    """Each note as the judge reads it: title, type and the start of its body."""
    from thinkweave.retrieval.search import Search

    search = Search(config=cfg)
    try:
        rows = {nid: search.get_note_by_id(nid) for nid in ids}
    finally:
        search.close()
    return {
        nid: (
            f"{row['title']} ({row['type']}): {(row['body_text'] or '')[:_NOTE_TEXT_CHARS]}"
            if row else f"{nid} (no longer in the vault)"
        )
        for nid, row in rows.items()
    }


def _uncited_decisions(cfg: Config, vault) -> list[UncitedDecision]:
    """Each decision the backfill still owes a call, with the vault notes
    its session was served (the decision and its own session left out).

    A decision is owed when the vault log holds no ``citation`` verdict for
    it, its body cites no note id, and its session served at least one note.
    """
    from thinkweave.core.indexer import Indexer
    from thinkweave.operations.rlvr_export import extract_cited_ids, served_rows
    from thinkweave.synthesis.hub import build_id_path_map

    log = Ledger.from_streams(None, [verdicts_log_path(cfg.vault_root)])
    judged = {
        ev.get("source") for ev in log.events
        if ev.get("type") == "verdict" and ev.get("question_set") == CITATION
    }
    owed = []
    idx = Indexer(config=cfg)
    try:
        paths = build_id_path_map(idx.db)
        rows = idx.db.execute(
            "SELECT id, path, frontmatter FROM notes WHERE type = 'decision' ORDER BY date, id"
        ).fetchall()
        for row in rows:
            session = str(json.loads(row["frontmatter"] or "{}").get("source_session") or "")
            if not session or row["id"] in judged:
                continue
            served = sorted({
                r["note_id"] for r in served_rows(idx, session)
                if _judged(r["note_id"]) and r["note_id"] in paths
                and r["note_id"] not in (row["id"], session)
            })
            if not served:
                continue
            note = vault.read_note(vault.root / row["path"])
            if not extract_cited_ids(note.body):
                owed.append(UncitedDecision(note, {nid: paths[nid] for nid in served}))
    finally:
        idx.close()
    return owed


def _rebuild(cfg: Config) -> None:
    from thinkweave.core.indexer import Indexer

    idx = Indexer(config=cfg)
    try:
        idx.rebuild()
    finally:
        idx.close()


def _reproject(cfg: Config, folder: Path) -> None:
    from thinkweave.core.indexer import Indexer

    idx = Indexer(config=cfg)
    try:
        idx.index_file(folder / "session.md")
    finally:
        idx.close()


def _session_ledgers(cfg: Config, project: str, since: str):
    """The ledger of each indexed session, or one project's, dated ``since`` on."""
    from thinkweave.core.indexer import Indexer

    idx = Indexer(config=cfg)
    try:
        rows = idx.db.execute(
            "SELECT path, frontmatter FROM notes WHERE type = 'session' "
            "AND (? = '' OR project = ?) AND (? = '' OR date >= ?)",
            (project, project, since, since),
        ).fetchall()
    finally:
        idx.close()
    for row in rows:
        try:
            fm = json.loads(row["frontmatter"] or "{}")
        except json.JSONDecodeError:
            fm = {}
        folder = (cfg.vault_root / row["path"]).parent
        yield Ledger.read(cfg, str(fm.get("source_session") or ""), folder)


def _nearest_rank(values: list[int], q: float) -> int | None:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)] if ordered else None


_PACKAGED_TEXTS = Path(__file__).parent / "questions"


def _text_path(cfg: Config, question_set: str) -> Path:
    if cfg.rsi_rail is not None:
        rail = cfg.rsi_rail / "thinkweave" / "questions" / f"{question_set}.toml"
        if rail.exists():
            return rail
    packaged = _PACKAGED_TEXTS / f"{question_set}.toml"
    if not packaged.exists():
        raise FileNotFoundError(
            f"no text for question set {question_set!r}: neither the rail's "
            f"thinkweave/questions/{question_set}.toml nor a packaged default exists"
        )
    return packaged


def _event_log(cfg: Config, session_id: str) -> Path:
    if not session_id:
        return verdicts_log_path(cfg.vault_root)
    live = buffer_path(cfg.weave_dir, session_id)
    if not live.exists():
        raise FileNotFoundError(
            f"session {session_id!r} has no live buffer at {live}; a session "
            "verdict needs a live session, a non-session one passes no session_id"
        )
    return live


def _answer_row(answer, action: str, actions: dict[str, str]) -> dict:
    row = {"question": answer.question, "kind": answer.kind,
           "value": answer.value, "probs": answer.probs}
    if answer.subject:
        row["subject_id"] = answer.subject
        row["action"] = actions.get(answer.subject, action)
    return row


def _within(deadline: float, call):
    """``call()``'s result, or :class:`TimeoutError` once ``deadline`` seconds pass.

    The call runs in a daemon thread, so an abandoned call never holds up exit.
    """
    holder: dict = {}

    def run() -> None:
        try:
            holder["result"] = call()
        except BaseException as exc:
            holder["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=max(0.0, deadline))
    if worker.is_alive():
        raise TimeoutError(f"no answer within {deadline:.2f}s")
    if "error" in holder:
        raise holder["error"]
    return holder["result"]
