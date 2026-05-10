"""News-item triage against the active-themes catalog.

Stage-1 of the news pipeline (replaces the per-worker FOCUS gate). One
Haiku call per drain batch decides for each title whether it (a) fits
an active theme, (b) is substantive but theme-unmatched, or (c) is
noise. Stage-2 (the Sonnet writer) only runs on (a) and (b) — (c) is
archived with reason.

Architecture choices worth flagging:

- **Title-only triage.** The classifier sees the title + outlet/tier,
  not the body. Cost: misclassified clickbait headlines. Benefit: cheap
  enough to run on every queued item; no curl, no fetch_failed. False
  accepts are caught downstream (Sonnet writer produces a thin note);
  false rejects are caught by periodic review of the rejection archive.

- **Catalog-as-system-message with prompt caching.** The active-theme
  catalog (~5KB) goes into the system block with
  ``cache_control={"type": "ephemeral"}``. Subsequent triage calls in
  the same drain reuse the cached context — meaningful when the
  orchestrator runs more than one batch back-to-back. The user message
  carries only the per-batch item list.

- **THEMES.md is the single source of truth.** The triage helper reads
  the rendered ``## Catalog (active)`` section from THEMES.md rather
  than glob-walking ``vault/themes/``. Two reasons: (1) the catalog
  already filters to active themes (dormant/resolved/merged correctly
  excluded by ``themes_ledger``); (2) the section structure is stable
  and human-edited additions there flow into triage automatically.

- **Strict JSON output.** Haiku is instructed to emit a single JSON
  object keyed by item index. The parser tolerates code-fenced JSON
  but otherwise refuses to guess — items missing from the response are
  flagged ``drop`` with a "no verdict" reason so nothing silently
  slips through the gate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable

CATALOG_HEADING = "## Catalog (active)"
DEFAULT_MODEL = "claude-haiku-4-5"

VERDICT_KEEP = "keep"
VERDICT_UNFILED = "keep_unfiled"
VERDICT_DROP = "drop"
ALLOWED_VERDICTS = {VERDICT_KEEP, VERDICT_UNFILED, VERDICT_DROP}


def extract_catalog_section(themes_md_text: str) -> str:
    """Slice ``## Catalog (active)`` ... up to the next ``## `` heading.

    Returns the full section body including the heading line. Empty
    string if the section isn't found — that signals "no active themes
    catalog" and the triage helper should reject everything as
    ``keep_unfiled`` (we have no theme structure to match against).
    """
    needle = CATALOG_HEADING
    start = themes_md_text.find(needle)
    if start < 0:
        return ""
    # Find the next sibling H2 heading after `start`. The slice ends
    # there (exclusive). If no later H2 exists, take to EOF.
    end = themes_md_text.find("\n## ", start + len(needle))
    return themes_md_text[start:] if end < 0 else themes_md_text[start:end]


def build_triage_messages(
    catalog: str,
    items: list[dict],
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 2048,
) -> dict:
    """Build the Anthropic ``messages.create`` kwargs dict.

    Returns a dict ready to splat into ``client.messages.create(**...)``.
    The catalog is placed in the system block with cache_control so
    repeat invocations within the cache TTL pay the cached input price.

    Each item must carry: ``id`` (queue id), ``title``, ``outlet``
    (slug), ``tier`` (int). Missing fields are tolerated — they default
    to empty/0 — but the LLM uses them all when present.
    """
    system_text = (
        "You are a news triage classifier for a personal vault. The vault "
        "tracks a small set of active narrative themes; news items are "
        "admitted only if they fit a theme or are substantive enough to be "
        "filed for later theme-creation review.\n\n"
        "For each news title, output ONE of three verdicts:\n"
        "- \"keep\": clearly fits an active theme. Set theme_id to the "
        "matching `thm-XXXXXXXX`.\n"
        "- \"keep_unfiled\": the title carries genuine macro / market / "
        "geopolitics / AI-infrastructure / Polish-economy signal but does "
        "not cleanly match any current theme. Set theme_id to null. "
        "These items go to a periodic-review pile so emerging arcs can "
        "be promoted to themes.\n"
        "- \"drop\": noise. Sports, celebrity, lifestyle, personal-finance "
        "Q&A, single-name corporate drama with no macro angle, generic "
        "'AI is going to change everything' think pieces, US partisan-"
        "politics horse-race coverage, non-watchlist single-name earnings "
        "previews. Set theme_id to null.\n\n"
        "Bias: when in genuine doubt between drop and keep_unfiled, "
        "prefer keep_unfiled — the unfiled review pile catches emerging "
        "patterns; an over-aggressive drop loses signal silently.\n\n"
        "Tier signal: tier-1 outlets (e.g. Reuters, FT preview, "
        "Calculated Risk) carry editorial filtering — a tier-1 title on "
        "a substantive macro topic is more likely keep/keep_unfiled. "
        "Tier-2 (e.g. ZeroHedge, Wolf Street, Moon of Alabama) is "
        "opinionated and clickbait-prone — require a clearer signal.\n\n"
        "Output format: a SINGLE JSON object. Keys are item indices "
        "(\"1\"..\"N\" as strings). Values are objects with fields: "
        "verdict (one of keep, keep_unfiled, drop), theme_id (string or "
        "null), reason (one short sentence, ≤120 chars). Output the JSON "
        "object only — no preamble, no code fence."
    )
    catalog_text = catalog.strip() or (
        f"{CATALOG_HEADING}\n\n_(no active themes — every substantive "
        "item should be keep_unfiled)_"
    )
    items_text = _render_items(items)
    user_text = (
        f"Active themes catalog:\n\n{catalog_text}\n\n"
        f"---\n\nTriage these {len(items)} news items:\n\n{items_text}\n\n"
        "Emit the JSON object now."
    )
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": [
            {"type": "text", "text": system_text},
            {
                "type": "text",
                "text": f"\n\nActive themes catalog:\n\n{catalog_text}",
                "cache_control": {"type": "ephemeral"},
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Triage these {len(items)} news items:\n\n"
                    f"{items_text}\n\nEmit the JSON object now."
                ),
            }
        ],
    }


def parse_triage_response(raw: str, items: list[dict]) -> list[dict]:
    """Parse Haiku's JSON output into a list of verdict dicts (one per item).

    Output shape:
        [{"id": "q-XXXX", "verdict": "keep|keep_unfiled|drop",
          "theme_id": "thm-XXXX" | None, "reason": "..."}]

    Tolerances:
      - Code-fenced JSON (``json ... ``) is unwrapped.
      - Items missing from the response default to verdict=drop with
        reason="no verdict from triage" — silent omission becomes a
        loud signal in the archive rather than a phantom accept.
      - Unknown verdicts collapse to drop with the raw value preserved
        in reason so the audit trail is honest.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Strip first fence + optional language tag, drop trailing fence.
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [
            {
                "id": item["id"],
                "verdict": VERDICT_DROP,
                "theme_id": None,
                "reason": "triage produced invalid JSON",
            }
            for item in items
        ]

    out: list[dict] = []
    for n, item in enumerate(items, start=1):
        key = str(n)
        verdict_obj = data.get(key) if isinstance(data, dict) else None
        if not isinstance(verdict_obj, dict):
            out.append(
                {
                    "id": item["id"],
                    "verdict": VERDICT_DROP,
                    "theme_id": None,
                    "reason": "no verdict from triage",
                }
            )
            continue
        v_raw = str(verdict_obj.get("verdict") or "").strip().lower()
        if v_raw not in ALLOWED_VERDICTS:
            out.append(
                {
                    "id": item["id"],
                    "verdict": VERDICT_DROP,
                    "theme_id": None,
                    "reason": f"unknown verdict {v_raw!r}",
                }
            )
            continue
        theme_id = verdict_obj.get("theme_id")
        if theme_id is not None and not (
            isinstance(theme_id, str) and theme_id.startswith("thm-")
        ):
            theme_id = None
        out.append(
            {
                "id": item["id"],
                "verdict": v_raw,
                "theme_id": theme_id,
                "reason": str(verdict_obj.get("reason") or "").strip()[:200],
            }
        )
    return out


