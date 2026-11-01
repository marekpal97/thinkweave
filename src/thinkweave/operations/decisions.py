"""Decision operations — the decision corpus and the System 1 decision sites.

``weave_extract`` writes new decisions (via ``operations.extract``); this
module owns reads and the ``weave_judge`` mutation pass that scores existing
decisions against structural evidence and flips ``status`` accordingly.

It also owns System 1 decision sites: :func:`ask_site` renders a site's text
and asks a decision provider, :func:`record_verdict` records the verdict as a
``decision`` event that the indexer projects into the ``verdicts`` table, and
:func:`run_site` does both.

The ex-post judge reads a session's :class:`Ledger`: :func:`ex_post_pass`
asks, per closed turn, whether the turn's work relied on each note it was
offered or served, and asks the same of startup items against the whole
session. ``wrap-finalize`` runs the pass, :func:`sweep` runs it for sessions
no wrap judged, and :func:`readout` reports the verdicts per site and source.
"""

from __future__ import annotations

import json
import math
import threading
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from thinkweave.core.buffer import TURN_END, append_event, buffer_path, verdicts_log_path
from thinkweave.core.config import Config
from thinkweave.core.decision_provider import (
    Decision,
    DecisionProvider,
    Question,
    build_from_vault,
)


def list_by_file(
    cfg: Config,
    file_path: str,
    *,
    project: str = "",
    status: str = "",
    limit: int = 50,
):
    """Return decisions whose ``file_paths`` frontmatter touches the given file."""
    from thinkweave.retrieval.search import Search

    s = Search(config=cfg)
    try:
        return s.search_decisions_by_file(
            file_path, project=project, status=status, limit=limit
        )
    finally:
        s.close()


def judge(
    cfg: Config,
    *,
    decision_id: str = "",
    decision_ids: list[str] | None = None,
    session_id: str = "",
    project: str = "",
):
    """Evaluate decisions against structural evidence; return list of (note, result).

    ``decision_ids`` is the batch form of ``decision_id`` — judge an explicit
    set in one index pass (used by the supersession-predecessor flip, which
    has a worklist of ids rather than a session/project scope).
    """
    from thinkweave.core.indexer import Indexer
    from thinkweave.core.vault import VaultManager
    from thinkweave.retrieval.search import Search
    from thinkweave.synthesis.judge import evaluate_decision, find_decisions

    vm = VaultManager(config=cfg)
    s = Search(config=cfg)
    idx = Indexer(config=cfg)
    target = []
    if decision_id:
        row = s.get_note_by_id(decision_id)
        if row and row["type"] == "decision":
            target.append(vm.read_note(vm.root / row["path"]))
    elif decision_ids:
        seen: set[str] = set()
        for did in decision_ids:
            if not did or did in seen:
                continue
            seen.add(did)
            row = s.get_note_by_id(did)
            if row and row["type"] == "decision":
                target.append(vm.read_note(vm.root / row["path"]))
    elif session_id:
        target = find_decisions(idx.db, vm, session_id=session_id)
    elif project:
        target = find_decisions(idx.db, vm, project=project)

    if not target:
        idx.close()
        s.close()
        return []

    all_decisions = find_decisions(idx.db, vm)
    idx.close()

    out = []
    for dec in target:
        sess_id = dec.frontmatter.get("source_session", "")
        sess_meta = None
        if sess_id:
            row = s.get_note_by_id(sess_id)
            if row:
                sess_meta = vm.read_note(vm.root / row["path"])
        result = evaluate_decision(dec, all_decisions, sess_meta)
        out.append((dec, result))
    s.close()
    return out


def judge_and_writeback(
    cfg: Config,
    *,
    decision_id: str = "",
    decision_ids: list[str] | None = None,
    session_id: str = "",
    project: str = "",
):
    """Run :func:`judge` and persist verdict/status to each decision's frontmatter.

    Returns the same ``[(NoteMeta, result_dict), ...]`` shape as :func:`judge`.
    Verdict → status mapping: ``kept→accepted``, ``superseded→superseded``,
    ``reverted→deprecated``. Decisions with no matching session evidence are
    skipped silently (and an empty list is returned).

    Batched: one frontmatter write combines verdict + status fields, and a
    single :class:`Indexer` instance re-indexes all touched files at the end
    (vs. open/close per decision).
    """
    from thinkweave.core.indexer import Indexer
    from thinkweave.core.vault import VaultManager

    results = judge(
        cfg,
        decision_id=decision_id,
        decision_ids=decision_ids,
        session_id=session_id,
        project=project,
    )
    if not results:
        return results

    vm = VaultManager(config=cfg)
    status_map = {
        "kept": "accepted",
        "superseded": "superseded",
        "reverted": "deprecated",
    }
    touched_paths = []
    for dec, result in results:
        fm_updates: dict = {
            "verdict": result["verdict"],
            "confidence": result["confidence"],
            "judged_at": result["judged_at"],
        }
        if result["blame_lines"] >= 0:
            fm_updates["blame_lines"] = result["blame_lines"]
        if result.get("commit_refs"):
            fm_updates["commit_refs"] = result["commit_refs"]
            if not dec.frontmatter.get("committed"):
                fm_updates["committed"] = True
        # NOTE: prediction_match writebacks now live in the
        # `/judge-prediction` skill — this writeback only handles
        # structural verdict/status/blame/commit data.
        new_status = status_map.get(result["verdict"])
        if new_status and new_status != dec.frontmatter.get("status"):
            fm_updates["status"] = new_status
        vm.update_note(vm.root / dec.path, frontmatter_updates=fm_updates)
        touched_paths.append(vm.root / dec.path)

    idx = Indexer(config=cfg)
    try:
        for path in touched_paths:
            idx.index_file(path)
    finally:
        idx.close()
    return results


