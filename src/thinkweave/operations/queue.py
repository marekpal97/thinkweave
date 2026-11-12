"""Queue operations — thin pass-through to sources/queue.py.

Centralises the per-source-type queue API behind operation-level functions
that the CLI and MCP both call into. Enqueue applies two dedup checks: the
deterministic ``dedup_keys`` against the queue, then, for a concept-grain
type, the ``acquisition_dedup`` question set against the vault's sources.
"""

from __future__ import annotations

from thinkweave.core.config import Config
from thinkweave.core.decision_provider import DecisionProvider
from thinkweave.acquisition.sources import all_specs, get_spec, load_user_config
from thinkweave.acquisition.sources.queue import Queue, QueueItem
from thinkweave.operations.verdicts import ACQUISITION_DEDUP, judge_duplicate


def list_queues(cfg: Config) -> list[dict]:
    """Return [{source_type, count}, …] for every active or registered queue."""
    seen: set[str] = set()
    out: list[dict] = []
    for spec in all_specs():
        seen.add(spec.slug)
        q = Queue.for_source_type(spec.slug, cfg.vault_root)
        out.append({"source_type": spec.slug, "count": len(q.peek(10_000))})
    queues_dir = cfg.vault_root / ".weave" / "queues"
    if queues_dir.exists():
        for child in sorted(queues_dir.glob("*.jsonl")):
            if child.stem in seen:
                continue
            q = Queue.for_source_type(child.stem, cfg.vault_root)
            out.append({"source_type": child.stem, "count": len(q.peek(10_000))})
    return out


def peek(cfg: Config, source_type: str, n: int = 5) -> list[QueueItem]:
    q = Queue.for_source_type(source_type, cfg.vault_root)
    return q.peek(n)


def inspect(cfg: Config, source_type: str) -> list[QueueItem]:
    return peek(cfg, source_type, n=10_000)


def enqueue(
    cfg: Config,
    source_type: str,
    item: QueueItem | dict,
    *,
    provider: DecisionProvider | None = None,
) -> dict:
    """Enqueue ``item`` unless it duplicates a queued item or a vault source.

    Returns {"id": ..., "deduped": bool, "conflict": str|None}; ``conflict``
    is the queue item that collides on ``sources.<type>.dedup_keys``, else
    the source note the ``acquisition_dedup`` question set judged ``item`` a
    duplicate of. The ``item`` is a :class:`QueueItem`-shaped dict — see
    ``sources/queue.py`` for the documented field set.
    """
    sources_cfg = load_user_config(cfg.vault_root).get("sources", {})
    keys = sources_cfg.get(source_type, {}).get("dedup_keys") or []
    q = Queue.for_source_type(source_type, cfg.vault_root)
    conflict = q.dedup_check(item, keys) if keys else None
    conflict = conflict or _duplicated_source(cfg, source_type, item, provider)
    if conflict:
        return {"id": "", "deduped": True, "conflict": conflict}
    new_id = q.enqueue(item)
    return {"id": new_id, "deduped": False, "conflict": None}


def enqueue_line(result: dict) -> str:
    """One line reporting an :func:`enqueue` result."""
    if result["deduped"]:
        return f"duplicate of {result['conflict']}; not enqueued"
    return f"enqueued {result['id']}"


def _duplicated_source(
    cfg: Config, source_type: str, item: dict, provider: DecisionProvider | None
) -> str | None:
    """The source note a concept-grain ``item`` confidently duplicates.

    An event-grain item is never judged: repeated coverage of an event is signal.
    """
    spec = get_spec(source_type, cfg.vault_root)
    title = str(item.get("title") or "")
    if spec is None or spec.temporal_grain != "concept" or not title:
        return None
    policy = cfg.question_set(ACQUISITION_DEDUP)
    if not policy.enabled:
        return None
    from thinkweave.retrieval.search import Search

    s = Search(config=cfg)
    try:
        near = s.search(title, note_type="source", limit=policy.pool_size, any_term=True)
    finally:
        s.close()
    subject = "\n".join(str(item[k]) for k in ("title", "url", "summary") if item.get(k))
    verdict = judge_duplicate(
        cfg, subject, {r.id: r.title for r in near}, ACQUISITION_DEDUP,
        subject_id=str(item.get("url") or title), provider=provider,
    )
    return verdict.of if verdict and verdict.confident else None
