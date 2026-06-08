"""Small shared helpers reused across core/synthesis/operations layers."""

from __future__ import annotations


def as_list(value) -> list[str]:
    """Normalise a frontmatter list-or-csv-string field to a list."""
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]
