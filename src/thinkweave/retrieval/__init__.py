"""Retrieval layer — the three modalities over the derived index.

Operations: FTS / similarity / hybrid search and graph traversal
(``Search``, yielding ``SearchResult`` rows), budgeted context
composition (``build_project_context``).

Invariants: read-only over ``.weave/index.db``; semantic failures raise
``SemanticSearchUnavailable`` rather than returning an empty list, so
"could not run" is never conflated with "no hits".

Storage: none of its own — queries the index ``core.Indexer`` builds.

Extension points: new modalities compose inside ``Search``; new serving
surfaces log through operations' retrieval log, not here.
"""


class SemanticSearchUnavailable(RuntimeError):
    """Semantic retrieval could not run, as distinct from returning no hits."""


# Defined-before-import: search.py imports SemanticSearchUnavailable from
# this package, so the class must precede the door imports.
from thinkweave.retrieval.context import build_project_context  # noqa: E402
from thinkweave.retrieval.search import Search, SearchResult  # noqa: E402

__all__ = [
    "Search",
    "SearchResult",
    "SemanticSearchUnavailable",
    "build_project_context",
]
