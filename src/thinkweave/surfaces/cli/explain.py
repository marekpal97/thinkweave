"""``weave explain`` — the agent-Bash rail under ``/explain``.

``locate`` finds the explain note a re-run updates, keyed by (session, scope,
target); ``check`` validates the explain-note contract. Neither prompts.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from thinkweave.core.config import load_config
from thinkweave.core.vault import read_session_fm

SCOPES = ("session", "diff")


def validate_explain_note(fm: dict, note_path: Path) -> list[str]:
    """Problems with an explain note's frontmatter; empty list = valid."""
    problems: list[str] = []
    if fm.get("kind") != "explain":
        problems.append("kind must be 'explain'")
    if fm.get("scope") not in SCOPES:
        problems.append(f"scope must be one of {', '.join(SCOPES)}")
    if not str(fm.get("target") or "").strip():
        problems.append("target is required")
    concepts = fm.get("concepts")
    if not (isinstance(concepts, list) and len(concepts) >= 2):
        problems.append("concepts needs at least 2 ontology slugs")
    html = str(fm.get("html_path") or "").strip()
    if html and not (note_path.parent / html).is_file():
        problems.append(f"html_path {html!r} has no file beside the note")
    return problems


def cmd_explain(args: argparse.Namespace) -> None:
    cfg = load_config()
    if args.explain_action == "locate":
        note_id = _locate(cfg, args.session, args.scope, args.target)
        if note_id:
            print(note_id)
    elif args.explain_action == "check":
        from thinkweave.operations.notes import read_note

        meta, _ = read_note(cfg, args.note)
        if meta is None:
            print(f"note {args.note} not in the index — run `weave index` first")
            sys.exit(2)
        problems = validate_explain_note(meta.frontmatter, cfg.vault_root / meta.path)
        for p in problems:
            print(f"  - {p}")
        print(f"{args.note}: {'ok' if not problems else f'{len(problems)} problem(s)'}")
        sys.exit(1 if problems else 0)


def _locate(cfg, session: str, scope: str, target: str) -> str:
    """The id of the explain note in ``session``'s folder with this scope and target."""
    from thinkweave.core.indexer import Indexer

    idx = Indexer(config=cfg)
    try:
        rows = idx.db.execute(
            "SELECT id, path FROM notes WHERE type = 'note' "
            "AND json_extract(frontmatter, '$.kind') = 'explain' "
            "AND json_extract(frontmatter, '$.scope') = ? "
            "AND TRIM(json_extract(frontmatter, '$.target')) = TRIM(?) "
            "ORDER BY date",
            (scope, target),
        ).fetchall()
    finally:
        idx.close()
    for row in rows:
        if _in_session(cfg.vault_root / row["path"], session):
            return row["id"]
    return ""


def _in_session(note_path: Path, session: str) -> bool:
    """True when the note's folder belongs to ``session`` (harness UUID or ``ses-`` id).

    Hook-created folders start with the harness UUID; a folder's ``session.md``
    also carries the ``ses-`` id and ``source_session`` once it exists.
    """
    folder = note_path.parent
    if folder.parent.name != "sessions" or folder.name == "misc":
        return False
    if folder.name.startswith(session):
        return True
    fm = read_session_fm(folder / "session.md") or {}
    return session in (fm.get("id"), fm.get("source_session"))
