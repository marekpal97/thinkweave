"""One-off: mint candidate theme stubs from seeded proposals.

Reads /tmp/seed_proposals_clean.json + /tmp/event_corpus.json, mints one
candidate stub per proposal in vault/themes/_candidates/cand-XXXX-{slug}.md
matching the existing stub shape. Skips the existing canonical-theme slug
(handled separately) and the unassigned bucket.

Dry-run by default; --apply writes files and runs `mem index`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone

from personal_mem.core.config import load_config

EXISTING_THEME_SLUGS = {"ai-capex"}  # skip mint for these; they exist as thm-*


def _top_concepts(source_ids: list[str], corpus_index: dict, k: int = 3) -> list[str]:
    counter: Counter[str] = Counter()
    for sid in source_ids:
        for c in corpus_index.get(sid, {}).get("concepts", []) or []:
            counter[c] += 1
    return [c for c, _ in counter.most_common(k)]


def _stub_body(
    cand_id: str,
    slug: str,
    essence: str,
    source_entries: list[dict],
    cluster_concepts: list[str],
    today_iso: str,
) -> str:
    sources_block = "\n".join(
        f"- [[{e['id']}]] — {e['title']}" for e in source_entries
    )
    concepts_str = ", ".join(cluster_concepts) if cluster_concepts else "(none)"
    return f"""---
type: theme
id: {cand_id}
date: "{today_iso}"
candidacy: inferred-from-corpus-seed
status: candidate
cluster_size: {len(source_entries)}
cluster_sources: [{", ".join(e["id"] for e in source_entries)}]
cluster_concepts: [{", ".join(cluster_concepts)}]
proposed_slug: {slug}
aliases: [{cand_id}]
---

# Candidate: {slug}

## Proposed essence

{essence}

## Cluster

Seeded from corpus-wide pass over {len(source_entries)} event-grain sources sharing thematic focus. Top concepts: {concepts_str}.

{sources_block}

Promote with `mem themes promote-candidate {cand_id} --title {slug}` if this represents a real narrative arc; otherwise leave to age out, or delete the file.
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Write files (default: dry-run)")
    ap.add_argument(
        "--proposals",
        default="/tmp/seed_proposals_clean.json",
        help="Path to cleaned proposals JSON",
    )
    ap.add_argument(
        "--corpus",
        default="/tmp/event_corpus.json",
        help="Path to event corpus JSON (for concept lookup)",
    )
    args = ap.parse_args()

    cfg = load_config()
    cdir = cfg.vault_root / "themes" / "_candidates"

    proposals = json.load(open(args.proposals))
    corpus = json.load(open(args.corpus))
    corpus_index = {x["id"]: x for x in corpus}

    today = datetime.now(timezone.utc).isoformat()

    minted = 0
    skipped_existing = 0
    skipped_unassigned = 0
    for p in proposals:
        slug = p["slug"]
        if slug == "unassigned":
            skipped_unassigned = len(p["source_ids"])
            continue
        if slug in EXISTING_THEME_SLUGS:
            print(f"  [skip-existing] {slug}: {len(p['source_ids'])} sources → "
                  f"wire to existing thm- manually")
            skipped_existing += 1
            continue

        source_entries = [
            corpus_index[s] for s in p["source_ids"] if s in corpus_index
        ]
        cluster_concepts = _top_concepts(p["source_ids"], corpus_index)
        cand_id = f"cand-{uuid.uuid4().hex[:8]}"
        path = cdir / f"{cand_id}-{slug}.md"
        body = _stub_body(cand_id, slug, p["essence"], source_entries, cluster_concepts, today)

        if args.apply:
            cdir.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            print(f"  [wrote]  {path.name}  ({len(source_entries)} sources, concepts={cluster_concepts})")
        else:
            print(f"  [dry]    {path.name}  ({len(source_entries)} sources, concepts={cluster_concepts})")
        minted += 1

    print()
    print(f"Minted: {minted}  Skipped existing: {skipped_existing}  "
          f"Unassigned sources: {skipped_unassigned}")

    if args.apply and minted:
        print()
        print("Re-indexing...")
        subprocess.run(["uv", "run", "mem", "index"], check=False)

    return 0


if __name__ == "__main__":
    sys.exit(main())
