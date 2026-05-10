"""Decision operations — queries and judging over the decision corpus.

``mem_extract`` writes new decisions (via ``operations.extract``); this
module owns reads and the ``mem_judge`` mutation pass that scores existing
decisions against structural evidence and flips ``status`` accordingly.
"""

from __future__ import annotations

from personal_mem.core.config import Config


def list_by_file(
    cfg: Config,
    file_path: str,
    *,
    project: str = "",
    status: str = "",
    limit: int = 50,
):
    """Return decisions whose ``file_paths`` frontmatter touches the given file."""
    from personal_mem.retrieval.search import Search

    s = Search(config=cfg)
    try:
        return s.search_decisions_by_file(
            file_path, project=project, status=status, limit=limit
        )
    finally:
        s.close()


def judge(cfg: Config, *, decision_id: str = "", session_id: str = "", project: str = ""):
    """Evaluate decisions against structural evidence; return list of (note, result)."""
    from personal_mem.core.indexer import Indexer
    from personal_mem.core.vault import VaultManager
    from personal_mem.retrieval.search import Search
    from personal_mem.synthesis.judge import evaluate_decision, find_decisions

    vm = VaultManager(config=cfg)
    s = Search(config=cfg)
    idx = Indexer(config=cfg)
    target = []
    if decision_id:
        row = s.get_note_by_id(decision_id)
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
    from personal_mem.core.indexer import Indexer
    from personal_mem.core.vault import VaultManager

    results = judge(cfg, decision_id=decision_id, session_id=session_id, project=project)
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
