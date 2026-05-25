"""Queue operations — thin pass-through to sources/queue.py.

Centralises the per-source-type queue API behind operation-level functions
that the CLI and MCP both call into. Auto-applies dedup checks on enqueue.
"""

from __future__ import annotations

from personal_mem.core.config import Config
from personal_mem.sources import all_specs, load_user_config
from personal_mem.sources.queue import Queue


def list_queues(cfg: Config) -> list[dict]:
    """Return [{source_type, count}, …] for every active or registered queue."""
    seen: set[str] = set()
    out: list[dict] = []
    for spec in all_specs():
        seen.add(spec.slug)
        q = Queue.for_source_type(spec.slug, cfg.vault_root)
        out.append({"source_type": spec.slug, "count": len(q.peek(10_000))})
    queues_dir = cfg.vault_root / ".mem" / "queues"
    if queues_dir.exists():
        for child in sorted(queues_dir.glob("*.jsonl")):
            if child.stem in seen:
                continue
            q = Queue.for_source_type(child.stem, cfg.vault_root)
            out.append({"source_type": child.stem, "count": len(q.peek(10_000))})
    return out


def peek(cfg: Config, source_type: str, n: int = 5) -> list[dict]:
    q = Queue.for_source_type(source_type, cfg.vault_root)
    return q.peek(n)


def inspect(cfg: Config, source_type: str) -> list[dict]:
    return peek(cfg, source_type, n=10_000)


def enqueue(cfg: Config, source_type: str, item: dict) -> dict:
    """Enqueue with auto dedup-check from sources.yaml::sources.<type>.dedup_keys.

    Returns {"id": ..., "deduped": bool, "conflict": str|None}.
    """
    sources_cfg = load_user_config(cfg.vault_root).get("sources", {})
    keys = sources_cfg.get(source_type, {}).get("dedup_keys") or []
    q = Queue.for_source_type(source_type, cfg.vault_root)
    conflict = q.dedup_check(item, keys) if keys else None
    if conflict:
        return {"id": "", "deduped": True, "conflict": conflict}
    new_id = q.enqueue(item)
    return {"id": new_id, "deduped": False, "conflict": None}
