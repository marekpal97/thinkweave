"""Assign domain concepts to imported claude-mem notes based on title/content patterns.

Reads all imported notes from the vault index, assigns concepts using
keyword-based matching, then updates frontmatter and rebuilds the index.

Usage: uv run python scripts/enrich_concepts.py [--dry-run] [--project PROJECT]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from personal_mem.core.config import load_config
from personal_mem.core.indexer import Indexer
from personal_mem.core.vault import VaultManager, parse_frontmatter, render_frontmatter

# ── Concept keyword rules ───────────────────────────────────────────
# Each rule: (concept_name, [keyword_patterns])
# Patterns are matched case-insensitively against title + subtitle.
# Use word boundaries where precision matters.

GLOBAL_RULES: list[tuple[str, list[str]]] = [
    # Infrastructure
    ("sqlite", [r"\bsqlite\b", r"\bfts5?\b", r"\bwal\b"]),
    ("git", [r"\bgit\b", r"\bcommit\b", r"\bbranch\b", r"\bworktree\b", r"\bblame\b", r"\bstash\b"]),
    ("pytest", [r"\bpytest\b", r"\btest suite\b", r"\bunit test", r"\btest coverage\b", r"\btests? pass"]),
    ("cli", [r"\bcli\b", r"\bcommand.line\b", r"\bargparse\b", r"\bsubcommand"]),
    ("mcp", [r"\bmcp\b"]),
    ("api", [r"\bapi\b", r"\bendpoint", r"\brest\b"]),

    # Data / Storage
    ("parquet", [r"\bparquet\b"]),
    ("pandas", [r"\bpandas\b", r"\bdataframe\b"]),
    ("chromadb", [r"\bchromadb?\b", r"\bvector.?store\b", r"\bchroma\b"]),
    ("embeddings", [r"\bembedding", r"\bvector.?search\b", r"\bsemantic.?search\b", r"\bcosine"]),

    # ML / Neural
    ("training", [r"\btraining\b", r"\btrain(?:er|ing)\b", r"\bepoch\b", r"\bloss\b", r"\bconvergence\b"]),
    ("wandb", [r"\bwandb\b", r"\bw&b\b", r"\bweights.?(?:and|&).?biases\b"]),
    ("attention", [r"\battention\b"]),
    ("gradient", [r"\bgradient\b", r"\bbackprop\b"]),
    ("checkpoint", [r"\bcheckpoint\b"]),

    # Frameworks
    ("langgraph", [r"\blanggraph\b"]),
    ("langchain", [r"\blangchain\b"]),
    ("gradio", [r"\bgradio\b"]),
    ("streamlit", [r"\bstreamlit\b"]),
    ("obsidian", [r"\bobsidian\b"]),

    # Claude / AI
    ("claude-code", [r"\bclaude.?code\b", r"\bclaude.?cli\b"]),
    ("hooks", [r"\bhook\b", r"\bpretooluse\b", r"\bposttooluse\b"]),
    ("llm", [r"\bllm\b", r"\blarge.?language\b", r"\bclaude\b(?!.?code)", r"\bgpt\b", r"\banthropic\b"]),

    # DevOps
    ("tmux", [r"\btmux\b"]),
    ("docker", [r"\bdocker\b", r"\bcontainer(?:ized)?\b"]),
]

PROJECT_RULES: dict[str, list[tuple[str, list[str]]]] = {
    "options_engine": [
        ("ibkr", [r"\bibkr\b", r"\binteractive.?broker", r"\bib.?insync\b"]),
        ("volatility-surface", [r"\bvolatility\b", r"\bivol\b", r"\biv\b", r"\bhv\b", r"\bvol\b", r"\bsmile\b", r"\bskew\b"]),
        ("options-strategy", [r"\bstrategy\b", r"\bstraddle\b", r"\biron.?condor\b", r"\bspread\b", r"\boption"]),
        ("pipeline", [r"\bpipeline\b", r"\borchestrat"]),
        ("statusline", [r"\bstatusline\b", r"\bstatus.?line\b"]),
        ("market-data", [r"\bmarket.?data\b", r"\byfinance\b", r"\bfred\b", r"\brisk.?free"]),
    ],
    "hive_swarm": [
        ("dag", [r"\bdag\b", r"\bdirected.?acyclic"]),
        ("captain", [r"\bcaptain\b", r"\bbriefing\b"]),
        ("cell-lifecycle", [r"\bcell\b", r"\bspawn\b", r"\blifecycle\b", r"\bworker\b"]),
        ("event-system", [r"\bevent\b", r"\bcorrelat", r"\banomaly\b", r"\balert"]),
        ("rlvr", [r"\brlvr\b", r"\breinforcement\b"]),
        ("agent-lens", [r"\bagent.?lens\b"]),
        ("knowledge-graph", [r"\bknowledge.?(?:graph|store)\b", r"\btemporal.?(?:graph|link)"]),
        ("companion-mode", [r"\bcompanion\b"]),
        ("multi-project", [r"\bmulti.?(?:project|hive)\b"]),
        ("decision-tracking", [r"\bdecision\b(?!.*status)", r"\bjudg(?:e|ment)\b", r"\bverdict\b"]),
        ("log-rotation", [r"\blog.?rotation\b", r"\bsummariz"]),
        ("smoke-test", [r"\bsmoke.?test\b"]),
    ],
    "thinkmesh_neural": [
        ("novelty-detection", [r"\bnovelty\b", r"\banomal"]),
        ("gate-mechanism", [r"\bgate\b", r"\buniform"]),
        ("decoder", [r"\bdecoder\b", r"\bbasis.?decoder\b", r"\bresidual"]),
        ("state-update", [r"\bstate\b.*\b(?:update|norm|bound|ceiling)\b", r"\bgru\b"]),
        ("autoresearch", [r"\bautoresearch\b", r"\bauto.?research\b"]),
        ("concept-data", [r"\bconcept\b(?!.*merge)", r"\bchunk", r"\bsibling"]),
        ("diagnostics", [r"\bdiagnostic", r"\bmetric"]),
        ("colab", [r"\bcolab\b", r"\bnotebook\b"]),
        ("v1-model", [r"\bv1\b"]),
        ("v2-model", [r"\bv2\b"]),
        ("v3-model", [r"\bv3\b"]),
        ("v4-model", [r"\bv4\b"]),
        ("paradigm", [r"\bparadigm\b"]),
        ("ranking", [r"\branking\b", r"\bhuman.?eval", r"\bannotat"]),
        ("sprint", [r"\bsprint\b"]),
        ("fd-baseline", [r"\bfd\b", r"\bfinite.?diff", r"\bbaseline\b"]),
        ("linear-issues", [r"\blinear\b", r"\bmar-\d+\b"]),
        ("manifold", [r"\bmanifold\b", r"\bprojection\b", r"\bsubspace\b"]),
    ],
    "code_graph": [
        ("tree-sitter", [r"\btree.?sitter\b", r"\bast\b"]),
        ("neo4j", [r"\bneo4j\b"]),
        ("directory-hierarchy", [r"\bdirectory\b"]),
        ("code-navigation", [r"\bnavigat", r"\btraversa", r"\bcontext.?build"]),
        ("graph-schema", [r"\bschema\b", r"\bnode.?type\b", r"\bedge.?type\b"]),
        ("indexing", [r"\bindex\b", r"\bincremental\b"]),
    ],
    "personal_finance_assistant": [
        ("transaction-pipeline", [r"\btransaction\b", r"\bingestion\b", r"\bcsv\b", r"\bpipeline\b"]),
        ("classifier", [r"\bclassif", r"\bcategor", r"\brule.?based\b", r"\bmerchant"]),
        ("dashboard", [r"\bdashboard\b", r"\bplotly\b", r"\bvisualiz"]),
        ("savings", [r"\bsavings?\b", r"\bbalance\b"]),
        ("rag", [r"\brag\b", r"\bretrieval\b", r"\bvector.?store\b"]),
        ("langsmith", [r"\blangsmith\b"]),
    ],
    "research_assistant": [
        ("knowledge-graph", [r"\bknowledge.?graph\b", r"\bdag\b"]),
        ("hive-integration", [r"\bhive\b"]),
        ("discovery-loop", [r"\bdiscovery\b", r"\barxiv\b", r"\bsemantic.?scholar"]),
        ("memory-backend", [r"\bmemory.?backend\b", r"\bstorage.?layer\b"]),
        ("paper-management", [r"\bpaper\b", r"\bcitation\b"]),
    ],
    "_claude_config": [
        ("skills", [r"\bskill\b"]),
        ("wandb", [r"\bwandb\b", r"\bw&b\b"]),
        ("wrap", [r"\bwrap\b"]),
        ("project-plan", [r"\bproject.?plan\b"]),
        ("memory-system", [r"\bmemory\b"]),
        ("linear", [r"\blinear\b"]),
        ("ralph-loop", [r"\bralph\b"]),
    ],
    "_unscoped": [
        # These are mixed, so use broader hive/thinkmesh/captain rules
        ("dag", [r"\bdag\b"]),
        ("captain", [r"\bcaptain\b", r"\bbriefing\b"]),
        ("autoresearch", [r"\bautoresearch\b", r"\bauto.?research\b"]),
        ("gate-mechanism", [r"\bgate\b"]),
        ("decoder", [r"\bdecoder\b", r"\bresidual"]),
        ("state-update", [r"\bstate\b.*\b(?:update|norm|bound|model)\b"]),
        ("cell-lifecycle", [r"\bcell\b", r"\bspawn\b"]),
        ("event-system", [r"\bevent\b", r"\bcorrelat"]),
        ("log-rotation", [r"\blog.?rotation\b"]),
        ("sprint", [r"\bsprint\b"]),
        ("linear-sprint", [r"\blinear.?sprint\b"]),
        ("companion-mode", [r"\bcompanion\b"]),
        ("multi-project", [r"\bmulti.?(?:project|hive)\b"]),
        ("concept-data", [r"\bconcept\b", r"\bchunk"]),
        ("diagnostics", [r"\bdiagnostic"]),
        ("colab", [r"\bcolab\b", r"\bnotebook\b"]),
        ("novelty-detection", [r"\bnovelty\b"]),
        ("v2-model", [r"\bv2\b"]),
        ("v3-model", [r"\bv3\b"]),
        ("fd-baseline", [r"\bfd\b.*\bbaseline\b", r"\bbaseline\b.*\bfd\b"]),
        ("modelkit", [r"\bmodelkit\b"]),
        ("pipeline", [r"\bpipeline\b"]),
    ],
    "_automated": [
        ("ranking", [r"\branking\b", r"\bannotat", r"\bstreamlit\b"]),
        ("sprint", [r"\bsprint\b", r"\bmar-\d+\b"]),
        ("calculator", [r"\bcalculator\b"]),
        ("pipeline", [r"\bpipeline\b", r"\bingestion\b"]),
        ("classifier", [r"\bclassif"]),
        ("dashboard", [r"\bdashboard\b"]),
        ("langgraph", [r"\blanggraph\b", r"\breact.?agent\b"]),
        ("training", [r"\btraining\b", r"\bepoch\b"]),
        ("gate-mechanism", [r"\bgate\b"]),
        ("decoder", [r"\bdecoder\b", r"\bresidual\b"]),
        ("autoresearch", [r"\bautoresearch\b"]),
        ("concept-data", [r"\bconcept\b"]),
    ],
}


def _compile_rules(
    rules: list[tuple[str, list[str]]],
) -> list[tuple[str, list[re.Pattern]]]:
    return [(concept, [re.compile(p, re.IGNORECASE) for p in patterns]) for concept, patterns in rules]


def assign_concepts(title: str, project: str) -> list[str]:
    """Assign concepts to a note based on its title and project."""
    text = title.lower()
    concepts: list[str] = []
    seen: set[str] = set()

    # Apply global rules first, then project-specific
    all_rules = _compile_rules(GLOBAL_RULES)
    proj_rules = PROJECT_RULES.get(project, [])
    if proj_rules:
        all_rules = _compile_rules(proj_rules) + all_rules

    for concept, patterns in all_rules:
        if concept in seen:
            continue
        for pattern in patterns:
            if pattern.search(text):
                concepts.append(concept)
                seen.add(concept)
                break

    return concepts


def main():
    parser = argparse.ArgumentParser(description="Assign concepts to imported notes")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--project", "-p", default="")
    args = parser.parse_args()

    config = load_config()
    vm = VaultManager(config=config)

    # Query all imported notes (non-session)
    conn = sqlite3.connect(str(config.index_db))
    conn.row_factory = sqlite3.Row

    query = """
        SELECT id, title, type, project, path, frontmatter
        FROM notes
        WHERE frontmatter LIKE '%imported_from%' AND type != 'session'
    """
    params: list = []
    if args.project:
        query += " AND project = ?"
        params.append(args.project)
    query += " ORDER BY project, title"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    # Stats
    updated = 0
    skipped_has_concepts = 0
    skipped_no_match = 0
    concept_counts: dict[str, int] = defaultdict(int)
    by_project: dict[str, dict] = defaultdict(lambda: {"updated": 0, "skipped": 0, "concepts": set()})

    for row in rows:
        note_id = row["id"]
        title = row["title"]
        project = row["project"]
        path = row["path"]

        # Check if already has concepts
        fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
        existing = fm.get("concepts", [])
        if existing:
            skipped_has_concepts += 1
            continue

        concepts = assign_concepts(title, project)
        if not concepts:
            skipped_no_match += 1
            by_project[project]["skipped"] += 1
            continue

        if args.dry_run:
            print(f"  {project:30s} {title[:60]:60s} → {concepts}")
            updated += 1
            for c in concepts:
                concept_counts[c] += 1
                by_project[project]["concepts"].add(c)
            by_project[project]["updated"] += 1
            continue

        # Update frontmatter
        full_path = config.vault_root / path
        if not full_path.exists():
            continue

        vm.update_note(full_path, frontmatter_updates={"concepts": concepts})
        updated += 1
        for c in concepts:
            concept_counts[c] += 1
            by_project[project]["concepts"].add(c)
        by_project[project]["updated"] += 1

    # Summary
    action = "Would update" if args.dry_run else "Updated"
    print(f"\n── Summary ────────────────────────────────────────")
    print(f"  {action}: {updated} notes")
    print(f"  Already had concepts: {skipped_has_concepts}")
    print(f"  No concept match: {skipped_no_match}")

    print(f"\n── By Project ─────────────────────────────────────")
    for proj in sorted(by_project):
        ps = by_project[proj]
        print(f"  {proj:30s}  {ps['updated']:4d} enriched, {ps['skipped']:4d} no match, {len(ps['concepts']):2d} distinct concepts")

    print(f"\n── Top Concepts ───────────────────────────────────")
    for concept, count in sorted(concept_counts.items(), key=lambda x: -x[1])[:30]:
        print(f"  {count:4d}  {concept}")

    # Rebuild index
    if not args.dry_run and updated > 0:
        print(f"\n  Rebuilding index...")
        idx = Indexer(config=config)
        stats = idx.rebuild(full=True)
        idx.close()
        print(f"  Indexed: {stats['indexed']}, Edges: {stats['edges']}")


if __name__ == "__main__":
    main()
