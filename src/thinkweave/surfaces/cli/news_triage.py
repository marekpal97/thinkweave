"""``python -m thinkweave.surfaces.cli.news_triage`` — CLI shell for the
news-item triage fallback.

The pure triage logic (catalog slicing, prompt building, response parsing,
the OpenAI call) lives in :mod:`thinkweave.operations.news_triage`; that
module stays adapter-free (no ``print`` / ``sys.exit``) so a second adapter
can reuse it. This surface owns stdin/stdout and the exit codes.

Invoked by the drain fallback as a Bash subprocess when the subagent triage
path is disabled (``vault/config/api.yaml::overrides.news_triage_fallback``):

    echo '[{"id":"q-1","title":"...","outlet":"zerohedge","tier":2}, ...]' | \\
      uv run python -m thinkweave.surfaces.cli.news_triage \\
        --themes <vault>/THEMES.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from thinkweave.operations.news_triage import (
    DEFAULT_MODEL,
    VERDICT_DROP,
    build_triage_messages,
    extract_catalog_section,
    triage_items,
)


def main() -> int:
    """Read items as JSON from stdin, print verdicts as JSON to stdout."""
    parser = argparse.ArgumentParser(
        description=(
            "Triage news items against the active-themes catalog. Items "
            "from stdin (JSON list); verdicts to stdout (JSON list)."
        )
    )
    parser.add_argument(
        "--themes",
        required=True,
        help="Path to THEMES.md (the source of the catalog section).",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenAI model id. Default: {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Build the request and print the prompt + items to stderr "
            "without calling the LLM. Verdicts on stdout default to "
            "drop. Useful for inspecting the prompt."
        ),
    )
    args = parser.parse_args()

    themes_path = Path(args.themes)
    if not themes_path.exists():
        print(f"THEMES.md not found at {themes_path}", file=sys.stderr)
        return 1
    themes_md = themes_path.read_text(encoding="utf-8")
    catalog = extract_catalog_section(themes_md)

    try:
        items = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        print(f"Bad JSON on stdin: {e}", file=sys.stderr)
        return 2
    if not isinstance(items, list):
        print("Expected a JSON list of items.", file=sys.stderr)
        return 2

    if args.dry_run:
        request = build_triage_messages(catalog, items, model=args.model)
        print(json.dumps(request, indent=2), file=sys.stderr)
        verdicts = [
            {
                "id": item["id"],
                "verdict": VERDICT_DROP,
                "theme_id": None,
                "reason": "dry-run",
            }
            for item in items
        ]
    else:
        verdicts = triage_items(catalog, items, model=args.model)

    print(json.dumps(verdicts, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
