"""``mem_queue`` — list / inspect / peek / enqueue / archive per-source-type queues."""

from __future__ import annotations

import json

from personal_mem.core.config import Config


def tool_schemas() -> list:
    from mcp.types import Tool

    return [
        Tool(
            name="mem_queue",
            description=(
                "Single MCP tool for the per-source-type acquisition "
                "queues backing /research and /drain.\n\n"
                "Actions:\n"
                "  list              — show all queues with item counts\n"
                "  inspect           — full listing for one source_type\n"
                "  peek              — first N items for one source_type\n"
                "  enqueue           — add a new item to a queue\n"
                "  archive           — move a claimed item to the dated archive\n\n"
                "Queues live at vault/.mem/queues/<source_type>.jsonl."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "inspect", "peek", "enqueue", "archive"],
                    },
                    "source_type": {
                        "type": "string",
                        "description": "Queue slug (paper, repo, article, …). Required for inspect/peek/enqueue/archive.",
                    },
                    "n": {
                        "type": "integer",
                        "default": 5,
                        "description": "With peek: number of items to return.",
                    },
                    "item": {
                        "type": "object",
                        "description": "With enqueue: the dict to enqueue (url, title, …).",
                    },
                    "item_id": {
                        "type": "string",
                        "description": "With archive: the queue item id to move into the dated archive.",
                    },
                    "status": {
                        "type": "string",
                        "default": "done",
                        "description": "With archive: status stamped onto the archived row (done/failed/rejected/duplicate/…).",
                    },
                    "reason": {
                        "type": "string",
                        "description": "With archive: optional explanation (e.g. worker rejection reason). Preserved on the archived record.",
                    },
                },
                "required": ["action"],
            },
        ),
    ]


def handle(cfg: Config, args: dict):
    from mcp.types import TextContent

    from personal_mem.acquisition.sources import all_specs, load_user_config
    from personal_mem.acquisition.sources.queue import Queue

    action = args.get("action", "")
    source_type = args.get("source_type", "") or ""

    if action == "list":
        seen: set[str] = set()
        payload: list[dict] = []
        for spec in all_specs():
            seen.add(spec.slug)
            q = Queue.for_source_type(spec.slug, cfg.vault_root)
            payload.append({
                "source_type": spec.slug,
                "count": len(q.peek(10_000)),
            })
        queues_dir = cfg.vault_root / ".mem" / "queues"
        if queues_dir.exists():
            for child in sorted(queues_dir.glob("*.jsonl")):
                if child.stem in seen:
                    continue
                q = Queue.for_source_type(child.stem, cfg.vault_root)
                payload.append({
                    "source_type": child.stem,
                    "count": len(q.peek(10_000)),
                })
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]

    if action == "inspect":
        if not source_type:
            return [TextContent(type="text", text="inspect requires source_type")]
        q = Queue.for_source_type(source_type, cfg.vault_root)
        return [TextContent(
            type="text",
            text=json.dumps(q.peek(10_000), indent=2, ensure_ascii=False),
        )]

    if action == "peek":
        if not source_type:
            return [TextContent(type="text", text="peek requires source_type")]
        q = Queue.for_source_type(source_type, cfg.vault_root)
        n = int(args.get("n", 5))
        return [TextContent(
            type="text",
            text=json.dumps(q.peek(n), indent=2, ensure_ascii=False),
        )]

    if action == "enqueue":
        if not source_type:
            return [TextContent(type="text", text="enqueue requires source_type")]
        item = args.get("item") or {}
        if not isinstance(item, dict):
            return [TextContent(type="text", text="enqueue requires item dict")]
        sources_cfg = load_user_config(cfg.vault_root).get("sources", {})
        keys = sources_cfg.get(source_type, {}).get("dedup_keys") or []
        q = Queue.for_source_type(source_type, cfg.vault_root)
        conflict = q.dedup_check(item, keys) if keys else None
        if conflict:
            return [TextContent(
                type="text",
                text=f"duplicate of {conflict}; not enqueued",
            )]
        new_id = q.enqueue(item)
        return [TextContent(type="text", text=f"enqueued {new_id}")]

    if action == "archive":
        if not source_type:
            return [TextContent(type="text", text="archive requires source_type")]
        item_id = args.get("item_id", "") or ""
        if not item_id:
            return [TextContent(type="text", text="archive requires item_id")]
        status = args.get("status") or "done"
        reason = args.get("reason") or None
        q = Queue.for_source_type(source_type, cfg.vault_root)
        q.archive(item_id, status, reason=reason)
        suffix = f" reason={reason!r}" if reason else ""
        return [TextContent(
            type="text",
            text=f"archived {item_id} with status={status}{suffix}",
        )]

    return [TextContent(type="text", text=f"Unknown queue action: {action}")]
