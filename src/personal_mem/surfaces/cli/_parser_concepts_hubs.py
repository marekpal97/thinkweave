"""Argparse subcommand builders — concepts / hubs / queue / drain / discover."""

from __future__ import annotations


def add_concepts_subparsers(sub) -> None:
    p_concepts = sub.add_parser("concepts", help="List, drift, merge, prune concepts")
    concepts_sub = p_concepts.add_subparsers(dest="concepts_action")
    p_concepts_list = concepts_sub.add_parser("list", help="List all concepts with counts")
    p_concepts_list.add_argument("--prefix", default="", help="Filter by prefix")
    p_concepts_list.add_argument("--min-count", type=int, default=1, help="Minimum note count")
    p_merge = concepts_sub.add_parser("merge", help="Merge one concept into another")
    p_merge.add_argument("from_concept", help="Concept to rename/remove")
    p_merge.add_argument("to_concept", help="Canonical concept to merge into")
    p_prune = concepts_sub.add_parser("prune", help="Remove low-count concepts from notes")
    p_prune.add_argument("--dry-run", action="store_true", help="Show what would be pruned")
    p_prune_singletons = concepts_sub.add_parser(
        "prune-singletons",
        help=(
            "Strip count=1 concepts not in the ontology and not matching "
            "DOMAIN_MARKERS — the noise floor of LLM enrichment. Default "
            "step in /mem-resolve-concepts."
        ),
    )
    p_prune_singletons.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be pruned without writing files or rebuilding index.",
    )
    p_demote = concepts_sub.add_parser(
        "demote-non-ontology",
        help=(
            "Move every non-ontology term from `concepts:` to "
            "`proposed_concepts:` on every note. One-shot retroactive "
            "application of the strict creation policy."
        ),
    )
    p_demote.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be demoted without writing files or rebuilding index.",
    )
    p_consolidate = concepts_sub.add_parser(
        "consolidate-parents",
        help=(
            "Drop a domain concept from `concepts:` when any of its leaves "
            "is also present on the same note. Counterpart to the strict "
            "ontology gate — gates writes vs cleans post-hoc redundancy."
        ),
    )
    p_consolidate.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be consolidated without writing files or rebuilding index.",
    )
    p_proposed = concepts_sub.add_parser(
        "proposed-counts",
        help=(
            "List proposed_concepts terms aggregated by occurrence count. "
            "Use with /mem-resolve-concepts to find promotion candidates."
        ),
    )
    p_proposed.add_argument(
        "--min-count", type=int, default=1,
        help="Hide terms below this count (default: 1).",
    )
    p_proposed.add_argument(
        "--prefix", default="",
        help="Filter terms by prefix.",
    )
    p_promote = concepts_sub.add_parser(
        "promote",
        help=(
            "Promote a proposed_concept to canonical ontology status: "
            "add to vault ontology.yaml, walk every note carrying the "
            "term in proposed_concepts: and move it to concepts:, "
            "ensure hub skeleton, rebuild index."
        ),
    )
    p_promote.add_argument("concept", help="Term to promote (will be lowercased).")
    p_promote.add_argument(
        "--domain", required=True,
        help="Ontology domain to attach the term to (e.g. ml-training, swe-python).",
    )
    p_concepts_hubs = concepts_sub.add_parser(
        "hubs", help="Generate or prune Obsidian hub pages"
    )
    p_concepts_hubs.add_argument(
        "--prune",
        action="store_true",
        help=(
            "Find and delete orphan hub pages (concepts with zero vault "
            "notes that aren't in ontology.yaml). Read-only without --apply."
        ),
    )
    p_concepts_hubs.add_argument(
        "--apply",
        action="store_true",
        help="With --prune, actually delete the orphans (otherwise list only).",
    )
    p_drift = concepts_sub.add_parser(
        "drift",
        help="Advisory drift report (near-dupes, new ontology candidates, stale hubs)",
    )
    p_drift.add_argument("--project", "-p", default="", help="Optional project scope")
    p_drift.add_argument("--threshold", type=int, default=5, help="Min count for candidates")
    p_drift.add_argument("--max-items", type=int, default=5, help="Max per category")
    p_drift.add_argument(
        "--hubs",
        action="store_true",
        help=(
            "Also surface redundant-hub candidates: pairs of concept hubs "
            "with overlapping essence content (Jaccard pre-filter; LLM "
            "judgment lives in /mem-resolve-concepts)."
        ),
    )
    p_drift.add_argument(
        "--hub-jaccard",
        type=float,
        default=0.4,
        help="Minimum Jaccard similarity for hub-pair candidates (default: 0.4)",
    )
    p_notes = concepts_sub.add_parser("notes", help="List notes for a specific concept")
    p_notes.add_argument("concept", help="Concept to search for")
    p_notes.add_argument("--project", "-p", default="", help="Filter by project")


