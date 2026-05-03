"""Decision operations — read-only queries over the decision corpus.

Mutation lives elsewhere: ``mem_extract`` writes new decisions; ``mem_judge``
writes verdicts/status. This module is the read-side seam.
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
