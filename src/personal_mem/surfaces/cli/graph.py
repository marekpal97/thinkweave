"""``mem graph`` — local graph rendering."""

from __future__ import annotations

import argparse

from personal_mem.core.config import load_config


def cmd_graph(args: argparse.Namespace) -> None:
    from personal_mem.retrieval.search import Search

    cfg = load_config()
    s = Search(config=cfg)

    if args.format == "mermaid":
        print(s.render_graph_mermaid(args.id, depth=args.depth))
    else:
        print(s.render_graph_text(args.id, depth=args.depth))
    s.close()
