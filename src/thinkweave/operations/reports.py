"""Report-file helpers for autonomous-cycle runs (dream, discover).

``vault/reports/<kind>/`` is the user-visible home for per-run markdown
reports. The whole ``reports/`` tree is excluded from the SQLite index
(see ``Indexer.rebuild``), like landing docs — materialized narrative,
not source material. Landing's "Recent Maintenance" section links the
newest reports across kinds so the user can click through to what each
autonomous run did.
"""

from __future__ import annotations

from pathlib import Path

from thinkweave.core.config import Config

REPORTS_RELDIR = Path("reports")


def reports_dir(cfg: Config, kind: str) -> Path:
    """Directory holding per-run human-readable reports for ``kind``."""
    return cfg.vault_root / REPORTS_RELDIR / kind


def recent_reports(cfg: Config, kind: str, n: int = 3) -> list[dict]:
    """Return up to ``n`` most recent report descriptors (newest first).

    Each entry: ``{run_id, kind, path, mtime}`` where ``run_id`` is the
    file stem. Sorted by file mtime descending. Returns ``[]`` if the
    reports directory doesn't exist (the cycle has never run).
    """
    directory = reports_dir(cfg, kind)
    if not directory.exists():
        return []
    rows: list[dict] = []
    for path in directory.glob("*.md"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        rows.append({
            "run_id": path.stem,
            "kind": kind,
            "path": str(path),
            "mtime": mtime,
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:n]
