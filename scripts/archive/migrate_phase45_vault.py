"""Phase 4 + 5b vault migration — pure-slug theme files + reports/ home.

Phase 4: rename ``themes/thm-XXXX-slug.md`` -> ``themes/slug.md``. The
thm-id stays in frontmatter + aliases (untouched), so every ``relates_to``
ref and ``[[thm-XXXX]]`` link still resolves; path links re-materialise to
the new filename on the next index pass.

Phase 5b: move hidden ``.mem/dream_reports/*.md`` -> visible
``reports/dream/`` (the new user-facing home for cron synthesis reports).

Gated behind ``--apply`` (default dry run). Backs up themes/ + the reports
first (vault is not git-tracked).

Usage:
    uv run python scripts/migrate_phase45_vault.py            # dry run
    uv run python scripts/migrate_phase45_vault.py --apply
"""
from __future__ import annotations

import re
import shutil
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/home/marekpal97/python_projects/personal_mem/src")

from personal_mem.core.config import load_config

APPLY = "--apply" in sys.argv
_THM_PREFIX = re.compile(r"^thm-[0-9a-f]{6,}-")


def log(m: str) -> None:
    print(m)


def backup(vault: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path.home() / "vault-backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"phase45-{ts}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        for p in [vault / "themes", vault / ".mem" / "dream_reports"]:
            if p.exists():
                tar.add(p, arcname=str(p.relative_to(vault)))
    return out


def rename_themes(vault: Path) -> None:
    log("\n=== Phase 4: pure-slug theme filenames ===")
    themes = vault / "themes"
    for tf in sorted(themes.glob("thm-*.md")):
        slug = _THM_PREFIX.sub("", tf.stem)
        if not slug:
            log(f"  ! {tf.name}: no slug after id prefix — SKIPPED")
            continue
        dest = themes / f"{slug}.md"
        n = 1
        while dest.exists() and dest != tf:
            dest = themes / f"{slug}-{n}.md"
            n += 1
        log(f"  {tf.name}  ->  {dest.name}")
        if APPLY:
            tf.rename(dest)


def move_reports(vault: Path) -> None:
    log("\n=== Phase 5b: dream reports -> reports/dream/ ===")
    old = vault / ".mem" / "dream_reports"
    new = vault / "reports" / "dream"
    if not old.exists():
        log("  (.mem/dream_reports absent — nothing to move)")
        return
    mds = sorted(old.glob("*.md"))
    log(f"  move {len(mds)} report(s) -> reports/dream/")
    if APPLY:
        new.mkdir(parents=True, exist_ok=True)
        for md in mds:
            dest = new / md.name
            if not dest.exists():
                shutil.move(str(md), str(dest))
        # Drop the now-empty hidden dir.
        if not any(old.iterdir()):
            old.rmdir()


def main() -> int:
    vault = load_config().vault_root
    log(f"Phase 4+5b migration [{'APPLY' if APPLY else 'DRY-RUN'}]  vault={vault}")
    if APPLY:
        log(f"Backup: {backup(vault)}")
    rename_themes(vault)
    move_reports(vault)
    log("\nDone." + ("" if APPLY else "  (dry run — re-run with --apply)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