def add_hubs_subparsers(sub) -> None:
    p_hubs = sub.add_parser(
        "hubs",
        help="Concept hub pages — plan, run (backfill), and status",
    )
    hubs_sub = p_hubs.add_subparsers(dest="hubs_action")

    p_hubs_plan = hubs_sub.add_parser(
        "plan", help="Walk the vault and write a JSON plan for hub backfill"
    )
    p_hubs_plan.add_argument(
        "--out", default="", help="Plan output path (default: .mem/hubs_plan.json)"
    )
    p_hubs_plan.add_argument("--concept", default="", help="Restrict to one concept")
    p_hubs_plan.add_argument("--project", default="", help="Restrict to one project")
    p_hubs_plan.add_argument("--note-type", default="", help="Restrict to one note type")
    p_hubs_plan.add_argument(
        "--limit-notes",
        type=int,
        default=0,
        help="Cap unprocessed notes per concept (0 = no cap)",
    )
    p_hubs_plan.add_argument(
        "--limit-concepts",
        type=int,
        default=0,
        help="Cap total concepts in the plan (0 = no cap)",
    )

    p_hubs_status = hubs_sub.add_parser(
        "status",
        help="Show processed state per concept (cited vs total)",
    )
    p_hubs_status.add_argument("--concept", default="", help="Restrict to one concept")

    p_hubs_repair = hubs_sub.add_parser(
        "repair",
        help=(
            "Retroactively fix hub log entries: swap backfill dates for the "
            "cited note's real date, strip duplicated inline wikilink citations."
        ),
    )
    p_hubs_repair.add_argument("--concept", default="", help="Restrict to one concept")
    p_hubs_repair.add_argument(
        "--dry-run",
        action="store_true",
        help="Report changes per hub without writing",
    )

    p_hubs_link = hubs_sub.add_parser(
        "link",
        help=(
            "Temporal-DAG linkage pass: rewrite flat `new` flags into "
            "agrees/contradicts/extends relationships via gpt-5-mini Batches API."
        ),
    )
    p_hubs_link.add_argument("--concept", default="", help="Restrict to one concept")
    p_hubs_link.add_argument(
        "--model",
        default="gpt-5-mini",
        help="OpenAI model to use (default: gpt-5-mini)",
    )
    p_hubs_link.add_argument(
        "--max-tokens",
        type=int,
        default=32768,
        help=(
            "Max output tokens per request (default: 32768). gpt-5-mini is "
            "a reasoning model — visible JSON output for a 150-entry hub is "
            "~6K tokens but hidden reasoning can consume 10-20K. Smaller caps "
            "starve the model and trigger finish_reason=length with empty content."
        ),
    )
    p_hubs_link.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        help="Seconds between batch status polls (default: 30)",
    )
    p_hubs_link.add_argument(
        "--max-input-tokens",
        type=int,
        default=4_500_000,
        help="Cap enqueued input tokens per batch (default: 4,500,000, under OpenAI's 5M org limit). 0 = no cap.",
    )
    p_hubs_link.add_argument(
        "--min-entries",
        type=int,
        default=2,
        help="Skip hubs with fewer than N entries (default: 2)",
    )
    p_hubs_link.add_argument(
        "--dry-run",
        action="store_true",
        help="Build requests and print the first one, but don't submit to the API",
    )


def add_themes_subparsers(sub) -> None:
    p_themes = sub.add_parser(
        "themes",
        help="Theme registry maintenance.",
    )
    themes_sub = p_themes.add_subparsers(dest="themes_action")

    rebuild = themes_sub.add_parser(
        "rebuild-registry",
        help="Rebuild themes.yaml from canonical theme markdown files.",
    )
    rebuild.add_argument("--project", default="")
def add_drain_subparsers(sub) -> None:
    p_queue = sub.add_parser(
        "queue",
        help="Inspect per-source-type acquisition queues (.mem/queues/*.jsonl)",
    )
    p_queue.add_argument(
        "action",
        choices=["list", "inspect", "peek"],
        help="list — all queues with counts; inspect <type> — full listing; peek <type> — first N items",
    )
    p_queue.add_argument(
        "source_type", nargs="?", default="",
        help="Source type slug (required for inspect / peek)",
    )
    p_queue.add_argument(
        "--source-type", dest="source_type_flag", default="",
        help="Alternative to positional for `list --source-type X`",
    )
    p_queue.add_argument(
        "--n", type=int, default=5, help="With peek: number of items (default: 5)"
    )

    p_drain = sub.add_parser(
        "drain",
        help=(
            "Drain a queue or backfill concept hubs. Replaces `mem hubs run` "
            "and the inline hub-backfill skill."
        ),
    )
    p_drain.add_argument("--target", default="", choices=["", "hubs"])
    p_drain.add_argument("--source-type", default="")
    p_drain.add_argument("--source", default="")
    p_drain.add_argument("--via", default="inline", choices=["inline", "batch"])
    p_drain.add_argument("--concept", default="")
    p_drain.add_argument("--project", default="")
    p_drain.add_argument("--limit", type=int, default=0)
    p_drain.add_argument("--dry-run", action="store_true")
    p_drain.add_argument("--plan", default="")
    p_drain.add_argument("--model", default="gpt-5-mini")
    p_drain.add_argument("--max-tokens", type=int, default=8192)
    p_drain.add_argument("--poll-interval", type=int, default=30)
    p_drain.add_argument("--max-input-tokens", type=int, default=4_500_000)

    p_discover = sub.add_parser(
        "discover",
        help=(
            "Run discovery strategies (concept_coverage, decision_review, "
            "external_tool_runner, rss_poll, mail_poll). Returns gap "
            "descriptors as JSON."
        ),
    )
    p_discover.add_argument(
        "--project", "-p", default="",
        help="Project name. Loads `projects.<name>.discover_strategies` from sources.yaml.",
    )
    p_discover.add_argument(
        "--strategy", "-s", default="",
        help="Run a single named strategy instead of the project's configured list.",
    )
    p_discover.add_argument(
        "--source-type", default="",
        help=(
            "Limit external-trigger strategies (rss_poll, mail_poll) to one "
            "source type. Ignored by internal-state strategies."
        ),
    )
    p_discover.add_argument(
        "--list", action="store_true",
        help="List registered strategies and exit.",
    )
