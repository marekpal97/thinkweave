"""News-item triage against the active-themes catalog.

Stage-1 of the news pipeline. As of 2026-06-06 (plan A2,
``go-back-to-the-scalable-firefly.md``) the canonical triage path is
the ``news-triage-worker`` CC Task subagent invoked from
``commands/drain.md``. This module survives as the **explicit-opt-out
fallback** — when the user disables the subagent path via
``vault/config/api.yaml::overrides.news_triage_fallback``, the drain
shells out to ``python -m personal_mem.operations.news_triage`` and
this code runs against whatever provider+model the override specifies
(default still OpenAI ``gpt-5-mini``, mirroring legacy behaviour).

Architecture choices worth flagging:

- **Title-only triage.** The classifier sees the title + outlet/tier,
  not the body. Cost: misclassified clickbait headlines. Benefit: cheap
  enough to run on every queued item; no curl, no fetch_failed. False
  accepts are caught downstream (writer produces a thin note); false
  rejects are caught by periodic review of the rejection archive.

- **THEMES.md is the single source of truth.** The triage helper reads
  the rendered ``## Catalog (active)`` section from THEMES.md rather
  than glob-walking ``vault/themes/``. Two reasons: (1) the catalog
  already filters to active themes (dormant/resolved/merged correctly
  excluded by ``themes_ledger``); (2) the section structure is stable
  and human-edited additions there flow into triage automatically.

- **Strict JSON output.** The classifier is instructed to emit a single
  JSON object keyed by item index. The parser tolerates code-fenced
  JSON but otherwise refuses to guess — items missing from the response
  are flagged ``drop`` with a "no verdict" reason so nothing silently
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
DEFAULT_MODEL = "gpt-5-mini"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

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
    end = themes_md_text.find("\n## ", start + len(needle))
    return themes_md_text[start:] if end < 0 else themes_md_text[start:end]


def build_triage_messages(
    catalog: str,
    items: list[dict],
    *,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Build the OpenAI ``chat.completions`` request body dict.

    Returns a dict ready to JSON-encode into the httpx POST body. The
    catalog is folded into the system message; OpenAI prompt caching
    is implicit for prefixes ≥1024 tokens, so no explicit cache
    directive is needed.

    Each item must carry: ``id`` (queue id), ``title``, ``outlet``
    (slug), ``tier`` (int). Missing fields are tolerated — they default
    to empty/0 — but the LLM uses them all when present.
    """
    system_text = (
        "You are a news triage classifier for the personal vault of a "
        "generalist quantamental analyst — someone who combines fundamental "
        "and quantitative analysis across ALL equity sectors and also follows "
        "macro closely. The vault tracks a set of active narrative themes; "
        "each news item is either attached to a theme, filed unfiled for "
        "later theme-creation review, or dropped as noise.\n\n"
        "WHAT IS IN SCOPE — admit (keep or keep_unfiled):\n"
        "- Single-name equity news with a fundamental or quantitative angle: "
        "earnings surprises, guidance changes, margin / cost shifts, capital "
        "allocation, M&A, management or strategy changes, unusual price / "
        "volume moves with a stated cause. A quantamental analyst cares about "
        "single names — do NOT drop these for being single-name.\n"
        "- Sector and industry dynamics in ANY sector: demand cycles, "
        "pricing, supply chains, capacity, regulation, competitive shifts.\n"
        "- Macro: growth, inflation, labour, central banks, rates, credit, "
        "FX, commodities, fiscal policy, housing.\n"
        "- Market structure and flows: positioning, fund flows, factor / "
        "style rotation, liquidity, volatility, breadth.\n"
        "- AI infrastructure and the semiconductor / datacenter / power "
        "complex.\n"
        "- Polish economy and markets (a standing interest of the analyst).\n"
        "- Geopolitics WHEN it has a clear market or economic transmission "
        "channel (energy supply, trade routes, sanctions, defense spending).\n"
        "\n"
        "VERDICTS:\n"
        "- \"keep\": fits an active theme. Set theme_id to the matching "
        "`thm-XXXXXXXX`.\n"
        "- \"keep_unfiled\": in scope per the list above and substantive, but "
        "no active theme matches. theme_id null. Goes to the periodic-review "
        "pile so emerging arcs can be promoted to themes.\n"
        "- \"drop\": out of scope or noise. theme_id null.\n\n"
        "WHAT TO DROP — be strict, these waste the analyst's attention:\n"
        "- Non-financial content: sports, celebrity, lifestyle, "
        "entertainment, movie / TV reviews, true-crime, human-interest.\n"
        "- Link-roundup / open-thread / 'Links 5/10' aggregation posts with "
        "no single story.\n"
        "- Site-meta posts: blog announcements, schedule changes, newsletter "
        "housekeeping ('This is the End and a New Beginning').\n"
        "- Personal-finance advice columns and reader Q&A.\n"
        "- Partisan political horse-race coverage with no economic / market "
        "channel.\n"
        "- Pure clickbait listicles and stock-tip promotions ('1500% gains').\n"
        "- Generic 'AI will change everything' think-pieces with no specific "
        "company, number, or mechanism.\n\n"
        "BORDERLINE: when an item is genuinely ambiguous between keep_unfiled "
        "and drop, prefer keep_unfiled — the review pile catches emerging "
        "patterns and an over-aggressive drop loses signal silently. But "
        "'borderline' means the item is plausibly in scope; it does NOT "
        "rescue clearly out-of-scope content.\n\n"
        "OUTLET TIER: tier-1 outlets carry editorial filtering; tier-2 are "
        "opinionated and clickbait-prone. Tier is a TIE-BREAKER on genuinely "
        "borderline items ONLY — a borderline tier-1 item leans keep_unfiled, "
        "a borderline tier-2 item leans drop. Tier NEVER overrides an obvious "
        "call: a clearly out-of-scope item from a tier-1 outlet is still a "
        "drop, and a clearly substantive item from a tier-2 outlet is still "
        "admitted.\n\n"
        "Output format: a SINGLE JSON object. Keys are item indices "
        "(\"1\"..\"N\" as strings). Values are objects with fields: "
        "verdict (one of keep, keep_unfiled, drop), theme_id (string or "
        "null), reason (one short sentence, ≤120 chars)."
    )
    catalog_text = catalog.strip() or (
        f"{CATALOG_HEADING}\n\n_(no active themes — every substantive "
        "item should be keep_unfiled)_"
    )
    system_full = f"{system_text}\n\nActive themes catalog:\n\n{catalog_text}"
    items_text = _render_items(items)
    user_text = (
        f"Triage these {len(items)} news items:\n\n{items_text}\n\n"
        "Emit the JSON object now."
    )
    return {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_full},
            {"role": "user", "content": user_text},
        ],
    }


