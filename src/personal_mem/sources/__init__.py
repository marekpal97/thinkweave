"""Source primitive — declarative source-type registry and helpers.

A *source* is a note_type in personal_mem representing external content
(papers, repos, articles, newsletters, conversations, …). Each source type
declares how notes of that type are routed on disk (layout + bucket) and
which skills handle its ingestion (import / acquire / discover).

Adding a new source type means adding one ``SourceTypeSpec`` entry to
``registry.py`` and writing a skill file under ``commands/``. No edits to
``vault.py`` are required — the vault dispatches on ``spec.layout``.
"""

from personal_mem.sources.config import DEFAULT_CONFIG, load_user_config
from personal_mem.sources.frontmatter import build_source_frontmatter
from personal_mem.sources.intake import (
    InboxEntry,
    archive_to_processed,
    enumerate_inbox,
)
from personal_mem.sources.registry import (
    REGISTRY,
    Layout,
    SourceTypeSpec,
    all_specs,
    get_spec,
    normalize,
)

__all__ = [
    "DEFAULT_CONFIG",
    "REGISTRY",
    "InboxEntry",
    "Layout",
    "SourceTypeSpec",
    "all_specs",
    "archive_to_processed",
    "build_source_frontmatter",
    "enumerate_inbox",
    "get_spec",
    "load_user_config",
    "normalize",
]
