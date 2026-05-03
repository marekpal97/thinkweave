"""``mem concepts`` — list / merge / prune / hubs / drift / notes."""

from __future__ import annotations

import argparse

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
                concepts = fm.get("concepts", [])
                if isinstance(concepts, str):
                    concepts = [c.strip() for c in concepts.split(",") if c.strip()]
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
                    "\nDry run. Re-run with --apply to delete these files."
                )
                return

            for _, path in orphans:
                path.unlink()
            print(f"\nDeleted {len(orphans)} orphan hub(s).")
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
