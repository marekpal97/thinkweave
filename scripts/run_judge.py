"""Run mem_judge on imported decisions, from each project's git repo.

Usage: uv run python scripts/run_judge.py [--dry-run] [--project PROJECT]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from personal_mem.core.config import load_config
from personal_mem.core.indexer import Indexer
from personal_mem.synthesis.judge import evaluate_decision
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager

# Map vault project names to git repo directories
PROJECT_REPOS: dict[str, str] = {
    "thinkmesh_neural": "/home/marekpal97/python_projects/thinkmesh_neural",
    "hive_swarm": "/home/marekpal97/python_projects/hive_swarm",
    "options_engine": "/home/marekpal97/python_projects/options_engine",
    "research_assistant": "/home/marekpal97/python_projects/research_assistant",
}


def main():
    parser = argparse.ArgumentParser(description="Run mem_judge on imported decisions")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--project", "-p", default="")
    args = parser.parse_args()

    config = load_config()
    vm = VaultManager(config=config)

    # Get all imported decisions
    all_decisions = list(vm.list_notes(note_type=NoteType.DECISION, limit=500))
    imported_decisions = [
        d for d in all_decisions
        if d.frontmatter.get("imported_from") == "claude-mem"
    ]

    if args.project:
        imported_decisions = [d for d in imported_decisions if d.project == args.project]

    print(f"Found {len(imported_decisions)} imported decisions to judge.")

    # Group by project
    by_project: dict[str, list] = {}
    for dec in imported_decisions:
        by_project.setdefault(dec.project, []).append(dec)

    verdicts: dict[str, int] = {"kept": 0, "superseded": 0, "reverted": 0, "unknown": 0}
    judged = 0
    skipped = 0

    for project, decisions in sorted(by_project.items()):
        repo_dir = PROJECT_REPOS.get(project)
        if not repo_dir or not Path(repo_dir).is_dir():
            print(f"  {project}: no git repo, skipping {len(decisions)} decisions")
            skipped += len(decisions)
            continue

        # Change to repo dir so git commands work
        original_dir = os.getcwd()
        os.chdir(repo_dir)

        print(f"\n  {project} ({len(decisions)} decisions, repo: {repo_dir})")

        for dec in decisions:
            # Skip already-judged decisions
            if dec.frontmatter.get("verdict") and not args.dry_run:
                skipped += 1
                continue

            result = evaluate_decision(dec, all_decisions)
            verdict = result["verdict"]
            confidence = result["confidence"]
            evidence = result["evidence"]
            verdicts[verdict] += 1
            judged += 1

            if args.dry_run:
                print(f"    {dec.title[:60]:60s} → {verdict} ({confidence:.1f}) {evidence[:40]}")
                continue

            # Update frontmatter
            fm_updates: dict = {
                "verdict": verdict,
                "confidence": confidence,
                "judged_at": result["judged_at"],
            }
            if result["blame_lines"] >= 0:
                fm_updates["blame_lines"] = result["blame_lines"]
            if result.get("commit_refs"):
                fm_updates["commit_refs"] = result["commit_refs"]
                fm_updates["committed"] = True

            vm.update_note(
                config.vault_root / dec.path,
                frontmatter_updates=fm_updates,
            )

        os.chdir(original_dir)

    # Summary
    action = "Would judge" if args.dry_run else "Judged"
    print(f"\n── Summary ────────────────────────────────────────")
    print(f"  {action}: {judged}")
    print(f"  Skipped (no repo or already judged): {skipped}")
    for v, count in sorted(verdicts.items()):
        if count:
            print(f"  {v}: {count}")

    # Rebuild index if changes were made
    if not args.dry_run and judged > 0:
        print("  Rebuilding index...")
        idx = Indexer(config=config)
        idx.rebuild(full=True)
        idx.close()


if __name__ == "__main__":
    main()