def rejudge_supersession_predecessors(
    cfg: Config, predecessor_ids: list[str]
) -> list:
    """Evidence-gated supersession flip over a worklist of predecessor ids.

    Replaces the old eager ``status: superseded`` flip that fired the moment a
    new decision declared ``supersedes: [dec-X]``. A declaration is only a
    re-judge *trigger*; this re-runs the structural judge over each predecessor
    so the verdict comes from evidence:

    - ``_check_re_edited`` confirms a later, different-session decision actually
      re-touched the predecessor's files (the same-session sibling guard keeps
      co-feature decisions from false-flagging each other), and
    - blame survival decides kept-vs-superseded — a predecessor whose committed
      lines were replaced flips to ``superseded``; one whose lines still survive
      stays ``kept`` (co-contributor). With no superseder committed yet, the
      lines survive and nothing flips — the predecessor waits in the queue.

    Called from ``wrap-finalize`` (the wrap worker, holding this session's
    commits) and ``dream apply`` (the headless/deferred backlog). Both are
    git-bearing contexts, which is what the blame check needs. Returns the same
    ``[(NoteMeta, result_dict), ...]`` shape as :func:`judge_and_writeback`
    (empty list when nothing resolved).
    """
    ids = [pid for pid in dict.fromkeys(predecessor_ids) if pid]
    if not ids:
        return []
    return judge_and_writeback(cfg, decision_ids=ids)


# ---------------------------------------------------------------------------
# System 1 decision sites
# ---------------------------------------------------------------------------

PHASES = ("ex_ante", "ex_post")

#: Buffer event type for a site call that produced no verdict: an error, a
#: 429 or a missed deadline.
DECISION_MISS = "decision_miss"

#: The decision site that gates the prompt-time pool.
PROMPT_GATE = "prompt_gate"

#: The decision site that judges, after the fact, whether work relied on a note.
EX_POST = "ex_post"

#: Characters of the final assistant message a ``turn_end`` event keeps.
FINAL_MESSAGE_CHARS = 2000


