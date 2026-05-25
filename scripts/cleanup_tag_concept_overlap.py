"""Migrate tag/concept overlaps surfaced by `mem doctor`.

Buckets (deterministic, no LLM):
- B1 (move tag → concept): concept_count >= 5 * tag_count AND concept_count >= 5
- B2 (move concept → tag): term is in tag_vocabulary
- Tail: skipped (left for /mem-resolve-concepts to handle case-by-case)

Migration is information-preserving: when stripping a term from one field,
add it to the other field if not already present.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from personal_mem.synthesis.concepts import load_tag_vocabulary
from personal_mem.core.config import load_config
from personal_mem.core.indexer import Indexer
from personal_mem.core.vault import VaultManager, parse_frontmatter, render_frontmatter


SKIP_DIRS = {"templates", ".obsidian", ".trash"}


def _normalize_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v) for v in value]


def collect_overlaps(vault_root: Path) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for md in vault_root.rglob("*.md"):
        rel = md.relative_to(vault_root)
        if any(p in SKIP_DIRS for p in rel.parts):
            continue
        try:
            text = md.read_text(encoding="utf-8")
            fm, _ = parse_frontmatter(text)
        except Exception:
            continue
        for t in _normalize_list(fm.get("tags")):
            counts.setdefault(t, {"tag": 0, "concept": 0})["tag"] += 1
        for c in _normalize_list(fm.get("concepts")):
            counts.setdefault(c, {"tag": 0, "concept": 0})["concept"] += 1
    return {k: v for k, v in counts.items() if v["tag"] > 0 and v["concept"] > 0}


def bucketize(
    overlaps: dict[str, dict[str, int]],
    tag_vocab: set[str],
) -> tuple[set[str], set[str], list[tuple[str, int, int]]]:
    b1: set[str] = set()
    b2: set[str] = set()
    tail: list[tuple[str, int, int]] = []
    for term, c in overlaps.items():
        t_n, c_n = c["tag"], c["concept"]
        if term in tag_vocab:
            b2.add(term)
        elif c_n >= 5 * t_n and c_n >= 5:
            b1.add(term)
        elif c_n >= 3 * t_n and c_n >= 3:
            # Tail-pass: clear concept lean — move tag → concept
            b1.add(term)
        elif t_n >= 3 * c_n and t_n >= 3:
            # Tail-pass: clear tag lean — move concept → tag
            b2.add(term)
        else:
            tail.append((term, t_n, c_n))
    return b1, b2, tail


def migrate_note(
    path: Path, b1: set[str], b2: set[str]
) -> tuple[list[str], list[str]] | None:
    text = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    tags = _normalize_list(fm.get("tags"))
    concepts = _normalize_list(fm.get("concepts"))

    moved_t_to_c = [t for t in tags if t in b1]
    moved_c_to_t = [c for c in concepts if c in b2]
    if not moved_t_to_c and not moved_c_to_t:
        return None

    new_tags = [t for t in tags if t not in b1]
    new_concepts = list(concepts)
    for t in moved_t_to_c:
        if t not in new_concepts:
            new_concepts.append(t)

    new_concepts = [c for c in new_concepts if c not in b2]
    for c in moved_c_to_t:
        if c not in new_tags:
            new_tags.append(c)

    fm["tags"] = new_tags
    fm["concepts"] = new_concepts
    path.write_text(render_frontmatter(fm) + "\n\n" + body, encoding="utf-8")
    return moved_t_to_c, moved_c_to_t


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit changes (default: dry run)")
    parser.add_argument("--show", type=int, default=15, help="how many sample edits to print")
    args = parser.parse_args()

    cfg = load_config()
    vm = VaultManager(cfg)
    tag_vocab = load_tag_vocabulary()

    overlaps = collect_overlaps(vm.root)
    b1, b2, tail = bucketize(overlaps, tag_vocab)

    print(f"Vault: {vm.root}")
    print(f"Overlap terms: {len(overlaps)} (B1 strip-tag={len(b1)}, B2 strip-concept={len(b2)}, tail={len(tail)})")
    print(f"\nB1 (concept ≫ tag — move tag → concept):")
    for t in sorted(b1):
        print(f"  {t}  (tag={overlaps[t]['tag']}, concept={overlaps[t]['concept']})")
    print(f"\nB2 (term in tag_vocabulary — move concept → tag):")
    for t in sorted(b2):
        print(f"  {t}  (tag={overlaps[t]['tag']}, concept={overlaps[t]['concept']})")
    print(f"\nTail ({len(tail)} terms — left for /mem-resolve-concepts):")
    for t, tn, cn in sorted(tail, key=lambda x: -(x[1] + x[2]))[:args.show]:
        print(f"  {t}  (tag={tn}, concept={cn})")
    if len(tail) > args.show:
        print(f"  ... +{len(tail) - args.show} more")

    edits: list[tuple[Path, list[str], list[str]]] = []
    for md in vm.root.rglob("*.md"):
        rel = md.relative_to(vm.root)
        if any(p in SKIP_DIRS for p in rel.parts):
            continue
        try:
            if args.apply:
                result = migrate_note(md, b1, b2)
            else:
                text = md.read_text(encoding="utf-8")
                fm, _ = parse_frontmatter(text)
                tags = _normalize_list(fm.get("tags"))
                concepts = _normalize_list(fm.get("concepts"))
                t_to_c = [t for t in tags if t in b1]
                c_to_t = [c for c in concepts if c in b2]
                result = (t_to_c, c_to_t) if (t_to_c or c_to_t) else None
        except Exception as e:
            print(f"  ! skip {rel}: {e}", file=sys.stderr)
            continue
        if result is None:
            continue
        edits.append((md, result[0], result[1]))

    print(f"\n{'APPLIED' if args.apply else 'WOULD CHANGE'}: {len(edits)} notes "
          f"({sum(len(e[1]) for e in edits)} tag→concept moves, "
          f"{sum(len(e[2]) for e in edits)} concept→tag moves)")
    for path, t_to_c, c_to_t in edits[:args.show]:
        rel = path.relative_to(vm.root)
        bits = []
        if t_to_c:
            bits.append(f"tag→concept: {t_to_c}")
        if c_to_t:
            bits.append(f"concept→tag: {c_to_t}")
        print(f"  {rel}  —  {' / '.join(bits)}")
    if len(edits) > args.show:
        print(f"  ... +{len(edits) - args.show} more")

    if args.apply and edits:
        print("\nRebuilding index (full)...")
        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()
        print("Done.")
    elif not args.apply:
        print("\n[dry run; pass --apply to commit]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