def triage_items(
    catalog: str,
    items: list[dict],
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
) -> list[dict]:
    """End-to-end triage: build messages, call Anthropic, parse response.

    Returns the verdict list. Caller archives each item per verdict
    (drop → archive rejected; keep / keep_unfiled → dispatch to writer).

    The Anthropic SDK is imported lazily so importing this module
    doesn't require the SDK installed — only the actual call does.
    Missing SDK or API key both raise RuntimeError so the caller can
    surface a clear failure rather than crashing mid-batch.
    """
    if not items:
        return []
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(
            "anthropic SDK is required for news triage. "
            "Install with: uv add anthropic"
        ) from e

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Export it or pass api_key=..."
        )

    client = anthropic.Anthropic(api_key=key)
    request = build_triage_messages(catalog, items, model=model)
    response = client.messages.create(**request)
    raw = "".join(
        block.text for block in response.content if block.type == "text"
    )
    return parse_triage_response(raw, items)


def _render_items(items: Iterable[dict]) -> str:
    lines: list[str] = []
    for n, item in enumerate(items, start=1):
        title = (item.get("title") or "").strip().replace("\n", " ")
        outlet = item.get("outlet") or ""
        tier = item.get("tier", "?")
        lines.append(f"{n}. [outlet={outlet}, tier={tier}] {title}")
    return "\n".join(lines)


def main() -> int:
    """CLI: read items as JSON from stdin, print verdicts as JSON.

    The drain skill invokes this as a Bash subprocess:

        echo '[{"id":"q-1","title":"...","outlet":"zerohedge","tier":2}, ...]' | \\
          uv run python -m personal_mem.operations.news_triage \\
            --themes /mnt/c/Users/marek/vault/THEMES.md
    """
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
        help=f"Anthropic model id. Default: {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Build the request and print the prompt + items to stderr "
            "without calling Anthropic. Verdicts on stdout default to "
            "drop. Useful for inspecting the cache key."
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
        # Stub verdicts — every item drops with reason "dry-run".
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