def parse_triage_response(raw: str, items: list[dict]) -> list[dict]:
    """Parse the LLM's JSON output into a list of verdict dicts.

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
    """End-to-end triage: build request, call OpenAI, parse response.

    Returns the verdict list. Caller archives each item per verdict
    (drop → archive rejected; keep / keep_unfiled → dispatch to writer).

    Reads ``OPENAI_API_KEY`` from env or the project ``.env`` via
    ``personal_mem.synthesis.enrich.load_openai_api_key``. Uses httpx for the
    POST — no ``openai`` SDK import needed, matching the pattern in
    ``enrich.py`` and ``surfaces/cli/_hubs_link.py``.
    """
    if not items:
        return []
    try:
        import httpx
    except ImportError as e:
        raise RuntimeError(
            "httpx is required for news triage. "
            "Install with: uv add --optional embeddings httpx"
        ) from e

    if api_key:
        key = api_key
    else:
        from personal_mem.synthesis.enrich import load_openai_api_key
        key = load_openai_api_key() or os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Export it, add it to .env, "
            "or pass api_key=..."
        )

    request = build_triage_messages(catalog, items, model=model)
    response = httpx.post(
        OPENAI_API_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json=request,
        timeout=60.0,
    )
    response.raise_for_status()
    data = response.json()
    raw = data["choices"][0]["message"]["content"]
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
            --themes <vault>/THEMES.md
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
