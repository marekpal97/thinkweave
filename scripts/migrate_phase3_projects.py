"""Phase-3 vault remediation — project-name normalization + merges.

Merges separator/name-duplicate project folders into their canonical
underscore form and sweeps empty agent-run junk folders. Gated behind
``--apply`` (default dry run); backs up ``projects/`` first because the
live vault is NOT git-tracked.

The boundary fix (``normalize_project_name`` applied in config load +
``VaultManager.create_note``) prevents *new* dash/case duplicates; this
script reconciles the ones already on disk.

Merges (source -> canonical):
  * personal-mem    -> personal_mem      (separator dup)
  * trade-ideas     -> trade_ideas       (separator dup)
  * thinkmesh       -> thinkmesh_neural  (early fragment, user-confirmed)

For each note under a source project (except the regenerated landing
docs), move it to the same relative sub-path under the canonical project,
rewriting its ``project:`` frontmatter. Source landing docs are dropped
(regenerated for the canonical project afterward). Empty source folder is
then removed.

Junk sweep (delete only if it holds no non-empty markdown):
  projects/agent-a12e848803bd0c01d, projects/agent-ad76606573a672455,
  projects/_automated

After ``--apply`` run:  mem index --full ; mem index --materialize-links ;
mem landing --project <canonical> --doc all   (paths changed, so links
must be re-materialised).

Usage:
    uv run python scripts/migrate_phase3_projects.py            # dry run
    uv run python scripts/migrate_phase3_projects.py --apply
"""
from __future__ import annotations

import shutil
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/home/marekpal97/python_projects/personal_mem/src")

from personal_mem.core.config import load_config
from personal_mem.core.vault import parse_frontmatter, render_frontmatter
from personal_mem.synthesis.landing import landing_filename_set

APPLY = "--apply" in sys.argv

MERGES = [
    ("personal-mem", "personal_mem"),
    ("trade-ideas", "trade_ideas"),
    ("thinkmesh", "thinkmesh_neural"),
]

JUNK = ["agent-a12e848803bd0c01d", "agent-ad76606573a672455", "_automated"]


def log(m: str) -> None:
    print(m)


def backup(vault: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path.home() / "vault-backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"phase3-{ts}.tar.gz"
    srcs = [vault / "projects" / s for s, _ in MERGES]
    srcs += [vault / "projects" / j for j in JUNK]
    srcs += [vault / "projects" / d for _, d in MERGES]
    with tarfile.open(out, "w:gz") as tar:
        for p in srcs:
            if p.exists():
                tar.add(p, arcname=str(p.relative_to(vault)))
    return out


def _nonconflicting(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suf = dest.stem, dest.suffix
    i = 1
    while True:
        cand = dest.with_name(f"{stem}-merged{i}{suf}")
        if not cand.exists():
            return cand
        i += 1


def merge_projects(vault: Path) -> None:
    landing_names = landing_filename_set(vault)
    log("\n=== Project merges ===")
    for src_name, dst_name in MERGES:
        src = vault / "projects" / src_name
        dst = vault / "projects" / dst_name
        if not src.exists():
            log(f"  {src_name}: (absent, skip)")
            continue
        moved = dropped = 0
        for md in sorted(src.rglob("*.md")):
            rel = md.relative_to(src)
            if md.name in landing_names:
                dropped += 1
                continue  # regenerated for the canonical project
            dest = _nonconflicting(dst / rel)
            if APPLY:
                # Rewrite project: frontmatter to the canonical name.
                try:
                    fm, body = parse_frontmatter(md.read_text(encoding="utf-8"))
                    if fm.get("project") == src_name:
                        fm["project"] = dst_name
                        md.write_text(render_frontmatter(fm) + "\n\n" + body.lstrip("\n"), encoding="utf-8")
                except Exception as exc:  # noqa: BLE001
                    log(f"    ! frontmatter rewrite failed {rel}: {exc}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(md), str(dest))
            moved += 1
        log(f"  {src_name} -> {dst_name}: move {moved} note(s), drop {dropped} landing doc(s)")
        if APPLY:
            shutil.rmtree(src)


def sweep_junk(vault: Path) -> None:
    # These three folders were inspected and explicitly authorized for
    # deletion. agent-* hold only throwaway empty sessions (files_touched
    # [], empty body) where an agent set the project to its own generated
    # id; _automated holds only "No items" landing docs. We delete them
    # whole — but refuse to touch any *other* folder, and refuse if a
    # folder unexpectedly contains a non-session, non-landing note.
    log("\n=== Junk folder sweep ===")
    landing_names = landing_filename_set(vault)
    for j in JUNK:
        p = vault / "projects" / j
        if not p.exists():
            log(f"  {j}: (absent)")
            continue
        suspicious = [
            m for m in p.rglob("*.md")
            if m.name not in landing_names and m.name != "session.md"
        ]
        if suspicious:
            log(f"  ! {j}: unexpected note(s) {[m.name for m in suspicious]} — SKIPPED")
            continue
        log(f"  rm projects/{j}/ ({len(list(p.rglob('*.md')))} md: empty sessions / empty landing)")
        if APPLY:
            shutil.rmtree(p)


def main() -> int:
    cfg = load_config()
    vault = cfg.vault_root
    log(f"Phase-3 project migration [{'APPLY' if APPLY else 'DRY-RUN'}]  vault={vault}")
    if APPLY:
        log(f"Backup written: {backup(vault)}")
    merge_projects(vault)
    sweep_junk(vault)
    log("\nDone." + ("" if APPLY else "  (dry run — re-run with --apply)"))
    if APPLY:
        log("Next: mem index --full ; mem index --materialize-links ; "
            "mem landing --project {personal_mem,trade_ideas,thinkmesh_neural} --doc all")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
