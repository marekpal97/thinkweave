"""The three-form path matcher — the package's one leaf util.

Two callers need these exact semantics (leaf-util doctrine, boundary spec
§6): triage's sensitive/watched paths and the diff gate's forbidden_paths.
"""

from __future__ import annotations

import fnmatch


def match(path: str, pattern: str) -> bool:
    """Match one repo-relative path against one sensitive/watched pattern.

    Three forms, dispatched by shape (issue #59):
      - dir prefix — trailing ``/`` (``hooks/``, ``src/thinkweave/surfaces/``):
        the path is under that directory (``startswith``, same convention as
        the diff-guard gate's ``forbidden_paths``).
      - glob — contains ``*``/``?``/``[`` (``*schema*``): fnmatched
        case-insensitively against the basename (or the whole path when the
        glob itself spans directories), so ``docs/SCHEMA.md`` is caught too.
      - bare filename — anything else (``ontology.yaml``): the path's basename
        equals it, so the file matches at any depth (a different basename that
        merely shares the stem as a prefix does not).
    """
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if any(c in pattern for c in "*?["):
        target = path if "/" in pattern else path.rsplit("/", 1)[-1]
        return fnmatch.fnmatch(target.lower(), pattern.lower())
    return path.rsplit("/", 1)[-1] == pattern


def hits(files: list[str], patterns: list[str]) -> list[str]:
    """``"<path> (matches <pattern>)"`` for each file that hits any pattern,
    deduped and sorted — the human-readable reason fragments."""
    found = set()
    for path in files:
        for pat in patterns:
            if match(path, pat):
                found.add(f"{path} (matches {pat})")
    return sorted(found)
