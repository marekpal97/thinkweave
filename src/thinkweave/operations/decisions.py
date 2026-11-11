"""Decision operations — queries and judging over the decision corpus.

``weave_extract`` writes new decisions (via ``operations.extract``); this
module owns reads and the ``weave_judge`` mutation pass that scores existing
decisions against structural evidence and flips ``status`` accordingly.
"""

from __future__ import annotations

from thinkweave.core.config import Config


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
