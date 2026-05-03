"""``mem landing`` — regenerate landing documents."""

from __future__ import annotations

import argparse
import sys

from personal_mem.core.config import load_config


def cmd_landing(args: argparse.Namespace) -> None:
    from personal_mem.synthesis.landing import write_landing_docs

    cfg = load_config()
    project = args.project or cfg.default_project

    if args.doc != "themes" and not project:
        print("Project name required. Use --project or set PERSONAL_MEM_PROJECT.")
        sys.exit(1)

    written = write_landing_docs(cfg, project, docs=args.doc)
    for filename, path in written.items():
        print(f"  {filename} → {path.relative_to(cfg.vault_root)}")
    print(f"Generated {len(written)} landing document(s).")
