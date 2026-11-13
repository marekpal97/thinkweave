"""``weave queue`` — inspect per-source-type acquisition queues and enqueue into them."""

from __future__ import annotations

import argparse
import json
import sys

from thinkweave.core.config import load_config


def cmd_queue(args: argparse.Namespace) -> None:
    """Inspect per-source-type acquisition queues."""
    from thinkweave.acquisition.sources import all_specs
    from thinkweave.acquisition.sources.queue import Queue

    cfg = load_config()
    action = args.action
    source_type = (args.source_type or args.source_type_flag or "").strip()

    if action == "list":
        seen: set[str] = set()
        rows: list[tuple[str, int]] = []
        for spec in all_specs():
            if source_type and spec.slug != source_type:
                continue
            q = Queue.for_source_type(spec.slug, cfg.vault_root)
            seen.add(spec.slug)
            rows.append((spec.slug, len(q.peek(10_000))))
        queues_dir = cfg.vault_root / ".weave" / "queues"
        if queues_dir.exists():
            for child in sorted(queues_dir.glob("*.jsonl")):
                if child.stem in seen:
                    continue
                if source_type and child.stem != source_type:
                    continue
                q = Queue.for_source_type(child.stem, cfg.vault_root)
                rows.append((child.stem, len(q.peek(10_000))))
        if not rows:
            print("No queues found.")
            return
        print(f"{'SOURCE_TYPE':<20} {'COUNT':>8}")
        print("-" * 30)
        for slug, count in rows:
            print(f"{slug:<20} {count:>8}")
        return

    if action == "inspect":
        if not source_type:
            print("inspect requires a source_type. Usage: weave queue inspect <slug>")
            sys.exit(1)
        q = Queue.for_source_type(source_type, cfg.vault_root)
        items = q.peek(10_000)
        if not items:
            print(f"Queue '{source_type}' is empty.")
            return
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return

    if action == "enqueue":
        from thinkweave.operations.queue import enqueue

        if not source_type or not args.item:
            print("enqueue requires a source_type and --item '<json object>'.")
            sys.exit(1)
        print(enqueue(cfg, source_type, json.loads(args.item)).line())
        return

    if action == "peek":
        if not source_type:
            print("peek requires a source_type. Usage: weave queue peek <slug> [--n N]")
            sys.exit(1)
        q = Queue.for_source_type(source_type, cfg.vault_root)
        items = q.peek(args.n)
        if not items:
            print(f"Queue '{source_type}' is empty.")
            return
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return
