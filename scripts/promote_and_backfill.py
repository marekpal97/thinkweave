"""One-off: promote all seeded candidates and backfill source `relates_to:` edges.

For each `cand-*-*.md` stub (not archived):
  1. Read the proposed essence and cluster_sources from the stub.
  2. Call `promote_candidate` → mints `thm-XXXX-{slug}.md`, removes the stub.
  3. Append the new `thm-XXXX` id to each cluster source's `relates_to:` frontmatter.

Also handles the existing canonical theme `ai-capex` (thm-45d301dc):
reads its assigned source ids from /tmp/seed_proposals_clean.json and
backfills `relates_to:` on those sources too.

Dry-run by default; --apply writes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

from personal_mem.core.config import load_config
from personal_mem.core.vault import parse_frontmatter, render_frontmatter
from personal_mem.synthesis.theme_candidates import promote_candidate


AI_CAPEX_THM_ID = "thm-45d301dc"


def _extract_proposed_essence(body: str) -> str:
    """Pull the text between '## Proposed essence' and the next '##' heading."""
    m = re.search(
        r"##\s*Proposed essence\s*\n\s*(.*?)(?=\n##\s|\Z)",
        body,
        re.DOTALL,
    )
    return m.group(1).strip() if m else ""


def _source_path_by_id(cfg, src_id: str) -> str | None:
    """Look up a source note's vault-relative path via SQLite."""
    import sqlite3

    conn = sqlite3.connect(cfg.index_db)
    row = conn.execute(
        "SELECT path FROM notes WHERE id = ?", (src_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _append_relates_to(path, thm_id: str, dry_run: bool) -> str:
    """Add `thm_id` to a note's `relates_to:` list. Returns status string."""
    text = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    rel = fm.get("relates_to") or []
    if isinstance(rel, str):
        rel = [rel] if rel else []
    if thm_id in rel:
        return "already-linked"
    rel = list(rel) + [thm_id]
    fm["relates_to"] = rel
    if dry_run:
        return "would-link"
    new_text = render_frontmatter(fm) + body
    path.write_text(new_text, encoding="utf-8")
    return "linked"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--proposals", default="/tmp/seed_proposals_clean.json")
    args = ap.parse_args()

    cfg = load_config()
    cdir = cfg.vault_root / "themes" / "_candidates"
    stubs = sorted(p for p in cdir.glob("cand-*.md") if p.is_file())

    # Track (thm_id, [source_ids]) pairs for backfill.
    pairs: list[tuple[str, list[str]]] = []

    print(f"=== Promoting {len(stubs)} candidates ===")
    for stub in stubs:
        text = stub.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        cand_id = fm["id"]
        slug = fm.get("proposed_slug") or fm.get("title") or cand_id
        essence = _extract_proposed_essence(body)
        sources = fm.get("cluster_sources") or []
        if isinstance(sources, str):
            sources = [s.strip() for s in sources.split(",") if s.strip()]

        if args.apply:
            new_path = promote_candidate(
                cfg,
                cand_id,
                title=slug,
                essence=essence,
                rebuild_index=False,
            )
            # Parse the new thm-id out of the created filename: thm-XXXX-slug.md
            thm_id = new_path.stem.split("-", 2)[0] + "-" + new_path.stem.split("-", 2)[1]
            print(f"  [promoted] {cand_id} → {thm_id}  ({slug}, {len(sources)} sources)")
        else:
            thm_id = "thm-DRYRUN-" + slug[:6]
            print(f"  [dry]      {cand_id} → would mint thm-XXXX  ({slug}, {len(sources)} sources)")
        pairs.append((thm_id, list(sources)))

    # ai-capex assignments from the proposals
    proposals = json.load(open(args.proposals))
    for p in proposals:
        if p["slug"] == "ai-capex":
            pairs.append((AI_CAPEX_THM_ID, list(p["source_ids"])))
            print(f"\n=== ai-capex (existing) ===")
            print(f"  Will backfill {AI_CAPEX_THM_ID} on {len(p['source_ids'])} sources")
            break

    print()
    print(f"=== Backfilling relates_to: edges ===")
    total_linked = 0
    total_already = 0
    total_missing = 0
    for thm_id, source_ids in pairs:
        for src_id in source_ids:
            rel_path = _source_path_by_id(cfg, src_id)
            if not rel_path:
                print(f"  [missing] {src_id} not in index — skipping")
                total_missing += 1
                continue
            path = cfg.vault_root / rel_path
            status = _append_relates_to(path, thm_id, dry_run=not args.apply)
            if status == "already-linked":
                total_already += 1
            elif status in ("linked", "would-link"):
                total_linked += 1

    print()
    print(f"Edges: linked={total_linked}, already={total_already}, missing={total_missing}")

    if args.apply:
        print()
        print("Re-indexing...")
        subprocess.run(["uv", "run", "mem", "index"], check=False)

    return 0


if __name__ == "__main__":
    sys.exit(main())