def run_site(
    cfg: Config,
    site: str,
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
    """Ask ``site``'s questions about ``slots`` and record the verdict."""
    decision = ask_site(cfg, site, slots, provider=provider)
    record_verdict(
        cfg, site, decision, session_id=session_id, turn=turn,
        subject_id=subject_id, phase=phase, action=action, source=source,
    )
    return decision


def ask_site(
    cfg: Config,
    site: str,
    slots: dict[str, str],
    *,
    subjects: dict[str, dict[str, str]] | None = None,
    provider: DecisionProvider | None = None,
    deadline: float | None = None,
) -> Decision:
    """Render ``site``'s text and ask a decision provider; records nothing.

    ``subjects`` maps each subject id to its own slots, for the site's
    per-subject questions. ``provider`` defaults to the site's configured
    backend and model. With ``deadline`` (seconds) an unanswered call raises
    :class:`TimeoutError`.
    """
    state, questions = SiteText.load(cfg, site).render(slots, subjects)
    if provider is None:
        policy = cfg.decision_site(site)
        provider = build_from_vault(cfg.vault_root, policy.backend, policy.model)
    if deadline is None:
        return provider.decide(state, questions)
    return _within(deadline, lambda: provider.decide(state, questions))


def record_verdict(
    cfg: Config,
    site: str,
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
    """Write ``decision`` as one ``decision`` event.

    The event goes to ``log`` when given; else, with ``session_id``, to that
    session's live buffer, which must exist; else to the vault's
    ``verdicts.jsonl``. A per-subject answer carries its subject and its
    action from ``actions``, else ``action``.
    """
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")
    event = {
        "type": "decision",
        "ts": datetime.now(timezone.utc).isoformat(),
        "site": site,
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


@dataclass(frozen=True)
class SiteText:
    """A site's state-assembly text: a state template and its question templates."""

    state: str
    questions: dict[str, dict]

    @classmethod
    def load(cls, cfg: Config, site: str) -> "SiteText":
        """The rail's ``thinkweave/decisions/<site>.toml``, else the packaged default."""
        path = _site_text_path(cfg, site)
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls(state=data["state"], questions=data.get("questions", {}))

    def render(
        self, slots: dict[str, str], subjects: dict[str, dict[str, str]] | None = None
    ) -> tuple[str, list[Question]]:
        """Fill every template with ``slots`` via ``str.format``.

        A question with ``per_subject = true`` is asked once per entry of
        ``subjects``, its template filled with that subject's slots too.
        """
        questions = []
        for name, spec in self.questions.items():
            asked = (subjects or {}).items() if spec.get("per_subject") else [("", {})]
            for subject, own in asked:
                filled = {**slots, **own}
                questions.append(Question(
                    name=name,
                    kind=spec["type"],
                    instructions=spec["instructions"].format(**filled),
                    criteria={k: v.format(**filled) for k, v in spec.get("criteria", {}).items()},
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

    A call that fails is logged in the folder as a ``decision_miss`` and the
    pass goes on. Does nothing while the ``ex_post`` site is off.
    """
    result = ExPostResult()
    if ledger.folder is None or not cfg.decision_site(EX_POST).enabled:
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
            decision = ask_site(cfg, EX_POST, ask.slots(), subjects=subjects, provider=provider)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            ledger.miss(ask, reason)
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
) -> list["SiteReadout"]:
    """The verdicts of indexed sessions, per site and source.

    Each decision call counts once, with its latency and cost, however many
    rows it wrote. ``threshold`` (default: the ``ex_post`` site's ``accept``)
    splits ex-post verdicts into used and unused.
    """
    from thinkweave.core.indexer import Indexer

    if threshold is None:
        threshold = cfg.decision_site(EX_POST).accept
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

    sites: dict[tuple[str, str], SiteReadout] = {}

    def at(site: str, source: str) -> SiteReadout:
        return sites.setdefault((site, source), SiteReadout(site, source))

    calls: dict[tuple, dict] = {}
    ex_post: dict[tuple, float] = {}
    for r in rows:
        at(r["site"], r["source"] or "").rows += 1
        calls.setdefault(
            (r["site"], r["source"] or "", r["session_id"], r["turn"], r["phase"], r["ts"]), r
        )
        if r["phase"] == "ex_post" and r["kind"] == "noul":
            ex_post[(r["session_id"], r["subject_id"], r["turn"], r["source"])] = r["value"]
            at(r["site"], r["source"] or "").count_use(r["action"], r["value"] >= threshold)
    for (site, source, *_key), r in calls.items():
        at(site, source).count_call(r["latency_ms"], r["cost"])
    for r in rows:
        if r["phase"] == "ex_ante" and r["kind"] == "noul":
            used = ex_post.get((r["session_id"], r["subject_id"], r["turn"], r["source"]))
            at(r["site"], r["source"] or "").calibrate(r["value"], used)
    for ledger in _session_ledgers(cfg, project, since):
        for ev in ledger.events:
            if ev.get("type") == DECISION_MISS:
                at(str(ev.get("site") or ""), str(ev.get("source") or "")).misses += 1
    return [sites[key].finish() for key in sorted(sites)]


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
        elif kind == "decision" and ev.get("site") == PROMPT_GATE:
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
        self, folder: Path | None, events: list[dict], malformed: tuple[str, ...] = ()
    ) -> None:
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
        return cls.from_streams(folder, streams)

    @classmethod
    def from_streams(cls, folder: Path | None, streams: list[Path]) -> "Ledger":
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
        return cls(folder, sorted(events, key=_when), tuple(malformed))

    def prompts(self) -> list[str]:
        return [str(ev.get("text", "")) for ev in self.events if ev.get("type") == "prompt"]

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
            if ev.get("type") == "decision" and ev.get("phase") == "ex_post"
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

    def miss(self, ask: ExPostAsk, reason: str) -> None:
        """Log an ex-post call that gave no verdict in the session's folder."""
        append_event(self.folder / "events.jsonl", {
            "ts": datetime.now(timezone.utc).isoformat(), "type": DECISION_MISS,
            "site": EX_POST, "source": ask.source, "turn": ask.turn, "reason": reason,
        }, harness="")

    @property
    def verdict_log(self) -> Path:
        return self.folder / "retrieval_log.jsonl"


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
class SiteReadout:
    """One site and source in a :func:`readout`.

    ``usage`` maps each ex-post verdict's action (served, capped, rejected) to
    its used and unused counts. ``calibration`` bins ex-ante probabilities and
    gives the mean ex-post probability of the same subject in the same turn.
    """

    site: str
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

    def finish(self) -> "SiteReadout":
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


_PACKAGED_TEXTS = Path(__file__).parent / "decision_texts"


def _site_text_path(cfg: Config, site: str) -> Path:
    if cfg.rsi_rail is not None:
        rail = cfg.rsi_rail / "thinkweave" / "decisions" / f"{site}.toml"
        if rail.exists():
            return rail
    packaged = _PACKAGED_TEXTS / f"{site}.toml"
    if not packaged.exists():
        raise FileNotFoundError(
            f"no text for decision site {site!r}: neither the rail's "
            f"thinkweave/decisions/{site}.toml nor a packaged default exists"
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
