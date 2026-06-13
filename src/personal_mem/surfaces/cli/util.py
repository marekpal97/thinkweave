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
    """Copy any default files from the package-bundled ``vault_templates/``
    into the vault if they don't already exist.

    Seeds ``vault/config/`` with the shipped templates: sources.yaml,
    PRIORITIES.yaml, scheduling.yaml. Feed registries (news / podcast
    outlets) live in PRIORITIES.yaml::intake as of Phase 3.1 — the
    standalone ``*_feeds.yaml`` stubs were retired 2026-06-13.
    """
    pkg_root = Path(__file__).resolve().parents[1].parent  # → .../src/personal_mem
    templates_dir = pkg_root / "vault_templates" / "config"
    target_dir = Path(vault_root) / "config"
    target_dir.mkdir(parents=True, exist_ok=True)
    for filename in (
        "sources.yaml",
        "PRIORITIES.yaml",
        "scheduling.yaml",
    ):
        source = templates_dir / filename
        target = target_dir / filename
        if source.exists() and not target.exists():
            shutil.copyfile(source, target)

    # Per-source note-format skeletons → vault/config/note_formats/. The research
    # writers Read these directly to shape their brief; the user edits them in
    # place (plain markdown in the vault, Obsidian-native). Seeded once at init,
    # then user-owned — survives upgrades like every other vault/config file.
    nf_src = pkg_root / "vault_templates" / "note_formats"
    if nf_src.exists():
        nf_dst = target_dir / "note_formats"
        nf_dst.mkdir(parents=True, exist_ok=True)
        for tpl in nf_src.glob("*.md"):
            target = nf_dst / tpl.name
            if not target.exists():
                shutil.copyfile(tpl, target)


def cmd_prune_orphans(args: argparse.Namespace) -> None:
    """Delete orphan session folders under the vault.

    Safety: defaults to dry-run unless ``--yes`` is passed. An orphan is a
    session folder with no derived notes/decisions, no real events.jsonl
    (< 500 bytes), empty ``files_touched``, empty ``commits``, older than
    ``--min-age`` seconds, and NOT the currently running session.
    """
    from personal_mem.operations.prune import find_orphans, prune_orphans

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
    from personal_mem.acquisition.sources import REGISTRY, all_specs, get_spec, load_user_specs
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

    Writes three artifacts and refuses on collisions. All three land in
    upgrade-safe homes (the vault, or the user's machine-global commands dir) —
    never inside the installed package:
      1. SourceTypeSpec entry → <vault>/config/source_types.yaml
      2. Skill file → ~/.claude/commands/<slug>.md (default --skill-target user;
         'repo' targets the contributor checkout's commands/)
      3. Default behaviour-config block → <vault>/config/sources.yaml (the live
         overlay the runtime reads), seeded from the template if absent, only if
         not already present for the slug
    """
    slug = args.slug.strip()
    if not slug:
        print("error: slug must not be empty.", file=sys.stderr)
        sys.exit(2)

    # --- collision checks (in-code REGISTRY + user-side overlay) ---
    if slug in registry:
        print(
            f"error: '{slug}' is already a built-in source type "
            f"(in src/personal_mem/acquisition/sources/registry.py). Pick a different slug.",
            file=sys.stderr,
        )
        sys.exit(1)
    existing_user = load_user_specs(vault_root)
    if slug in existing_user:
        print(
            f"error: '{slug}' is already declared in "
            f"{vault_root}/config/source_types.yaml. Edit that file directly to change it.",
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

    # --- artifact 1: <vault>/config/source_types.yaml ---
    user_yaml = Path(vault_root) / "config" / "source_types.yaml"
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

    # --- artifact 2: skill file ---
    skill_target = getattr(args, "skill_target", "user")
    pkg_root = Path(__file__).resolve().parents[1].parent  # .../src/personal_mem
    template_path = pkg_root / "vault_templates" / "source_skill_template.md"
    if skill_target == "user":
        commands_dir = Path.home() / ".claude" / "commands"
    elif skill_target == "repo":
        repo_root = pkg_root.parent.parent  # .../personal_mem
        commands_dir = repo_root / "commands"
    else:
        commands_dir = None

    if commands_dir is not None:
        skill_path = commands_dir / f"{slug}.md"
        if skill_path.exists():
            print(
                f"error: {skill_path} already exists. Refusing to overwrite. "
                f"(The config/source_types.yaml entry was still written.)",
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

    # --- artifact 3: behaviour-config block → <vault>/config/sources.yaml ---
    # Write to the vault overlay the runtime actually reads (load_user_config →
    # <vault>/config/sources.yaml), NOT the shipped package template. The template
    # is seed-only — a block written there never reaches the running system and is
    # clobbered on the next `mem`/plugin upgrade. Seed the vault file from the
    # template on first use so it has the sources:/projects: skeleton, then append.
    vault_sources = Path(vault_root) / "config" / "sources.yaml"
    if not vault_sources.exists():
        template_sources = pkg_root / "vault_templates" / "config" / "sources.yaml"
        vault_sources.parent.mkdir(parents=True, exist_ok=True)
        if template_sources.exists():
            shutil.copyfile(template_sources, vault_sources)
        else:
            vault_sources.write_text("sources:\n\nprojects:\n", encoding="utf-8")
    existing = vault_sources.read_text(encoding="utf-8")
    marker = f"\n  {slug}:\n"
    if marker not in existing and not existing.startswith(f"  {slug}:\n"):
        block = (
            f"  {slug}:\n"
            f"    drain_strategy: inline\n"
            f"    dedup_keys: [url, title]\n"
        )
        # Insert under the `sources:` mapping, just before the `projects:`
        # boundary if present; otherwise append.
        if "\nprojects:\n" in existing:
            head, _, tail = existing.partition("\nprojects:\n")
            head = head.rstrip("\n") + "\n" + block
            vault_sources.write_text(head + "\nprojects:\n" + tail, encoding="utf-8")
        else:
            vault_sources.write_text(
                existing.rstrip("\n") + "\n" + block, encoding="utf-8"
            )
        written.append(vault_sources)

    # --- confirmation ---
    print(f"Scaffolded source type '{slug}':")
    for p in written:
        print(f"  wrote {p}")
    print()
    print(
        f"Next: edit commands/{slug}.md to fill in the FETCH STRATEGY, then run "
        f"`mem sources show {slug}` to confirm."
    )
