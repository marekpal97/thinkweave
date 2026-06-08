"""``mem concepts`` — list / merge / prune / hubs / drift / notes."""

from __future__ import annotations

import argparse

from personal_mem.core._utils import as_list
from personal_mem.core.config import load_config


def cmd_concepts(args: argparse.Namespace) -> None:
    from personal_mem.core.indexer import Indexer
    from personal_mem.synthesis.concepts import (
        get_all_concepts,
        load_aliases,
        merge_concept_in_notes,
        save_aliases,
    )

    cfg = load_config()

    action = args.concepts_action
    if not action:
        action = "list"

    if action == "list":
        idx = Indexer(config=cfg)
        concept_counts = get_all_concepts(idx.db)
        idx.close()

        prefix = args.prefix.lower() if hasattr(args, "prefix") else ""
        min_count = args.min_count if hasattr(args, "min_count") else 1

        filtered = sorted(
            ((c, n) for c, n in concept_counts.items()
             if n >= min_count and c.startswith(prefix)),
            key=lambda x: (-x[1], x[0]),
        )
        if not filtered:
            print("No concepts found.")
            return
        print(f"Concepts ({len(filtered)} total):\n")
        for concept, count in filtered:
            print(f"  {count:3d}  {concept}")

    elif action == "merge":
        from personal_mem.synthesis.concepts import delete_concept_hub

        from_c = args.from_concept.lower()
        to_c = args.to_concept.lower()
        if from_c == to_c:
            print("from and to concepts are the same.")
            return

        changed = merge_concept_in_notes(cfg.vault_root, from_c, to_c)

        aliases = load_aliases(cfg)
        existing = aliases.get(to_c, [])
        if from_c not in existing:
            existing.append(from_c)
        if from_c in aliases:
            for old in aliases.pop(from_c):
                if old != to_c and old not in existing:
                    existing.append(old)
        aliases[to_c] = existing
        save_aliases(cfg, aliases)

        hub_removed = delete_concept_hub(cfg, from_c)

        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()

        suffix = " Stale hub removed." if hub_removed else ""
        print(
            f"Merged '{from_c}' → '{to_c}': {changed} notes updated. "
            f"Alias saved. Index rebuilt.{suffix}"
        )

    elif action == "proposed-counts":
        from personal_mem.synthesis.concepts import get_all_proposed_concepts

        idx = Indexer(config=cfg)
        try:
            counts = get_all_proposed_concepts(idx.db)
        finally:
            idx.close()

        prefix = (getattr(args, "prefix", "") or "").lower()
        min_count = getattr(args, "min_count", 1)
        rows = sorted(
            ((c, n) for c, n in counts.items() if n >= min_count and c.startswith(prefix)),
            key=lambda x: (-x[1], x[0]),
        )
        if not rows:
            print("No proposed_concepts found.")
            return
        print(f"Proposed concepts ({len(rows)} matching):\n")
        for concept, count in rows:
            print(f"  {count:3d}  {concept}")

    elif action == "promote":
        from personal_mem.synthesis.concepts import promote_proposed_concept

        stats = promote_proposed_concept(cfg, args.concept, domain=args.domain)
        flags = []
        if stats["ontology_updated"]:
            flags.append("ontology updated")
        if stats["hub_created"]:
            flags.append("hub created")
        suffix = f" ({', '.join(flags)})" if flags else ""
        print(
            f"Promoted '{args.concept.lower()}' under '{args.domain}': "
            f"{stats['notes_modified']} notes shifted{suffix}."
        )

    elif action == "demote-non-ontology":
        from personal_mem.synthesis.concepts import demote_non_ontology_concepts

        stats = demote_non_ontology_concepts(cfg, dry_run=args.dry_run)
        verb = "Would demote" if args.dry_run else "Demoted"
        print(
            f"{verb} {stats['concepts_demoted']} concept occurrences "
            f"({len(stats['terms_demoted'])} distinct terms) from "
            f"{stats['files_modified']} files."
        )
        if args.dry_run and stats["terms_demoted"]:
            print("\nFirst 30 distinct terms moving to proposed_concepts:")
            for c in stats["terms_demoted"][:30]:
                print(f"  {c}")
            if len(stats["terms_demoted"]) > 30:
                print(f"  ... and {len(stats['terms_demoted']) - 30} more")
        elif not args.dry_run and stats["files_modified"] > 0:
            print("Index rebuilt.")
            archived = stats.get("hubs_archived") or []
            if archived:
                print(
                    f"Archived {len(archived)} orphan hub(s) to "
                    f"vault/concepts/topics/_archive/."
                )

    elif action == "consolidate-parents":
        from personal_mem.synthesis.concepts import consolidate_parent_leaf_concepts

        stats = consolidate_parent_leaf_concepts(cfg, dry_run=args.dry_run)
        verb = "Would drop" if args.dry_run else "Dropped"
        print(
            f"{verb} {stats['occurrences_dropped']} parent-occurrences "
            f"({len(stats['domains_touched'])} distinct domains) from "
            f"{stats['files_modified']} files."
        )
        if stats["domains_touched"]:
            print("\nDomains touched:")
            for d in stats["domains_touched"]:
                print(f"  {d}")
        if not args.dry_run and stats["files_modified"] > 0:
            print("Index rebuilt.")

    elif action == "prune-singletons":
        from personal_mem.synthesis.concepts import prune_noisy_singletons

        stats = prune_noisy_singletons(cfg, dry_run=args.dry_run)
        verb = "Would prune" if args.dry_run else "Pruned"
        print(
            f"Singletons: {stats['singletons']} total — "
            f"kept {stats['kept_ontology']} (ontology) + "
            f"{stats['kept_domain']} (domain markers), "
            f"removing {len(stats['removed'])}."
        )
        print(
            f"{verb} {stats['instances_removed']} concept instances from "
            f"{stats['files_modified']} files."
        )
        if args.dry_run and stats["removed"]:
            print("\nFirst 30 removals:")
            for c in stats["removed"][:30]:
                print(f"  {c}")
            if len(stats["removed"]) > 30:
                print(f"  ... and {len(stats['removed']) - 30} more")
        elif not args.dry_run and stats["files_modified"] > 0:
            print("Index rebuilt.")

    elif action == "prune":
        from personal_mem.synthesis.concepts import build_keep_set, load_ontology, prune_concepts

        ontology = load_ontology()
        if not ontology:
            print("No ontology.yaml found.")
            return

        keep_set = build_keep_set(ontology)
        print(f"Ontology defines {len(keep_set)} concepts across {len(ontology)} domains.")

        if args.dry_run:
            from personal_mem.core.vault import VaultManager, parse_frontmatter
            vm = VaultManager(config=cfg)
            would_remove = 0
            would_modify = 0
            for md_file in vm.root.rglob("*.md"):
                text = md_file.read_text(encoding="utf-8")
                fm, _ = parse_frontmatter(text)
                if not fm:
                    continue
                concepts = as_list(fm.get("concepts"))
                removed = sum(1 for c in concepts if c.lower() not in keep_set)
                if removed:
                    would_modify += 1
                    would_remove += removed
            print(f"Would modify {would_modify} files, removing {would_remove} concepts.")
            return

        stats = prune_concepts(cfg.vault_root, keep_set)
        print(f"Pruned {stats['concepts_removed']} concepts from {stats['files_modified']} files.")

        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()
        print("Index rebuilt.")

    elif action == "notes":
        from personal_mem.retrieval.search import Search

        s = Search(config=cfg)
        concept = args.concept.lower()
        project = args.project if hasattr(args, "project") else ""
        results = s.search_by_concept(concept, project=project, limit=50)
        s.close()

        if not results:
            print(f"No notes with concept '{concept}'.")
            return

        print(f"Notes with concept '{concept}' ({len(results)}):\n")
        for r in results:
            tag_str = f" [{', '.join(r.tags)}]" if r.tags else ""
            proj_str = f" | {r.project}" if r.project else ""
            print(f"  [{r.type}] {r.title} ({r.id}){tag_str}{proj_str}")

    elif action == "hubs":
        from personal_mem.synthesis.concepts import (
            add_hub_wikilinks,
            find_orphan_hubs,
            generate_concept_hub_skeletons,
            generate_domain_hubs,
            hubs_marker_path,
            load_ontology,
        )

        if getattr(args, "prune", False):
            from personal_mem.synthesis.concepts import archive_orphan_hubs

            orphans = find_orphan_hubs(cfg)
            if not orphans:
                print("No orphan hubs.")
                return

            print(f"Orphan hubs ({len(orphans)}):")
            for concept, path in orphans:
                rel = path.relative_to(cfg.vault_root)
                print(f"  {concept} → {rel}")

            if not getattr(args, "apply", False):
                print(
                    "\nDry run. Re-run with --apply to archive these files "
                    "to vault/concepts/topics/_archive/ (lossless — re-promotion "
                    "can move them back)."
                )
                return

            archived = archive_orphan_hubs(cfg)
            print(
                f"\nArchived {len(archived)} orphan hub(s) to "
                f"vault/concepts/topics/_archive/."
            )
            idx = Indexer(config=cfg)
            idx.rebuild(full=False)
            idx.close()
            return

        ontology = load_ontology()
        if not ontology:
            print("No ontology.yaml found.")
            return

        domain_hubs = generate_domain_hubs(cfg, ontology)
        print(f"Generated {len(domain_hubs)} domain hub(s) in vault/concepts/:")
        for domain, path in sorted(domain_hubs.items()):
            print(f"  {domain} → {path.name}")

        concept_hubs = generate_concept_hub_skeletons(cfg, ontology)
        print(
            f"\nEnsured {len(concept_hubs)} concept hub skeleton(s) in "
            "vault/concepts/topics/ (existing files preserved)."
        )

        modified = add_hub_wikilinks(cfg, ontology)
        print(f"\nAdded domain wikilinks to {modified} notes.")

        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()
        print("Index rebuilt.")

        marker = hubs_marker_path(cfg)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()

    elif action == "drift":
        from personal_mem.synthesis.concepts import (
            drift_report,
            find_redundant_hub_candidates,
            format_drift_report,
        )

        report = drift_report(
            cfg,
            project=args.project,
            threshold=args.threshold,
            max_items=args.max_items,
        )
        print(format_drift_report(report))

        if getattr(args, "hubs", False):
            jaccard = getattr(args, "hub_jaccard", 0.4)
            candidates = find_redundant_hub_candidates(cfg, min_jaccard=jaccard)
            print()
            if not candidates:
                print(
                    f"No redundant-hub candidates (Jaccard ≥ {jaccard:.2f})."
                )
            else:
                print(
                    f"Redundant-hub candidates (Jaccard ≥ {jaccard:.2f}): "
                    f"{len(candidates)} pair(s)"
                )
                for a, b, score in candidates[:args.max_items]:
                    print(
                        f"  {a} ↔ {b}  (Jaccard {score:.2f}) — "
                        f"review via `/mem-resolve-concepts`"
                    )
