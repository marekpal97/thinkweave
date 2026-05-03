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
    from personal_mem.sources import REGISTRY, all_specs, get_spec, load_user_specs
    from personal_mem.surfaces.cli.skill import skills_for_source_type

    action = getattr(args, "sources_action", None) or "list"
    cfg = load_config()
    vault_root = cfg.vault_root

    if action == "list":
        specs = all_specs(vault_root=vault_root)
        if not specs:
            print("No source types registered.")
            return
        user_slugs = set(load_user_specs(vault_root))
        print(f"{'SLUG':<14} {'BUCKET':<14} {'LAYOUT':<15} {'SKILLS':<24} {'ORIGIN':<8} DESCRIPTION")
        print("-" * 110)
        for spec in specs:
            skills = ", ".join(spec.skills) if spec.skills else "—"
            origin = "user" if spec.slug in user_slugs else "core"
            print(
                f"{spec.slug:<14} {spec.bucket:<14} {spec.layout:<15} "
                f"{skills:<24} {origin:<8} {spec.description}"
            )
        print()
        print(
            "To add a new source type: run `mem sources scaffold <slug> "
            "--bucket <name> --layout {flat|folder|author_folder}`."
        )
        return

    if action == "show":
        slug = args.slug
        spec = get_spec(slug, vault_root=vault_root)
        if spec is None:
            print(f"No registered source type for '{slug}'.")
            print("Unregistered types still work — they land in sources/<slug>/source.md.")
            sys.exit(1)
        origin = "user" if slug in load_user_specs(vault_root) else "core"
        print(f"# {spec.slug}")
        print(f"origin:       {origin}")
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

    if action == "scaffold":
        _cmd_sources_scaffold(args, vault_root, REGISTRY, load_user_specs)
        return

    cmd_sources(argparse.Namespace(sources_action="list"))


def _cmd_sources_scaffold(
    args: argparse.Namespace,
    vault_root: Path,
    registry: dict,
    load_user_specs,
) -> None:
    """Register a new source type via the vault-side YAML overlay.

    Writes three artifacts and refuses on collisions:
      1. SourceTypeSpec entry → <vault>/.mem/source_types.yaml
      2. Skill file → commands/<slug>.md  (from the parametrized template)
      3. Default config block → vault_templates/.mem/sources.yaml (only if
         not already present for the slug)
    """
    slug = args.slug.strip()
    if not slug:
        print("error: slug must not be empty.", file=sys.stderr)
        sys.exit(2)

    # --- collision checks (in-code REGISTRY + user-side overlay) ---
    if slug in registry:
        print(
            f"error: '{slug}' is already a built-in source type "
            f"(in src/personal_mem/sources/registry.py). Pick a different slug.",
            file=sys.stderr,
        )
        sys.exit(1)
    existing_user = load_user_specs(vault_root)
    if slug in existing_user:
        print(
            f"error: '{slug}' is already declared in "
            f"{vault_root}/.mem/source_types.yaml. Edit that file directly to change it.",
            file=sys.stderr,
        )
        sys.exit(1)

    bucket = args.bucket.strip()
    layout = args.layout
    description = args.description.strip()
    aliases_raw = args.aliases.strip()
    aliases = (
        [a.strip() for a in aliases_raw.split(",") if a.strip()]
        if aliases_raw
        else []
    )

    written: list[Path] = []

    # --- artifact 1: <vault>/.mem/source_types.yaml ---
    user_yaml = Path(vault_root) / ".mem" / "source_types.yaml"
    user_yaml.parent.mkdir(parents=True, exist_ok=True)
    block_lines = [
        f"{slug}:",
        f"  bucket: {bucket}",
        f"  layout: {layout}",
    ]
    if description:
        # Quote the description so commas/colons inside don't confuse the parser.
        escaped = description.replace('"', '\\"')
        block_lines.append(f'  description: "{escaped}"')
    if aliases:
        block_lines.append(f"  aliases: [{', '.join(aliases)}]")
    block = "\n".join(block_lines) + "\n"

    if user_yaml.exists():
        existing = user_yaml.read_text(encoding="utf-8").rstrip("\n") + "\n\n"
        user_yaml.write_text(existing + block, encoding="utf-8")
    else:
        header = (
            "# personal_mem user-side source-type registry\n"
            "# Each top-level key is a source_type slug. Edit by hand to\n"
            "# change bucket/layout/aliases, or run `mem sources scaffold` again.\n"
            "\n"
        )
        user_yaml.write_text(header + block, encoding="utf-8")
    written.append(user_yaml)

    # --- artifact 2: commands/<slug>.md ---
    pkg_root = Path(__file__).resolve().parents[1].parent  # .../src/personal_mem
    template_path = pkg_root / "vault_templates" / "source_skill_template.md"
    repo_root = pkg_root.parent.parent  # .../personal_mem
    commands_dir = repo_root / "commands"
    skill_path = commands_dir / f"{slug}.md"
    if skill_path.exists():
        print(
            f"error: {skill_path} already exists. Refusing to overwrite. "
            f"(The .mem/source_types.yaml entry was still written.)",
            file=sys.stderr,
        )
        sys.exit(1)
    commands_dir.mkdir(parents=True, exist_ok=True)
    template = template_path.read_text(encoding="utf-8")
    rendered = template.format(
        slug=slug,
        bucket=bucket,
        layout=layout,
        description=description or f"Ingest {slug} entries into the vault.",
    )
    skill_path.write_text(rendered, encoding="utf-8")
    written.append(skill_path)

    # --- artifact 3: vault_templates/.mem/sources.yaml default block ---
    template_sources = pkg_root / "vault_templates" / ".mem" / "sources.yaml"
    if template_sources.exists():
        existing = template_sources.read_text(encoding="utf-8")
        marker = f"\n  {slug}:\n"
        if marker not in existing and not existing.startswith(f"  {slug}:\n"):
            block = (
                f"  {slug}:\n"
                f"    drain_strategy: inline\n"
                f"    dedup_keys: [url, title]\n"
            )
            # Insert under the `sources:` mapping. The template ships with
            # a `projects:` block immediately after the sources map; insert
            # our block just before that boundary if we can find it,
            # otherwise append.
            if "\nprojects:\n" in existing:
                head, _, tail = existing.partition("\nprojects:\n")
                head = head.rstrip("\n") + "\n" + block
                template_sources.write_text(
                    head + "\nprojects:\n" + tail, encoding="utf-8"
                )
            else:
                template_sources.write_text(
                    existing.rstrip("\n") + "\n" + block, encoding="utf-8"
                )
            written.append(template_sources)

    # --- confirmation ---
    print(f"Scaffolded source type '{slug}':")
    for p in written:
        print(f"  wrote {p}")
    print()
    print(
        f"Next: edit commands/{slug}.md to fill in the FETCH STRATEGY, then run "
        f"`mem sources show {slug}` to confirm."
    )
