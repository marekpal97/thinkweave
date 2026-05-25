"""``mem themes`` — candidate scan, stale archival, candidate promotion."""

from __future__ import annotations

import argparse

from personal_mem.core.config import load_config


def cmd_themes(args: argparse.Namespace) -> None:
    cfg = load_config()
    action = getattr(args, "themes_action", "") or ""

    if action == "scan-candidates":
        from personal_mem.synthesis.theme_candidates import (
            DEFAULT_MIN_CLUSTER_SIZE,
            DEFAULT_MIN_SHARED_CONCEPTS,
            DEFAULT_RECENT_DAYS,
            scan_candidates,
        )

        outcome = scan_candidates(
            cfg,
            source_type=args.source_type or "",
            recent_days=args.recent_days or DEFAULT_RECENT_DAYS,
            min_cluster_size=args.min_cluster_size or DEFAULT_MIN_CLUSTER_SIZE,
            min_shared_concepts=args.min_shared_concepts
            or DEFAULT_MIN_SHARED_CONCEPTS,
            dry_run=args.dry_run,
        )
        verb = "Would create" if args.dry_run else "Created"
        print(
            f"Inspected {outcome.sources_inspected} recent event-grain "
            f"sources. {verb} {len(outcome.candidates_created)} candidate(s); "
            f"skipped {outcome.clusters_skipped_covered} already-covered, "
            f"{outcome.clusters_skipped_existing_candidate} duplicate."
        )
        for path in outcome.candidates_created:
            print(f"  {path}")

    elif action == "archive-stale-candidates":
        from personal_mem.synthesis.theme_candidates import (
            DEFAULT_STALE_DAYS,
            archive_stale_candidates,
        )

        moved = archive_stale_candidates(
            cfg,
            stale_days=args.stale_days or DEFAULT_STALE_DAYS,
            dry_run=args.dry_run,
        )
        verb = "Would archive" if args.dry_run else "Archived"
        print(f"{verb} {len(moved)} stale candidate(s).")
        for p in moved:
            print(f"  {p.name}")

    elif action == "promote-candidate":
        import re as _re

        from personal_mem.core.vault import parse_frontmatter
        from personal_mem.synthesis.theme_candidates import (
            _candidates_dir,
            promote_candidate,
        )

        title = args.title or ""
        essence = args.essence or ""
        # Auto-default title/essence from the stub when caller didn't pass
        # them — lets LLM-named candidates (carrying `proposed_slug:` and
        # `## Proposed essence`) promote with just the candidate id.
        if not title or not essence:
            cdir = _candidates_dir(cfg)
            matches = list(cdir.glob(f"{args.candidate_id}-*.md"))
            if matches:
                stub_text = matches[0].read_text(encoding="utf-8")
                fm, body = parse_frontmatter(stub_text)
                if not title:
                    title = str(fm.get("proposed_slug", "")).strip()
                if not essence:
                    m = _re.search(
                        r"##\s*Proposed essence\s*\n+(.*?)(?=\n##\s|\Z)",
                        body,
                        _re.DOTALL,
                    )
                    if m:
                        essence = m.group(1).strip()

        if not title:
            print(
                f"error: --title required (stub {args.candidate_id} has no "
                "`proposed_slug:` to default from)"
            )
            return

        path = promote_candidate(
            cfg,
            args.candidate_id,
            title=title,
            essence=essence,
            project=args.project or "",
            parent=getattr(args, "parent", "") or "",
        )
        print(f"Promoted {args.candidate_id} → {path}")

    elif action == "rebuild-registry":
        from personal_mem.synthesis.theme_registry import rebuild

        n = rebuild(cfg)
        print(f"Rebuilt themes.yaml with {n} entries.")

    else:
        print("Usage: mem themes {scan-candidates|archive-stale-candidates|promote-candidate|rebuild-registry}")
