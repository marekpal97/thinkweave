"""One-off: dump event-grain sources as compacted JSON for the theme-seeding pass.

Reads SQLite for type=source notes, filters by `source_type` in frontmatter
against the event-grain set from the registry, and emits one JSON array on
stdout. Each entry: id, title, source_type, concepts, first_para (≤400 chars).
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

from personal_mem.core.config import load_config
from personal_mem.core.vault import parse_frontmatter
from personal_mem.acquisition.sources.registry import REGISTRY


_WIKILINK_ONLY = re.compile(r"^-\s*\[\[[^\]]+\]\][^\n]*$")


def _essence_snippet(body: str, cap: int = 400) -> str:
    """Take the first ~cap chars of prose, skipping headings and wikilink-only
    bullet lists (which dominate the ``## See Also`` / ``## Vault Connections``
    sections at the bottom of source notes)."""
    keep: list[str] = []
    running = 0
    for line in (body or "").split("\n"):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            continue
        if _WIKILINK_ONLY.match(s):
            continue
        keep.append(s)
        running += len(s) + 1
        if running >= cap:
            break
    return " ".join(keep)[:cap]


def main() -> int:
    cfg = load_config()
    event_types = {
        slug for slug, spec in REGISTRY.items() if spec.temporal_grain == "event"
    }

    conn = sqlite3.connect(cfg.index_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, title, path, frontmatter FROM notes WHERE type='source'"
    ).fetchall()
    conn.close()

    out: list[dict] = []
    for row in rows:
        try:
            fm = json.loads(row["frontmatter"] or "{}")
        except json.JSONDecodeError:
            continue
        stype = fm.get("source_type") or ""
        if stype not in event_types:
            continue
        path = cfg.vault_root / row["path"]
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        _, body = parse_frontmatter(text)
        out.append(
            {
                "id": row["id"],
                "title": row["title"],
                "source_type": stype,
                "concepts": fm.get("concepts") or [],
                "first_para": _essence_snippet(body),
            }
        )

    json.dump(out, sys.stdout, indent=2)
    print(file=sys.stderr)
    print(f"# {len(out)} event-grain sources dumped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
