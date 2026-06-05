"""Phase-1 vault remediation — one-off data migration.

Companion to the code fixes in the 2026-06 vault review. Four steps, all
idempotent, gated behind ``--apply`` (default is a dry run). On ``--apply``
a tarball backup of every directory we touch is written first, because the
live vault is NOT git-tracked.

Steps:
  1. Move stray source notes out of ``projects/<p>/sources/...`` into the
     global ``sources/<bucket>/<slug>/`` location the registry dictates.
     (The routing bug that put them there is already fixed in vault.py.)
  2. Purge ``themes/_candidates/`` — the 37 pre-2026-05-30-teardown
     ``id: cand-*`` / ``status: candidate`` stubs that still index as
     ``type: theme`` and pollute THEMES.md.
  3. Delete zero-byte ``*.md`` phantom stubs at the vault root.
  4. Normalise theme bodies: inject the shared hub skeleton where a theme
     is a bare husk, and convert legacy catalyst-log lines
     (``- DATE: text [[src]] *flag*``) to the canonical Hub grammar
     (``- DATE · *flag* — text — [[src]]``) so they actually parse/render.

Usage:
    uv run python scripts/migrate_phase1_vault.py            # dry run
    uv run python scripts/migrate_phase1_vault.py --apply    # mutate (backs up first)
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
from personal_mem.core.vault import parse_frontmatter, render_frontmatter
from personal_mem.sources import registry as source_registry
from personal_mem.synthesis.hub import (
    ALLOWED_FLAGS,
    CATALYST_LOG_HEADING,
    ESSENCE_HEADING,
    OPEN_QUESTIONS_HEADING,
)

APPLY = "--apply" in sys.argv

_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
# Legacy catalyst line: "- 2026-05-25: cluster seed [[src-x]] *new*".
# Canonical lines use "DATE ·" (no colon) and are left untouched.
_LEGACY_ENTRY = re.compile(r"^(\s*)-\s+(\d{4}-\d{2}-\d{2}):\s*(.*)$")


def log(msg: str) -> None:
    print(msg)


# --------------------------------------------------------------------------
# Step 0 — backup
# --------------------------------------------------------------------------
def backup(vault: Path, targets: list[Path]) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path.home() / "vault-backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"phase1-{ts}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        for t in targets:
            if t.exists():
                tar.add(t, arcname=str(t.relative_to(vault)))
    return out


# --------------------------------------------------------------------------
# Step 1 — relocate stray source notes
# --------------------------------------------------------------------------
def stray_source_dirs(vault: Path) -> list[Path]:
    """Source-note leaf dirs that are not under a registry bucket.

    Two leakage shapes:
      * ``projects/<p>/sources/**/source.md`` — mis-routed by the old
        project-scoping bug.
      * ``sources/<slug>/source.md`` — sitting flat at the sources root
        with no bucket between (older notes / unbucketed creates). A real
        bucketed note is ``sources/<bucket>/<slug>/source.md`` (one level
        deeper), so this glob never matches those.
    """
    out: list[Path] = []
    for src_md in vault.glob("projects/*/sources/**/source.md"):
        out.append(src_md.parent)
    for src_md in vault.glob("sources/*/source.md"):
        out.append(src_md.parent)
    return out


def relocate_sources(vault: Path) -> None:
    log("\n=== Step 1: relocate stray source notes ===")
    sources_root = vault / "sources"
    for leaf in stray_source_dirs(vault):
        fm, _ = parse_frontmatter((leaf / "source.md").read_text(encoding="utf-8"))
        st = source_registry.normalize(fm.get("source_type", "") or "", vault_root=vault)
        spec = source_registry.get_spec(st, vault_root=vault)
        bucket = spec.bucket if spec else ""
        dest = sources_root / bucket / leaf.name if bucket else sources_root / leaf.name
        note = "" if (spec and bucket) else f"  [unregistered type {st!r} → flat under sources/; consider reclassifying]"
        if dest.resolve() == leaf.resolve():
            continue  # already in its canonical (bucketless) home
        if dest.exists():
            log(f"  ! COLLISION, skipped: {leaf.relative_to(vault)} -> {dest.relative_to(vault)}")
            continue
        log(f"  {leaf.relative_to(vault)}  ->  {dest.relative_to(vault)}{note}")
        if APPLY:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(leaf), str(dest))
    # Prune now-empty projects/*/sources/ scaffolding.
    if APPLY:
        for sources_dir in vault.glob("projects/*/sources"):
            for d in sorted(sources_dir.rglob("*"), reverse=True):
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            if sources_dir.is_dir() and not any(sources_dir.iterdir()):
                sources_dir.rmdir()


# --------------------------------------------------------------------------
# Step 2 — purge _candidates stubs
# --------------------------------------------------------------------------
def purge_candidates(vault: Path) -> None:
    log("\n=== Step 2: purge themes/_candidates/ ===")
    cand = vault / "themes" / "_candidates"
    if not cand.exists():
        log("  (nothing to purge)")
        return
    n = len(list(cand.rglob("*.md")))
    log(f"  removing {cand.relative_to(vault)}/ ({n} stub notes, all id:cand-* status:candidate)")
    if APPLY:
        shutil.rmtree(cand)


# --------------------------------------------------------------------------
# Step 3 — delete empty root phantom stubs
# --------------------------------------------------------------------------
def delete_root_stubs(vault: Path) -> None:
    log("\n=== Step 3: delete zero-byte phantom stubs at vault root ===")
    for md in vault.glob("*.md"):
        if md.stat().st_size == 0:
            log(f"  rm {md.name}")
            if APPLY:
                md.unlink()


# --------------------------------------------------------------------------
# Step 4 — normalise theme bodies
# --------------------------------------------------------------------------
def convert_legacy_catalyst(line: str) -> str | None:
    """Return canonical form of a legacy catalyst line, or None if not legacy."""
    m = _LEGACY_ENTRY.match(line)
    if not m:
        return None
    indent, date, rest = m.groups()
    # Strip a trailing *flag*.
    trail = re.search(r"\*([A-Za-z]+)\*\s*$", rest)
    trail_flag = trail.group(1).lower() if trail else ""
    if trail:
        rest = rest[: trail.start()].rstrip()
    cites = _WIKILINK.findall(rest)
    cit = cites[-1] if cites else ""
    text = _WIKILINK.sub("", rest).strip(" —-·\t")
    low = text.lower()
    if low.startswith("extend"):
        flag = "extends"
        text = re.sub(r"^extend(s)?\b[\s—\-]*", "", text, flags=re.I).strip(" —-")
    elif low.startswith("cluster seed"):
        flag = "new"
    elif trail_flag in ALLOWED_FLAGS:
        flag = trail_flag
    else:
        flag = "new"
    if not text:
        text = "cluster seed"
    cit_part = f" — [[{cit}]]" if cit else ""
    return f"{indent}- {date} · *{flag}* — {text}{cit_part}"


def ensure_section(body: str, heading: str, *, before: str | None = None) -> str:
    if heading in body:
        return body
    block = f"\n{heading}\n\n"
    if before and before in body:
        return body.replace(before, block.lstrip() + "\n" + before, 1)
    return body.rstrip() + "\n" + block


def normalise_themes(vault: Path) -> None:
    log("\n=== Step 4: normalise theme bodies (skeleton + catalyst grammar) ===")
    for tf in sorted((vault / "themes").glob("*.md")):
        fm, body = parse_frontmatter(tf.read_text(encoding="utf-8"))
        title = str(fm.get("title") or tf.stem)
        orig = body
        # Ensure H1.
        if not re.search(r"^#\s+\S", body, re.M):
            body = f"# {title}\n\n" + body.lstrip("\n")
        # Ensure the three sections (preserve any existing content).
        body = ensure_section(body, ESSENCE_HEADING, before=CATALYST_LOG_HEADING)
        if ESSENCE_HEADING in body and "_No synthesis yet._" not in body:
            # Add an essence placeholder only if the section is empty.
            pass
        body = ensure_section(body, CATALYST_LOG_HEADING, before=OPEN_QUESTIONS_HEADING)
        body = ensure_section(body, OPEN_QUESTIONS_HEADING)
        # Convert legacy catalyst lines.
        converted = 0
        out_lines = []
        for line in body.splitlines():
            c = convert_legacy_catalyst(line)
            if c is not None and c != line:
                converted += 1
                out_lines.append(c)
            else:
                out_lines.append(line)
        body = "\n".join(out_lines)
        if body != orig:
            changed = []
            if not re.search(r"^#\s+\S", orig, re.M):
                changed.append("H1")
            for h, nm in [(ESSENCE_HEADING, "Essence"), (CATALYST_LOG_HEADING, "Catalyst"), (OPEN_QUESTIONS_HEADING, "OpenQ")]:
                if h not in orig:
                    changed.append(f"+{nm}")
            if converted:
                changed.append(f"{converted} catalyst→canonical")
            log(f"  {tf.name}: {', '.join(changed) or 'reformat'}")
            if APPLY:
                tf.write_text(render_frontmatter(fm) + "\n\n" + body.lstrip("\n"), encoding="utf-8")


def main() -> int:
    cfg = load_config()
    vault = cfg.vault_root
    mode = "APPLY" if APPLY else "DRY-RUN"
    log(f"Phase-1 vault migration [{mode}]  vault={vault}")

    if APPLY:
        targets = [vault / "themes", vault / "sources"]
        targets += list(vault.glob("projects/*/sources"))
        targets += [p for p in vault.glob("*.md")]
        b = backup(vault, [t for t in targets if t.exists()])
        log(f"Backup written: {b}")

    relocate_sources(vault)
    purge_candidates(vault)
    delete_root_stubs(vault)
    normalise_themes(vault)

    log("\nDone." + ("" if APPLY else "  (dry run — re-run with --apply to mutate)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
