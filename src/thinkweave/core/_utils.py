"""Small shared helpers reused across core/synthesis/operations layers."""

from __future__ import annotations

import subprocess


def git_stdout(*args: str, cwd: str = "") -> str:
    """One git call's stdout, stripped; ``""`` when git refuses or cannot
    run."""
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=5,
            check=True, cwd=cwd or None,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def as_list(value) -> list[str]:
    """Normalise a frontmatter list-or-csv-string field to a list."""
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]
