"""``mem init`` / ``prune-orphans`` / ``sources`` — small administrative commands."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from personal_mem.core.config import load_config


def cmd_init(args: argparse.Namespace) -> None:
    from personal_mem.core.vault import VaultManager

    cfg = load_config()
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    _seed_vault_templates(cfg.vault_root)
    print(f"Vault initialized at {cfg.vault_root}")


def cmd_mcp(args: argparse.Namespace) -> None:
    """Run the personal_mem MCP server over stdio.

    Thin shim around ``personal_mem.surfaces.mcp.server.main``. Used by
    the Claude Code plugin shell (``plugin.json`` invokes ``mem mcp``).
    """
    from personal_mem.surfaces.mcp.server import main as mcp_main

    mcp_main()


def _seed_vault_templates(vault_root: Path) -> None:
    """Copy any default files from the package-bundled `vault_templates/`
    into the vault if they don't already exist. Currently seeds
    `.mem/sources.yaml`."""
    pkg_root = Path(__file__).resolve().parents[1].parent  # → .../src/personal_mem
    sources_template = pkg_root / "vault_templates" / ".mem" / "sources.yaml"
    if sources_template.exists():
        target = Path(vault_root) / ".mem" / "sources.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(sources_template, target)


def cmd_prune_orphans(args: argparse.Namespace) -> None:
    """Delete orphan session folders under the vault.

    Safety: defaults to dry-run unless ``--yes`` is passed. An orphan is a
    session folder with no derived notes/decisions, no real events.jsonl
    (< 500 bytes), empty ``files_touched``, empty ``commits``, older than
    ``--min-age`` seconds, and NOT the currently running session.
    """
    from personal_mem.prune import find_orphans, prune_orphans

    cfg = load_config()
    project = args.project or cfg.default_project or ""
    dry_run = not args.yes

    orphans = find_orphans(
        cfg,
        project=project,
        min_age_seconds=args.min_age,
    )

    if not orphans:
        print("No orphan sessions found.")
        return

    label = "Would delete" if dry_run else "Deleting"
    scope = f" in project '{project}'" if project else ""
    print(f"{label} {len(orphans)} orphan session folder(s){scope}:\n")
    for p in orphans[:30]:
        print(f"  {p.relative_to(cfg.vault_root)}")
    if len(orphans) > 30:
        print(f"  ... and {len(orphans) - 30} more")

    result = prune_orphans(orphans, dry_run=dry_run)
    mb = result.freed_bytes / (1024 * 1024)
    print(
        f"\n{'Would free' if dry_run else 'Freed'}: {mb:.1f} MB across "
        f"{len(orphans)} folders."
    )

    if dry_run:
        print("\n(Dry run — re-run with --yes to actually delete.)")
        return

    try:
        from personal_mem.core.indexer import Indexer

        idx = Indexer(config=cfg)
        removed = 0
        for session_dir in orphans:
            prefix = str(session_dir.relative_to(cfg.vault_root))
            removed += _remove_notes_by_path_prefix(idx, prefix)
        idx.db.commit()
        idx.close()
        print(f"Removed {removed} index row(s).")
    except Exception as e:
        print(f"Warning: index cleanup failed — run `mem index --full` to rebuild. ({e})")


def _remove_notes_by_path_prefix(idx, prefix: str) -> int:
    """Drop notes whose path starts with ``prefix`` from every index table."""
    rows = idx.db.execute(
        "SELECT id FROM notes WHERE path LIKE ?", (prefix + "%",)
    ).fetchall()
    note_ids = [r["id"] for r in rows]
    if not note_ids:
        return 0

    placeholders = ",".join("?" for _ in note_ids)
    idx.db.execute(
        f"DELETE FROM notes_fts WHERE id IN ({placeholders})", note_ids
    )
    idx.db.execute(
        f"DELETE FROM note_concepts WHERE note_id IN ({placeholders})", note_ids
    )
    idx.db.execute(
        f"DELETE FROM edges WHERE source IN ({placeholders}) "
        f"OR target IN ({placeholders})",
        note_ids + note_ids,
    )
    idx.db.execute(f"DELETE FROM notes WHERE id IN ({placeholders})", note_ids)
    return len(note_ids)


def cmd_sources(args: argparse.Namespace) -> None:
    from personal_mem.sources import all_specs, get_spec
    from personal_mem.surfaces.cli.skill import skills_for_source_type

    action = getattr(args, "sources_action", None) or "list"

    if action == "list":
        specs = all_specs()
        if not specs:
            print("No source types registered.")
            return
        print(f"{'SLUG':<14} {'BUCKET':<14} {'LAYOUT':<15} {'SKILLS':<24} DESCRIPTION")
        print("-" * 100)
        for spec in specs:
            skills = ", ".join(spec.skills) if spec.skills else "—"
            print(
                f"{spec.slug:<14} {spec.bucket:<14} {spec.layout:<15} "
                f"{skills:<24} {spec.description}"
            )
        print()
        print(
            "To add a new source type: edit src/personal_mem/sources/registry.py "
            "and copy commands/_source_template.md."
        )
        return

    if action == "show":
        slug = args.slug
        spec = get_spec(slug)
        if spec is None:
            print(f"No registered source type for '{slug}'.")
            print("Unregistered types still work — they land in sources/<slug>/source.md.")
            sys.exit(1)
        print(f"# {spec.slug}")
        print(f"bucket:       {spec.bucket}")
        print(f"layout:       {spec.layout}")
        print(f"aliases:      {', '.join(spec.aliases) if spec.aliases else '—'}")
        print(f"skills:       {', '.join(spec.skills) if spec.skills else '—'}")
        print(f"description:  {spec.description}")
        skills_found = skills_for_source_type(spec.slug)
        if skills_found:
            print()
            print("skill files handling this type:")
            for name, desc in skills_found:
                print(f"  /{name:<20} {desc}")
        return

    cmd_sources(argparse.Namespace(sources_action="list"))
