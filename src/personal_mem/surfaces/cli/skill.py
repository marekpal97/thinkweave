"""``mem skill`` — list and inspect skill files in ``commands/``.

Walks ``commands/*.md`` (skipping leading-underscore files), parses YAML
frontmatter, and prints a table or per-skill detail. The package's own
``commands/`` directory ships at the repo root; the ``_commands_dir``
helper resolves it relative to this file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def cmd_skill(args: argparse.Namespace) -> None:
    action = getattr(args, "skill_action", None) or "list"

    if action == "list":
        skills = _load_all_skills()
        if not skills:
            print("No skills found in commands/.")
            return
        print(f"{'NAME':<22} {'OWNS_MECHANIC':<22} {'SOURCE_TYPE':<22} {'CAPABILITIES':<18} DESCRIPTION")
        print("-" * 130)
        for skill in skills:
            mech = _format_list_field(skill["fm"].get("owns_mechanic"))
            st = _format_list_field(skill["fm"].get("source_type"))
            caps = _format_list_field(skill["fm"].get("capabilities"))
            desc = skill["fm"].get("description", "").strip().replace("\n", " ")
            print(f"{skill['name']:<22} {mech:<22} {st:<22} {caps:<18} {desc}")
        return

    if action == "show":
        skill = _load_skill(args.name)
        if skill is None:
            print(f"No skill found at commands/{args.name}.md")
            sys.exit(1)
        fm = skill["fm"]
        print(f"# /{skill['name']}")
        for key in ("source_type", "capabilities", "tools", "description"):
            if key in fm:
                val = fm[key]
                if isinstance(val, list):
                    print(f"{key}:")
                    for item in val:
                        print(f"  - {item}")
                else:
                    print(f"{key}: {val}")
        print()
        print("--- head (first 30 lines of body) ---")
        body_lines = skill["body"].splitlines()
        for line in body_lines[:30]:
            print(line)
        if len(body_lines) > 30:
            print(f"... ({len(body_lines) - 30} more lines)")
        return

    cmd_skill(argparse.Namespace(skill_action="list"))


def _commands_dir() -> Path:
    """Return the commands/ directory shipped with the package.

    This file lives at ``src/personal_mem/surfaces/cli/skill.py``;
    ``commands/`` is at the repo root, four levels up.
    """
    return Path(__file__).resolve().parents[4] / "commands"


def _load_skill(name: str) -> dict | None:
    """Load a single skill file by name. Returns None if not found."""
    from personal_mem.core.vault import parse_frontmatter

    path = _commands_dir() / f"{name}.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    return {"name": name, "path": path, "fm": fm, "body": body}


def _load_all_skills() -> list[dict]:
    """Return every skill in commands/ (excluding files starting with _)."""
    cmd_dir = _commands_dir()
    if not cmd_dir.exists():
        return []
    out = []
    for path in sorted(cmd_dir.glob("*.md")):
        if path.name.startswith("_"):
            continue
        skill = _load_skill(path.stem)
        if skill is not None:
            out.append(skill)
    return out


def skills_for_source_type(slug: str) -> list[tuple[str, str]]:
    """Return (name, description) for each skill claiming this source_type."""
    out = []
    for skill in _load_all_skills():
        st = skill["fm"].get("source_type")
        types = st if isinstance(st, list) else [st] if st else []
        if slug in types:
            desc = skill["fm"].get("description", "").strip().replace("\n", " ")
            out.append((skill["name"], desc))
    return out


def _format_list_field(value) -> str:
    """Render a list-or-scalar frontmatter field for the CLI table."""
    if value is None or value == "":
        return "—"
    if isinstance(value, list):
        if not value:
            return "—"
        return ",".join(str(v) for v in value)
    return str(value)
