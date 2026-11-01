"""Decision operations — the decision corpus and the System 1 decision sites.

``weave_extract`` writes new decisions (via ``operations.extract``); this
module owns reads and the ``weave_judge`` mutation pass that scores existing
decisions against structural evidence and flips ``status`` accordingly.

It also owns System 1 decision sites: :func:`ask_site` renders a site's text
and asks a decision provider, :func:`record_verdict` records the verdict as a
``decision`` event that the indexer projects into the ``verdicts`` table, and
:func:`run_site` does both.
"""

from __future__ import annotations

import threading
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from thinkweave.core.buffer import append_event, buffer_path, verdicts_log_path
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
) -> None:
    """Write ``decision`` as one ``decision`` event.

    With ``session_id`` the event goes to that session's live buffer, which
    must exist; without it, to the vault's ``verdicts.jsonl``. A per-subject
    answer carries its subject and its action from ``actions``, else ``action``.
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
    append_event(_event_log(cfg, session_id), event, harness="")


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
