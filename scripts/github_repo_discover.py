#!/usr/bin/env python
"""GitHub repo-discovery tool for /discover's ``external_tool_runner``.

Emits JSONL queue items (``source_type: repo``) for top-starred GitHub
repositories matching the vault's ``focus.research_concepts`` that aren't
already in the vault. Wired into ``vault/config/sources.yaml`` as::

    projects:
      default:
        discover_strategies: [focus_research, decision_review, external_tool_runner]
        external_tool_runner:
          timeout: 180
          # inline list of whole-command strings — thinkweave's sources.yaml
          # parser has no block-sequence support (see external_tool_runner.py).
          tools: ["/abs/.venv/bin/python /abs/scripts/github_repo_discover.py"]

The ``external_tool_runner`` strategy invokes this with the project name as
the final argv (ignored here — focus concepts are vault-global), reads our
stdout line-by-line, and hands each JSON line to the ``/discover`` skill,
which enqueues the ones with ``url``/``title`` into the ``repo`` queue.

Contract this script honours:
  * **stdout is pure JSONL** — one queue item per line, nothing else.
    Diagnostics go to stderr (captured but unused by the strategy).
  * **Deduped** against repos already noted in the vault index, so we only
    surface genuinely-new repos (the queue's own dedup is the backstop).
  * **Repo-deficit first** — concepts with the fewest existing repo notes
    are searched first, so a capped run spends its budget where coverage
    is thinnest.

Auth + rate limits: uses ``$GITHUB_TOKEN`` / ``$GH_TOKEN`` if present
(30 search req/min); otherwise unauthenticated (10/min) and paces itself.
Tunable via env: ``GH_DISCOVER_MAX_CONCEPTS`` (8), ``GH_DISCOVER_PER_CONCEPT``
(3), ``GH_DISCOVER_MIN_STARS`` (50).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Make the thinkweave package importable regardless of cwd (cron runs from
# the project dir; a manual run might not).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from thinkweave.acquisition.sources.priorities import (  # noqa: E402
    focus_concepts,
    load_priorities,
)
from thinkweave.core.config import load_config  # noqa: E402
from thinkweave.core.indexer import Indexer  # noqa: E402

MAX_CONCEPTS = int(os.environ.get("GH_DISCOVER_MAX_CONCEPTS", "8"))
PER_CONCEPT = int(os.environ.get("GH_DISCOVER_PER_CONCEPT", "3"))
MIN_STARS = int(os.environ.get("GH_DISCOVER_MIN_STARS", "50"))
API = "https://api.github.com/search/repositories"


def _log(msg: str) -> None:
    print(f"[github_repo_discover] {msg}", file=sys.stderr)


def _existing_repo_urls(cfg) -> set[str]:
    """Lowercased, trailing-slash-stripped URLs of repo notes already in the vault."""
    idx = Indexer(config=cfg)
    try:
        urls: set[str] = set()
        for row in idx.db.execute(
            "SELECT frontmatter FROM notes WHERE type='source' "
            "AND json_extract(frontmatter,'$.source_type')='repo'"
        ):
            try:
                fm = json.loads(row["frontmatter"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            url = (fm.get("url") or "").rstrip("/").lower()
            if url:
                urls.add(url)
        return urls
    finally:
        idx.close()


def _repo_deficit_order(cfg, concepts: list[str]) -> list[str]:
    """Concepts sorted by fewest existing repo notes first (deficit-first)."""
    idx = Indexer(config=cfg)
    try:
        counts: dict[str, int] = {}
        for c in concepts:
            counts[c] = idx.db.execute(
                "SELECT COUNT(*) FROM notes n "
                "JOIN note_concepts nc ON nc.note_id = n.id "
                "WHERE nc.concept = ? AND n.type='source' "
                "AND json_extract(n.frontmatter,'$.source_type')='repo'",
                (c,),
            ).fetchone()[0]
    finally:
        idx.close()
    return sorted(concepts, key=lambda c: counts.get(c, 0))


def _search(query: str, token: str | None) -> list[dict]:
    params = urllib.parse.urlencode(
        {"q": query, "sort": "stars", "order": "desc", "per_page": "15"}
    )
    req = urllib.request.Request(f"{API}?{params}")
    req.add_header("User-Agent", "thinkweave-repo-discover")
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp).get("items", []) or []


def main() -> int:
    cfg = load_config()
    concepts = focus_concepts(load_priorities(cfg.vault_root))
    if not concepts:
        _log("no focus.research_concepts configured — nothing to search")
        return 0

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    seen = _existing_repo_urls(cfg)
    ordered = _repo_deficit_order(cfg, concepts)[:MAX_CONCEPTS]
    _log(
        f"{len(concepts)} focus concepts; searching {len(ordered)} deficit-first "
        f"(token={'yes' if token else 'no'}); {len(seen)} repos already in vault"
    )

    emitted: set[str] = set()
    total = 0
    for i, concept in enumerate(ordered):
        query = concept.replace("-", " ")
        try:
            items = _search(query, token)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                _log(f"rate-limited at concept '{concept}' — stopping early")
                break
            _log(f"HTTP {e.code} for '{concept}' — skipping")
            continue
        except Exception as e:  # noqa: BLE001 — network is best-effort
            _log(f"search failed for '{concept}': {e}")
            continue

        kept = 0
        for it in items:
            url = (it.get("html_url") or "").rstrip("/")
            key = url.lower()
            if not url or key in seen or key in emitted:
                continue
            if (it.get("stargazers_count") or 0) < MIN_STARS:
                continue
            emitted.add(key)
            print(json.dumps({
                "source_type": "repo",
                "url": url,
                "title": it.get("full_name") or url,
                "concept": concept,
                "summary": (it.get("description") or "")[:300],
                "stars": it.get("stargazers_count"),
            }))
            kept += 1
            total += 1
            if kept >= PER_CONCEPT:
                break

        # Pace unauthenticated runs (~10 search req/min).
        if not token and i < len(ordered) - 1:
            time.sleep(7)

    _log(f"emitted {total} new repo candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
